"""REST API routes for the Connect ACGR Resource Replicator."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from aws.arn_utils import extract_region, parse_arn, resolve_target_region
from aws.client_factory import create_client, create_source_client, create_target_client
from discovery.orchestrator import run_discovery
from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase
from models.session import ReplicationJob, ReplicationProgress, Session
from audit.target_region_audit import audit_target_region
from replication.orchestrator import run_replication, retry_resource
from store.factory import create_session_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_error_detail(exc: Exception, user_message: str) -> str:
    """Log the full exception server-side at ERROR level and return a safe
    user-facing message.

    The return value contains no raw exception content, class name, or
    traceback. The full exception is captured in CloudWatch via
    logger.exception().
    """
    logger.exception(user_message)
    return user_message


# Shared session store instance
_session_store = create_session_store()

# Regex for basic ARN format validation.
# The account-id group allows 0-12 digits so that service ARNs with an empty
# account field (notably S3, e.g. "arn:aws:s3:::my-bucket") validate, while a
# non-numeric account (garbage) is still rejected.
_ARN_PATTERN = re.compile(
    r"^arn:[a-z\-]+:[a-z0-9\-]+:[a-z0-9\-]*:\d{0,12}:.+"
)


def _validate_arn_format(arn: str, field_name: str = "ARN") -> str:
    """Validate that a string looks like a valid ARN. Returns the ARN or raises HTTPException."""
    if not arn or not arn.strip():
        raise HTTPException(status_code=400, detail=f"{field_name} must not be empty")
    arn = arn.strip()
    if not _ARN_PATTERN.match(arn):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ARN format for {field_name}: '{arn}'. "
            "Expected format: arn:<partition>:<service>:<region>:<account-id>:<resource>",
        )
    return arn


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ValidateInstanceRequest(BaseModel):
    instanceArn: str

    @field_validator("instanceArn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("instanceArn must not be empty")
        if not _ARN_PATTERN.match(v):
            raise ValueError(
                f"Invalid ARN format: '{v}'. "
                "Expected format: arn:<partition>:<service>:<region>:<account-id>:<resource>"
            )
        return v


class ValidateInstanceResponse(BaseModel):
    instanceId: str
    instanceName: str
    instanceArn: str
    sourceRegion: str
    targetRegion: str
    status: str
    identityManagementType: str  # SAML, CONNECT_MANAGED, EXISTING_DIRECTORY
    isSaml: bool
    hasReplica: bool
    replicaArn: str | None = None
    replicaRegion: str | None = None
    replicaStatus: str | None = None  # e.g. INSTANCE_REPLICATION_COMPLETE
    replicaAlias: str | None = None


class DiscoverRequest(BaseModel):
    instanceArn: str

    @field_validator("instanceArn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("instanceArn must not be empty")
        if not _ARN_PATTERN.match(v):
            raise ValueError(
                f"Invalid ARN format: '{v}'. "
                "Expected format: arn:<partition>:<service>:<region>:<account-id>:<resource>"
            )
        return v


class DiscoverResponse(BaseModel):
    sessionId: str
    inventory: list[dict[str, Any]]


class AddResourceRequest(BaseModel):
    arn: str

    @field_validator("arn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("arn must not be empty")
        if not _ARN_PATTERN.match(v):
            raise ValueError(
                f"Invalid ARN format: '{v}'. "
                "Expected format: arn:<partition>:<service>:<region>:<account-id>:<resource>"
            )
        return v


# ---------------------------------------------------------------------------
# POST /api/validate-instance
# ---------------------------------------------------------------------------

@router.post("/api/validate-instance", response_model=ValidateInstanceResponse)
async def validate_instance(request: ValidateInstanceRequest):
    """Validate a Connect instance ARN, resolve the ACGR region pair, and return instance details.

    1. Parse the ARN and extract the source region.
    2. Resolve the target region via the ACGR pair mapping (400 if unsupported).
    3. Call Connect DescribeInstance to verify the instance exists and is ACTIVE.
    4. Return instance details including both regions.
    """
    instance_arn = request.instanceArn

    # --- Parse ARN and extract region ---
    try:
        parsed = parse_arn(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    if parsed["service"] != "connect":
        raise HTTPException(
            status_code=400,
            detail=f"ARN is not a Connect resource. Expected service 'connect', got '{parsed['service']}'.",
        )

    try:
        source_region = extract_region(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    # --- Resolve ACGR target region ---
    try:
        target_region = resolve_target_region(source_region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    # --- Extract instance ID from the resource part ---
    resource_part = parsed["resource"]  # e.g. "instance/abc-123-def"
    if not resource_part.startswith("instance/"):
        # nosemgrep: python.django.security.injection.raw-html-format
        # Not Django, not HTML — this is a FastAPI JSON 400 detail string. resource_part
        # is the "resource" portion of a Connect ARN parsed by validate_arn(); it's not
        # raw user-controlled HTML. The error is rendered as JSON by FastAPI, never as HTML.
        raise HTTPException(
            status_code=400,
            detail=f"ARN resource is not a Connect instance. Expected 'instance/<id>', got '{resource_part}'.",
        )
    instance_id = resource_part.split("/", 1)[1]

    # --- Call Connect DescribeInstance ---
    try:
        connect_client = create_source_client("connect", source_region)
        response = connect_client.describe_instance(InstanceId=instance_id)
    except connect_client.exceptions.ResourceNotFoundException:
        raise HTTPException(
            status_code=404,
            detail=f"Connect instance not found: {instance_arn}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Error validating Connect instance"),
        )

    instance = response.get("Instance", {})
    instance_status = instance.get("InstanceStatus", "UNKNOWN")

    if instance_status != "ACTIVE":
        raise HTTPException(
            status_code=400,
            detail=f"Connect instance is not ACTIVE. Current status: {instance_status}",
        )

    # --- Identity management type (SAML detection) ---
    identity_mgmt_type = instance.get("IdentityManagementType", "UNKNOWN")
    is_saml = identity_mgmt_type in ("SAML", "SAML_2_0")

    # --- Replica detection via ReplicationConfiguration ---
    has_replica = False
    replica_arn: str | None = None
    replica_region: str | None = None
    replica_status: str | None = None

    replication_config = response.get("ReplicationConfiguration")
    if replication_config:
        status_list = replication_config.get("ReplicationStatusSummaryList", [])
        for entry in status_list:
            entry_region = entry.get("Region", "")
            # The replica is in a different region than the source
            if entry_region and entry_region != source_region:
                has_replica = True
                replica_region = entry_region
                replica_status = entry.get("ReplicationStatus", "")
                # Build the replica instance ARN (same instance ID, different region)
                replica_arn = f"arn:{parsed['partition']}:connect:{entry_region}:{parsed['account']}:instance/{instance_id}"
                break

    # If no ReplicationConfiguration, also try ListTrafficDistributionGroups as fallback
    if not has_replica:
        try:
            tdg_response = connect_client.list_traffic_distribution_groups(
                InstanceId=instance_id, MaxResults=10
            )
            tdg_list = tdg_response.get("TrafficDistributionGroupSummaryList", [])
            for tdg in tdg_list:
                if tdg.get("IsDefault") and tdg.get("Status") == "ACTIVE":
                    # A default TDG exists — instance has been replicated
                    has_replica = True
                    replica_region = target_region
                    replica_arn = f"arn:{parsed['partition']}:connect:{target_region}:{parsed['account']}:instance/{instance_id}"
                    replica_status = "ACTIVE"
                    break
        except Exception:
            logger.debug("ListTrafficDistributionGroups fallback failed — not critical")

    # --- Fetch replica instance alias if replica exists ---
    replica_alias: str | None = None
    if has_replica and replica_region:
        try:
            replica_connect = create_target_client("connect", replica_region)
            replica_resp = replica_connect.describe_instance(InstanceId=instance_id)
            replica_alias = replica_resp.get("Instance", {}).get("InstanceAlias", "")
        except Exception:
            logger.debug("Failed to fetch replica instance alias — not critical")

    return ValidateInstanceResponse(
        instanceId=instance.get("Id", instance_id),
        instanceName=instance.get("InstanceAlias", ""),
        instanceArn=instance_arn,
        sourceRegion=source_region,
        targetRegion=target_region,
        status=instance_status,
        identityManagementType=identity_mgmt_type,
        isSaml=is_saml,
        hasReplica=has_replica,
        replicaArn=replica_arn,
        replicaRegion=replica_region,
        replicaStatus=replica_status,
        replicaAlias=replica_alias,
    )


# ---------------------------------------------------------------------------
# POST /api/discover
# ---------------------------------------------------------------------------

@router.post("/api/discover", response_model=DiscoverResponse)
async def discover(request: DiscoverRequest):
    """Run full discovery for a Connect instance and return the resource inventory.

    1. Parse the instance ARN and extract region + instance ID.
    2. Resolve the ACGR target region.
    3. Run the discovery orchestrator across all resource types.
    4. Persist the session with the inventory.
    5. Return the session ID and inventory as JSON.
    """
    instance_arn = request.instanceArn

    # --- Parse ARN ---
    try:
        parsed = parse_arn(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    if parsed["service"] != "connect":
        raise HTTPException(
            status_code=400,
            detail=f"ARN is not a Connect resource. Expected service 'connect', got '{parsed['service']}'.",
        )

    try:
        source_region = extract_region(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    try:
        target_region = resolve_target_region(source_region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        # nosemgrep: python.django.security.injection.raw-html-format
        # Not Django, not HTML — this is a FastAPI JSON 400 detail string. resource_part
        # is the parsed ARN resource segment (validated above), rendered as JSON, never HTML.
        raise HTTPException(
            status_code=400,
            detail=f"ARN resource is not a Connect instance. Expected 'instance/<id>', got '{resource_part}'.",
        )
    instance_id = resource_part.split("/", 1)[1]

    # --- Run discovery ---
    try:
        session_id, inventory = run_discovery(instance_id, source_region)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Discovery failed"),
        )

    # --- Build and persist session ---
    now = datetime.now(timezone.utc)
    inventory_dict = {r.id: r for r in inventory.get_all()}

    session = Session(
        session_id=session_id,
        instance_arn=instance_arn,
        instance_name="",
        source_region=source_region,
        target_region=target_region,
        inventory=inventory_dict,
        created_at=now,
        updated_at=now,
    )

    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    # --- Return response ---
    inventory_list = [r.model_dump() for r in inventory.get_all()]
    return DiscoverResponse(sessionId=session_id, inventory=inventory_list)


# ---------------------------------------------------------------------------
# GET /api/inventory/{session_id}
# ---------------------------------------------------------------------------

@router.get("/api/inventory/{session_id}")
async def get_inventory(session_id: str):
    """Retrieve the resource inventory for an existing session.

    Loads the session from the store and returns the inventory as JSON.
    """
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    inventory_list = [r.model_dump() for r in session.inventory.values()]
    return {
        "sessionId": session.session_id,
        "sourceRegion": session.source_region,
        "targetRegion": session.target_region,
        "instanceArn": session.instance_arn,
        "inventory": inventory_list,
    }


# ---------------------------------------------------------------------------
# Replication request / response models
# ---------------------------------------------------------------------------

class ReplicateRequest(BaseModel):
    sessionId: str
    resourceIds: list[str]
    resourceTags: dict[str, str] = {}
    dryRun: bool = False
    concurrency: int = 1
    async_mode: bool | None = None  # None = auto (async for >5 resources)

    @field_validator("sessionId")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sessionId must not be empty")
        return v

    @field_validator("resourceIds")
    @classmethod
    def validate_resource_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("resourceIds must not be empty")
        return v

    @field_validator("concurrency")
    @classmethod
    def validate_concurrency(cls, v: int) -> int:
        return max(1, min(v, 10))


class ReplicateResponse(BaseModel):
    jobId: str
    sessionId: str
    status: str
    progress: dict[str, int]
    sfnExecutionArn: str | None = None


class ReplicateStatusResponse(BaseModel):
    jobId: str
    status: str
    progress: dict[str, int]
    resources: list[dict[str, Any]]


class RetryFailedResponse(BaseModel):
    """Response model for the bulk retry-failed endpoint."""
    jobId: str
    sessionId: str
    status: str
    progress: dict[str, int]
    retriedResourceIds: list[str]


class AuditRequest(BaseModel):
    """Request model for the audit endpoint."""
    resourceTags: dict[str, str] = {}


class AuditResponse(BaseModel):
    """Response model for the audit endpoint."""
    sessionId: str
    inventory: list[dict[str, Any]]
    auditSummary: dict[str, int]



# ---------------------------------------------------------------------------
# POST /api/replicate
# ---------------------------------------------------------------------------

@router.post("/api/replicate", response_model=ReplicateResponse)
async def replicate(request: ReplicateRequest):
    """Start replication for selected resources in a session.

    Creates a placeholder job, persists it, then invokes this Lambda
    asynchronously (InvocationType='Event') to run the actual replication.
    The frontend polls /api/replicate/{job_id}/status for progress.

    Previous approach used threading.Thread which doesn't work in Lambda
    because Lambda freezes the execution environment after the response.
    """
    session_id = request.sessionId
    resource_ids = request.resourceIds

    if not resource_ids:
        raise HTTPException(status_code=400, detail="No resource IDs provided")

    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Validate resource IDs exist in inventory
    missing = [rid for rid in resource_ids if rid not in session.inventory]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Resource IDs not found in inventory: {missing}",
        )

    # Create a placeholder job so the status endpoint can find it immediately
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    placeholder_job = ReplicationJob(
        job_id=job_id,
        status="IN_PROGRESS",
        selected_resource_ids=resource_ids,
        execution_order=[],
        progress=ReplicationProgress(
            total=len(resource_ids), completed=0, failed=0, blocked=0
        ),
        started_at=now,
    )
    session.replication_jobs.append(placeholder_job)

    # Mark all selected resources as IN_PROGRESS
    for rid in resource_ids:
        resource = session.inventory.get(rid)
        if resource is not None:
            resource.status = ReplicationStatus.IN_PROGRESS
            resource.error = None

    # Persist the session with the placeholder job
    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    # Invoke this Lambda asynchronously to run the replication.
    # Lambda async invocation runs in a separate execution context with
    # the full 900s timeout, unlike background threads which get frozen.
    resource_tags = request.resourceTags or {}
    dry_run = request.dryRun
    concurrency = request.concurrency

    # Persist resource_tags on session so retries can use them
    session.resource_tags = resource_tags

    # Re-save session with resource_tags
    try:
        await _session_store.save_session(session)
    except Exception:
        logger.debug("Failed to re-save session with resource_tags (non-fatal)")

    if dry_run:
        # Dry run: simulate replication without making AWS calls
        from replication.dry_run import simulate_replication
        dry_result = simulate_replication(session, resource_ids, job_id, resource_tags)
        # Update session with dry run results
        for rid, resource in dry_result.items():
            session.inventory[rid] = resource
        # Update the placeholder job
        for j in session.replication_jobs:
            if j.job_id == job_id:
                j.status = "COMPLETED"
                j.progress = ReplicationProgress(
                    total=len(resource_ids), completed=len(resource_ids), failed=0, blocked=0
                )
                j.completed_at = datetime.now(timezone.utc)
                break
        session.updated_at = datetime.now(timezone.utc)
        try:
            await _session_store.save_session(session)
        except Exception:
            logger.debug("Failed to persist dry run session (non-fatal)")
        return ReplicateResponse(
            jobId=job_id,
            sessionId=session_id,
            status="COMPLETED",
            progress={"total": len(resource_ids), "completed": len(resource_ids), "failed": 0, "blocked": 0},
        )

    # Determine whether to use Step Functions (async) or Lambda self-invocation (sync)
    use_sfn = _should_use_step_functions(request.async_mode, len(resource_ids))

    if use_sfn:
        try:
            from replication.step_functions_orchestrator import start_replication_execution
            execution_arn = start_replication_execution(
                session, resource_ids, job_id, resource_tags
            )
            session.sfn_execution_arn = execution_arn
            await _session_store.save_session(session)
            return ReplicateResponse(
                jobId=job_id,
                sessionId=session_id,
                status="IN_PROGRESS",
                progress={"total": len(resource_ids), "completed": 0, "failed": 0, "blocked": 0},
                sfnExecutionArn=execution_arn,
            )
        except RuntimeError:
            # STATE_MACHINE_ARN not set — fall back to Lambda self-invocation
            logger.warning("Step Functions not available, falling back to Lambda self-invocation")

    _invoke_replication_async(session_id, resource_ids, job_id, resource_tags, concurrency)

    return ReplicateResponse(
        jobId=job_id,
        sessionId=session_id,
        status="IN_PROGRESS",
        progress={"total": len(resource_ids), "completed": 0, "failed": 0, "blocked": 0},
    )


def _invoke_replication_async(session_id: str, resource_ids: list[str], job_id: str, resource_tags: dict[str, str] | None = None, concurrency: int = 1) -> None:
    """Invoke this Lambda function asynchronously to run replication.

    Uses InvocationType='Event' so the call returns immediately (202)
    and the replication runs in a separate AWS Lambda execution context.

    When not running in Lambda (local dev/tests), falls back to a
    background thread (which works fine outside Lambda since the process
    doesn't freeze between requests).
    """
    import threading

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        # Not running in Lambda (local dev/tests) — use background thread
        # with the shared in-memory session store
        logger.info("Not in Lambda; running replication in background thread")

        def _bg():
            _run_replication_with_store(session_id, resource_ids, job_id, _session_store, resource_tags, concurrency)

        thread = threading.Thread(target=_bg, daemon=True)
        thread.start()
        return

    import boto3

    payload = {
        "_replication_task": True,
        "session_id": session_id,
        "resource_ids": resource_ids,
        "job_id": job_id,
        "resource_tags": resource_tags or {},
        "concurrency": concurrency,
    }

    try:
        lambda_client = boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # Async — returns 202 immediately
            Payload=json.dumps(payload).encode(),
        )
        logger.info(
            "Async replication invocation sent for job %s (session %s, %d resources)",
            job_id, session_id, len(resource_ids),
        )
    except Exception:
        logger.exception("Failed to invoke async replication for job %s", job_id)
        # Mark job as failed so the frontend doesn't poll forever
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            store = create_session_store()
            session = loop.run_until_complete(store.get_session(session_id))
            if session:
                for j in session.replication_jobs:
                    if j.job_id == job_id:
                        j.status = "FAILED"
                        j.progress.failed = j.progress.total
                        break
                for rid in resource_ids:
                    r = session.inventory.get(rid)
                    if r:
                        r.status = ReplicationStatus.FAILED
                        r.error = "Failed to start async replication"
                loop.run_until_complete(store.save_session(session))
        finally:
            loop.close()


def _run_replication_with_store(
    session_id: str, resource_ids: list[str], job_id: str, store, resource_tags: dict[str, str] | None = None, concurrency: int = 1
) -> None:
    """Run replication using the provided session store.

    Used by background threads (local dev) and async Lambda invocations.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        session = loop.run_until_complete(store.get_session(session_id))
        if session is None:
            logger.error("Session %s not found for replication job %s", session_id, job_id)
            return

        job = run_replication(session, resource_ids, existing_job_id=job_id, session_store=store, resource_tags=resource_tags, concurrency=concurrency)

        # Update the placeholder job in the session
        for i, j in enumerate(session.replication_jobs):
            if j.job_id == job_id:
                session.replication_jobs[i] = job
                break

        # Persist final results
        loop.run_until_complete(store.save_session(session))
        logger.info("Replication job %s completed and persisted", job_id)
    except Exception:
        logger.exception("Replication failed for job %s", job_id)
        try:
            session = loop.run_until_complete(store.get_session(session_id))
            if session:
                for j in session.replication_jobs:
                    if j.job_id == job_id:
                        j.status = "FAILED"
                        break
                for rid in resource_ids:
                    r = session.inventory.get(rid)
                    if r and r.status == ReplicationStatus.IN_PROGRESS:
                        r.status = ReplicationStatus.FAILED
                        r.error = "Replication process crashed"
                loop.run_until_complete(store.save_session(session))
        except Exception:
            logger.exception("Failed to mark job %s as failed", job_id)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# GET /api/replicate/{job_id}/status
# ---------------------------------------------------------------------------

@router.get("/api/replicate/{job_id}/status", response_model=ReplicateStatusResponse)
async def get_replication_status(job_id: str, session_id: str | None = None):
    """Poll replication progress for a given job.

    Accepts an optional session_id query param to look up the job directly
    from the session store (required for DynamoDB-backed deployments).
    Falls back to scanning in-memory store if no session_id is provided.
    """
    store = _session_store
    session = None
    job = None

    # If session_id is provided, look up directly (works with Amazon DynamoDB store)
    if session_id:
        try:
            session = await store.get_session(session_id)
        except Exception:
            logger.exception("Failed to load session %s for job lookup", session_id)

        if session is not None:
            for j in session.replication_jobs:
                if j.job_id == job_id:
                    job = j
                    break

    # Fallback: scan in-memory store if no session_id or not found
    if job is None and hasattr(store, "_sessions"):
        for s in store._sessions.values():
            for j in s.replication_jobs:
                if j.job_id == job_id:
                    session = s
                    job = j
                    break
            if job:
                break

    if job is None:
        raise HTTPException(status_code=404, detail=f"Replication job not found: {job_id}")

    # Build per-resource status list
    resource_statuses = []
    for rid in job.selected_resource_ids:
        resource = session.inventory.get(rid)
        if resource is not None:
            resource_statuses.append(resource.model_dump())

    return ReplicateStatusResponse(
        jobId=job.job_id,
        status=job.status,
        progress=job.progress.model_dump(),
        resources=resource_statuses,
    )


# ---------------------------------------------------------------------------
# POST /api/replicate/{job_id}/retry/{resource_id}
# ---------------------------------------------------------------------------

@router.post("/api/replicate/{job_id}/retry/{resource_id}")
async def retry_failed_resource(job_id: str, resource_id: str, session_id: str | None = None):
    """Retry replication of a single failed or blocked resource.

    Finds the session containing the job, retries the resource, persists
    the updated session, and returns the updated resource status.

    Accepts an optional session_id query param to look up the job directly
    from the session store (required for DynamoDB-backed deployments).
    Falls back to scanning in-memory store if no session_id is provided.
    """
    store = _session_store

    # Find the session containing this job
    session = None
    job = None

    # If session_id is provided, look up directly (works with Amazon DynamoDB store)
    if session_id:
        try:
            session = await store.get_session(session_id)
        except Exception:
            logger.exception("Failed to load session %s for retry lookup", session_id)

        if session is not None:
            for j in session.replication_jobs:
                if j.job_id == job_id:
                    job = j
                    break

    # Fallback: scan in-memory store if no session_id or not found
    if job is None and hasattr(store, "_sessions"):
        for s in store._sessions.values():
            for j in s.replication_jobs:
                if j.job_id == job_id:
                    session = s
                    job = j
                    break
            if job:
                break

    if job is None:
        raise HTTPException(status_code=404, detail=f"Replication job not found: {job_id}")

    # Retry the resource
    try:
        updated_resource = retry_resource(session, job_id, resource_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Retry failed"),
        )

    # Persist updated session
    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session after retry"),
        )

    return {
        "resourceId": resource_id,
        "status": updated_resource.status,
        "replicatedArn": updated_resource.replicated_arn,
        "error": updated_resource.error,
        "jobProgress": job.progress.model_dump(),
    }

