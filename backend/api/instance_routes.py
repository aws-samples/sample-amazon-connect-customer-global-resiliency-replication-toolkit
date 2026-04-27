"""Instance listing routes for the Connect ACGR Resource Replicator."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from aws.client_factory import create_client

logger = logging.getLogger(__name__)


def _safe_error_detail(exc: Exception, user_message: str) -> str:
    """Log the full exception server-side at ERROR level and return a safe
    user-facing message.

    The return value contains no raw exception content, class name, or
    traceback. The full exception is captured in CloudWatch via
    logger.exception().
    """
    logger.exception(user_message)
    return user_message


instance_router = APIRouter()

# Eight ACGR-supported regions
ACGR_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-central-1",
    "eu-west-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-southeast-1",
    "ap-southeast-2",
]


@instance_router.get("/api/list-instances")
async def list_instances(region: Optional[str] = Query(None)):
    """List Connect instances in a region.

    Args:
        region: AWS region to list instances from. Must be an ACGR-supported region.

    Returns:
        List of instances with instanceId, instanceAlias, and instanceArn.
    """
    if not region:
        raise HTTPException(status_code=400, detail="Region parameter is required")

    if region not in ACGR_REGIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid or unsupported region: {region}. Supported regions: {', '.join(sorted(ACGR_REGIONS))}",
        )

    try:
        connect = create_client("connect", region)
        instances = []
        paginator = connect.get_paginator("list_instances")

        for page in paginator.paginate():
            for instance in page.get("InstanceSummaryList", []):
                instances.append({
                    "instanceId": instance.get("Id", ""),
                    "instanceAlias": instance.get("InstanceAlias", ""),
                    "instanceArn": instance.get("Arn", ""),
                })

        return {"instances": instances}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_error_detail(exc, f"Failed to list instances in {region}"),
        )
