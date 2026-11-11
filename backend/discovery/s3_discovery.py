"""S3 bucket discovery for Connect ACGR Resource Replicator.

Discovers S3 buckets used by Connect instance storage configurations
(call recordings, chat transcripts, scheduled reports, etc.).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aws.client_factory import create_source_client
from models.resources import S3BucketResource

logger = logging.getLogger(__name__)

# Connect storage resource types that use S3
_S3_STORAGE_RESOURCE_TYPES = [
    "CALL_RECORDINGS",
    "CHAT_TRANSCRIPTS",
    "SCHEDULED_REPORTS",
    "MEDIA_STREAMS",
    "REAL_TIME_CONTACT_ANALYSIS_SEGMENTS",
    "ATTACHMENTS",
    "SCREEN_RECORDINGS",
]


def _generate_resource_id(arn: str) -> str:
    """Generate a deterministic resource ID from an ARN."""
    return hashlib.sha256(arn.encode()).hexdigest()[:12]


def _extract_s3_bucket_name(config: dict[str, Any]) -> str | None:
    """Extract S3 bucket name from a storage config.

    S3 configs live under S3Config.BucketName.
    """
    s3_config = config.get("S3Config")
    if not s3_config:
        return None
    return s3_config.get("BucketName") or None


def discover_s3_buckets(
    instance_id: str, source_region: str
) -> list[S3BucketResource]:
    """Discover S3 buckets used by Connect instance storage configs.

    Iterates over all S3-based storage resource types, extracts bucket names,
    deduplicates, and returns S3BucketResource objects.

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        A list of S3BucketResource objects.
    """
    connect_client = create_source_client("connect", source_region)
    s3_client = create_source_client("s3", source_region)

    # bucket_name -> list of storage types using it
    bucket_storage_types: dict[str, list[str]] = {}

    for resource_type in _S3_STORAGE_RESOURCE_TYPES:
        try:
            params: dict[str, Any] = {
                "InstanceId": instance_id,
                "ResourceType": resource_type,
            }
            while True:
                response = connect_client.list_instance_storage_configs(**params)
                for config in response.get("StorageConfigs", []):
                    storage_type = config.get("StorageType", "")
                    if storage_type != "S3":
                        continue
                    bucket_name = _extract_s3_bucket_name(config)
                    if bucket_name:
                        if bucket_name not in bucket_storage_types:
                            bucket_storage_types[bucket_name] = []
                        if resource_type not in bucket_storage_types[bucket_name]:
                            bucket_storage_types[bucket_name].append(resource_type)

                next_token = response.get("NextToken")
                if not next_token:
                    break
                params["NextToken"] = next_token
        except Exception:
            logger.debug(
                "No storage configs for %s on instance %s (may not be configured)",
                resource_type,
                instance_id,
            )

    resources: list[S3BucketResource] = []

    for bucket_name, storage_types in bucket_storage_types.items():
        # Build a pseudo-ARN for the bucket
        bucket_arn = f"arn:aws:s3:::{bucket_name}"
        resource_id = _generate_resource_id(bucket_arn)

        # Try to get bucket details
        encryption_type = None
        versioning_enabled = False
        try:
            enc_resp = s3_client.get_bucket_encryption(Bucket=bucket_name)
            rules = enc_resp.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if rules:
                encryption_type = rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
        except Exception:
            logger.debug("Could not get encryption for bucket %s", bucket_name)

        try:
            ver_resp = s3_client.get_bucket_versioning(Bucket=bucket_name)
            versioning_enabled = ver_resp.get("Status") == "Enabled"
        except Exception:
            logger.debug("Could not get versioning for bucket %s", bucket_name)

        config_summary = {
            "storage_types": ", ".join(storage_types),
        }
        if encryption_type:
            config_summary["encryption"] = encryption_type

        resources.append(
            S3BucketResource(
                id=resource_id,
                name=bucket_name,
                arn=bucket_arn,
                bucket_region=source_region,
                storage_types=storage_types,
                encryption_type=encryption_type,
                versioning_enabled=versioning_enabled,
                config_summary=config_summary,
            )
        )

    logger.info(
        "S3 discovery complete: %d buckets for instance %s",
        len(resources),
        instance_id,
    )

    return resources
