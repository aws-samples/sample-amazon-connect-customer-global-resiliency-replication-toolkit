"""Resource Lambda handler for single-resource replication.

This is a SEPARATE Lambda function from the API Lambda. It is invoked
by the Step Functions state machine to replicate a single resource.

Requirements: 9.4, 9.5, 9.6
"""

from __future__ import annotations

import logging
import os
import random
import time

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

logger = logging.getLogger(__name__)

# Throttle error codes that should trigger retry with backoff
_THROTTLE_CODES = frozenset({
    "Throttling", "ThrottlingException", "TooManyRequestsException",
    "RequestLimitExceeded", "BandwidthLimitExceeded", "SlowDown",
    "ProvisionedThroughputExceededException", "RequestThrottled",
    "EC2ThrottledException",
})

_MAX_RETRIES = 5
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 30.0  # seconds


def _is_throttle_error(exc: Exception) -> bool:
    """Check if an exception is a throttle/rate-limit error from AWS."""
    error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    if error_code in _THROTTLE_CODES:
        return True
    exc_name = type(exc).__name__
    return exc_name in _THROTTLE_CODES


def handler(event, context):
    """Lambda handler for single-resource replication.

    Event structure:
    {
        "resource_id": "...",
        "resource_data": {...},  # serialized ResourceBase
        "resource_type": "...",
        "resource_name": "...",
        "resource_arn": "...",
        "target_region": "...",
        "session_id": "...",
        "job_id": "...",
        "instance_id": "...",
        "resource_tags": {...},
    }

    Returns:
    {
        "resource_id": "...",
        "status": "REPLICATED" | "FAILED" | "SKIPPED",
        "replicated_arn": "..." | null,
        "error": "..." | null,
        "error_classification": {...} | null,
        "arn_mapping_update": {"type": "...", "source_arn": "...", "target_arn": "..."} | null
    }
    """
    resource_id = event.get("resource_id", "")
    resource_data = event.get("resource_data", {})
    target_region = event.get("target_region", "")
    session_id = event.get("session_id", "")
    job_id = event.get("job_id", "")
    instance_id = event.get("instance_id", "")
    resource_tags = event.get("resource_tags", {})

    logger.info(
        "Resource Lambda invoked: resource_id=%s session=%s job=%s",
        resource_id, session_id, job_id,
    )

    try:
        result = _replicate_resource(
            resource_id=resource_id,
            resource_data=resource_data,
            target_region=target_region,
            session_id=session_id,
            job_id=job_id,
            instance_id=instance_id,
            resource_tags=resource_tags,
        )
        logger.info(
            "Resource Lambda completed: resource_id=%s status=%s",
            resource_id, result.get("status"),
        )
        return result
    except Exception as exc:
        logger.exception("Resource Lambda failed: resource_id=%s", resource_id)
        from replication.error_classification import classify_error
        classification = classify_error(exc)
        return {
            "resource_id": resource_id,
            "status": "FAILED",
            "replicated_arn": None,
            "error": str(exc),
            "error_classification": classification.model_dump(),
            "arn_mapping_update": None,
        }



