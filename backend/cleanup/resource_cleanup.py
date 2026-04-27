"""Resource cleanup — disassociate and delete replicated resources from the target region.

Disassociates resources from the DR Connect instance first, then deletes them
in reverse dependency order (Lex → Lambda → IAM, etc.) to avoid orphaned references.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aws.client_factory import create_client, create_target_client
from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase
from replication.dependency_graph import build_dependency_graph, topological_sort

logger = logging.getLogger(__name__)


@dataclass
class CleanupEntry:
    """Result of cleaning up a single resource."""

    resource_id: str
    resource_name: str
    resource_type: str
    deleted: bool = False
    error: str | None = None


@dataclass
class CleanupResult:
    """Aggregated cleanup result."""

    entries: list[CleanupEntry] = field(default_factory=list)
    deleted_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0


def _delete_lambda(target_region: str, replicated_arn: str, name: str) -> None:
    client = create_target_client("lambda", target_region)
    client.delete_function(FunctionName=replicated_arn)


def _delete_lex_bot(
    target_region: str,
    replicated_arn: str,
    name: str,
    source_region: str = "",
) -> None:
    """Delete a Lex bot from the target region.

    For ALGR replicas, tries DeleteBotReplica from the source region first
    (cleaner removal). Falls back to DeleteBot if that fails.

    Args:
        target_region: The target AWS region where the replica lives.
        replicated_arn: ARN of the replicated bot.
        name: Human-readable bot name (for logging).
        source_region: The source region (used for DeleteBotReplica calls).
    """
    parts = replicated_arn.split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot extract bot ID from ARN: {replicated_arn}")
    bot_id = parts[-1]

    # Determine source region for DeleteBotReplica
    effective_source = source_region
    if not effective_source:
        from replication.lex_replication import _ALGR_SUPPORTED_PAIRS
        for src, tgt in _ALGR_SUPPORTED_PAIRS:
            if tgt == target_region:
                effective_source = src
                break

    # Try DeleteBotReplica first (works for ALGR-replicated bots)
    if effective_source:
        try:
            source_client = create_client("lexv2-models", effective_source)
            source_client.delete_bot_replica(
                botId=bot_id, replicaRegion=target_region
            )
            logger.info(
                "Deleted ALGR replica of bot '%s' from %s via DeleteBotReplica",
                name, target_region,
            )
            return
        except Exception as exc:
            logger.debug(
                "DeleteBotReplica failed for bot '%s' (will try DeleteBot): %s",
                name, exc,
            )

    # Fall back to DeleteBot
    client = create_target_client("lexv2-models", target_region)
    client.delete_bot(botId=bot_id, skipResourceInUseCheck=True)


def _delete_kinesis_stream(target_region: str, replicated_arn: str, name: str) -> None:
    stream_name = replicated_arn.split("/")[-1] if "/" in replicated_arn else name
    client = create_target_client("kinesis", target_region)
    client.delete_stream(StreamName=stream_name, EnforceConsumerDeletion=True)


def _delete_firehose(target_region: str, replicated_arn: str, name: str) -> None:
    stream_name = replicated_arn.split("/")[-1] if "/" in replicated_arn else name
    client = create_target_client("firehose", target_region)
    client.delete_delivery_stream(DeliveryStreamName=stream_name)


def _delete_kvs(target_region: str, replicated_arn: str, name: str) -> None:
    """Delete a KVS stream from the target region.

    For synthetic config-based ARNs (containing /config/media-streams/),
    there is no physical stream to delete — Connect creates KVS streams
    on-the-fly during calls. The MEDIA_STREAMS storage config disassociation
    (handled separately) is sufficient.

    Only attempt to delete a physical stream if the ARN looks like a real
    KVS stream ARN (containing :stream/).
    """
    if "config/media-streams/" in replicated_arn:
        logger.info(
            "KVS '%s' has synthetic config ARN — no physical stream to delete. "
            "Disassociation of MEDIA_STREAMS storage config is sufficient.",
            name,
        )
        return

    # Real KVS stream ARN — delete the physical stream
    client = create_target_client("kinesisvideo", target_region)
    client.delete_stream(StreamARN=replicated_arn)


def _delete_iam_role(replicated_arn: str, name: str) -> None:
    """Delete an IAM role and its attached/inline policies."""
    client = create_client("iam", "us-east-1")
    role_name = replicated_arn.split("/")[-1] if "/" in replicated_arn else name

    # Detach managed policies first
    try:
        attached = client.list_attached_role_policies(RoleName=role_name)
        for policy in attached.get("AttachedPolicies", []):
            client.detach_role_policy(
                RoleName=role_name, PolicyArn=policy["PolicyArn"]
            )
    except Exception:
        logger.debug("Failed to detach policies from role %s", role_name, exc_info=True)

    # Delete inline policies
    try:
        inline = client.list_role_policies(RoleName=role_name)
        for policy_name in inline.get("PolicyNames", []):
            client.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
    except Exception:
        logger.debug("Failed to delete inline policies from role %s", role_name, exc_info=True)

    client.delete_role(RoleName=role_name)


def _delete_s3_bucket(replicated_arn: str, name: str) -> None:
    """Delete an S3 bucket. Must be empty first."""
    bucket_name = replicated_arn.split(":::")[-1] if ":::" in replicated_arn else name
    client = create_client("s3", "us-east-1")

    # Empty the bucket first (delete all objects)
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = page.get("Contents", [])
            if objects:
                client.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
    except Exception:
        logger.debug("Failed to empty bucket %s", bucket_name, exc_info=True)

    client.delete_bucket(Bucket=bucket_name)


# Dispatcher mapping
_DELETERS = {
    ResourceType.LAMBDA: lambda tr, arn, name, **kw: _delete_lambda(tr, arn, name),
    ResourceType.LEX_BOT: lambda tr, arn, name, **kw: _delete_lex_bot(tr, arn, name, source_region=kw.get("source_region", "")),
    ResourceType.KINESIS_STREAM: lambda tr, arn, name, **kw: _delete_kinesis_stream(tr, arn, name),
    ResourceType.KINESIS_FIREHOSE: lambda tr, arn, name, **kw: _delete_firehose(tr, arn, name),
    ResourceType.KINESIS_VIDEO_STREAM: lambda tr, arn, name, **kw: _delete_kvs(tr, arn, name),
    ResourceType.IAM_ROLE: lambda tr, arn, name, **kw: _delete_iam_role(arn, name),
    ResourceType.S3_BUCKET: lambda tr, arn, name, **kw: _delete_s3_bucket(arn, name),
}

# Reverse dependency order: delete dependents first, then dependencies
_DELETE_ORDER = [
    ResourceType.LEX_BOT,
    ResourceType.LAMBDA,
    ResourceType.KINESIS_FIREHOSE,
    ResourceType.KINESIS_STREAM,
    ResourceType.KINESIS_VIDEO_STREAM,
    ResourceType.S3_BUCKET,
    ResourceType.IAM_ROLE,
]


def cleanup_replicated_resources(
    inventory: dict[str, ResourceBase],
    target_region: str,
    source_region: str = "",
    instance_id: str = "",
    target_instance_id: str = "",
    resource_ids: list[str] | None = None,
) -> CleanupResult:
    """Disassociate and delete replicated resources from the target region.

    Disassociates resources from the DR Connect instance first, then deletes
    them in reverse dependency order.

    Args:
        inventory: Session inventory mapping resource_id → ResourceBase.
        target_region: The target AWS region.
        source_region: The source AWS region (used for DeleteBotReplica).
        instance_id: The source Connect instance ID (for disassociation and listing source origins).
        target_instance_id: The target Connect instance ID from the ACGR replica
                            (for listing target origins and disassociating). If empty,
                            falls back to instance_id for backward compatibility.
        resource_ids: Optional list of specific resource IDs to clean up.
                      If None, cleans up all replicated resources.

    Returns:
        CleanupResult with per-resource outcomes.
    """
    result = CleanupResult()

    # Resolve the target instance ID: use explicit target_instance_id if provided,
    # otherwise fall back to instance_id for backward compatibility.
    resolved_target_id = target_instance_id or instance_id

    # Collect replicated resources — skip IAM roles (global, never delete)
    replicated = {
        rid: r for rid, r in inventory.items()
        if r.status == ReplicationStatus.REPLICATED
        and r.replicated_arn
        and (r.resource_type if isinstance(r.resource_type, ResourceType) else ResourceType(r.resource_type)) != ResourceType.IAM_ROLE
    }

    # Filter to specific resource IDs if provided
    if resource_ids is not None:
        replicated = {rid: r for rid, r in replicated.items() if rid in resource_ids}

    if not replicated:
        # Even with no replicated resources, still clean up approved origins
        if instance_id and source_region:
            origin_entries = _cleanup_approved_origins(
                source_region, target_region, instance_id, resolved_target_id,
            )
            for entry in origin_entries:
                result.entries.append(entry)
                if entry.deleted:
                    result.deleted_count += 1
                elif entry.error:
                    result.failed_count += 1
        return result

    # Step 1: Disassociate resources from the DR Connect instance
    if instance_id:
        _disassociate_resources(replicated, target_region, instance_id, result)

    # Step 2: Delete resources in reverse dependency order
    by_type: dict[ResourceType, list[tuple[str, ResourceBase]]] = {}
    for rid, r in replicated.items():
        rtype = r.resource_type if isinstance(r.resource_type, ResourceType) else ResourceType(r.resource_type)
        by_type.setdefault(rtype, []).append((rid, r))

    for rtype in _DELETE_ORDER:
        for rid, resource in by_type.get(rtype, []):
            deleter = _DELETERS.get(rtype)
            if deleter is None:
                result.entries.append(CleanupEntry(
                    resource_id=rid,
                    resource_name=resource.name,
                    resource_type=str(rtype.value),
                    deleted=False,
                    error=f"No cleanup handler for {rtype.value}",
                ))
                result.skipped_count += 1
                continue

            try:
                deleter(target_region, resource.replicated_arn, resource.name, source_region=source_region)
                result.entries.append(CleanupEntry(
                    resource_id=rid,
                    resource_name=resource.name,
                    resource_type=str(rtype.value),
                    deleted=True,
                ))
                result.deleted_count += 1
                logger.info("Deleted %s '%s' (%s)", rtype.value, resource.name, resource.replicated_arn)
            except Exception as exc:
                result.entries.append(CleanupEntry(
                    resource_id=rid,
                    resource_name=resource.name,
                    resource_type=str(rtype.value),
                    deleted=False,
                    error=str(exc),
                ))
                result.failed_count += 1
                logger.error("Failed to delete %s '%s': %s", rtype.value, resource.name, exc)

    # Step 3: Clean up approved origins from the target instance
    if instance_id and source_region:
        origin_entries = _cleanup_approved_origins(
            source_region, target_region, instance_id, resolved_target_id,
        )
        for entry in origin_entries:
            result.entries.append(entry)
            if entry.deleted:
                result.deleted_count += 1
            elif entry.error:
                result.failed_count += 1

    return result


def _cleanup_approved_origins(
    source_region: str, target_region: str,
    source_instance_id: str, target_instance_id: str,
) -> list[CleanupEntry]:
    """Remove approved origins from the target instance that exist on both source and target.

    Lists origins on both source (using source_instance_id) and target
    (using target_instance_id), then disassociates any origin that appears
    in both sets (the intersection) from the target instance.
    """
    from botocore.exceptions import ClientError

    entries: list[CleanupEntry] = []

    # List origins from the source instance
    try:
        source_connect = create_client("connect", source_region)
        source_origins: list[str] = []
        params: dict = {"InstanceId": source_instance_id, "MaxResults": 25}
        while True:
            resp = source_connect.list_approved_origins(**params)
            source_origins.extend(resp.get("Origins", []))
            nt = resp.get("NextToken")
            if not nt:
                break
            params["NextToken"] = nt
    except Exception as exc:
        logger.warning("Failed to list source approved origins: %s", exc)
        return entries

    if not source_origins:
        return entries

    # List origins from the target instance using target_instance_id
    try:
        target_connect = create_target_client("connect", target_region)
        target_origins: list[str] = []
        params = {"InstanceId": target_instance_id, "MaxResults": 25}
        while True:
            resp = target_connect.list_approved_origins(**params)
            target_origins.extend(resp.get("Origins", []))
            nt = resp.get("NextToken")
            if not nt:
                break
            params["NextToken"] = nt
    except Exception as exc:
        logger.warning("Failed to list target approved origins: %s", exc)
        return entries

    # Only disassociate origins that exist on both source and target
    origins_to_remove = set(source_origins) & set(target_origins)

    if not origins_to_remove:
        return entries

    for origin in origins_to_remove:
        try:
            target_connect.disassociate_approved_origin(
                InstanceId=target_instance_id, Origin=origin,
            )
            entries.append(CleanupEntry(
                resource_id=f"approved-origin-{origin}",
                resource_name=origin,
                resource_type="APPROVED_ORIGIN",
                deleted=True,
            ))
            logger.info("Removed approved origin '%s' from target instance", origin)
        except ClientError as exc:
            error_msg = str(exc)
            if "not found" in error_msg.lower() or "does not exist" in error_msg.lower():
                logger.debug("Approved origin '%s' not on target — skipping", origin)
            else:
                entries.append(CleanupEntry(
                    resource_id=f"approved-origin-{origin}",
                    resource_name=origin,
                    resource_type="APPROVED_ORIGIN",
                    deleted=False,
                    error=error_msg,
                ))
                logger.warning("Failed to remove approved origin '%s': %s", origin, exc)
        except Exception as exc:
            entries.append(CleanupEntry(
                resource_id=f"approved-origin-{origin}",
                resource_name=origin,
                resource_type="APPROVED_ORIGIN",
                deleted=False,
                error=str(exc),
            ))

    return entries


def _disassociate_resources(
    replicated: dict[str, ResourceBase],
    target_region: str,
    instance_id: str,
    result: CleanupResult,
) -> None:
    """Disassociate resources from the DR Connect instance before deletion."""
    connect_client = create_target_client("connect", target_region)

    for rid, resource in replicated.items():
        rtype = resource.resource_type if isinstance(resource.resource_type, ResourceType) else ResourceType(resource.resource_type)
        try:
            if rtype == ResourceType.LAMBDA:
                _disassociate_lambda(connect_client, instance_id, resource.replicated_arn)
            elif rtype == ResourceType.LEX_BOT:
                _disassociate_lex_bot(connect_client, instance_id, resource.replicated_arn, target_region)
            elif rtype in (ResourceType.S3_BUCKET, ResourceType.KINESIS_STREAM,
                           ResourceType.KINESIS_FIREHOSE, ResourceType.KINESIS_VIDEO_STREAM):
                _disassociate_storage_config(connect_client, instance_id, resource, target_region)
            logger.info("Disassociated %s '%s' from instance %s", rtype.value, resource.name, instance_id)
        except Exception as exc:
            # Disassociation failures are non-fatal — log and continue to deletion
            logger.warning("Failed to disassociate %s '%s': %s", rtype.value, resource.name, exc)


def _disassociate_lambda(connect_client, instance_id: str, function_arn: str) -> None:
    """Disassociate a Lambda function from the Connect instance."""
    try:
        connect_client.disassociate_lambda_function(
            InstanceId=instance_id,
            FunctionArn=function_arn,
        )
    except Exception as exc:
        if "not found" in str(exc).lower() or "not associated" in str(exc).lower():
            logger.debug("Lambda %s already not associated", function_arn)
        else:
            raise


def _disassociate_lex_bot(connect_client, instance_id: str, bot_arn: str, target_region: str) -> None:
    """Disassociate a Lex V2 bot from the Connect instance."""
    # We need the alias ARN, not the bot ARN. List associated bots to find it.
    try:
        resp = connect_client.list_bots(InstanceId=instance_id, LexVersion="V2", MaxResults=25)
        for bot_summary in resp.get("LexBots", []):
            lex_bot = bot_summary.get("LexV2Bot", {})
            alias_arn = lex_bot.get("AliasArn", "")
            # Match by bot ID from the ARN
            parts = bot_arn.split("/")
            bot_id = parts[-1] if len(parts) >= 2 else ""
            if bot_id and bot_id in alias_arn:
                connect_client.disassociate_bot(
                    InstanceId=instance_id,
                    LexV2Bot={"AliasArn": alias_arn},
                )
                return
        logger.debug("Lex bot %s not found in associated bots", bot_arn)
    except Exception as exc:
        if "not found" in str(exc).lower():
            logger.debug("Lex bot %s already not associated", bot_arn)
        else:
            raise


def _disassociate_storage_config(connect_client, instance_id: str, resource: ResourceBase, target_region: str) -> None:
    """Disassociate storage configs that reference this resource."""
    rtype = resource.resource_type if isinstance(resource.resource_type, ResourceType) else ResourceType(resource.resource_type)

    # Map resource types to the storage config resource types they can be associated with
    storage_resource_types = []
    if rtype == ResourceType.S3_BUCKET:
        storage_resource_types = ["CALL_RECORDINGS", "CHAT_TRANSCRIPTS", "SCHEDULED_REPORTS"]
    elif rtype == ResourceType.KINESIS_STREAM:
        storage_resource_types = ["AGENT_EVENTS", "CONTACT_TRACE_RECORDS"]
    elif rtype == ResourceType.KINESIS_FIREHOSE:
        storage_resource_types = ["CONTACT_TRACE_RECORDS", "AGENT_EVENTS"]
    elif rtype == ResourceType.KINESIS_VIDEO_STREAM:
        storage_resource_types = ["MEDIA_STREAMS"]

    for srt in storage_resource_types:
        try:
            resp = connect_client.list_instance_storage_configs(
                InstanceId=instance_id,
                ResourceType=srt,
            )
            for config in resp.get("StorageConfigs", []):
                assoc_id = config.get("AssociationId", "")
                if assoc_id:
                    connect_client.disassociate_instance_storage_config(
                        InstanceId=instance_id,
                        AssociationId=assoc_id,
                        ResourceType=srt,
                    )
                    logger.info("Disassociated storage config %s (%s) from instance", assoc_id, srt)
        except Exception as exc:
            logger.warning("Failed to disassociate storage config %s for %s: %s", srt, resource.name, exc)