# ---------------------------------------------------------------------------
# POST /api/retry-failed/{session_id} — Bulk retry all FAILED/BLOCKED resources
# ---------------------------------------------------------------------------

@router.post("/api/retry-failed/{session_id}", response_model=RetryFailedResponse)
async def retry_failed(session_id: str):
    """Bulk retry all FAILED and BLOCKED resources in a session.

    1. Load the session from the store.
    2. Filter inventory for resources with FAILED or BLOCKED status.
    3. Reset those resources to NOT_REPLICATED, then mark IN_PROGRESS.
    4. Create a new ReplicationJob and invoke async replication.
    5. Return the job details and list of retried resource IDs.
    """
    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Filter for FAILED and BLOCKED resources
    failed_resource_ids = [
        rid for rid, resource in session.inventory.items()
        if resource.status in (ReplicationStatus.FAILED, ReplicationStatus.BLOCKED)
    ]

    if not failed_resource_ids:
        raise HTTPException(
            status_code=400,
            detail="No failed or blocked resources to retry",
        )

    # Reset failed/blocked resources to NOT_REPLICATED, then mark IN_PROGRESS
    for rid in failed_resource_ids:
        resource = session.inventory[rid]
        resource.status = ReplicationStatus.IN_PROGRESS
        resource.error = None

    # Create a new ReplicationJob
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    placeholder_job = ReplicationJob(
        job_id=job_id,
        status="IN_PROGRESS",
        selected_resource_ids=failed_resource_ids,
        execution_order=[],
        progress=ReplicationProgress(
            total=len(failed_resource_ids), completed=0, failed=0, blocked=0
        ),
        started_at=now,
    )
    session.replication_jobs.append(placeholder_job)

    # Persist session with the new job
    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    # Use session.resource_tags for tags
    resource_tags = session.resource_tags if hasattr(session, 'resource_tags') else {}

    _invoke_replication_async(session_id, failed_resource_ids, job_id, resource_tags)

    return RetryFailedResponse(
        jobId=job_id,
        sessionId=session_id,
        status="IN_PROGRESS",
        progress={"total": len(failed_resource_ids), "completed": 0, "failed": 0, "blocked": 0},
        retriedResourceIds=failed_resource_ids,
    )