def _replicate_resource(
    resource_id: str,
    resource_data: dict,
    target_region: str,
    session_id: str,
    job_id: str,
    instance_id: str,
    resource_tags: dict[str, str],
) -> dict:
    """Replicate a single resource and return the result.

    Loads the session from DynamoDB to get the resource, performs replication
    using the existing _replicate_single_resource logic, updates the resource
    status, and saves back to DynamoDB.
    """
    import asyncio
    from models.enums import ReplicationStatus, ResourceType
    from models.resources import ResourceBase
    from replication.error_classification import classify_error
    from store.factory import create_session_store

    store = create_session_store()
    loop = asyncio.new_event_loop()

    try:
        session = loop.run_until_complete(store.get_session(session_id))
        if session is None:
            return {
                "resource_id": resource_id,
                "status": "FAILED",
                "replicated_arn": None,
                "error": f"Session not found: {session_id}",
                "error_classification": None,
                "arn_mapping_update": None,
            }

        resource = session.inventory.get(resource_id)
        if resource is None:
            return {
                "resource_id": resource_id,
                "status": "FAILED",
                "replicated_arn": None,
                "error": f"Resource not found in session inventory: {resource_id}",
                "error_classification": None,
                "arn_mapping_update": None,
            }

        # Build ARN mappings from already-replicated resources in this session
        role_arn_mapping: dict[str, str] = {}
        lambda_arn_mapping: dict[str, str] = {}
        s3_bucket_mapping: dict[str, str] = {}
        layer_arn_mapping: dict[str, str] = {}

        for rid, r in session.inventory.items():
            if r.status == ReplicationStatus.REPLICATED and r.replicated_arn:
                if r.resource_type == ResourceType.IAM_ROLE:
                    role_arn_mapping[r.arn] = r.replicated_arn
                elif r.resource_type == ResourceType.LAMBDA:
                    lambda_arn_mapping[r.arn] = r.replicated_arn
                elif r.resource_type == ResourceType.S3_BUCKET:
                    target_bucket = r.replicated_arn.split(":::")[-1] if ":::" in r.replicated_arn else ""
                    if target_bucket:
                        s3_bucket_mapping[r.name] = target_bucket

        # Handle IAM roles — they are global, auto-mark as REPLICATED
        if resource.resource_type == ResourceType.IAM_ROLE:
            resource.status = ReplicationStatus.REPLICATED
            resource.replicated_arn = resource.arn
            resource.error = None
            resource.error_classification = None

            session.inventory[resource_id] = resource
            loop.run_until_complete(store.save_session(session))

            return {
                "resource_id": resource_id,
                "status": "REPLICATED",
                "replicated_arn": resource.arn,
                "error": None,
                "error_classification": None,
                "arn_mapping_update": {
                    "type": "role",
                    "source_arn": resource.arn,
                    "target_arn": resource.arn,
                },
            }

        # Perform replication using the existing dispatch logic
        from replication.orchestrator import _replicate_single_resource

        resource.status = ReplicationStatus.IN_PROGRESS
        resource.error = None

        try:
            # Retry with exponential backoff + jitter for throttle errors
            last_exc = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    replicated_arn = _replicate_single_resource(
                        resource=resource,
                        target_region=target_region,
                        role_arn_mapping=role_arn_mapping,
                        lambda_arn_mapping=lambda_arn_mapping,
                        resource_tags=resource_tags,
                        instance_id=instance_id,
                        layer_arn_mapping=layer_arn_mapping,
                        s3_bucket_mapping=s3_bucket_mapping,
                    )
                    break  # success
                except Exception as exc:
                    if _is_throttle_error(exc) and attempt < _MAX_RETRIES:
                        delay = min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY)
                        jitter = random.uniform(0, delay * 0.5)
                        wait = delay + jitter
                        logger.warning(
                            "Throttled on attempt %d/%d for %s, retrying in %.1fs: %s",
                            attempt + 1, _MAX_RETRIES + 1, resource_id, wait, exc,
                        )
                        time.sleep(wait)
                        last_exc = exc
                        continue
                    raise  # non-throttle error or max retries exhausted

            resource.status = ReplicationStatus.REPLICATED
            resource.replicated_arn = replicated_arn
            resource.error = None
            resource.error_classification = None

            # Determine ARN mapping type
            arn_mapping_update = _build_arn_mapping_update(resource, replicated_arn)

            # Save updated resource to session
            session.inventory[resource_id] = resource
            loop.run_until_complete(store.save_session(session))

            return {
                "resource_id": resource_id,
                "status": "REPLICATED",
                "replicated_arn": replicated_arn,
                "error": None,
                "error_classification": None,
                "arn_mapping_update": arn_mapping_update,
            }

        except Exception as exc:
            from replication.lex_replication import (
                LexAlgrInProgressError,
                LexAlgrSkippedError,
            )

            if isinstance(exc, LexAlgrInProgressError):
                # ALGR replica is enabling asynchronously — mark IN_PROGRESS
                # (not FAILED) and carry the replica ARN. Session-status polls
                # re-check ListBotReplicas and flip to REPLICATED once Enabled.
                resource.status = ReplicationStatus.IN_PROGRESS
                resource.replicated_arn = exc.replicated_arn
                resource.error = None
                resource.error_classification = None
                session.inventory[resource_id] = resource
                loop.run_until_complete(store.save_session(session))
                return {
                    "resource_id": resource_id,
                    "status": "IN_PROGRESS",
                    "replicated_arn": exc.replicated_arn,
                    "error": None,
                    "error_classification": None,
                    "arn_mapping_update": None,
                }

            if isinstance(exc, LexAlgrSkippedError):
                resource.status = ReplicationStatus.SKIPPED
                resource.error = str(exc)
                resource.error_classification = None
                status = "SKIPPED"
                error_class = None
            else:
                resource.status = ReplicationStatus.FAILED
                resource.error = str(exc)
                error_class = classify_error(exc)
                resource.error_classification = error_class.model_dump()
                status = "FAILED"

            # Save updated resource to session
            session.inventory[resource_id] = resource
            loop.run_until_complete(store.save_session(session))

            return {
                "resource_id": resource_id,
                "status": status,
                "replicated_arn": None,
                "error": str(exc),
                "error_classification": error_class.model_dump() if error_class else None,
                "arn_mapping_update": None,
            }

    finally:
        loop.close()


def _build_arn_mapping_update(resource: ResourceBase, replicated_arn: str) -> dict | None:
    """Build the ARN mapping update dict for a successfully replicated resource."""
    from models.enums import ResourceType

    type_map = {
        ResourceType.IAM_ROLE: "role",
        ResourceType.LAMBDA: "lambda",
        ResourceType.S3_BUCKET: "s3",
        ResourceType.KINESIS_STREAM: "kinesis",
        ResourceType.KINESIS_FIREHOSE: "kinesis",
        ResourceType.KINESIS_VIDEO_STREAM: "kinesis",
    }

    mapping_type = type_map.get(resource.resource_type)
    if mapping_type:
        return {
            "type": mapping_type,
            "source_arn": resource.arn,
            "target_arn": replicated_arn,
        }
    return None
