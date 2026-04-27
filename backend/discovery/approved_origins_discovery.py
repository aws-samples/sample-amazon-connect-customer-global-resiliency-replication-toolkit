"""Discovery module for Connect Approved Origins.

Lists all approved origins (URLs) associated with a Connect instance
so they can be replicated to the ACGR target instance.
"""

from __future__ import annotations

import hashlib
import logging

from aws.client_factory import create_source_client
from models.resources import ApprovedOriginResource

logger = logging.getLogger(__name__)


def _generate_resource_id(origin_url: str) -> str:
    """Generate a deterministic resource ID from the origin URL."""
    return f"ao-{hashlib.sha256(origin_url.encode()).hexdigest()[:12]}"


def discover_approved_origins(
    instance_id: str,
    source_region: str,
) -> list[ApprovedOriginResource]:
    """Discover approved origins for a Connect instance.

    Calls connect:ListApprovedOrigins and returns one
    ApprovedOriginResource per origin URL.

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        List of ApprovedOriginResource objects.
    """
    connect_client = create_source_client("connect", source_region)
    resources: list[ApprovedOriginResource] = []

    try:
        paginator = connect_client.get_paginator("list_approved_origins")
        for page in paginator.paginate(InstanceId=instance_id):
            for origin in page.get("Origins", []):
                rid = _generate_resource_id(origin)
                resources.append(ApprovedOriginResource(
                    id=rid,
                    name=origin,
                    arn=f"arn:aws:connect:{source_region}:approved-origin:{origin}",
                    origin_url=origin,
                    config_summary={"origin_url": origin},
                ))
    except Exception:
        logger.warning(
            "Failed to list approved origins for instance %s",
            instance_id,
            exc_info=True,
        )

    logger.info(
        "Discovered %d approved origins for instance %s",
        len(resources),
        instance_id,
    )
    return resources
