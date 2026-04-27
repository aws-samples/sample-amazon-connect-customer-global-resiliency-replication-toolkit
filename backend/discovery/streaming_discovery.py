"""Streaming configuration discovery for Connect ACGR Resource Replicator.

Discovers CTR (Contact Trace Records) and Agent Event Stream configurations
associated with a Connect instance. For each storage config, retrieves full
Kinesis Data Stream or Kinesis Firehose delivery stream details.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aws.client_factory import create_source_client
from models.resources import KinesisFirehoseResource, KinesisStreamResource

logger = logging.getLogger(__name__)

# Connect storage resource types for streaming discovery
_STORAGE_RESOURCE_TYPES = ["CONTACT_TRACE_RECORDS", "AGENT_EVENTS"]


def _generate_resource_id(arn: str) -> str:
    """Generate a deterministic resource ID from an ARN using a SHA-256 hash prefix."""
    return hashlib.sha256(arn.encode()).hexdigest()[:12]


def _get_storage_configs(
    connect_client: Any, instance_id: str, resource_type: str
) -> list[dict[str, Any]]:
    """Call Connect ListInstanceStorageConfigs for a given resource type.

    Handles pagination via NextToken.

    Args:
        connect_client: A boto3 Connect client.
        instance_id: The Connect instance ID.
        resource_type: The storage resource type (e.g. CONTACT_TRACE_RECORDS).

    Returns:
        A list of storage config dicts.
    """
    configs: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "InstanceId": instance_id,
        "ResourceType": resource_type,
    }

    while True:
        response = connect_client.list_instance_storage_configs(**params)
        configs.extend(response.get("StorageConfigs", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token

    return configs


def _extract_kinesis_stream_arn(config: dict[str, Any]) -> str | None:
    """Extract a Kinesis Data Stream ARN from a storage config.

    The ARN lives under KinesisStreamConfig.StreamArn.

    Returns:
        The stream ARN, or None if not present.
    """
    kinesis_config = config.get("KinesisStreamConfig")
    if not kinesis_config:
        return None
    return kinesis_config.get("StreamArn") or None


def _extract_firehose_arn(config: dict[str, Any]) -> str | None:
    """Extract a Kinesis Firehose delivery stream ARN from a storage config.

    The ARN lives under KinesisFirehoseConfig.FirehoseArn.

    Returns:
        The delivery stream ARN, or None if not present.
    """
    firehose_config = config.get("KinesisFirehoseConfig")
    if not firehose_config:
        return None
    return firehose_config.get("FirehoseArn") or None


def _describe_kinesis_stream(
    kinesis_client: Any, stream_arn: str
) -> dict[str, Any] | None:
    """Call Kinesis DescribeStream to retrieve full stream configuration.

    Returns None if the stream cannot be described.
    """
    try:
        # Extract stream name from ARN (arn:aws:kinesis:region:account:stream/name)
        stream_name = stream_arn.rsplit("/", 1)[-1]
        response = kinesis_client.describe_stream(StreamName=stream_name)
        return response.get("StreamDescription")
    except Exception:
        logger.exception("Failed to describe Kinesis stream: %s", stream_arn)
        return None


def _describe_firehose_stream(
    firehose_client: Any, delivery_stream_arn: str
) -> dict[str, Any] | None:
    """Call Firehose DescribeDeliveryStream to retrieve full delivery stream config.

    Returns None if the delivery stream cannot be described.
    """
    try:
        # Extract delivery stream name from ARN
        stream_name = delivery_stream_arn.rsplit("/", 1)[-1]
        response = firehose_client.describe_delivery_stream(
            DeliveryStreamName=stream_name
        )
        return response.get("DeliveryStreamDescription")
    except Exception:
        logger.exception(
            "Failed to describe Firehose delivery stream: %s", delivery_stream_arn
        )
        return None


def _build_kinesis_stream_resource(
    stream_arn: str, description: dict[str, Any]
) -> KinesisStreamResource:
    """Build a KinesisStreamResource from DescribeStream response."""
    stream_name = description.get("StreamName", "")
    shard_count = len(description.get("Shards", []))
    retention_period = description.get("RetentionPeriodHours", 24)
    encryption_type = description.get("EncryptionType")
    stream_mode = (
        description.get("StreamModeDetails", {}).get("StreamMode", "PROVISIONED")
    )

    config_summary = {
        "shard_count": str(shard_count),
        "retention_hours": str(retention_period),
        "stream_mode": stream_mode,
    }
    if encryption_type and encryption_type != "NONE":
        config_summary["encryption"] = encryption_type

    resource_id = _generate_resource_id(stream_arn)

    return KinesisStreamResource(
        id=resource_id,
        name=stream_name,
        arn=stream_arn,
        shard_count=shard_count,
        retention_period=retention_period,
        encryption_type=encryption_type,
        stream_mode=stream_mode,
        config_summary=config_summary,
    )


def _build_firehose_resource(
    delivery_stream_arn: str, description: dict[str, Any]
) -> KinesisFirehoseResource:
    """Build a KinesisFirehoseResource from DescribeDeliveryStream response.

    If the Firehose destination is S3/ExtendedS3, the resource's dependencies
    list will include the deterministic resource ID for the S3 bucket ARN,
    enabling the dependency graph to link Firehose → S3.
    """
    stream_name = description.get("DeliveryStreamName", "")

    # Determine destination type and config from Destinations
    destinations = description.get("Destinations", [])
    destination_type = "Unknown"
    s3_destination: dict | None = None
    buffering_hints: dict | None = None

    if destinations:
        dest = destinations[0]
        if dest.get("ExtendedS3DestinationDescription"):
            destination_type = "ExtendedS3"
            s3_desc = dest["ExtendedS3DestinationDescription"]
            s3_destination = {
                "BucketARN": s3_desc.get("BucketARN", ""),
                "RoleARN": s3_desc.get("RoleARN", ""),
                "Prefix": s3_desc.get("Prefix", ""),
                "ErrorOutputPrefix": s3_desc.get("ErrorOutputPrefix", ""),
            }
            buffering_hints = s3_desc.get("BufferingHints")
        elif dest.get("S3DestinationDescription"):
            destination_type = "S3"
            s3_desc = dest["S3DestinationDescription"]
            s3_destination = {
                "BucketARN": s3_desc.get("BucketARN", ""),
                "RoleARN": s3_desc.get("RoleARN", ""),
                "Prefix": s3_desc.get("Prefix", ""),
            }
            buffering_hints = s3_desc.get("BufferingHints")
        elif dest.get("RedshiftDestinationDescription"):
            destination_type = "Redshift"
        elif dest.get("ElasticsearchDestinationDescription"):
            destination_type = "Elasticsearch"
        elif dest.get("SplunkDestinationDescription"):
            destination_type = "Splunk"
        elif dest.get("HttpEndpointDestinationDescription"):
            destination_type = "HttpEndpoint"

    config_summary: dict[str, str] = {
        "destination_type": destination_type,
    }
    if s3_destination:
        config_summary["s3_bucket"] = s3_destination.get("BucketARN", "")
    if buffering_hints:
        size_mb = buffering_hints.get("SizeInMBs", "")
        interval_s = buffering_hints.get("IntervalInSeconds", "")
        config_summary["buffering"] = f"{size_mb}MB / {interval_s}s"

    resource_id = _generate_resource_id(delivery_stream_arn)

    # Build dependency on S3 bucket if destination is S3-based
    dependencies: list[str] = []
    if s3_destination:
        bucket_arn = s3_destination.get("BucketARN", "")
        if bucket_arn:
            dependencies.append(_generate_resource_id(bucket_arn))

    return KinesisFirehoseResource(
        id=resource_id,
        name=stream_name,
        arn=delivery_stream_arn,
        destination_type=destination_type,
        s3_destination=s3_destination,
        buffering_hints=buffering_hints,
        config_summary=config_summary,
        dependencies=dependencies,
    )


def discover_streaming_resources(
    instance_id: str, source_region: str
) -> list[KinesisStreamResource | KinesisFirehoseResource]:
    """Discover streaming resources associated with a Connect instance.

    This function:
    1. Calls Connect ListInstanceStorageConfigs for CONTACT_TRACE_RECORDS
       and AGENT_EVENTS resource types
    2. For each Kinesis Data Stream ARN found, calls Kinesis DescribeStream
       to get full config (shard count, retention, encryption, stream mode)
    3. For each Firehose delivery stream ARN found, calls Firehose
       DescribeDeliveryStream to get full config (destination type, S3
       destination, buffering hints)

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        A list of KinesisStreamResource and KinesisFirehoseResource objects.
    """
    connect_client = create_source_client("connect", source_region)
    kinesis_client = create_source_client("kinesis", source_region)
    firehose_client = create_source_client("firehose", source_region)

    resources: list[KinesisStreamResource | KinesisFirehoseResource] = []
    seen_arns: set[str] = set()

    for resource_type in _STORAGE_RESOURCE_TYPES:
        try:
            configs = _get_storage_configs(connect_client, instance_id, resource_type)
        except Exception:
            logger.exception(
                "Failed to list storage configs for %s on instance %s",
                resource_type,
                instance_id,
            )
            continue

        logger.info(
            "Found %d storage configs for %s on instance %s",
            len(configs),
            resource_type,
            instance_id,
        )

        for config in configs:
            # Check for Kinesis Data Stream
            kinesis_arn = _extract_kinesis_stream_arn(config)
            if kinesis_arn and kinesis_arn not in seen_arns:
                seen_arns.add(kinesis_arn)
                description = _describe_kinesis_stream(kinesis_client, kinesis_arn)
                if description is not None:
                    resources.append(
                        _build_kinesis_stream_resource(kinesis_arn, description)
                    )

            # Check for Kinesis Firehose
            firehose_arn = _extract_firehose_arn(config)
            if firehose_arn and firehose_arn not in seen_arns:
                seen_arns.add(firehose_arn)
                description = _describe_firehose_stream(firehose_client, firehose_arn)
                if description is not None:
                    resources.append(
                        _build_firehose_resource(firehose_arn, description)
                    )

    logger.info(
        "Streaming discovery complete: %d resources for instance %s",
        len(resources),
        instance_id,
    )

    return resources
