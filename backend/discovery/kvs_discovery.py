"""Kinesis Video Stream discovery for Connect ACGR Resource Replicator.

Discovers KVS (Kinesis Video Streams) used by a Connect instance for media
streaming (e.g., live call audio for contact lens, voicemail). For each
storage config, retrieves full KVS stream details and identifies Lambda
consumers that process those streams.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aws.client_factory import create_source_client
from models.resources import KinesisVideoResource

logger = logging.getLogger(__name__)

# Connect storage resource type for KVS discovery
_STORAGE_RESOURCE_TYPE = "MEDIA_STREAMS"


def _generate_resource_id(arn: str) -> str:
    """Generate a deterministic resource ID from an ARN using a SHA-256 hash prefix."""
    return hashlib.sha256(arn.encode()).hexdigest()[:12]


def _get_storage_configs(
    connect_client: Any, instance_id: str
) -> list[dict[str, Any]]:
    """Call Connect ListInstanceStorageConfigs for MEDIA_STREAMS.

    Handles pagination via NextToken.

    Args:
        connect_client: A boto3 Connect client.
        instance_id: The Connect instance ID.

    Returns:
        A list of storage config dicts.
    """
    configs: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "InstanceId": instance_id,
        "ResourceType": _STORAGE_RESOURCE_TYPE,
    }

    while True:
        response = connect_client.list_instance_storage_configs(**params)
        configs.extend(response.get("StorageConfigs", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token

    return configs


def _extract_kvs_stream_arns(config: dict[str, Any]) -> list[str]:
    """Extract Kinesis Video Stream ARNs from a MEDIA_STREAMS storage config.

    The Connect MEDIA_STREAMS storage config contains a KinesisVideoStreamConfig
    with an EncryptionConfig and a Prefix. The prefix is used by Connect to
    create KVS streams on-the-fly during calls. Since individual stream ARNs
    are not stored in the config, we use the KVS ListStreams API to find
    streams matching the prefix.

    However, the storage config may also reference a specific stream ARN
    under KinesisVideoStreamConfig.  We extract whatever ARN information
    is available.

    Returns:
        A list of KVS stream ARNs found, or an empty list.
    """
    kvs_config = config.get("KinesisVideoStreamConfig")
    if not kvs_config:
        return []

    arns: list[str] = []

    # Some configs may have a direct stream ARN
    stream_arn = kvs_config.get("StreamArn")
    if stream_arn:
        arns.append(stream_arn)

    return arns


def _get_kvs_stream_prefix(config: dict[str, Any]) -> str | None:
    """Extract the KVS stream name prefix from a MEDIA_STREAMS storage config.

    Connect creates KVS streams with this prefix during calls.

    Returns:
        The prefix string, or None if not present.
    """
    kvs_config = config.get("KinesisVideoStreamConfig")
    if not kvs_config:
        return None
    return kvs_config.get("Prefix") or None


def _list_kvs_streams_by_prefix(
    kvs_client: Any, prefix: str
) -> list[dict[str, Any]]:
    """List KVS streams matching a name prefix.

    Handles pagination via NextToken.

    Args:
        kvs_client: A boto3 Kinesis Video client.
        prefix: The stream name prefix to filter by.

    Returns:
        A list of stream info dicts from ListStreams.
    """
    streams: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "StreamNameCondition": {
            "ComparisonOperator": "BEGINS_WITH",
            "ComparisonValue": prefix,
        }
    }

    try:
        while True:
            response = kvs_client.list_streams(**params)
            streams.extend(response.get("StreamInfoList", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except Exception:
        logger.exception("Failed to list KVS streams with prefix: %s", prefix)

    return streams


def _describe_kvs_stream(
    kvs_client: Any, stream_arn: str
) -> dict[str, Any] | None:
    """Call KVS DescribeStream to retrieve full stream configuration.

    Returns None if the stream cannot be described.
    """
    try:
        response = kvs_client.describe_stream(StreamARN=stream_arn)
        return response.get("StreamInfo")
    except Exception:
        logger.exception("Failed to describe KVS stream: %s", stream_arn)
        return None


def _build_kvs_resource(
    stream_arn: str, stream_info: dict[str, Any]
) -> KinesisVideoResource:
    """Build a KinesisVideoResource from DescribeStream response."""
    stream_name = stream_info.get("StreamName", "")
    data_retention = stream_info.get("DataRetentionInHours", 0)
    kms_key_id = stream_info.get("KmsKeyId")

    # Determine encryption type from KMS key presence
    encryption_type: str | None = None
    if kms_key_id:
        encryption_type = "KMS"

    config_summary: dict[str, str] = {
        "data_retention_hours": str(data_retention),
    }
    if encryption_type:
        config_summary["encryption"] = encryption_type

    resource_id = _generate_resource_id(stream_arn)

    return KinesisVideoResource(
        id=resource_id,
        name=stream_name,
        arn=stream_arn,
        data_retention_in_hours=data_retention,
        encryption_type=encryption_type,
        config_summary=config_summary,
    )


def _find_lambda_consumers(
    lambda_client: Any, kvs_stream_arns: list[str]
) -> list[str]:
    """Identify Lambda functions that consume from KVS streams.

    Checks Lambda event source mappings for any that reference the given
    KVS stream ARNs.

    Args:
        lambda_client: A boto3 Lambda client.
        kvs_stream_arns: List of KVS stream ARNs to check.

    Returns:
        A list of Lambda function ARNs that consume from the KVS streams.
    """
    consumer_arns: list[str] = []
    seen: set[str] = set()

    for stream_arn in kvs_stream_arns:
        try:
            params: dict[str, Any] = {"EventSourceArn": stream_arn}
            while True:
                response = lambda_client.list_event_source_mappings(**params)
                for mapping in response.get("EventSourceMappings", []):
                    func_arn = mapping.get("FunctionArn", "")
                    if func_arn and func_arn not in seen:
                        seen.add(func_arn)
                        consumer_arns.append(func_arn)
                next_marker = response.get("NextMarker")
                if not next_marker:
                    break
                params["Marker"] = next_marker
        except Exception:
            logger.exception(
                "Failed to list event source mappings for KVS stream: %s",
                stream_arn,
            )

    return consumer_arns



def discover_kvs_resources(
    instance_id: str, source_region: str
) -> tuple[list[KinesisVideoResource], list[str]]:
    """Discover KVS resources associated with a Connect instance.

    This function:
    1. Calls Connect ListInstanceStorageConfigs for MEDIA_STREAMS
    2. For each config, extracts KVS stream ARNs (direct or via prefix listing)
    3. For each KVS stream, calls KVS DescribeStream to get full config
       (data retention, encryption)
    4. If no actual streams are found but a storage config with a prefix exists,
       creates a synthetic KinesisVideoResource representing the config so it
       appears in the inventory and can be replicated/associated on the DR instance.
    5. Identifies Lambda consumers that process KVS streams via ESM

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        A tuple of:
            - kvs_resources: List of discovered KinesisVideoResource objects
            - lambda_consumer_arns: List of Lambda ARNs that consume from
              KVS streams (for cross-discovery)
    """
    connect_client = create_source_client("connect", source_region)
    kvs_client = create_source_client("kinesisvideo", source_region)
    lambda_client = create_source_client("lambda", source_region)

    resources: list[KinesisVideoResource] = []
    seen_arns: set[str] = set()
    all_stream_arns: list[str] = []

    # Step 1: Get MEDIA_STREAMS storage configs
    try:
        configs = _get_storage_configs(connect_client, instance_id)
    except Exception:
        logger.exception(
            "Failed to list MEDIA_STREAMS storage configs for instance %s",
            instance_id,
        )
        return [], []

    logger.info(
        "Found %d MEDIA_STREAMS storage configs for instance %s",
        len(configs),
        instance_id,
    )

    # Track prefixes from configs for synthetic resource creation
    config_prefixes: list[dict] = []

    # Step 2: Extract stream ARNs from configs
    for config in configs:
        # Try direct ARNs first
        direct_arns = _extract_kvs_stream_arns(config)
        for arn in direct_arns:
            if arn not in seen_arns:
                seen_arns.add(arn)
                all_stream_arns.append(arn)

        # Also try prefix-based listing
        prefix = _get_kvs_stream_prefix(config)
        if prefix:
            # Save config details for synthetic resource creation if needed
            kvs_config = config.get("KinesisVideoStreamConfig", {})
            config_prefixes.append({
                "prefix": prefix,
                "retention_hours": kvs_config.get("RetentionPeriodHours", 0),
                "encryption_config": kvs_config.get("EncryptionConfig"),
            })

            prefix_streams = _list_kvs_streams_by_prefix(kvs_client, prefix)
            for stream_info in prefix_streams:
                stream_arn = stream_info.get("StreamARN", "")
                if stream_arn and stream_arn not in seen_arns:
                    seen_arns.add(stream_arn)
                    all_stream_arns.append(stream_arn)

    # Step 3: Describe each stream
    for stream_arn in all_stream_arns:
        stream_info = _describe_kvs_stream(kvs_client, stream_arn)
        if stream_info is not None:
            resources.append(_build_kvs_resource(stream_arn, stream_info))

    # Step 4: If no actual streams were found but storage configs with prefixes
    # exist, create synthetic KinesisVideoResource entries so the MEDIA_STREAMS
    # config appears in the inventory. KVS streams are ephemeral (created
    # on-the-fly during calls), so the config itself is what needs replicating.
    if not resources and config_prefixes:
        for cp in config_prefixes:
            prefix = cp["prefix"]
            retention = cp["retention_hours"]
            enc = cp.get("encryption_config")

            # Build a synthetic ARN using the instance and prefix
            synthetic_arn = (
                f"arn:aws:kinesisvideo:{source_region}:"
                f"config/media-streams/{instance_id}/{prefix}"
            )
            resource_id = _generate_resource_id(synthetic_arn)

            encryption_type: str | None = None
            if enc and enc.get("KeyId"):
                encryption_type = enc.get("EncryptionType", "KMS")

            config_summary: dict[str, str] = {
                "prefix": prefix,
                "data_retention_hours": str(retention),
                "type": "MEDIA_STREAMS storage config",
            }
            if encryption_type:
                config_summary["encryption"] = encryption_type

            resources.append(
                KinesisVideoResource(
                    id=resource_id,
                    name=prefix,
                    arn=synthetic_arn,
                    data_retention_in_hours=retention,
                    encryption_type=encryption_type,
                    config_summary=config_summary,
                )
            )
            logger.info(
                "Created synthetic KVS resource for MEDIA_STREAMS config: prefix=%s",
                prefix,
            )

    # Step 5: Find Lambda consumers
    lambda_consumer_arns = _find_lambda_consumers(lambda_client, list(seen_arns))

    logger.info(
        "KVS discovery complete: %d streams/configs, %d Lambda consumers for instance %s",
        len(resources),
        len(lambda_consumer_arns),
        instance_id,
    )

    return resources, lambda_consumer_arns