# ---------------------------------------------------------------------------
# POST /api/audit/{session_id} — Target region audit
# ---------------------------------------------------------------------------

@router.post("/api/audit/{session_id}", response_model=AuditResponse)
async def audit_session(session_id: str, request: AuditRequest):
    """Audit the target region to check which resources already exist.

    1. Load the session from the store.
    2. Call audit_target_region() with the session inventory.
    3. Apply audit results to session inventory (update statuses and replicated_arn).
    4. Store resourceTags on session for later use.
    5. Persist updated session.
    6. Return inventory and audit summary counts.
    """
    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Run the audit
    audit_result = audit_target_region(
        session.inventory, session.target_region, ""
    )

    # Apply audit results to session inventory
    for resource_id, entry in audit_result.entries.items():
        resource = session.inventory.get(resource_id)
        if resource is None:
            continue

        if entry.exists_in_target:
            resource.status = ReplicationStatus.REPLICATED
            resource.replicated_arn = entry.target_arn
        elif entry.audit_error:
            # Keep status as NOT_REPLICATED, set error
            resource.status = ReplicationStatus.NOT_REPLICATED
            resource.error = entry.audit_error
        else:
            # Not found, no error — keep as NOT_REPLICATED
            resource.status = ReplicationStatus.NOT_REPLICATED

    # Store resourceTags on session
    session.resource_tags = request.resourceTags

    # Update timestamp
    session.updated_at = datetime.now(timezone.utc)

    # Persist updated session
    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    # Build response
    inventory_list = [r.model_dump() for r in session.inventory.values()]
    audit_summary = {
        "alreadyReplicated": audit_result.already_replicated_count,
        "missing": audit_result.missing_count,
        "errors": audit_result.error_count,
    }

    return AuditResponse(
        sessionId=session.session_id,
        inventory=inventory_list,
        auditSummary=audit_summary,
    )



