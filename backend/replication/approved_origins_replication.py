"""Replication module for Connect Approved Origins.

Associates approved origin URLs with the target Connect instance
using connect:AssociateApprovedOrigin.
"""

from __future__ import annotations

import logging

from aws.client_factory import create_target_client
from models.resources import ApprovedOriginResource

logger = logging.getLogger(__name__)


def replicate_approved_origin(
    resource: ApprovedOriginResource,
    target_region: str,
    target_instance_id: str,
) -> str:
    """Replicate an approved origin to the target Connect instance.

    Calls AssociateApprovedOrigin on the target instance. This is
    idempotent — if the origin already exists, Connect won't error.

    Args:
        resource: The ApprovedOriginResource to replicate.
        target_region: The ACGR target region.
        target_instance_id: The Connect instance ID in the target region.

    Returns:
        A synthetic ARN for the replicated origin.
    """
    connect_client = create_target_client("connect", target_region)
    # When deserialized from DynamoDB, resources may be plain ResourceBase
    # objects without the origin_url attribute. Fall back to config_summary
    # or name.
    origin_url = (
        getattr(resource, "origin_url", None)
        or resource.config_summary.get("origin_url")
        or resource.name
    )

    connect_client.associate_approved_origin(
        InstanceId=target_instance_id,
        Origin=origin_url,
    )

    replicated_arn = (
        f"arn:aws:connect:{target_region}:approved-origin:{origin_url}"
    )
    logger.info(
        "Replicated approved origin %s to instance %s in %s",
        origin_url,
        target_instance_id,
        target_region,
    )
    return replicated_arn
