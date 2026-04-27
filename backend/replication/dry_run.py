"""Dry run simulation — shows what would be created without making AWS calls.

Generates simulated target ARNs and marks resources as REPLICATED
without actually creating anything.
"""

from __future__ import annotations

import logging

from aws.arn_utils import parse_arn
from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase
from models.session import Session
from replication.dependency_graph import build_dependency_graph, topological_sort

logger = logging.getLogger(__name__)

# Region mapping for ARN rewriting
_REGION_REWRITE = {
    "us-east-1": "us-west-2",
    "us-west-2": "us-east-1",
    "eu-west-2": "eu-central-1",
    "eu-central-1": "eu-west-2",
    "ap-southeast-1": "ap-northeast-1",
    "ap-northeast-1": "ap-southeast-1",
    "ap-southeast-2": "ap-northeast-2",
    "ap-northeast-2": "ap-southeast-2",
}


def _simulate_target_arn(resource: ResourceBase, target_region: str, resource_tags: dict[str, str] | None = None) -> str:
    """Generate a simulated target ARN for a resource.

    S3 buckets get a hardcoded '-dr' suffix. All other resources use the same name.
    """
    rtype = resource.resource_type
    if isinstance(rtype, str):
        rtype = ResourceType(rtype)

    # S3 gets hardcoded "-dr" suffix, everything else uses same name
    if rtype == ResourceType.S3_BUCKET:
        target_name = resource.name + "-dr"
    else:
        target_name = resource.name

    try:
        parsed = parse_arn(resource.arn)
        account = parsed["account"]
        partition = parsed["partition"]
    except Exception:
        account = "123456789012"
        partition = "aws"

    if rtype == ResourceType.IAM_ROLE:
        return f"arn:{partition}:iam::{account}:role/{target_name}"
    elif rtype == ResourceType.LAMBDA:
        return f"arn:{partition}:lambda:{target_region}:{account}:function:{target_name}"
    elif rtype == ResourceType.LEX_BOT:
        return f"arn:{partition}:lex:{target_region}:{account}:bot/{target_name}"
    elif rtype == ResourceType.KINESIS_STREAM:
        return f"arn:{partition}:kinesis:{target_region}:{account}:stream/{target_name}"
    elif rtype == ResourceType.KINESIS_FIREHOSE:
        return f"arn:{partition}:firehose:{target_region}:{account}:deliverystream/{target_name}"
    elif rtype == ResourceType.KINESIS_VIDEO_STREAM:
        return f"arn:{partition}:kinesisvideo:{target_region}:{account}:stream/{target_name}/0"
    elif rtype == ResourceType.S3_BUCKET:
        return f"arn:{partition}:s3:::{target_name}"
    else:
        return f"arn:{partition}:unknown:{target_region}:{account}:{target_name}"


def simulate_replication(
    session: Session,
    resource_ids: list[str],
    job_id: str,
    resource_tags: dict[str, str] | None = None,
) -> dict[str, ResourceBase]:
    """Simulate replication without making any AWS calls.

    Returns a dict of resource_id → updated ResourceBase with simulated
    target ARNs and REPLICATED status.
    """
    selected: dict[str, ResourceBase] = {}
    for rid in resource_ids:
        resource = session.inventory.get(rid)
        if resource is not None:
            selected[rid] = resource

    if not selected:
        return {}

    # Build dependency graph and sort
    resource_list = list(selected.values())
    graph = build_dependency_graph(resource_list)
    try:
        execution_order = topological_sort(graph)
    except ValueError:
        execution_order = list(selected.keys())

    target_region = session.target_region

    for rid in execution_order:
        resource = selected.get(rid)
        if resource is None:
            continue

        simulated_arn = _simulate_target_arn(resource, target_region, resource_tags)
        resource.status = ReplicationStatus.REPLICATED
        resource.replicated_arn = f"[DRY RUN] {simulated_arn}"
        resource.error = None

    logger.info("Dry run complete: %d resources simulated for job %s", len(selected), job_id)
    return selected
