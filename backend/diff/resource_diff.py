"""Resource diff — compare source configs with target region resources.

Fetches target resource configurations and compares them with source
configs to highlight differences.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from audit.target_region_audit import compute_expected_target_name
from aws.client_factory import create_client, create_target_client
from models.enums import ResourceType
from models.resources import ResourceBase

logger = logging.getLogger(__name__)


class DiffEntry(BaseModel):
    """Config comparison for a single resource."""

    resourceId: str
    resourceName: str
    resourceType: str
    sourceConfig: dict[str, Any] = {}
    targetConfig: dict[str, Any] = {}
    existsInTarget: bool = False
    differences: list[str] = []


def _get_lambda_config(region: str, name: str) -> dict[str, Any] | None:
    try:
        client = create_target_client("lambda", region)
        resp = client.get_function(FunctionName=name)
        cfg = resp.get("Configuration", {})
        return {
            "Runtime": cfg.get("Runtime", ""),
            "Handler": cfg.get("Handler", ""),
            "MemorySize": cfg.get("MemorySize", 0),
            "Timeout": cfg.get("Timeout", 0),
            "CodeSize": cfg.get("CodeSize", 0),
            "Layers": [l.get("Arn", "") for l in cfg.get("Layers", [])],
        }
    except Exception:
        return None


def _get_kinesis_config(region: str, name: str) -> dict[str, Any] | None:
    try:
        client = create_target_client("kinesis", region)
        resp = client.describe_stream_summary(StreamName=name)
        summary = resp.get("StreamDescriptionSummary", {})
        return {
            "ShardCount": summary.get("OpenShardCount", 0),
            "RetentionPeriod": summary.get("RetentionPeriodHours", 0),
            "StreamMode": summary.get("StreamModeDetails", {}).get(
                "StreamMode", ""
            ),
            "EncryptionType": summary.get("EncryptionType", "NONE"),
        }
    except Exception:
        return None


def _get_lex_config(region: str, name: str) -> dict[str, Any] | None:
    try:
        client = create_target_client("lexv2-models", region)
        resp = client.list_bots(
            filters=[{"name": "BotName", "values": [name], "operator": "EQ"}],
            maxResults=10,
        )
        for bot in resp.get("botSummaries", []):
            if bot.get("botName") == name:
                return {
                    "BotId": bot.get("botId", ""),
                    "BotStatus": bot.get("botStatus", ""),
                    "BotType": bot.get("botType", ""),
                }
        return None
    except Exception:
        return None

def _get_firehose_config(region: str, name: str) -> dict[str, Any] | None:
    """Fetch Firehose delivery stream config from the target region."""
    try:
        client = create_target_client("firehose", region)
        resp = client.describe_delivery_stream(DeliveryStreamName=name)
        desc = resp.get("DeliveryStreamDescription", {})
        destinations = desc.get("Destinations", [])
        dest_type = "Unknown"
        s3_bucket = ""
        if destinations:
            dest = destinations[0]
            if dest.get("ExtendedS3DestinationDescription"):
                dest_type = "ExtendedS3"
                s3_bucket = dest["ExtendedS3DestinationDescription"].get("BucketARN", "")
            elif dest.get("S3DestinationDescription"):
                dest_type = "S3"
                s3_bucket = dest["S3DestinationDescription"].get("BucketARN", "")
            elif dest.get("RedshiftDestinationDescription"):
                dest_type = "Redshift"
            elif dest.get("ElasticsearchDestinationDescription"):
                dest_type = "Elasticsearch"
            elif dest.get("SplunkDestinationDescription"):
                dest_type = "Splunk"
            elif dest.get("HttpEndpointDestinationDescription"):
                dest_type = "HttpEndpoint"
        return {
            "DestinationType": dest_type,
            "S3Bucket": s3_bucket,
        }
    except Exception:
        return None


def _get_kvs_config(region: str, name: str) -> dict[str, Any] | None:
    """Fetch Kinesis Video Stream config from the target region."""
    try:
        client = create_target_client("kinesisvideo", region)
        resp = client.describe_stream(StreamName=name)
        info = resp.get("StreamInfo", {})
        return {
            "DataRetentionInHours": info.get("DataRetentionInHours", 0),
        }
    except Exception:
        return None


def _get_iam_config(_region: str, name: str) -> dict[str, Any] | None:
    """IAM is global — region param ignored."""
    try:
        client = create_client("iam", "us-east-1")
        resp = client.get_role(RoleName=name)
        role = resp.get("Role", {})
        policies = client.list_attached_role_policies(RoleName=name)
        inline = client.list_role_policies(RoleName=name)
        return {
            "Arn": role.get("Arn", ""),
            "AttachedPolicies": len(policies.get("AttachedPolicies", [])),
            "InlinePolicies": len(inline.get("PolicyNames", [])),
        }
    except Exception:
        return None


def _get_s3_config(_region: str, name: str) -> dict[str, Any] | None:
    """S3 buckets are global — but we verify the bucket is in the target region."""
    try:
        client = create_client("s3", "us-east-1")
        client.head_bucket(Bucket=name)
        loc = client.get_bucket_location(Bucket=name)
        bucket_region = loc.get("LocationConstraint") or "us-east-1"
        # Only report as existing if the bucket is in the target region
        if bucket_region != _region:
            return None
        return {
            "BucketRegion": bucket_region,
        }
    except Exception:
        return None



def _extract_source_config(resource: ResourceBase) -> dict[str, Any]:
    """Extract key config fields from a source resource."""
    rtype = resource.resource_type
    if isinstance(rtype, str):
        rtype = ResourceType(rtype)

    config: dict[str, Any] = {}

    if rtype == ResourceType.LAMBDA:
        config = {
            "Runtime": getattr(resource, "runtime", resource.config_summary.get("runtime", "")),
            "Handler": getattr(resource, "handler", resource.config_summary.get("handler", "")),
            "MemorySize": getattr(resource, "memory_size", resource.config_summary.get("memory_size", "")),
            "Timeout": getattr(resource, "timeout", resource.config_summary.get("timeout", "")),
            "Layers": getattr(resource, "layers", []),
        }
    elif rtype == ResourceType.KINESIS_STREAM:
        config = {
            "ShardCount": getattr(resource, "shard_count", resource.config_summary.get("shard_count", "")),
            "RetentionPeriod": getattr(resource, "retention_period", resource.config_summary.get("retention_period", "")),
            "StreamMode": getattr(resource, "stream_mode", resource.config_summary.get("stream_mode", "")),
        }
    elif rtype == ResourceType.LEX_BOT:
        config = {
            "BotId": getattr(resource, "bot_id", resource.config_summary.get("bot_id", "")),
            "Locales": getattr(resource, "locales", []),
        }
    elif rtype == ResourceType.IAM_ROLE:
        config = {
            "AttachedPolicies": len(getattr(resource, "attached_policies", [])),
            "InlinePolicies": len(getattr(resource, "inline_policies", [])),
        }
    elif rtype == ResourceType.S3_BUCKET:
        config = {
            "BucketRegion": getattr(resource, "bucket_region", ""),
            "StorageTypes": getattr(resource, "storage_types", []),
        }
    elif rtype == ResourceType.KINESIS_FIREHOSE:
        config = {
            "DestinationType": resource.config_summary.get("destination_type", ""),
            "S3Bucket": resource.config_summary.get("s3_bucket", ""),
        }
    elif rtype == ResourceType.KINESIS_VIDEO_STREAM:
        config = {
            "DataRetentionInHours": getattr(resource, "data_retention_in_hours", 0),
        }

    return config


def _compute_differences(source: dict[str, Any], target: dict[str, Any]) -> list[str]:
    """Compare two config dicts and return a list of difference descriptions."""
    diffs: list[str] = []
    all_keys = set(source.keys()) | set(target.keys())
    for key in sorted(all_keys):
        src_val = source.get(key)
        tgt_val = target.get(key)
        if src_val != tgt_val:
            diffs.append(f"{key}: source={src_val} → target={tgt_val}")
    return diffs


def compute_resource_diffs(
    inventory: dict[str, ResourceBase],
    target_region: str,
    resource_tags: dict[str, str] | None = None,
) -> list[DiffEntry]:
    """Compute config diffs for all resources in the inventory.

    For each resource, fetches the target region config (if it exists)
    and compares it with the source config.
    """
    entries: list[DiffEntry] = []

    _TARGET_FETCHERS = {
        ResourceType.LAMBDA: _get_lambda_config,
        ResourceType.KINESIS_STREAM: _get_kinesis_config,
        ResourceType.LEX_BOT: _get_lex_config,
        ResourceType.IAM_ROLE: _get_iam_config,
        ResourceType.S3_BUCKET: _get_s3_config,
        ResourceType.KINESIS_FIREHOSE: _get_firehose_config,
        ResourceType.KINESIS_VIDEO_STREAM: _get_kvs_config,
    }

    for resource_id, resource in inventory.items():
        rtype = resource.resource_type
        if isinstance(rtype, str):
            rtype = ResourceType(rtype)

        source_config = _extract_source_config(resource)
        expected_name = compute_expected_target_name(
            resource.name, "", resource_type=rtype,
            is_lex_codehook=getattr(resource, "is_lex_codehook", False),
        )

        fetcher = _TARGET_FETCHERS.get(rtype)
        target_config: dict[str, Any] = {}
        exists = False

        if rtype == ResourceType.IAM_ROLE:
            # IAM is global — source role IS the target role, always exists
            target_config = source_config.copy()
            exists = True
        elif fetcher:
            result = fetcher(target_region, expected_name)
            if result is not None:
                target_config = result
                exists = True

        differences = _compute_differences(source_config, target_config) if exists else []

        entries.append(DiffEntry(
            resourceId=resource_id,
            resourceName=resource.name,
            resourceType=str(rtype.value),
            sourceConfig=source_config,
            targetConfig=target_config,
            existsInTarget=exists,
            differences=differences,
        ))

    return entries
