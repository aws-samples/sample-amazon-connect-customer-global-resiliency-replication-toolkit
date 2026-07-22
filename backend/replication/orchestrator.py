"""Replication orchestrator for Connect ACGR Resource Replicator.

Executes replication in dependency order using topological sort,
tracks progress (total, completed, failed, blocked), handles failures
by marking dependents as Blocked, and supports retry of individual
failed resources.

Requirements: 14.1, 14.5, 15.1, 15.2, 15.3, 15.4
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase
from models.session import ReplicationJob, ReplicationProgress, Session
from replication.dependency_graph import (
    build_dependency_graph,
    get_dependents,
    topological_sort,
)
from replication.iam_replication import replicate_iam_role
from replication.lambda_replication import replicate_lambda_function, replicate_lambda_layers
from replication.lex_replication import (
    LexAlgrInProgressError,
    LexAlgrSkippedError,
    replicate_lex_bot,
)
from replication.s3_replication import replicate_s3_bucket
from replication.streaming_replication import (
    replicate_firehose_stream,
    replicate_kinesis_stream,
    replicate_kvs_stream,
)
from replication.approved_origins_replication import replicate_approved_origin

logger = logging.getLogger(__name__)


def _attach_kms_info(resource: ResourceBase, target_region: str) -> None:
    """Attach KMS handling info to a resource if it has encryption config.

    Probes the resource's encryption settings and generates a KmsInfo dict
    describing how the KMS key was resolved for the target region.
    """
    from aws.kms_utils import ensure_kms_key_exists

    rtype = resource.resource_type

    if rtype == ResourceType.KINESIS_STREAM:
        enc_type = getattr(resource, "encryption_type", None)
        if enc_type and enc_type != "NONE":
            _, kms_info = ensure_kms_key_exists("alias/aws/kinesis", target_region)
            resource.kms_info = kms_info
        return

    if rtype == ResourceType.KINESIS_VIDEO_STREAM:
        enc_type = getattr(resource, "encryption_type", None)
        if enc_type and enc_type != "NONE":
            _, kms_info = ensure_kms_key_exists("alias/aws/kinesisvideo", target_region)
            resource.kms_info = kms_info
        return

    if rtype == ResourceType.S3_BUCKET:
        enc_type = getattr(resource, "encryption_type", None)
        if enc_type:
            _, kms_info = ensure_kms_key_exists("alias/aws/s3", target_region)
            resource.kms_info = kms_info
        return


def _replicate_single_resource(
    resource: ResourceBase,
    target_region: str,
    role_arn_mapping: dict[str, str],
    lambda_arn_mapping: dict[str, str],
    resource_tags: dict[str, str] | None = None,
    instance_id: str = "",
    layer_arn_mapping: dict[str, str] | None = None,
    s3_bucket_mapping: dict[str, str] | None = None,
) -> str:
    """Dispatch replication to the appropriate service-specific replicator.

    Uses resource_type enum for dispatch instead of isinstance() to handle
    resources deserialized from DynamoDB that may be base ResourceBase objects.

    Args:
        resource: The resource to replicate.
        target_region: The ACGR target region.
        role_arn_mapping: Mapping of source IAM role ARN → replicated role ARN.
        lambda_arn_mapping: Mapping of source Lambda ARN → replicated Lambda ARN.
        instance_id: Connect instance ID for post-replication association.

    Returns:
        The ARN of the newly created resource in the target region.

    Raises:
        Any exception from the underlying replicator.
    """
    rtype = resource.resource_type

    if rtype == ResourceType.IAM_ROLE:
        return replicate_iam_role(resource, target_region, resource_tags=resource_tags)

    if rtype == ResourceType.LAMBDA:
        return replicate_lambda_function(
            resource, target_region, role_arn_mapping=role_arn_mapping,
            resource_tags=resource_tags, instance_id=instance_id,
            layer_arn_mapping=layer_arn_mapping,
        )

    if rtype == ResourceType.LEX_BOT:
        return replicate_lex_bot(
            resource, target_region,
            lambda_arn_mapping=lambda_arn_mapping,
            role_arn_mapping=role_arn_mapping,
            resource_tags=resource_tags,
            instance_id=instance_id,
        )

    if rtype == ResourceType.KINESIS_STREAM:
        return replicate_kinesis_stream(resource, target_region, resource_tags=resource_tags)

    if rtype == ResourceType.KINESIS_FIREHOSE:
        # Look up the target S3 bucket name from the mapping
        s3_bucket_name = None
        if s3_bucket_mapping:
            s3_dest = getattr(resource, "s3_destination", None) or {}
            source_bucket_arn = s3_dest.get("BucketARN", "")
            if source_bucket_arn:
                source_bucket_name = source_bucket_arn.split(":::")[-1] if ":::" in source_bucket_arn else ""
                if source_bucket_name and source_bucket_name in s3_bucket_mapping:
                    s3_bucket_name = s3_bucket_mapping[source_bucket_name]
        return replicate_firehose_stream(
            resource, target_region, s3_bucket_name=s3_bucket_name,
            resource_tags=resource_tags,
            role_arn_mapping=role_arn_mapping,
        )

    if rtype == ResourceType.KINESIS_VIDEO_STREAM:
        return replicate_kvs_stream(resource, target_region, resource_tags=resource_tags)

    if rtype == ResourceType.S3_BUCKET:
        return replicate_s3_bucket(resource, target_region, resource_tags=resource_tags)

    if rtype == ResourceType.APPROVED_ORIGIN:
        return replicate_approved_origin(resource, target_region, target_instance_id=instance_id)

    raise ValueError(f"Unsupported resource type for replication: {resource.resource_type}")


def _compute_progress(resources: dict[str, ResourceBase]) -> ReplicationProgress:
    """Compute progress counters from the current resource statuses."""
    total = len(resources)
    completed = sum(
        1 for r in resources.values()
        if r.status in (ReplicationStatus.REPLICATED, ReplicationStatus.SKIPPED)
    )
    failed = sum(
        1 for r in resources.values() if r.status == ReplicationStatus.FAILED
    )
    blocked = sum(
        1 for r in resources.values() if r.status == ReplicationStatus.BLOCKED
    )
    return ReplicationProgress(
        total=total, completed=completed, failed=failed, blocked=blocked
    )


def _compute_dependency_levels(graph: dict[str, list[str]], execution_order: list[str]) -> list[list[str]]:
    """Group resources into dependency levels for parallel execution.

    Resources at the same level have no dependencies on each other
    and can be replicated concurrently.
    """
    # Compute in-degree for each node
    in_degree: dict[str, int] = {node: 0 for node in graph}
    for node in graph:
        for neighbor in graph[node]:
            in_degree.setdefault(neighbor, 0)
            in_degree[neighbor] += 1

    remaining = set(execution_order)
    levels: list[list[str]] = []

    while remaining:
        # Find all nodes with in-degree 0 among remaining
        level = [
            node for node in execution_order
            if node in remaining and in_degree.get(node, 0) == 0
        ]
        if not level:
            # Fallback: just take remaining in order (shouldn't happen with DAG)
            levels.append(list(remaining))
            break

        levels.append(level)
        for node in level:
            remaining.discard(node)
            for neighbor in graph.get(node, []):
                if neighbor in in_degree:
                    in_degree[neighbor] -= 1

    return levels


def run_replication(
    session: Session,
    resource_ids: list[str],
    existing_job_id: str | None = None,
    session_store=None,
    resource_tags: dict[str, str] | None = None,
    concurrency: int = 1,
) -> ReplicationJob:
    """Execute replication for selected resources in dependency order.

    Steps:
    1. Collect selected resources from the session inventory.
    2. Build a dependency graph and topologically sort.
    3. Iterate in order, replicating each resource:
       - On success: mark REPLICATED, record replicated_arn, update mappings.
       - On failure: mark FAILED with error, cascade-block all dependents.
       - After each resource: persist intermediate progress to DynamoDB.
    4. Return a ReplicationJob with final progress.

    Args:
        session: The current session with inventory.
        resource_ids: IDs of resources selected for replication.
        existing_job_id: If provided, reuse this job ID instead of generating a new one.
        session_store: Optional session store for persisting intermediate progress.

    Returns:
        A ReplicationJob capturing the execution results.
    """
    job_id = existing_job_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Collect selected resources
    selected: dict[str, ResourceBase] = {}
    for rid in resource_ids:
        resource = session.inventory.get(rid)
        if resource is not None:
            selected[rid] = resource

    if not selected:
        return ReplicationJob(
            job_id=job_id,
            status="COMPLETED",
            selected_resource_ids=resource_ids,
            execution_order=[],
            progress=ReplicationProgress(total=0, completed=0, failed=0, blocked=0),
            started_at=now,
            completed_at=now,
        )

    # Build dependency graph and sort
    resource_list = list(selected.values())
    graph = build_dependency_graph(resource_list)
    try:
        execution_order = topological_sort(graph)
    except ValueError:
        # Cycle detected — fall back to resource_ids order
        logger.warning("Cycle detected in dependency graph; using input order")
        execution_order = list(selected.keys())

    # Track ARN mappings for cross-resource references
    role_arn_mapping: dict[str, str] = {}
    lambda_arn_mapping: dict[str, str] = {}
    s3_bucket_mapping: dict[str, str] = {}
    kinesis_arn_mapping: dict[str, str] = {}
    firehose_arn_mapping: dict[str, str] = {}
    layer_arn_mapping: dict[str, str] = {}
    blocked_ids: set[str] = set()

    target_region = session.target_region

    # Extract instance ID for post-replication Connect association
    instance_id = ""
    try:
        from aws.arn_utils import parse_arn as _parse_arn
        _parsed = _parse_arn(session.instance_arn)
        _res = _parsed["resource"]
        if _res.startswith("instance/"):
            instance_id = _res.split("/", 1)[1]
    except Exception:
        pass

    # Pre-replicate Lambda layers before the main replication loop
    # so that layer ARN mappings are available when creating functions
    lambda_resources_for_layers = [
        r for r in selected.values()
        if r.resource_type == ResourceType.LAMBDA and getattr(r, "layers", [])
    ]
    if lambda_resources_for_layers:
        source_region = session.source_region
        try:
            layer_arn_mapping = replicate_lambda_layers(
                lambda_resources_for_layers, source_region, target_region
            )
            if layer_arn_mapping:
                logger.info(
                    "Pre-replicated %d Lambda layer(s) to %s",
                    len(layer_arn_mapping), target_region,
                )
        except Exception:
            logger.warning(
                "Lambda layer pre-replication failed; will fall back to region rewriting",
                exc_info=True,
            )

    # Auto-skip IAM roles — IAM is global, so the same role ARN works in all regions.
    # Mark them REPLICATED immediately and populate role_arn_mapping for downstream resources.
    for rid in list(selected.keys()):
        resource = selected[rid]
        if resource.resource_type == ResourceType.IAM_ROLE:
            resource.status = ReplicationStatus.REPLICATED
            resource.replicated_arn = resource.arn  # same ARN globally
            resource.error = None
            role_arn_mapping[resource.arn] = resource.arn
            logger.info(
                "IAM role '%s' is global — auto-marked REPLICATED (ARN: %s)",
                resource.name, resource.arn,
            )

    # Mark all remaining (non-IAM) selected as IN_PROGRESS initially
    for rid in selected:
        if selected[rid].resource_type != ResourceType.IAM_ROLE:
            selected[rid].status = ReplicationStatus.IN_PROGRESS
            selected[rid].error = None

    # Also, when using an existing job ID, don't append a new job — update the existing one
    append_job = existing_job_id is None

    logger.info(
        "Starting replication job %s: %d resources in order %s (concurrency=%d)",
        job_id,
        len(execution_order),
        execution_order,
        concurrency,
    )

    # Group resources into dependency levels for parallel execution
    if concurrency > 1:
        levels = _compute_dependency_levels(graph, execution_order)
    else:
        # Sequential: each resource is its own level
        levels = [[rid] for rid in execution_order]

    for level_resources in levels:
        # Filter out blocked and already-replicated resources (e.g. auto-skipped IAM roles)
        active_in_level = [
            rid for rid in level_resources
            if rid not in blocked_ids and rid in selected
            and selected[rid].status != ReplicationStatus.REPLICATED
        ]

        if not active_in_level:
            # Mark any blocked resources in this level
            for rid in level_resources:
                resource = selected.get(rid)
                if resource and rid in blocked_ids:
                    resource.status = ReplicationStatus.BLOCKED
                    resource.error = "Blocked: a dependency failed to replicate"
            continue

        if concurrency > 1 and len(active_in_level) > 1:
            # Parallel execution within this level
            import concurrent.futures
            import threading

            # Thread-safe lock for shared state
            lock = threading.Lock()

            def _replicate_one(rid: str) -> tuple[str, str | None, Exception | None]:
                resource = selected.get(rid)
                if resource is None:
                    return (rid, None, None)
                logger.info(
                    "Replicating resource %s (%s) [%s] (parallel)",
                    resource.name, rid, resource.resource_type,
                )
                try:
                    replicated_arn = _replicate_single_resource(
                        resource, target_region, role_arn_mapping, lambda_arn_mapping,
                        resource_tags=resource_tags,
                        instance_id=instance_id,
                        layer_arn_mapping=layer_arn_mapping,
                        s3_bucket_mapping=s3_bucket_mapping,
                    )
                    return (rid, replicated_arn, None)
                except Exception as exc:
                    return (rid, None, exc)

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(concurrency, len(active_in_level))) as executor:
                futures = {executor.submit(_replicate_one, rid): rid for rid in active_in_level}
                for future in concurrent.futures.as_completed(futures):
                    rid = futures[future]
                    resource = selected.get(rid)
                    if resource is None:
                        continue
                    try:
                        _, replicated_arn, exc = future.result()
                    except Exception as exc2:
                        replicated_arn = None
                        exc = exc2

                    if exc is None and replicated_arn:
                        resource.status = ReplicationStatus.REPLICATED
                        resource.replicated_arn = replicated_arn
                        resource.error = None
                        resource.error_classification = None
                        # Attach KMS info for resources with encryption
                        try:
                            _attach_kms_info(resource, target_region)
                        except Exception:
                            logger.debug("Failed to attach KMS info to '%s'", resource.name, exc_info=True)
                        with lock:
                            if resource.resource_type == ResourceType.IAM_ROLE:
                                role_arn_mapping[resource.arn] = replicated_arn
                            elif resource.resource_type == ResourceType.LAMBDA:
                                lambda_arn_mapping[resource.arn] = replicated_arn
                            elif resource.resource_type == ResourceType.S3_BUCKET:
                                target_bucket_name = replicated_arn.split(":::")[-1] if ":::" in replicated_arn else ""
                                if target_bucket_name:
                                    s3_bucket_mapping[resource.name] = target_bucket_name
                            elif resource.resource_type == ResourceType.KINESIS_STREAM:
                                kinesis_arn_mapping[resource.arn] = replicated_arn
                            elif resource.resource_type == ResourceType.KINESIS_FIREHOSE:
                                firehose_arn_mapping[resource.arn] = replicated_arn
                        logger.info("Successfully replicated '%s' → %s", resource.name, replicated_arn)
                    elif isinstance(exc, LexAlgrInProgressError):
                        # ALGR replica is enabling asynchronously — mark
                        # IN_PROGRESS (not FAILED) and carry the replica ARN.
                        # Each session-status poll re-checks and flips to
                        # REPLICATED once the replica reaches "Enabled".
                        resource.status = ReplicationStatus.IN_PROGRESS
                        resource.replicated_arn = exc.replicated_arn
                        resource.error = None
                        resource.error_classification = None
                        logger.info(
                            "Lex bot '%s' ALGR replica enabling asynchronously (in progress)",
                            resource.name,
                        )
                    else:
                        from replication.error_classification import classify_error
                        if isinstance(exc, LexAlgrSkippedError):
                            resource.status = ReplicationStatus.SKIPPED
                            resource.error = str(exc)
                        else:
                            resource.status = ReplicationStatus.FAILED
                            resource.error = str(exc)
                            resource.error_classification = classify_error(exc).model_dump()
                            dependents = get_dependents(graph, rid)
                            with lock:
                                for dep_id in dependents:
                                    if dep_id in selected:
                                        blocked_ids.add(dep_id)
                        logger.error("Failed to replicate '%s': %s", resource.name, exc)
        else:
            # Sequential execution (single resource or concurrency=1)
            for rid in active_in_level:
                resource = selected.get(rid)
                if resource is None:
                    continue

                # Skip if already blocked by a failed dependency
                if rid in blocked_ids:
                    resource.status = ReplicationStatus.BLOCKED
                    resource.error = "Blocked: a dependency failed to replicate"
                    logger.info("Resource '%s' (%s) blocked by dependency failure", resource.name, rid)
                    continue

                logger.info(
                    "Replicating resource %s (%s) [%s]",
                    resource.name,
                    rid,
                    resource.resource_type,
                )

                try:
                    replicated_arn = _replicate_single_resource(
                        resource, target_region, role_arn_mapping, lambda_arn_mapping,
                        resource_tags=resource_tags,
                        instance_id=instance_id,
                        layer_arn_mapping=layer_arn_mapping,
                        s3_bucket_mapping=s3_bucket_mapping,
                    )
                    resource.status = ReplicationStatus.REPLICATED
                    resource.replicated_arn = replicated_arn
                    resource.error = None
                    resource.error_classification = None

                    # Attach KMS info for resources with encryption
                    try:
                        _attach_kms_info(resource, target_region)
                    except Exception:
                        logger.debug("Failed to attach KMS info to '%s'", resource.name, exc_info=True)

                    # Update mappings for downstream resources
                    if resource.resource_type == ResourceType.IAM_ROLE:
                        role_arn_mapping[resource.arn] = replicated_arn
                    elif resource.resource_type == ResourceType.LAMBDA:
                        lambda_arn_mapping[resource.arn] = replicated_arn
                    elif resource.resource_type == ResourceType.S3_BUCKET:
                        target_bucket_name = replicated_arn.split(":::")[-1] if ":::" in replicated_arn else ""
                        if target_bucket_name:
                            s3_bucket_mapping[resource.name] = target_bucket_name
                    elif resource.resource_type == ResourceType.KINESIS_STREAM:
                        kinesis_arn_mapping[resource.arn] = replicated_arn
                    elif resource.resource_type == ResourceType.KINESIS_FIREHOSE:
                        firehose_arn_mapping[resource.arn] = replicated_arn

                    logger.info(
                        "Successfully replicated '%s' → %s", resource.name, replicated_arn
                    )

                except LexAlgrInProgressError as exc:
                    # ALGR replica is enabling asynchronously — mark IN_PROGRESS
                    # (not FAILED) and carry the replica ARN. Each session-status
                    # poll re-checks and flips to REPLICATED once "Enabled".
                    # Do NOT cascade-block dependents.
                    resource.status = ReplicationStatus.IN_PROGRESS
                    resource.replicated_arn = exc.replicated_arn
                    resource.error = None
                    resource.error_classification = None
                    logger.info(
                        "Lex bot '%s' (%s) ALGR replica enabling asynchronously (in progress)",
                        resource.name, rid,
                    )

                except Exception as exc:
                    # Handle Lex ALGR skip as a distinct status
                    from replication.error_classification import classify_error
                    if isinstance(exc, LexAlgrSkippedError):
                        resource.status = ReplicationStatus.SKIPPED
                        resource.error = str(exc)
                        logger.info(
                            "Skipped Lex bot '%s' (%s): ALGR failed, fallback disabled",
                            resource.name, rid,
                        )
                    else:
                        error_msg = str(exc)
                        resource.status = ReplicationStatus.FAILED
                        resource.error = error_msg
                        resource.error_classification = classify_error(exc).model_dump()
                        logger.error(
                            "Failed to replicate '%s' (%s): %s", resource.name, rid, error_msg
                        )

                    # Cascade-block all transitive dependents
                    dependents = get_dependents(graph, rid)
                    for dep_id in dependents:
                        if dep_id in selected:
                            blocked_ids.add(dep_id)

        # Update the placeholder job progress after each level (for real-time polling)
        if existing_job_id is not None:
            for rid in level_resources:
                resource = selected.get(rid)
                if resource is not None:
                    session.inventory[rid] = resource
            intermediate_progress = _compute_progress(selected)
            for j in session.replication_jobs:
                if j.job_id == job_id:
                    j.progress = intermediate_progress
                    j.execution_order = execution_order
                    break

            # Persist intermediate progress to DynamoDB so status polling sees updates
            if session_store is not None:
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(session_store.save_session(session))
                    finally:
                        loop.close()
                    logger.debug("Persisted intermediate progress for job %s", job_id)
                except Exception:
                    logger.warning(
                        "Failed to persist intermediate progress for job %s", job_id,
                        exc_info=True,
                    )

    # Update session inventory with final statuses
    for rid, resource in selected.items():
        session.inventory[rid] = resource

    # ---------------------------------------------------------------
    # Post-replication: Storage config association is now deferred
    # to the explicit Associate step (user clicks "Associate" button).
    # See resource_association.associate_resources() for the logic.
    # ---------------------------------------------------------------

    # Compute final progress
    progress = _compute_progress(selected)

    # Determine overall job status
    if progress.failed > 0 or progress.blocked > 0:
        job_status = "FAILED"
    else:
        job_status = "COMPLETED"

    job = ReplicationJob(
        job_id=job_id,
        status=job_status,
        selected_resource_ids=resource_ids,
        execution_order=execution_order,
        progress=progress,
        started_at=now,
        completed_at=datetime.now(timezone.utc),
    )

    if append_job:
        session.replication_jobs.append(job)
    session.updated_at = datetime.now(timezone.utc)

    logger.info(
        "Replication job %s complete: %d/%d succeeded, %d failed, %d blocked",
        job_id,
        progress.completed,
        progress.total,
        progress.failed,
        progress.blocked,
    )

    return job


def retry_resource(
    session: Session,
    job_id: str,
    resource_id: str,
) -> ResourceBase:
    """Retry replication of a single failed resource.

    Only retries the specified resource — does not re-run dependencies
    that already succeeded. If the retry succeeds, also attempts to
    unblock and replicate any previously-blocked dependents.

    Args:
        session: The current session.
        job_id: The replication job ID.
        resource_id: The ID of the failed resource to retry.

    Returns:
        The updated resource after retry.

    Raises:
        ValueError: If the job, resource, or status is invalid.
    """
    # Find the job
    job = None
    for j in session.replication_jobs:
        if j.job_id == job_id:
            job = j
            break

    if job is None:
        raise ValueError(f"Replication job not found: {job_id}")

    # Find the resource
    resource = session.inventory.get(resource_id)
    if resource is None:
        raise ValueError(f"Resource not found: {resource_id}")

    if resource.status not in (ReplicationStatus.FAILED, ReplicationStatus.BLOCKED):
        raise ValueError(
            f"Resource '{resource_id}' is not in FAILED or BLOCKED status "
            f"(current: {resource.status})"
        )

    target_region = session.target_region

    # Build ARN mappings from already-replicated resources in this job
    role_arn_mapping: dict[str, str] = {}
    lambda_arn_mapping: dict[str, str] = {}
    s3_bucket_mapping: dict[str, str] = {}
    for rid in job.selected_resource_ids:
        r = session.inventory.get(rid)
        if r is not None and r.status == ReplicationStatus.REPLICATED and r.replicated_arn:
            if r.resource_type == ResourceType.IAM_ROLE:
                role_arn_mapping[r.arn] = r.replicated_arn
            elif r.resource_type == ResourceType.LAMBDA:
                lambda_arn_mapping[r.arn] = r.replicated_arn
            elif r.resource_type == ResourceType.S3_BUCKET:
                target_bucket = r.replicated_arn.split(":::")[-1] if ":::" in r.replicated_arn else ""
                if target_bucket:
                    s3_bucket_mapping[r.name] = target_bucket

    # Retry the resource
    logger.info("Retrying replication of '%s' (%s)", resource.name, resource_id)

    # IAM roles are global — auto-mark as REPLICATED with their existing ARN
    if resource.resource_type == ResourceType.IAM_ROLE:
        resource.status = ReplicationStatus.REPLICATED
        resource.replicated_arn = resource.arn
        resource.error = None
        role_arn_mapping[resource.arn] = resource.arn
        logger.info(
            "IAM role '%s' is global — auto-marked REPLICATED on retry (ARN: %s)",
            resource.name, resource.arn,
        )
        # Attempt to unblock and replicate dependents
        _retry_blocked_dependents(
            session, job, resource_id, target_region, role_arn_mapping, lambda_arn_mapping,
            resource_tags=session.resource_tags if hasattr(session, 'resource_tags') else None,
            s3_bucket_mapping=s3_bucket_mapping,
        )
    else:
        resource.status = ReplicationStatus.IN_PROGRESS
        resource.error = None

        try:
            replicated_arn = _replicate_single_resource(
                resource, target_region, role_arn_mapping, lambda_arn_mapping,
                resource_tags=session.resource_tags if hasattr(session, 'resource_tags') else None,
                s3_bucket_mapping=s3_bucket_mapping,
            )
            resource.status = ReplicationStatus.REPLICATED
            resource.replicated_arn = replicated_arn
            resource.error = None

            # Update mappings
            if resource.resource_type == ResourceType.LAMBDA:
                lambda_arn_mapping[resource.arn] = replicated_arn
            elif resource.resource_type == ResourceType.S3_BUCKET:
                target_bucket = replicated_arn.split(":::")[-1] if ":::" in replicated_arn else ""
                if target_bucket:
                    s3_bucket_mapping[resource.name] = target_bucket

            logger.info("Retry succeeded for '%s' → %s", resource.name, replicated_arn)

            # Attempt to unblock and replicate dependents
            _retry_blocked_dependents(
                session, job, resource_id, target_region, role_arn_mapping, lambda_arn_mapping,
                resource_tags=session.resource_tags if hasattr(session, 'resource_tags') else None,
                s3_bucket_mapping=s3_bucket_mapping,
            )

        except LexAlgrInProgressError as exc:
            # ALGR replica is enabling asynchronously — mark IN_PROGRESS
            # (not FAILED) and carry the replica ARN. Each session-status
            # poll re-checks and flips to REPLICATED once "Enabled".
            resource.status = ReplicationStatus.IN_PROGRESS
            resource.replicated_arn = exc.replicated_arn
            resource.error = None
            logger.info(
                "Lex bot '%s' ALGR replica enabling asynchronously on retry (in progress)",
                resource.name,
            )

        except Exception as exc:
            error_msg = str(exc)
            resource.status = ReplicationStatus.FAILED
            resource.error = error_msg
            logger.error("Retry failed for '%s': %s", resource.name, error_msg)

    # Update job progress
    selected = {
        rid: session.inventory[rid]
        for rid in job.selected_resource_ids
        if rid in session.inventory
    }
    job.progress = _compute_progress(selected)

    # Update job status
    if job.progress.failed > 0 or job.progress.blocked > 0:
        job.status = "FAILED"
    else:
        job.status = "COMPLETED"

    session.updated_at = datetime.now(timezone.utc)

    return resource


def _retry_blocked_dependents(
    session: Session,
    job: ReplicationJob,
    parent_id: str,
    target_region: str,
    role_arn_mapping: dict[str, str],
    lambda_arn_mapping: dict[str, str],
    resource_tags: dict[str, str] | None = None,
    s3_bucket_mapping: dict[str, str] | None = None,
) -> None:
    """After a successful retry, attempt to replicate previously-blocked dependents.

    Only processes direct dependents whose dependencies are all now REPLICATED.
    """
    selected_resources = [
        session.inventory[rid]
        for rid in job.selected_resource_ids
        if rid in session.inventory
    ]
    graph = build_dependency_graph(selected_resources)

    # Get direct dependents of the retried resource
    direct_dependents = graph.get(parent_id, [])

    for dep_id in direct_dependents:
        dep_resource = session.inventory.get(dep_id)
        if dep_resource is None or dep_resource.status != ReplicationStatus.BLOCKED:
            continue

        # Check if all dependencies are now replicated
        all_deps_ok = all(
            session.inventory.get(d) is not None
            and session.inventory[d].status == ReplicationStatus.REPLICATED
            for d in dep_resource.dependencies
            if d in session.inventory
        )

        if not all_deps_ok:
            continue

        logger.info(
            "Unblocking and replicating dependent '%s' (%s)",
            dep_resource.name,
            dep_id,
        )
        dep_resource.status = ReplicationStatus.IN_PROGRESS
        dep_resource.error = None

        try:
            replicated_arn = _replicate_single_resource(
                dep_resource, target_region, role_arn_mapping, lambda_arn_mapping,
                resource_tags=resource_tags,
                s3_bucket_mapping=s3_bucket_mapping,
            )
            dep_resource.status = ReplicationStatus.REPLICATED
            dep_resource.replicated_arn = replicated_arn
            dep_resource.error = None

            if dep_resource.resource_type == ResourceType.IAM_ROLE:
                role_arn_mapping[dep_resource.arn] = replicated_arn
            elif dep_resource.resource_type == ResourceType.LAMBDA:
                lambda_arn_mapping[dep_resource.arn] = replicated_arn
            elif dep_resource.resource_type == ResourceType.S3_BUCKET:
                if s3_bucket_mapping is not None:
                    target_bucket = replicated_arn.split(":::")[-1] if ":::" in replicated_arn else ""
                    if target_bucket:
                        s3_bucket_mapping[dep_resource.name] = target_bucket

            logger.info(
                "Unblocked dependent '%s' replicated → %s",
                dep_resource.name,
                replicated_arn,
            )

            # Recursively try to unblock further dependents
            _retry_blocked_dependents(
                session, job, dep_id, target_region, role_arn_mapping, lambda_arn_mapping,
                resource_tags=resource_tags,
                s3_bucket_mapping=s3_bucket_mapping,
            )

        except LexAlgrInProgressError as exc:
            # ALGR replica enabling asynchronously — mark IN_PROGRESS, not FAILED.
            dep_resource.status = ReplicationStatus.IN_PROGRESS
            dep_resource.replicated_arn = exc.replicated_arn
            dep_resource.error = None
            logger.info(
                "Dependent Lex bot '%s' ALGR replica enabling asynchronously (in progress)",
                dep_resource.name,
            )

        except Exception as exc:
            dep_resource.status = ReplicationStatus.FAILED
            dep_resource.error = str(exc)
            logger.error(
                "Failed to replicate unblocked dependent '%s': %s",
                dep_resource.name,
                exc,
            )


def _update_replica_storage_configs(
    session: Session,
    s3_bucket_mapping: dict[str, str],
    kinesis_arn_mapping: dict[str, str],
    firehose_arn_mapping: dict[str, str],
) -> None:
    """Attempt to update the replica instance's storage configs after replication.

    This is a best-effort operation — failures are logged but don't fail the job.
    Only runs if the session has replica instance info stored from validation.
    """
    from aws.arn_utils import parse_arn

    instance_arn = session.instance_arn
    try:
        parsed = parse_arn(instance_arn)
    except ValueError:
        logger.warning("Cannot parse instance ARN for storage config update: %s", instance_arn)
        return

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        return

    source_instance_id = resource_part.split("/", 1)[1]
    source_region = session.source_region
    target_region = session.target_region

    # We need to check if a replica exists. Try to describe the instance in the target region.
    from aws.client_factory import create_target_client
    try:
        target_connect = create_target_client("connect", target_region)
        resp = target_connect.describe_instance(InstanceId=source_instance_id)
        replica_status = resp.get("Instance", {}).get("InstanceStatus", "")
        if replica_status != "ACTIVE":
            logger.info(
                "Replica instance %s in %s is not ACTIVE (status: %s), skipping storage config update",
                source_instance_id, target_region, replica_status,
            )
            return
    except Exception:
        logger.info(
            "No replica instance found in %s for %s, skipping storage config update",
            target_region, source_instance_id,
        )
        return

    # Replica exists and is active — update storage configs
    logger.info(
        "Updating replica instance storage configs: %d S3 buckets, %d Kinesis, %d Firehose",
        len(s3_bucket_mapping), len(kinesis_arn_mapping), len(firehose_arn_mapping),
    )

    try:
        from replication.storage_config_replication import update_replica_storage_configs

        results = update_replica_storage_configs(
            replica_instance_id=source_instance_id,
            target_region=target_region,
            source_instance_id=source_instance_id,
            source_region=source_region,
            s3_bucket_mapping=s3_bucket_mapping,
            kinesis_arn_mapping=kinesis_arn_mapping,
            firehose_arn_mapping=firehose_arn_mapping,
        )

        success_count = sum(1 for r in results if r.get("status") == "success")
        error_count = sum(1 for r in results if r.get("status") == "error")
        logger.info(
            "Storage config update complete: %d succeeded, %d failed",
            success_count, error_count,
        )
    except Exception:
        logger.exception("Failed to update replica storage configs (non-fatal)")