# ---------------------------------------------------------------------------
# POST /api/inventory/{session_id}/resources — Manual resource addition (Task 6.7)
# ---------------------------------------------------------------------------

# Map of ARN service names to ResourceType
_SERVICE_TO_RESOURCE_TYPE: dict[str, ResourceType] = {
    "lambda": ResourceType.LAMBDA,
    "lex": ResourceType.LEX_BOT,
    "kinesis": ResourceType.KINESIS_STREAM,
    "firehose": ResourceType.KINESIS_FIREHOSE,
    "kinesisvideo": ResourceType.KINESIS_VIDEO_STREAM,
    "iam": ResourceType.IAM_ROLE,
    "s3": ResourceType.S3_BUCKET,
}


@router.post("/api/inventory/{session_id}/resources")
async def add_resource_to_inventory(session_id: str, request: AddResourceRequest):
    """Manually add a resource ARN to an existing session's inventory.

    Parses the ARN to determine the resource type, creates a ResourceBase,
    and adds it to the session inventory.
    """
    arn = request.arn

    # Parse the ARN
    try:
        parsed = parse_arn(arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    service = parsed["service"]
    resource_type = _SERVICE_TO_RESOURCE_TYPE.get(service)
    if resource_type is None:
        supported = ", ".join(sorted(_SERVICE_TO_RESOURCE_TYPE.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported service '{service}' in ARN. Supported services: {supported}",
        )

    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Extract a human-readable name from the resource part of the ARN
    resource_part = parsed["resource"]
    # Resource part can be "type/name", "type:name", or just "name"
    if "/" in resource_part:
        name = resource_part.split("/")[-1]
    elif ":" in resource_part:
        name = resource_part.split(":")[-1]
    else:
        name = resource_part

    # Generate a unique resource ID
    resource_id = str(uuid.uuid4())

    # Check for duplicate ARN
    for existing in session.inventory.values():
        if existing.arn == arn:
            raise HTTPException(
                status_code=409,
                detail=f"Resource with ARN '{arn}' already exists in inventory",
            )

    # Create the resource
    resource = ResourceBase(
        id=resource_id,
        name=name,
        arn=arn,
        resource_type=resource_type,
    )

    # Add to session inventory
    session.inventory[resource_id] = resource
    session.updated_at = datetime.now(timezone.utc)

    # Persist
    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    return {
        "resourceId": resource_id,
        "resource": resource.model_dump(),
    }


# ---------------------------------------------------------------------------
# POST /api/cleanup/{session_id} — Delete replicated resources from DR
# ---------------------------------------------------------------------------

class CleanupResponse(BaseModel):
    """Response model for the cleanup endpoint."""
    sessionId: str
    deletedCount: int
    failedCount: int
    skippedCount: int
    entries: list[dict[str, Any]]


@router.post("/api/cleanup/{session_id}", response_model=CleanupResponse)
async def cleanup_session(session_id: str):
    """Disassociate and delete all replicated resources from the target region.

    Disassociates resources from the DR Connect instance first, then deletes
    them in reverse dependency order. After cleanup, resets resource statuses
    to NOT_REPLICATED and clears association results.
    """
    from cleanup.resource_cleanup import cleanup_replicated_resources

    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Extract instance ID from ARN for disassociation
    instance_id = ""
    try:
        # ARN format: arn:aws:connect:region:account:instance/instance-id
        arn_parts = session.instance_arn.split("/")
        if len(arn_parts) >= 2:
            instance_id = arn_parts[-1]
    except Exception:
        logger.warning("Could not extract instance ID from ARN: %s", session.instance_arn)

    # Resolve the target instance ID from the ACGR replica in the target region.
    # ACGR replicas may have a different instance ID than the source.
    target_instance_id = ""
    if instance_id:
        try:
            target_connect = create_target_client("connect", session.target_region)
            resp = target_connect.describe_instance(InstanceId=instance_id)
            target_instance_id = resp.get("Instance", {}).get("Id", "")
        except Exception:
            logger.debug(
                "Could not resolve target instance ID from replica — falling back to source instance ID"
            )

    # Run cleanup with disassociation
    result = cleanup_replicated_resources(
        session.inventory, session.target_region,
        source_region=session.source_region,
        instance_id=instance_id,
        target_instance_id=target_instance_id,
    )

    # Reset cleaned-up resources to NOT_REPLICATED
    for entry in result.entries:
        if entry.deleted:
            resource = session.inventory.get(entry.resource_id)
            if resource:
                resource.status = ReplicationStatus.NOT_REPLICATED
                resource.replicated_arn = None
                resource.error = None

    # Clear association results for cleaned-up resources
    if session.association_results:
        cleaned_names = {
            session.inventory[e.resource_id].name
            for e in result.entries if e.deleted and e.resource_id in session.inventory
        }
        session.association_results = [
            ar for ar in session.association_results
            if ar.get("resource", "") not in cleaned_names
        ]
        if not session.association_results:
            session.association_results = None

    session.updated_at = datetime.now(timezone.utc)

    # Persist
    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    return CleanupResponse(
        sessionId=session_id,
        deletedCount=result.deleted_count,
        failedCount=result.failed_count,
        skippedCount=result.skipped_count,
        entries=[
            {
                "resourceId": e.resource_id,
                "resourceName": e.resource_name,
                "resourceType": e.resource_type,
                "deleted": e.deleted,
                "error": e.error,
            }
            for e in result.entries
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/cleanup/{session_id}/selective — Selective resource cleanup
# ---------------------------------------------------------------------------

class SelectiveCleanupRequest(BaseModel):
    """Request model for selective cleanup."""
    resourceIds: list[str]


@router.post("/api/cleanup/{session_id}/selective", response_model=CleanupResponse)
async def selective_cleanup_session(session_id: str, request: SelectiveCleanupRequest):
    """Disassociate and delete specific replicated resources from the target region.

    Same as full cleanup but only for the specified resource IDs.
    """
    from cleanup.resource_cleanup import cleanup_replicated_resources

    if not request.resourceIds:
        raise HTTPException(status_code=400, detail="resourceIds must not be empty")

    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Extract instance ID from ARN for disassociation
    instance_id = ""
    try:
        arn_parts = session.instance_arn.split("/")
        if len(arn_parts) >= 2:
            instance_id = arn_parts[-1]
    except Exception:
        logger.warning("Could not extract instance ID from ARN: %s", session.instance_arn)

    # Resolve the target instance ID from the ACGR replica in the target region.
    target_instance_id = ""
    if instance_id:
        try:
            target_connect = create_target_client("connect", session.target_region)
            resp = target_connect.describe_instance(InstanceId=instance_id)
            target_instance_id = resp.get("Instance", {}).get("Id", "")
        except Exception:
            logger.debug(
                "Could not resolve target instance ID from replica — falling back to source instance ID"
            )

    # Run selective cleanup
    result = cleanup_replicated_resources(
        session.inventory, session.target_region,
        source_region=session.source_region,
        instance_id=instance_id,
        target_instance_id=target_instance_id,
        resource_ids=request.resourceIds,
    )

    # Reset cleaned-up resources
    for entry in result.entries:
        if entry.deleted:
            resource = session.inventory.get(entry.resource_id)
            if resource:
                resource.status = ReplicationStatus.NOT_REPLICATED
                resource.replicated_arn = None
                resource.error = None

    # Clear association results for cleaned-up resources
    if session.association_results:
        cleaned_names = {
            session.inventory[e.resource_id].name
            for e in result.entries if e.deleted and e.resource_id in session.inventory
        }
        session.association_results = [
            ar for ar in session.association_results
            if ar.get("resource", "") not in cleaned_names
        ]
        if not session.association_results:
            session.association_results = None

    session.updated_at = datetime.now(timezone.utc)

    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    return CleanupResponse(
        sessionId=session_id,
        deletedCount=result.deleted_count,
        failedCount=result.failed_count,
        skippedCount=result.skipped_count,
        entries=[
            {
                "resourceId": e.resource_id,
                "resourceName": e.resource_name,
                "resourceType": e.resource_type,
                "deleted": e.deleted,
                "error": e.error,
            }
            for e in result.entries
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/diff/{session_id} — Resource config diff (source vs target)
# ---------------------------------------------------------------------------

class DiffRequest(BaseModel):
    """Request model for the diff endpoint."""
    resourceTags: dict[str, str] = {}


class DiffEntry(BaseModel):
    """Config comparison for a single resource."""
    resourceId: str
    resourceName: str
    resourceType: str
    sourceConfig: dict[str, Any] = {}
    targetConfig: dict[str, Any] = {}
    existsInTarget: bool = False
    differences: list[str] = []


class DiffResponse(BaseModel):
    """Response model for the diff endpoint."""
    sessionId: str
    entries: list[DiffEntry]
    totalDifferences: int


@router.post("/api/diff/{session_id}", response_model=DiffResponse)
async def diff_session(session_id: str, request: DiffRequest):
    """Compare source resource configs with target region resources.

    For each resource in the inventory, fetches the target region config
    and returns a side-by-side comparison highlighting differences.
    """
    from diff.resource_diff import compute_resource_diffs

    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    entries = compute_resource_diffs(
        session.inventory, session.target_region, request.resourceTags
    )

    total_diffs = sum(len(e.differences) for e in entries)

    return DiffResponse(
        sessionId=session_id,
        entries=[e.model_dump() for e in entries],
        totalDifferences=total_diffs,
    )

class AssociateResponse(BaseModel):
    """Response model for the associate endpoint."""
    sessionId: str
    results: list[dict[str, Any]]
    totalAssociated: int
    totalAlreadyAssociated: int
    totalErrors: int
    totalEnabled: int
    totalPending: int = 0


@router.post("/api/associate/{session_id}")
async def associate_resources_endpoint(session_id: str):
    """Kick off association asynchronously (same pattern as replication).

    Returns immediately with 202-style response; the actual association
    runs in a separate Lambda invocation (or background thread locally).
    Poll GET /api/session/{session_id}/status to see results.
    """
    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Check that replication has been done
    has_replicated = any(
        r.status == ReplicationStatus.REPLICATED
        for r in session.inventory.values()
    )
    if not has_replicated:
        raise HTTPException(
            status_code=400,
            detail="No replicated resources found. Run replication first.",
        )

    _invoke_association_async(session_id)

    return {"sessionId": session_id, "status": "ASSOCIATING"}


def _invoke_association_async(session_id: str) -> None:
    """Invoke Lambda asynchronously to run association, or use a background thread locally."""
    import threading

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        logger.info("Not in Lambda; running association in background thread")

        def _bg():
            from association.resource_association import associate_resources
            from datetime import datetime, timezone
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                store = create_session_store()
                session = loop.run_until_complete(store.get_session(session_id))
                if session is None:
                    logger.error("Session not found for association: %s", session_id)
                    return
                results = associate_resources(session)
                session.association_results = results
                session.updated_at = datetime.now(timezone.utc)
                loop.run_until_complete(store.save_session(session))
                logger.info("Background association completed: session=%s", session_id)
            except Exception:
                logger.exception("Background association failed for session %s", session_id)
            finally:
                loop.close()

        thread = threading.Thread(target=_bg, daemon=True)
        thread.start()
        return

    import boto3

    payload = {
        "_association_task": True,
        "session_id": session_id,
    }

    try:
        lambda_client = boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        logger.info("Async association invocation sent for session %s", session_id)
    except Exception:
        logger.exception("Failed to invoke async association for session %s", session_id)


# ---------------------------------------------------------------------------
# Per-resource association retry
# ---------------------------------------------------------------------------


@router.post("/api/associate/{session_id}/retry/{resource_id}")
async def retry_association_resource(session_id: str, resource_id: str):
    """Retry association for a single resource.

    Used from the Session Status page to retry individual failed or pending
    association entries without re-running the full association.
    """
    from association.resource_association import associate_single_resource

    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    try:
        result = associate_single_resource(session, resource_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Association retry failed"),
        )

    # Update persisted association results
    try:
        if session.association_results is not None:
            # Replace the matching entry or append
            updated = False
            for i, existing in enumerate(session.association_results):
                if existing.get("resource") == result.get("resource") and existing.get("resource_type") == result.get("resource_type"):
                    session.association_results[i] = result
                    updated = True
                    break
            if not updated:
                session.association_results.append(result)
        else:
            session.association_results = [result]

        from datetime import datetime, timezone
        session.updated_at = datetime.now(timezone.utc)
        await _session_store.save_session(session)
    except Exception:
        logger.warning("Failed to persist retry result for session %s", session_id, exc_info=True)

    return result


# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------


class SessionStatusResponse(BaseModel):
    """Response model for the session status endpoint."""
    sessionId: str
    instanceArn: str
    sourceRegion: str
    targetRegion: str
    createdAt: str
    updatedAt: str
    inventory: list[dict[str, Any]]
    associationResults: list[dict[str, Any]] | None = None
    replicationJobs: list[dict[str, Any]]


async def _refresh_lex_replica_statuses(session: Session) -> bool:
    """Live-check ALGR replica status for IN_PROGRESS Lex bots.

    For each Lex bot resource that is IN_PROGRESS and carries a replicated ARN,
    call ListBotReplicas in the source region. When the replica reaches
    "Enabled", flip the resource to REPLICATED. Also mark FAILED if the replica
    reports "Failed".

    Returns True if any resource status changed (so the caller can persist).
    """
    from replication.lex_replication import _get_replica_status

    changed = False
    source_lex = None

    for resource in session.inventory.values():
        if resource.resource_type != ResourceType.LEX_BOT:
            continue
        if resource.status != ReplicationStatus.IN_PROGRESS:
            continue
        if not resource.replicated_arn:
            continue

        # bot_id is stable across ALGR regions; derive it from the source ARN
        # (arn:aws:lex:<region>:<account>:bot/<botId>).
        bot_id = resource.arn.rsplit("/", 1)[-1] if "/" in resource.arn else None
        if not bot_id:
            continue

        if source_lex is None:
            try:
                source_lex = create_source_client("lexv2-models", session.source_region)
            except Exception:
                logger.warning("Could not create Lex client for replica status check", exc_info=True)
                return changed

        try:
            replica_status = _get_replica_status(source_lex, bot_id, session.target_region)
        except Exception:
            logger.debug("Live ALGR replica status check failed for '%s'", resource.name, exc_info=True)
            continue

        if replica_status == "Enabled":
            resource.status = ReplicationStatus.REPLICATED
            resource.error = None
            resource.error_classification = None
            changed = True
            logger.info(
                "Lex bot '%s' ALGR replica now Enabled → marking REPLICATED",
                resource.name,
            )
        elif replica_status == "Failed":
            resource.status = ReplicationStatus.FAILED
            resource.error = "ALGR replica entered Failed state"
            changed = True
            logger.warning("Lex bot '%s' ALGR replica entered Failed state", resource.name)
        # "Enabling" (or None transient) → leave IN_PROGRESS, keep polling

    if changed:
        session.updated_at = datetime.now(timezone.utc)

    return changed


@router.get("/api/session/{session_id}/status")
async def get_session_status(session_id: str, response: Response):
    """Get comprehensive session status including replication and association state.

    Powers the Session Status dashboard page.
    """
    # Never let this be cached — the UI polls it and expects live association
    # status. Stale cached responses were requiring a full browser reload.
    response.headers["Cache-Control"] = "no-store"
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Live re-check for Lex bots whose ALGR replica was still enabling.
    # When CreateBotReplica succeeds the bot is marked IN_PROGRESS (not
    # REPLICATED) and carries the replica ARN. On each status poll we call
    # ListBotReplicas; once the replica reaches "Enabled" we flip the resource
    # to REPLICATED and persist. This is the "refresh checks completion"
    # behaviour — non-blocking replication that resolves on polling.
    lex_status_changed = await _refresh_lex_replica_statuses(session)
    if lex_status_changed:
        # Recompute job progress counters so the UI progress bar reflects the
        # newly-REPLICATED Lex bot(s).
        from replication.orchestrator import _compute_progress
        for job in session.replication_jobs:
            selected = {
                rid: session.inventory[rid]
                for rid in job.selected_resource_ids
                if rid in session.inventory
            }
            if selected:
                job.progress = _compute_progress(selected)
                if job.progress.failed == 0 and job.progress.blocked == 0:
                    job.status = "COMPLETED"
        try:
            await _session_store.save_session(session)
        except Exception:
            logger.warning("Failed to persist Lex replica status update", exc_info=True)

    # Build inventory list with association status merged in
    inventory_list = []
    association_map: dict[str, dict] = {}
    if session.association_results:
        for ar in session.association_results:
            key = ar.get("resource", "")
            if key:
                association_map[key] = ar

    for rid, resource in session.inventory.items():
        entry: dict[str, Any] = {
            "id": rid,
            "name": resource.name,
            "resource_type": resource.resource_type.value if hasattr(resource.resource_type, 'value') else str(resource.resource_type),
            "arn": resource.arn,
            "replicated_arn": resource.replicated_arn,
            "status": resource.status.value if hasattr(resource.status, 'value') else str(resource.status),
            "error": resource.error,
            "error_classification": resource.error_classification,
            "kms_info": resource.kms_info,
        }
        # Merge association status
        assoc = association_map.get(resource.name)
        if assoc:
            entry["association_status"] = assoc.get("status", "not_attempted")
            entry["association_error"] = assoc.get("error")
            entry["association_message"] = assoc.get("message")
            entry["retryable"] = assoc.get("retryable", False)
        else:
            entry["association_status"] = "not_attempted"
            entry["association_error"] = None
            entry["association_message"] = None
            entry["retryable"] = False
        inventory_list.append(entry)

    # Build replication jobs summary
    jobs_list = []
    for job in session.replication_jobs:
        jobs_list.append({
            "jobId": job.job_id,
            "status": job.status,
            "progress": {
                "total": job.progress.total,
                "completed": job.progress.completed,
                "failed": job.progress.failed,
                "blocked": job.progress.blocked,
            },
            "startedAt": job.started_at.isoformat() if job.started_at else None,
            "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        })

    return SessionStatusResponse(
        sessionId=session.session_id,
        instanceArn=session.instance_arn,
        sourceRegion=session.source_region,
        targetRegion=session.target_region,
        createdAt=session.created_at.isoformat(),
        updatedAt=session.updated_at.isoformat(),
        inventory=inventory_list,
        associationResults=session.association_results,
        replicationJobs=jobs_list,
    )


# ---------------------------------------------------------------------------
# GET /api/sessions/recent — List recent sessions
# ---------------------------------------------------------------------------


class SessionSummaryResponse(BaseModel):
    """Lightweight session summary for the sessions list page."""
    sessionId: str
    instanceArn: str
    sourceRegion: str
    targetRegion: str
    createdAt: str
    updatedAt: str
    resourceCount: int = 0


@router.get("/api/sessions/recent")
async def list_recent_sessions(limit: int = 4):
    """Return the most recent sessions for the sessions list page."""
    if limit < 1 or limit > 20:
        limit = 4
    summaries = await _session_store.list_recent_sessions(limit=limit)
    return [
        SessionSummaryResponse(
            sessionId=s.session_id,
            instanceArn=s.instance_arn,
            sourceRegion=s.source_region,
            targetRegion=s.target_region,
            createdAt=s.created_at,
            updatedAt=s.updated_at,
            resourceCount=s.resource_count,
        )
        for s in summaries
    ]


# ---------------------------------------------------------------------------
# Quota Comparison
# ---------------------------------------------------------------------------


class QuotaCompareRequest(BaseModel):
    """Request body for quota comparison."""
    instance_arn: str
    target_region: str | None = None

    @field_validator("instance_arn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        return _validate_arn_format(v, "instance_arn")


@router.post("/api/quota-compare")
async def quota_compare(request: QuotaCompareRequest):
    """Compare service quotas between source and target ACGR regions.

    Checks ACGR status, queries Service Quotas API for both regions,
    and returns a comparison table with discrepancies highlighted.
    """
    from quota.quota_comparison import compare_quotas
    from aws.arn_utils import parse_arn

    try:
        parsed = parse_arn(request.instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    source_region = parsed["region"]
    if not source_region:
        raise HTTPException(status_code=400, detail="Could not determine region from instance ARN")

    try:
        result = compare_quotas(
            instance_arn=request.instance_arn,
            source_region=source_region,
            target_region=request.target_region,
        )
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Quota comparison failed"),
        )


# ---------------------------------------------------------------------------
# Discovery-based association — discover existing target resources & associate
# ---------------------------------------------------------------------------


class DiscoverTargetRequest(BaseModel):
    """Request model for target resource discovery."""
    instanceArn: str

    @field_validator("instanceArn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        return _validate_arn_format(v, "instanceArn")


class DiscoverTargetResponse(BaseModel):
    """Response model for target resource discovery."""
    instanceArn: str
    sourceRegion: str
    targetRegion: str
    resources: list[dict[str, Any]]
    totalDiscovered: int
    totalAlreadyAssociated: int
    totalNotAssociated: int


@router.post("/api/discover-target", response_model=DiscoverTargetResponse)
async def discover_target(request: DiscoverTargetRequest):
    """Discover existing resources in the target region that can be associated.

    Scans the target region for resources matching the source instance's
    associated resources (Amazon Lex bots, AWS Lambda functions, Kinesis streams, etc.)
    and returns which ones are already associated vs available for association.
    """
    from discovery.target_discovery import discover_target_resources

    instance_arn = request.instanceArn

    try:
        parsed = parse_arn(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    if parsed["service"] != "connect":
        raise HTTPException(status_code=400, detail="ARN is not a Connect resource")

    try:
        source_region = extract_region(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    try:
        target_region = resolve_target_region(source_region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        raise HTTPException(status_code=400, detail="ARN is not a Connect instance")
    instance_id = resource_part.split("/", 1)[1]

    try:
        resources = discover_target_resources(instance_id, source_region, target_region)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Target discovery failed"),
        )

    already = sum(1 for r in resources if r.get("association_status") == "already_associated")
    not_assoc = sum(1 for r in resources if r.get("association_status") == "not_associated")

    return DiscoverTargetResponse(
        instanceArn=instance_arn,
        sourceRegion=source_region,
        targetRegion=target_region,
        resources=resources,
        totalDiscovered=len(resources),
        totalAlreadyAssociated=already,
        totalNotAssociated=not_assoc,
    )


class AssociateDiscoveredRequest(BaseModel):
    """Request model for associating discovered target resources."""
    instanceArn: str
    resources: list[dict[str, Any]]

    @field_validator("instanceArn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        return _validate_arn_format(v, "instanceArn")


class AssociateDiscoveredResponse(BaseModel):
    """Response model for associating discovered resources."""
    results: list[dict[str, Any]]
    totalAssociated: int
    totalAlreadyAssociated: int
    totalErrors: int


@router.post("/api/associate-discovered", response_model=AssociateDiscoveredResponse)
async def associate_discovered(request: AssociateDiscoveredRequest):
    """Associate selected discovered resources with the DR Connect instance.

    Takes a list of discovered resources (from /api/discover-target) and
    associates each one with the target Connect instance.
    """
    from association.resource_association import (
        _associate_lambda,
        _associate_lex_bot,
        _associate_kinesis_stream,
        _associate_firehose,
        _associate_kvs_stream,
        _associate_s3_bucket,
        _enable_instance_attributes,
        _ensure_lambda_connect_permission,
        _list_associated_lambdas,
        _list_associated_bots,
        _read_source_storage_configs,
        _build_source_mappings,
    )

    instance_arn = request.instanceArn

    try:
        parsed = parse_arn(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    try:
        source_region = extract_region(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    try:
        target_region = resolve_target_region(source_region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        raise HTTPException(status_code=400, detail="ARN is not a Connect instance")
    instance_id = resource_part.split("/", 1)[1]

    if not request.resources:
        raise HTTPException(status_code=400, detail="No resources provided")

    target_connect = create_target_client("connect", target_region)

    # Enable instance attributes
    _enable_instance_attributes(target_connect, instance_id)

    # Get existing associations for dedup
    existing_lambdas = _list_associated_lambdas(target_connect, instance_id)
    existing_bots = _list_associated_bots(target_connect, instance_id)

    # Read source storage configs for correct type mappings
    source_configs = _read_source_storage_configs(source_region, instance_id)
    s3_type_map, kinesis_type_map, firehose_type_map = _build_source_mappings(source_configs)

    results: list[dict[str, Any]] = []
    total_associated = 0
    total_already = 0
    total_errors = 0

    for res in request.resources:
        rtype = res.get("resource_type", "")
        arn = res.get("arn", "")
        name = res.get("name", "")

        try:
            if rtype == "LAMBDA":
                # Ensure Connect permission on the Lambda
                _ensure_lambda_connect_permission(arn, instance_id)
                if arn in existing_lambdas:
                    result = {"resource": name, "resource_type": "LAMBDA", "status": "already_associated", "message": "Already associated"}
                else:
                    try:
                        target_connect.associate_lambda_function(InstanceId=instance_id, FunctionArn=arn)
                        result = {"resource": name, "resource_type": "LAMBDA", "status": "associated", "message": "Lambda associated"}
                    except ClientError as exc:
                        if "already" in str(exc).lower():
                            result = {"resource": name, "resource_type": "LAMBDA", "status": "already_associated", "message": "Already associated"}
                        else:
                            result = {"resource": name, "resource_type": "LAMBDA", "status": "error", "error": str(exc)}

            elif rtype == "LEX_BOT":
                alias_arn = arn
                if alias_arn in existing_bots:
                    result = {"resource": name, "resource_type": "LEX_BOT", "status": "already_associated", "message": "Already associated"}
                else:
                    from association.resource_association import _ensure_lex_connect_permission
                    _ensure_lex_connect_permission(alias_arn, instance_id, target_region)
                    try:
                        target_connect.associate_bot(InstanceId=instance_id, LexV2Bot={"AliasArn": alias_arn})
                        result = {"resource": name, "resource_type": "LEX_BOT", "status": "associated", "message": "Lex bot associated"}
                    except ClientError as exc:
                        if "already" in str(exc).lower():
                            result = {"resource": name, "resource_type": "LEX_BOT", "status": "already_associated", "message": "Already associated"}
                        else:
                            result = {"resource": name, "resource_type": "LEX_BOT", "status": "error", "error": str(exc)}

            elif rtype == "KINESIS_STREAM":
                storage_type = res.get("storage_type", "AGENT_EVENTS")
                try:
                    target_connect.associate_instance_storage_config(
                        InstanceId=instance_id,
                        ResourceType=storage_type,
                        StorageConfig={"StorageType": "KINESIS_STREAM", "KinesisStreamConfig": {"StreamArn": arn}},
                    )
                    result = {"resource": name, "resource_type": "KINESIS_STREAM", "status": "associated", "message": f"Associated for {storage_type}"}
                except ClientError as exc:
                    if "already" in str(exc).lower() or "conflict" in str(exc).lower():
                        result = {"resource": name, "resource_type": "KINESIS_STREAM", "status": "already_associated", "message": "Already configured"}
                    else:
                        result = {"resource": name, "resource_type": "KINESIS_STREAM", "status": "error", "error": str(exc)}

            elif rtype == "KINESIS_FIREHOSE":
                storage_type = res.get("storage_type", "CONTACT_TRACE_RECORDS")
                try:
                    target_connect.associate_instance_storage_config(
                        InstanceId=instance_id,
                        ResourceType=storage_type,
                        StorageConfig={"StorageType": "KINESIS_FIREHOSE", "KinesisFirehoseConfig": {"FirehoseArn": arn}},
                    )
                    result = {"resource": name, "resource_type": "KINESIS_FIREHOSE", "status": "associated", "message": f"Associated for {storage_type}"}
                except ClientError as exc:
                    if "already" in str(exc).lower() or "conflict" in str(exc).lower():
                        result = {"resource": name, "resource_type": "KINESIS_FIREHOSE", "status": "already_associated", "message": "Already configured"}
                    else:
                        result = {"resource": name, "resource_type": "KINESIS_FIREHOSE", "status": "error", "error": str(exc)}

            elif rtype == "S3_BUCKET":
                storage_types = res.get("storage_types", ["CALL_RECORDINGS"])
                bucket_name = arn.split(":::")[-1] if ":::" in arn else name
                sub_results = []
                for st in storage_types:
                    try:
                        target_connect.associate_instance_storage_config(
                            InstanceId=instance_id,
                            ResourceType=st,
                            StorageConfig={"StorageType": "S3", "S3Config": {"BucketName": bucket_name, "BucketPrefix": f"connect/{st}"}},
                        )
                        sub_results.append({"resource": name, "resource_type": "S3_BUCKET", "status": "associated", "message": f"Associated for {st}"})
                    except ClientError as exc:
                        if "already" in str(exc).lower() or "conflict" in str(exc).lower():
                            sub_results.append({"resource": name, "resource_type": "S3_BUCKET", "status": "already_associated", "message": f"Already configured for {st}"})
                        else:
                            sub_results.append({"resource": name, "resource_type": "S3_BUCKET", "status": "error", "error": f"{st}: {exc}"})
                for sr in sub_results:
                    results.append(sr)
                    if sr["status"] == "associated":
                        total_associated += 1
                    elif sr["status"] == "already_associated":
                        total_already += 1
                    else:
                        total_errors += 1
                continue  # skip the common result handling below

            elif rtype == "KINESIS_VIDEO_STREAM":
                from association.resource_association import _ensure_kvs_kms_key
                kvs_prefix = res.get("kvs_prefix", "")
                retention = res.get("retention_hours", 0)
                kms_key_id = _ensure_kvs_kms_key(target_region)
                kvs_config: dict = {"Prefix": kvs_prefix, "RetentionPeriodHours": retention}
                if kms_key_id:
                    kvs_config["EncryptionConfig"] = {"EncryptionType": "KMS", "KeyId": kms_key_id}
                try:
                    target_connect.associate_instance_storage_config(
                        InstanceId=instance_id,
                        ResourceType="MEDIA_STREAMS",
                        StorageConfig={"StorageType": "KINESIS_VIDEO_STREAM", "KinesisVideoStreamConfig": kvs_config},
                    )
                    result = {"resource": name, "resource_type": "KINESIS_VIDEO_STREAM", "status": "associated", "message": "KVS media streaming configured"}
                except ClientError as exc:
                    if "already" in str(exc).lower() or "conflict" in str(exc).lower():
                        result = {"resource": name, "resource_type": "KINESIS_VIDEO_STREAM", "status": "already_associated", "message": "Already configured"}
                    else:
                        result = {"resource": name, "resource_type": "KINESIS_VIDEO_STREAM", "status": "error", "error": str(exc)}
            else:
                result = {"resource": name, "resource_type": rtype, "status": "error", "error": f"Unsupported resource type: {rtype}"}

        except Exception as exc:
            result = {"resource": name, "resource_type": rtype, "status": "error", "error": str(exc)}

        results.append(result)
        if result["status"] == "associated":
            total_associated += 1
        elif result["status"] == "already_associated":
            total_already += 1
        else:
            total_errors += 1

    return AssociateDiscoveredResponse(
        results=results,
        totalAssociated=total_associated,
        totalAlreadyAssociated=total_already,
        totalErrors=total_errors,
    )



# ---------------------------------------------------------------------------
# Contact Flow Analysis (async with progress tracking)
# ---------------------------------------------------------------------------

_flow_analysis_store = None


def _get_flow_analysis_store():
    """Lazy-init the flow analysis store."""
    global _flow_analysis_store
    if _flow_analysis_store is None:
        from store.flow_analysis_store import create_flow_analysis_store
        _flow_analysis_store = create_flow_analysis_store()
    return _flow_analysis_store


class ContactFlowAnalyzeRequest(BaseModel):
    """Request model for contact flow analysis."""

    instance_arn: str
    session_id: str | None = None  # optional, to cross-reference inventory

    @field_validator("instance_arn")
    @classmethod
    def validate_arn(cls, v: str) -> str:
        return _validate_arn_format(v, "instance_arn")


class ContactFlowAnalyzeAsyncResponse(BaseModel):
    """Response returned immediately when analysis is kicked off."""

    job_id: str
    status: str = "PENDING"


class ContactFlowAnalysisStatusResponse(BaseModel):
    """Response for polling the analysis job status."""

    job_id: str
    status: str
    total_flows: int = 0
    analyzed_flows: int = 0
    flows_with_lex: int = 0
    flows_with_lambda: int = 0
    truncated: bool = False
    error: str | None = None
    flows: list[dict] | None = None


@router.post("/api/contact-flows/analyze", response_model=ContactFlowAnalyzeAsyncResponse)
async def analyze_contact_flows_endpoint(request: ContactFlowAnalyzeRequest):
    """Start async contact flow analysis.

    Creates a job, kicks off analysis in a background Lambda invocation
    (or background thread for local dev), and returns the job_id immediately.
    Poll ``GET /api/contact-flows/analyze/{job_id}/status`` for progress.
    """
    from aws.arn_utils import extract_region, parse_arn

    instance_arn = request.instance_arn

    try:
        parsed = parse_arn(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    try:
        source_region = extract_region(instance_arn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_error_detail(exc, str(exc)))

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        raise HTTPException(status_code=400, detail="ARN is not a Connect instance")

    job_id = str(uuid.uuid4())
    store = _get_flow_analysis_store()
    store.create_job(job_id, instance_arn)

    # Kick off async analysis
    _invoke_flow_analysis_async(
        job_id=job_id,
        instance_arn=instance_arn,
        session_id=request.session_id,
    )

    return ContactFlowAnalyzeAsyncResponse(job_id=job_id, status="PENDING")


@router.get("/api/contact-flows/analyze/{job_id}/status", response_model=ContactFlowAnalysisStatusResponse)
async def get_flow_analysis_status(job_id: str):
    """Poll the status of an async contact flow analysis job."""
    store = _get_flow_analysis_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result = job.get("result")
    return ContactFlowAnalysisStatusResponse(
        job_id=job_id,
        status=job.get("status", "UNKNOWN"),
        total_flows=int(job.get("total_flows", 0)),
        analyzed_flows=int(job.get("analyzed_flows", 0)),
        flows_with_lex=result.get("flows_with_lex", 0) if result else 0,
        flows_with_lambda=result.get("flows_with_lambda", 0) if result else 0,
        truncated=result.get("truncated", False) if result else False,
        error=job.get("error_message"),
        flows=result.get("flows") if result else None,
    )


def _invoke_flow_analysis_async(job_id: str, instance_arn: str, session_id: str | None) -> None:
    """Invoke flow analysis asynchronously — Lambda Event invoke or background thread."""
    import threading

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        # Local dev — background thread
        logger.info("Not in Lambda; running flow analysis in background thread")

        def _bg():
            _run_flow_analysis(job_id, instance_arn, session_id)

        thread = threading.Thread(target=_bg, daemon=True)
        thread.start()
        return

    import boto3 as _boto3

    payload = {
        "_contact_flow_analysis_task": True,
        "job_id": job_id,
        "instance_arn": instance_arn,
        "session_id": session_id,
    }

    try:
        lambda_client = _boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        logger.info("Async flow analysis invocation sent for job %s", job_id)
    except Exception:
        logger.exception("Failed to invoke async flow analysis for job %s", job_id)
        store = _get_flow_analysis_store()
        store.fail_job(job_id, "Failed to start async analysis")


def _run_flow_analysis(job_id: str, instance_arn: str, session_id: str | None) -> None:
    """Execute the actual flow analysis and write results to the store.

    Called by the async Lambda handler or a background thread.
    """
    import time as _time
    from store.flow_analysis_store import create_flow_analysis_store

    store = create_flow_analysis_store()

    try:
        from aws.arn_utils import extract_region, parse_arn
        from discovery.contact_flow_analyzer import analyze_contact_flows

        parsed = parse_arn(instance_arn)
        source_region = extract_region(instance_arn)
        resource_part = parsed["resource"]
        instance_id = resource_part.split("/", 1)[1]

        # Load inventory ARNs if session_id provided
        inventory_arns: set[str] = set()
        if session_id:
            try:
                import asyncio
                _store = create_session_store()
                loop = asyncio.new_event_loop()
                try:
                    session = loop.run_until_complete(_store.get_session(session_id))
                finally:
                    loop.close()
                if session:
                    inventory_arns = {
                        res.arn for res in session.inventory.values() if res.arn
                    }
            except Exception:
                logger.warning("Failed to load session %s for inventory cross-reference", session_id)

        # Progress callback — update store every 25 flows
        _last_update = [0]

        def _progress(analyzed: int, total: int) -> None:
            if analyzed - _last_update[0] >= 25 or analyzed == total:
                store.update_progress(job_id, analyzed, total)
                _last_update[0] = analyzed

        # Deadline: 850 seconds from now (Lambda has 900s, leave 50s buffer)
        deadline = _time.time() + 850

        result = analyze_contact_flows(
            instance_id=instance_id,
            source_region=source_region,
            inventory_arns=inventory_arns,
            max_flows=0,  # unlimited — Lambda has 900s
            concurrency=5,
            progress_callback=_progress,
            deadline=deadline,
        )

        # Only include flows that have references to keep payload small
        flows_with_refs = [
            flow.model_dump()
            for flow in result.flows
            if flow.lex_references or flow.lambda_references
        ]
        # If payload would be too large, include all anyway (compressed)
        all_flows = [flow.model_dump() for flow in result.flows]

        result_data = {
            "total_flows": result.total_flows,
            "analyzed_flows": result.analyzed_flows,
            "truncated": result.truncated,
            "flows_with_lex": result.flows_with_lex,
            "flows_with_lambda": result.flows_with_lambda,
            "flows": all_flows,
        }

        store.complete_job(job_id, result_data)
        logger.info("Flow analysis job %s completed: %d/%d flows", job_id, result.analyzed_flows, result.total_flows)

    except Exception as exc:
        logger.exception("Flow analysis job %s failed", job_id)
        store.fail_job(job_id, str(exc))


# ---------------------------------------------------------------------------
# Step Functions replication endpoints
# ---------------------------------------------------------------------------

ASYNC_THRESHOLD = 5  # Default to async for > this many resources


def _should_use_step_functions(async_mode: bool | None, resource_count: int) -> bool:
    """Determine whether to use Step Functions based on async_mode and resource count.

    - async_mode=True  → always use SFN
    - async_mode=False → always use Lambda self-invocation
    - async_mode=None  → auto: SFN for >5 resources, Lambda for ≤5
    """
    if async_mode is True:
        return True
    if async_mode is False:
        return False
    return resource_count > ASYNC_THRESHOLD



class ReplicateAsyncRequest(BaseModel):
    """Request model for the replicate-async endpoint."""
    resourceIds: list[str]
    resourceTags: dict[str, str] = {}

    @field_validator("resourceIds")
    @classmethod
    def validate_resource_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("resourceIds must not be empty")
        return v


class ReplicateAsyncResponse(BaseModel):
    """Response model for the replicate-async endpoint."""
    jobId: str
    sessionId: str
    status: str
    sfnExecutionArn: str
    progress: dict[str, int]



@router.post(
    "/api/sessions/{session_id}/replicate-async",
    response_model=ReplicateAsyncResponse,
)
async def replicate_async(session_id: str, request: ReplicateAsyncRequest):
    """Start a AWS Step Functions execution for replication.

    Creates a ReplicationJob, starts the SFN execution, and stores
    the execution ARN on the session.
    """
    resource_ids = request.resourceIds

    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Validate resource IDs
    missing = [rid for rid in resource_ids if rid not in session.inventory]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Resource IDs not found in inventory: {missing}",
        )

    # Create placeholder job
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    placeholder_job = ReplicationJob(
        job_id=job_id,
        status="IN_PROGRESS",
        selected_resource_ids=resource_ids,
        execution_order=[],
        progress=ReplicationProgress(
            total=len(resource_ids), completed=0, failed=0, blocked=0
        ),
        started_at=now,
    )
    session.replication_jobs.append(placeholder_job)

    # Mark selected resources as IN_PROGRESS
    for rid in resource_ids:
        resource = session.inventory.get(rid)
        if resource is not None:
            resource.status = ReplicationStatus.IN_PROGRESS
            resource.error = None

    # Persist resource_tags on session
    resource_tags = request.resourceTags or {}
    session.resource_tags = resource_tags

    # Start AWS Step Functions execution
    from replication.step_functions_orchestrator import start_replication_execution

    try:
        execution_arn = start_replication_execution(
            session, resource_ids, job_id, resource_tags
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=_safe_error_detail(exc, str(exc)))

    session.sfn_execution_arn = execution_arn

    try:
        await _session_store.save_session(session)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to save session"),
        )

    return ReplicateAsyncResponse(
        jobId=job_id,
        sessionId=session_id,
        status="IN_PROGRESS",
        sfnExecutionArn=execution_arn,
        progress={"total": len(resource_ids), "completed": 0, "failed": 0, "blocked": 0},
    )



class ExecutionStatusResponse(BaseModel):
    """Response model for the execution-status endpoint."""
    executionArn: str
    status: str
    currentLevel: int | None = None
    totalLevels: int
    resources: list[dict[str, Any]]
    startedAt: str | None = None
    completedAt: str | None = None
    error: str | None = None
    summary: dict[str, int]


@router.get(
    "/api/sessions/{session_id}/execution-status",
    response_model=ExecutionStatusResponse,
)
async def get_sfn_execution_status(session_id: str):
    """Poll AWS Step Functions execution status for a session.

    Returns per-resource progress including which dependency level
    is currently executing.
    """
    # Load session
    try:
        session = await _session_store.get_session(session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to load session"),
        )

    if session is None:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if not session.sfn_execution_arn:
        raise HTTPException(
            status_code=400,
            detail="No Step Functions execution found for this session",
        )

    from replication.step_functions_orchestrator import (
        get_execution_status,
        process_execution_results,
    )

    try:
        status = get_execution_status(session.sfn_execution_arn)
    except Exception as exc:
        logger.exception("Failed to get SFN execution status")
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, "Failed to get execution status"),
        )

    # If execution completed, process results and update session
    if status["status"] in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
        try:
            process_execution_results(session, status)

            # Also update the replication job status to reflect SFN completion
            for job in session.replication_jobs:
                if job.status == "IN_PROGRESS":
                    summary = status.get("summary", {})
                    job.progress.completed = summary.get("succeeded", 0)
                    job.progress.failed = summary.get("failed", 0)
                    job.progress.blocked = summary.get("blocked", 0)
                    if summary.get("failed", 0) > 0 or summary.get("blocked", 0) > 0:
                        job.status = "FAILED"
                    else:
                        job.status = "COMPLETED"
                    if status.get("completed_at"):
                        from datetime import datetime
                        try:
                            job.completed_at = datetime.fromisoformat(status["completed_at"])
                        except (ValueError, TypeError):
                            pass

            await _session_store.save_session(session)
        except Exception:
            logger.warning("Failed to process/persist SFN execution results", exc_info=True)

    return ExecutionStatusResponse(
        executionArn=status["execution_arn"],
        status=status["status"],
        currentLevel=status.get("current_level"),
        totalLevels=status["total_levels"],
        resources=status["resources"],
        startedAt=status.get("started_at"),
        completedAt=status.get("completed_at"),
        error=status.get("error"),
        summary=status["summary"],
    )
