"""AWS Lambda entry point for the Connect ACGR Resource Replicator."""

import logging
import os

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

root_logger = logging.getLogger()
if root_logger.handlers:
    for h in root_logger.handlers:
        root_logger.removeHandler(h)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

logger = logging.getLogger(__name__)
logger.info(
    "Lambda handler module loaded - DEPLOYMENT_MODE=%s, LOG_LEVEL=%s",
    os.environ.get("DEPLOYMENT_MODE", "not set"),
    LOG_LEVEL,
)

from mangum import Mangum
from main import app

_mangum_handler = Mangum(app, lifespan="off")


def handler(event, context):
    """Lambda entry point - routes async tasks or API requests."""
    if event.get("_replication_task"):
        return _handle_replication_task(event, context)
    if event.get("_association_task"):
        return _handle_association_task(event, context)
    if event.get("_contact_flow_analysis_task"):
        return _handle_contact_flow_analysis_task(event, context)
    return _mangum_handler(event, context)


def _handle_replication_task(event, context):
    """Handle an async replication invocation with full 900s timeout."""
    from api.routes import _run_replication_with_store
    from store.factory import create_session_store

    session_id = event.get("session_id")
    resource_ids = event.get("resource_ids", [])
    job_id = event.get("job_id")
    resource_tags = event.get("resource_tags", {})
    concurrency = event.get("concurrency", 1)

    logger.info(
        "Async replication task received: job=%s session=%s resources=%d",
        job_id, session_id, len(resource_ids),
    )

    store = create_session_store()
    _run_replication_with_store(
        session_id, resource_ids, job_id, store,
        resource_tags, concurrency,
    )

    logger.info("Async replication task completed: job=%s", job_id)
    return {"statusCode": 200, "body": "Replication complete"}


def _handle_association_task(event, context):
    """Handle an async association invocation with full 900s timeout."""
    from association.resource_association import associate_resources
    from store.factory import create_session_store
    from datetime import datetime, timezone
    import asyncio

    session_id = event.get("session_id")
    logger.info("Async association task received: session=%s", session_id)

    loop = asyncio.new_event_loop()
    try:
        store = create_session_store()
        session = loop.run_until_complete(store.get_session(session_id))
        if session is None:
            logger.error("Session not found for association: %s", session_id)
            return {"statusCode": 404, "body": "Session not found"}

        from association.resource_association import merge_association_results
        results = associate_resources(session)

        session.association_results = merge_association_results(
            session.association_results, results
        )
        session.updated_at = datetime.now(timezone.utc)
        loop.run_until_complete(store.save_session(session))

        logger.info(
            "Async association task completed: session=%s results=%d",
            session_id, len(results),
        )
        return {"statusCode": 200, "body": "Association complete"}
    except Exception:
        logger.exception("Association task failed for session %s", session_id)
        return {"statusCode": 500, "body": "Association failed"}
    finally:
        loop.close()


def _handle_contact_flow_analysis_task(event, context):
    """Handle an async contact flow analysis invocation with full 900s timeout."""
    from api.routes import _run_flow_analysis

    job_id = event.get("job_id")
    instance_arn = event.get("instance_arn")
    session_id = event.get("session_id")

    logger.info(
        "Async flow analysis task received: job=%s instance=%s",
        job_id, instance_arn,
    )

    _run_flow_analysis(job_id, instance_arn, session_id)

    logger.info("Async flow analysis task completed: job=%s", job_id)
    return {"statusCode": 200, "body": "Flow analysis complete"}
