"""Step Functions orchestrator for parallel, dependency-ordered replication.

Replaces the synchronous replication loop with an AWS Step Functions state
machine that processes dependency levels sequentially and resources within
each level in parallel.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from aws.client_factory import create_client
from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase
from models.session import Session
from replication.dependency_graph import (
    build_dependency_graph,
    get_dependents,
    topological_sort,
)

logger = logging.getLogger(__name__)


def _compute_dependency_levels(
    graph: dict[str, list[str]], execution_order: list[str]
) -> list[list[str]]:
    """Group resources into dependency levels for parallel execution.

    Resources at the same level have no dependencies on each other
    and can be replicated concurrently. Levels are executed sequentially.

    This mirrors the logic in orchestrator._compute_dependency_levels but
    is kept here for the SFN module's self-containment.

    Args:
        graph: Adjacency list where graph[a] contains b means a must come before b.
        execution_order: Topologically sorted resource IDs.

    Returns:
        List of lists — each inner list is a set of resource IDs that can
        be processed in parallel.
    """
    in_degree: dict[str, int] = {node: 0 for node in graph}
    for node in graph:
        for neighbor in graph[node]:
            in_degree.setdefault(neighbor, 0)
            in_degree[neighbor] += 1

    remaining = set(execution_order)
    levels: list[list[str]] = []

    while remaining:
        level = [
            node for node in execution_order
            if node in remaining and in_degree.get(node, 0) == 0
        ]
        if not level:
            # Fallback: take remaining in order (shouldn't happen with a DAG)
            levels.append(list(remaining))
            break

        levels.append(level)
        for node in level:
            remaining.discard(node)
            for neighbor in graph.get(node, []):
                if neighbor in in_degree:
                    in_degree[neighbor] -= 1

    return levels



def _get_arn_mapping_type(resource_type: ResourceType) -> str | None:
    """Map a ResourceType to its ARN mapping category key."""
    mapping = {
        ResourceType.IAM_ROLE: "role",
        ResourceType.LAMBDA: "lambda",
        ResourceType.S3_BUCKET: "s3",
        ResourceType.KINESIS_STREAM: "kinesis",
        ResourceType.KINESIS_FIREHOSE: "kinesis",
        ResourceType.KINESIS_VIDEO_STREAM: "kinesis",
    }
    return mapping.get(resource_type)


def build_state_machine_input(
    session: Session,
    resource_ids: list[str],
    job_id: str,
    resource_tags: dict[str, str] | None = None,
) -> dict:
    """Build the Step Functions execution input.

    Computes dependency levels from the resource graph and structures
    the input for the SFN state machine.

    The output format is:
    {
        "session_id": "...",
        "job_id": "...",
        "target_region": "...",
        "instance_id": "...",
        "resource_tags": {...},
        "levels": [
            {"level": 0, "resources": [{"resource_id": "...", "resource_type": "...", ...}]},
            {"level": 1, "resources": [...]},
        ],
        "arn_mappings": {"role": {}, "lambda": {}, "s3": {}, "kinesis": {}}
    }
    """
    # Collect selected resources
    selected: dict[str, ResourceBase] = {}
    for rid in resource_ids:
        resource = session.inventory.get(rid)
        if resource is not None:
            selected[rid] = resource

    # Build dependency graph and sort
    resource_list = list(selected.values())
    graph = build_dependency_graph(resource_list)
    try:
        execution_order = topological_sort(graph)
    except ValueError:
        logger.warning("Cycle detected in dependency graph; using input order")
        execution_order = list(selected.keys())

    levels = _compute_dependency_levels(graph, execution_order)

    # Extract instance ID
    instance_id = ""
    try:
        from aws.arn_utils import parse_arn
        parsed = parse_arn(session.instance_arn)
        res = parsed["resource"]
        if res.startswith("instance/"):
            instance_id = res.split("/", 1)[1]
    except Exception:
        pass

    # Build level structures with serialized resource data
    level_dicts = []
    for idx, level_ids in enumerate(levels):
        resources_in_level = []
        for rid in level_ids:
            resource = selected.get(rid)
            if resource is None:
                continue
            resources_in_level.append({
                "resource_id": rid,
                "resource_type": resource.resource_type.value,
                "resource_name": resource.name,
                "resource_arn": resource.arn,
                "resource_data": resource.model_dump(mode="json"),
                "session_id": session.session_id,
                "job_id": job_id,
                "target_region": session.target_region,
                "instance_id": instance_id,
                "resource_tags": resource_tags or {},
            })
        level_dicts.append({
            "level": idx,
            "resources": resources_in_level,
        })

    return {
        "session_id": session.session_id,
        "job_id": job_id,
        "target_region": session.target_region,
        "instance_id": instance_id,
        "resource_tags": resource_tags or {},
        "levels": level_dicts,
        "arn_mappings": {
            "role": {},
            "lambda": {},
            "s3": {},
            "kinesis": {},
        },
    }



def start_replication_execution(
    session: Session,
    resource_ids: list[str],
    job_id: str,
    resource_tags: dict[str, str] | None = None,
) -> str:
    """Start a Step Functions execution for replication.

    Args:
        session: The current session with inventory.
        resource_ids: IDs of resources selected for replication.
        job_id: The replication job ID.
        resource_tags: Optional tags to apply to replicated resources.

    Returns:
        The Step Functions execution ARN.

    Raises:
        RuntimeError: If STATE_MACHINE_ARN is not configured or SFN start fails.
    """
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN")
    if not state_machine_arn:
        raise RuntimeError(
            "STATE_MACHINE_ARN environment variable is not set. "
            "Step Functions replication is not available."
        )

    sfn_input = build_state_machine_input(session, resource_ids, job_id, resource_tags)

    # Determine region from the state machine ARN
    # Format: arn:aws:states:<region>:<account>:stateMachine:<name>
    try:
        sfn_region = state_machine_arn.split(":")[3]
    except (IndexError, AttributeError):
        sfn_region = session.source_region

    sfn_client = create_client("stepfunctions", sfn_region)

    execution_name = f"repl-{job_id[:8]}-{uuid.uuid4().hex[:8]}"

    try:
        response = sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(sfn_input, default=str),
        )
        execution_arn = response["executionArn"]
        logger.info(
            "Started SFN execution %s for job %s (session %s, %d resources)",
            execution_arn, job_id, session.session_id, len(resource_ids),
        )
        return execution_arn
    except Exception as exc:
        logger.exception("Failed to start SFN execution for job %s", job_id)
        raise RuntimeError(f"Failed to start Step Functions execution: {exc}") from exc


def get_execution_status(execution_arn: str) -> dict:
    """Query Step Functions execution status and map to per-resource progress.

    Args:
        execution_arn: The SFN execution ARN to query.

    Returns:
        Dict with execution status and per-resource progress:
        {
            "execution_arn": "...",
            "status": "RUNNING" | "SUCCEEDED" | "FAILED" | "TIMED_OUT" | "ABORTED",
            "current_level": int | None,
            "total_levels": int,
            "resources": [{"resource_id": "...", "status": "...", ...}],
            "started_at": "...",
            "completed_at": "..." | None,
            "error": "..." | None,
            "summary": {"succeeded": N, "failed": N, "blocked": N, "in_progress": N, "total": N}
        }
    """
    # Determine region from execution ARN
    try:
        sfn_region = execution_arn.split(":")[3]
    except (IndexError, AttributeError):
        sfn_region = os.environ.get("AWS_REGION", "us-east-1")

    sfn_client = create_client("stepfunctions", sfn_region)

    try:
        response = sfn_client.describe_execution(executionArn=execution_arn)
    except Exception as exc:
        logger.exception("Failed to describe SFN execution %s", execution_arn)
        return {
            "execution_arn": execution_arn,
            "status": "FAILED",
            "current_level": None,
            "total_levels": 0,
            "resources": [],
            "started_at": None,
            "completed_at": None,
            "error": str(exc),
            "summary": {"succeeded": 0, "failed": 0, "blocked": 0, "in_progress": 0, "total": 0},
        }

    sfn_status = response.get("status", "RUNNING")
    started_at = response.get("startDate")
    stopped_at = response.get("stopDate")
    error = response.get("error")
    cause = response.get("cause")

    # Map SFN status to our status
    status_map = {
        "RUNNING": "RUNNING",
        "SUCCEEDED": "SUCCEEDED",
        "FAILED": "FAILED",
        "TIMED_OUT": "TIMED_OUT",
        "ABORTED": "ABORTED",
    }
    status = status_map.get(sfn_status, "RUNNING")

    # Parse the execution input to get resource info
    resources = []
    total_levels = 0
    sfn_input_str = response.get("input", "{}")
    try:
        sfn_input = json.loads(sfn_input_str)
        levels = sfn_input.get("levels", [])
        total_levels = len(levels)
        for level_data in levels:
            for res_data in level_data.get("resources", []):
                resources.append({
                    "resource_id": res_data.get("resource_id", ""),
                    "resource_name": res_data.get("resource_name", ""),
                    "resource_type": res_data.get("resource_type", ""),
                    "level": level_data.get("level", 0),
                    "status": "IN_PROGRESS" if status == "RUNNING" else status,
                })
    except (json.JSONDecodeError, KeyError):
        logger.warning("Failed to parse SFN execution input for %s", execution_arn)

    # If execution completed, try to parse the output for per-resource results
    if status == "SUCCEEDED":
        output_str = response.get("output", "")
        try:
            output = json.loads(output_str) if output_str else {}
            _apply_execution_output(resources, output)
        except (json.JSONDecodeError, KeyError):
            logger.warning("Failed to parse SFN execution output for %s", execution_arn)

    # Compute summary
    succeeded = sum(1 for r in resources if r.get("status") == "REPLICATED")
    failed = sum(1 for r in resources if r.get("status") == "FAILED")
    blocked = sum(1 for r in resources if r.get("status") == "BLOCKED")
    in_progress = sum(1 for r in resources if r.get("status") == "IN_PROGRESS")

    error_msg = None
    if error:
        error_msg = f"{error}: {cause}" if cause else error

    return {
        "execution_arn": execution_arn,
        "status": status,
        "current_level": None,  # Determined by SFN execution history if needed
        "total_levels": total_levels,
        "resources": resources,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": stopped_at.isoformat() if stopped_at else None,
        "error": error_msg,
        "summary": {
            "succeeded": succeeded,
            "failed": failed,
            "blocked": blocked,
            "in_progress": in_progress,
            "total": len(resources),
        },
    }


def _apply_execution_output(resources: list[dict], output: dict | list) -> None:
    """Apply SFN execution output to per-resource status entries.

    The output from the state machine is a list of level results, each
    containing per-resource results from the Resource Lambda.
    """
    if isinstance(output, list):
        level_results = output
    elif isinstance(output, dict):
        level_results = output.get("levels", output.get("results", []))
    else:
        return

    # Build a lookup of resource results by resource_id
    result_map: dict[str, dict] = {}
    for level_result in level_results:
        if isinstance(level_result, list):
            for res_result in level_result:
                if isinstance(res_result, dict) and "resource_id" in res_result:
                    result_map[res_result["resource_id"]] = res_result
        elif isinstance(level_result, dict):
            for res_result in level_result.get("resources", level_result.get("level_results", [])):
                if isinstance(res_result, dict) and "resource_id" in res_result:
                    result_map[res_result["resource_id"]] = res_result

    # Update resource entries with actual results
    for resource in resources:
        rid = resource.get("resource_id")
        if rid in result_map:
            result = result_map[rid]
            resource["status"] = result.get("status", resource["status"])
            resource["replicated_arn"] = result.get("replicated_arn")
            resource["error"] = result.get("error")
            resource["error_classification"] = result.get("error_classification")


def process_execution_results(
    session: Session,
    execution_status: dict,
) -> None:
    """Process SFN execution results and update session resources.

    Marks failed resources and their transitive dependents as BLOCKED.

    Args:
        session: The session to update.
        execution_status: The result from get_execution_status().
    """
    resource_results = execution_status.get("resources", [])

    # Build dependency graph for blocking computation
    selected_resources = [
        session.inventory[r["resource_id"]]
        for r in resource_results
        if r["resource_id"] in session.inventory
    ]
    graph = build_dependency_graph(selected_resources)

    failed_ids: set[str] = set()
    blocked_ids: set[str] = set()

    # First pass: apply direct results
    for res_result in resource_results:
        rid = res_result.get("resource_id")
        resource = session.inventory.get(rid)
        if resource is None:
            continue

        status = res_result.get("status", "")
        if status == "REPLICATED":
            resource.status = ReplicationStatus.REPLICATED
            resource.replicated_arn = res_result.get("replicated_arn")
            resource.error = None
            resource.error_classification = None
        elif status == "FAILED":
            resource.status = ReplicationStatus.FAILED
            resource.error = res_result.get("error", "Resource Lambda failed")
            resource.error_classification = res_result.get("error_classification")
            failed_ids.add(rid)
        elif status == "SKIPPED":
            resource.status = ReplicationStatus.SKIPPED
            resource.error = res_result.get("error")

    # Second pass: compute transitive dependents of failed resources and mark BLOCKED
    for failed_id in failed_ids:
        dependents = get_dependents(graph, failed_id)
        for dep_id in dependents:
            if dep_id not in failed_ids:
                blocked_ids.add(dep_id)

    for blocked_id in blocked_ids:
        resource = session.inventory.get(blocked_id)
        if resource is not None and resource.status != ReplicationStatus.REPLICATED:
            resource.status = ReplicationStatus.BLOCKED
            resource.error = "Blocked: a dependency failed to replicate"

    session.updated_at = datetime.now(timezone.utc)
