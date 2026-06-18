"""Discovery orchestrator for Amazon Connect ACGR Resource Replicator.

Coordinates all discovery modules (Lambda, Lex, Streaming, KVS, DynamoDB),
deduplicates resources by ARN, and builds a unified ResourceInventory.
Creates a Session with the inventory and persists it via the session store.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from models.inventory import ResourceInventory
from models.resources import ResourceBase, S3BucketResource
from models.session import Session

from discovery.lambda_discovery import discover_lambda_functions
from discovery.lex_discovery import discover_lex_bots
from discovery.streaming_discovery import discover_streaming_resources
from discovery.kvs_discovery import discover_kvs_resources
from discovery.s3_discovery import discover_s3_buckets
from discovery.approved_origins_discovery import discover_approved_origins

logger = logging.getLogger(__name__)


def _deduplicate_resources(
    resources: list[ResourceBase],
) -> list[ResourceBase]:
    """Deduplicate resources by ARN, keeping the first occurrence."""
    seen_arns: set[str] = set()
    unique: list[ResourceBase] = []

    for resource in resources:
        if resource.arn not in seen_arns:
            seen_arns.add(resource.arn)
            unique.append(resource)

    return unique


def run_discovery(
    instance_id: str,
    source_region: str,
) -> tuple[str, ResourceInventory]:
    """Run full discovery for a Connect instance across all resource types.

    Orchestrates discovery in this order:
    1. Lambda — discovers functions, IAM roles, ESM-triggered DynamoDB/Kinesis ARNs
    2. Lex — discovers bots, extracts fulfillment Lambda ARNs (added to Lambda results)
    3. Streaming — discovers CTR and Agent Event Stream Kinesis/Firehose resources
    4. KVS — discovers Amazon Kinesis Video Streams and Lambda consumers (added to Lambda results)
    5. DynamoDB — discovers tables from IAM policies, env vars, and ESM trigger ARNs

    After all modules run, resources are deduplicated by ARN and assembled
    into a ResourceInventory. A Session is created and its session_id returned.

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        A tuple of (session_id, inventory).
    """
    all_resources: list[ResourceBase] = []

    # ---------------------------------------------------------------
    # Step 1: Lambda discovery
    # ---------------------------------------------------------------
    logger.info("Starting Lambda discovery for instance %s", instance_id)
    lambda_resources, iam_role_resources, esm_dynamodb_arns, esm_kinesis_arns = (
        discover_lambda_functions(instance_id, source_region)
    )
    all_resources.extend(iam_role_resources)
    all_resources.extend(lambda_resources)

    # ---------------------------------------------------------------
    # Step 2: Lex discovery
    # ---------------------------------------------------------------
    logger.info("Starting Lex discovery for instance %s", instance_id)
    lex_resources, fulfillment_lambda_arns = discover_lex_bots(instance_id, source_region)
    all_resources.extend(lex_resources)

    # Fulfillment Lambdas that weren't already discovered need to be
    # discovered individually via lambda:GetFunction.
    # These are Lex-only lambdas (codehook/fulfillment) — NOT directly
    # associated with Connect, so mark them as is_lex_codehook=True.
    existing_lambda_arns = {r.arn for r in lambda_resources}
    new_fulfillment_arns = [
        arn for arn in fulfillment_lambda_arns if arn not in existing_lambda_arns
    ]
    if new_fulfillment_arns:
        logger.info(
            "Discovering %d additional fulfillment Lambda functions from Lex",
            len(new_fulfillment_arns),
        )
        from discovery.lambda_discovery import discover_single_lambda
        from models.resources import LambdaResource as _LR
        for arn in new_fulfillment_arns:
            try:
                extra_resources = discover_single_lambda(arn, source_region)
                # Mark Lambda resources discovered via Lex (not via Connect
                # ListLambdaFunctions) as Lex codehook — they should NOT be
                # associated directly with the Connect instance.
                for r in extra_resources:
                    if isinstance(r, _LR):
                        r.is_lex_codehook = True
                all_resources.extend(extra_resources)
            except Exception:
                logger.debug("Failed to discover fulfillment Lambda: %s", arn)

    # ---------------------------------------------------------------
    # Step 3: Streaming discovery
    # ---------------------------------------------------------------
    logger.info("Starting Streaming discovery for instance %s", instance_id)
    streaming_resources = discover_streaming_resources(instance_id, source_region)
    all_resources.extend(streaming_resources)

    # ---------------------------------------------------------------
    # Step 4: KVS discovery
    # ---------------------------------------------------------------
    logger.info("Starting KVS discovery for instance %s", instance_id)
    kvs_resources, lambda_consumer_arns = discover_kvs_resources(instance_id, source_region)
    all_resources.extend(kvs_resources)

    # KVS consumer Lambdas — discover individually via lambda:GetFunction
    existing_lambda_arns_after_lex = {r.arn for r in all_resources if hasattr(r, 'runtime')}
    new_consumer_arns = [
        arn for arn in lambda_consumer_arns if arn not in existing_lambda_arns_after_lex
    ]
    if new_consumer_arns:
        logger.info(
            "Discovering %d KVS consumer Lambda functions",
            len(new_consumer_arns),
        )
        from discovery.lambda_discovery import discover_single_lambda
        for arn in new_consumer_arns:
            try:
                extra_resources = discover_single_lambda(arn, source_region)
                all_resources.extend(extra_resources)
            except Exception:
                logger.debug("Failed to discover KVS consumer Lambda: %s", arn)

    # ---------------------------------------------------------------
    # Step 5: S3 bucket discovery
    # ---------------------------------------------------------------
    logger.info("Starting S3 bucket discovery for instance %s", instance_id)
    s3_resources = discover_s3_buckets(instance_id, source_region)
    all_resources.extend(s3_resources)

    # ---------------------------------------------------------------
    # Step 6: Approved Origins discovery
    # ---------------------------------------------------------------
    logger.info("Starting Approved Origins discovery for instance %s", instance_id)
    approved_origin_resources = discover_approved_origins(instance_id, source_region)
    all_resources.extend(approved_origin_resources)

    # ---------------------------------------------------------------
    # Step 5b: Discover Firehose destination S3 buckets not already found
    # ---------------------------------------------------------------
    existing_s3_arns = {r.arn for r in s3_resources}
    from models.enums import ResourceType as _RT
    for sr in streaming_resources:
        if sr.resource_type == _RT.KINESIS_FIREHOSE:
            bucket_arn = sr.config_summary.get("s3_bucket", "")
            if bucket_arn and bucket_arn not in existing_s3_arns:
                # Extract bucket name from ARN (arn:aws:s3:::bucket-name)
                bucket_name = bucket_arn.split(":::")[-1] if ":::" in bucket_arn else ""
                if bucket_name:
                    from discovery.s3_discovery import _generate_resource_id as _s3_rid
                    rid = _s3_rid(bucket_arn)
                    firehose_s3 = S3BucketResource(
                        id=rid,
                        name=bucket_name,
                        arn=bucket_arn,
                        bucket_region=source_region,
                        storage_types=["FIREHOSE_DESTINATION"],
                        config_summary={"storage_types": "FIREHOSE_DESTINATION"},
                    )
                    all_resources.append(firehose_s3)
                    existing_s3_arns.add(bucket_arn)
                    logger.info(
                        "Added Firehose destination S3 bucket: %s", bucket_name
                    )

    # ---------------------------------------------------------------
    # Auto-mark IAM roles as REPLICATED (IAM is global)
    # ---------------------------------------------------------------
    from models.enums import ReplicationStatus as _RS
    for resource in all_resources:
        if resource.resource_type == _RT.IAM_ROLE:
            resource.status = _RS.REPLICATED
            resource.replicated_arn = resource.arn

    # ---------------------------------------------------------------
    # Deduplicate and build inventory
    # ---------------------------------------------------------------
    unique_resources = _deduplicate_resources(all_resources)

    inventory = ResourceInventory()
    for resource in unique_resources:
        inventory.add_resource(resource)

    logger.info(
        "Discovery complete for instance %s: %d unique resources",
        instance_id,
        len(inventory),
    )

    # ---------------------------------------------------------------
    # Create session
    # ---------------------------------------------------------------
    session_id = str(uuid.uuid4())

    return session_id, inventory
