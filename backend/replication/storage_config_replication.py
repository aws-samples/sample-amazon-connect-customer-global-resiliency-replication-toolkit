"""Connect instance storage config replication for ACGR.

After S3 buckets and streaming resources are replicated, this module
updates the replica Connect instance's storage configurations to point
to the replicated resources in the DR region.
"""

from __future__ import annotations

import logging
from typing import Any

from aws.client_factory import create_target_client
from models.enums import ResourceType

logger = logging.getLogger(__name__)

# Storage resource types that use S3
_S3_STORAGE_TYPES = [
    "CALL_RECORDINGS",
    "CHAT_TRANSCRIPTS",
    "SCHEDULED_REPORTS",
    "ATTACHMENTS",
    "SCREEN_RECORDINGS",
]


def update_replica_storage_configs(
    replica_instance_id: str,
    target_region: str,
    source_instance_id: str,
    source_region: str,
    s3_bucket_mapping: dict[str, str],
    kinesis_arn_mapping: dict[str, str] | None = None,
    firehose_arn_mapping: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Update the replica instance's storage configs to use replicated resources.

    For each storage config on the source instance that uses S3, Kinesis, or
    Firehose, creates or updates the corresponding config on the replica
    instance to point to the replicated resource.

    Args:
        replica_instance_id: The replica Connect instance ID.
        target_region: The DR region where the replica lives.
        source_instance_id: The source Connect instance ID.
        source_region: The source region.
        s3_bucket_mapping: Mapping of source bucket name → replicated bucket name.
        kinesis_arn_mapping: Mapping of source Kinesis ARN → replicated ARN.
        firehose_arn_mapping: Mapping of source Firehose ARN → replicated ARN.

    Returns:
        A list of dicts describing what was updated.
    """
    from aws.client_factory import create_source_client

    source_connect = create_source_client("connect", source_region)
    target_connect = create_target_client("connect", target_region)

    results: list[dict[str, Any]] = []
    kinesis_arn_mapping = kinesis_arn_mapping or {}
    firehose_arn_mapping = firehose_arn_mapping or {}

    # Process S3-based storage configs
    for storage_type in _S3_STORAGE_TYPES:
        try:
            source_configs = _list_storage_configs(source_connect, source_instance_id, storage_type)
        except Exception:
            logger.debug("No %s configs on source instance", storage_type)
            continue

        for config in source_configs:
            if config.get("StorageType") != "S3":
                continue

            s3_config = config.get("S3Config", {})
            source_bucket = s3_config.get("BucketName", "")
            source_prefix = s3_config.get("BucketPrefix", "")

            if not source_bucket:
                continue

            # Find the replicated bucket name
            target_bucket = s3_bucket_mapping.get(source_bucket)
            if not target_bucket:
                logger.warning(
                    "No replicated bucket found for '%s' (storage type: %s)",
                    source_bucket, storage_type,
                )
                continue

            # Build the new storage config for the replica
            new_s3_config: dict[str, Any] = {
                "BucketName": target_bucket,
                "BucketPrefix": source_prefix,
            }

            # Resolve encryption config for the target region
            enc_config = s3_config.get("EncryptionConfig")
            if enc_config:
                from aws.kms_utils import ensure_kms_key_exists
                source_key_id = enc_config.get("KeyId", "")
                target_key_id, _kms_info = ensure_kms_key_exists(source_key_id, target_region) if source_key_id else ("", None)
                if target_key_id:
                    new_s3_config["EncryptionConfig"] = {
                        "EncryptionType": enc_config.get("EncryptionType", "KMS"),
                        "KeyId": target_key_id,
                    }
                else:
                    logger.warning(
                        "S3 encryption config skipped for %s — KMS key '%s' "
                        "cannot be resolved in %s. The bucket will use default "
                        "S3 encryption. If using a customer-managed key (CMK), "
                        "create a matching key in %s and update the storage "
                        "config manually.",
                        storage_type, source_key_id, target_region, target_region,
                    )

            try:
                target_connect.associate_instance_storage_config(
                    InstanceId=replica_instance_id,
                    ResourceType=storage_type,
                    StorageConfig={
                        "StorageType": "S3",
                        "S3Config": new_s3_config,
                    },
                )
                results.append({
                    "storage_type": storage_type,
                    "action": "associated",
                    "target_bucket": target_bucket,
                    "status": "success",
                })
                logger.info(
                    "Associated %s storage config on replica: bucket=%s",
                    storage_type, target_bucket,
                )
            except Exception as exc:
                error_msg = str(exc)
                # Handle "already exists" gracefully
                if "already" in error_msg.lower() or "conflict" in error_msg.lower():
                    results.append({
                        "storage_type": storage_type,
                        "action": "already_configured",
                        "target_bucket": target_bucket,
                        "status": "success",
                    })
                    logger.info(
                        "%s storage config already exists on replica for bucket %s",
                        storage_type, target_bucket,
                    )
                else:
                    results.append({
                        "storage_type": storage_type,
                        "action": "failed",
                        "target_bucket": target_bucket,
                        "status": "error",
                        "error": error_msg,
                    })
                    logger.error(
                        "Failed to associate %s storage config on replica: %s",
                        storage_type, error_msg,
                    )

    # Process Kinesis-based storage configs (CTR, Agent Events)
    for storage_type in ["CONTACT_TRACE_RECORDS", "AGENT_EVENTS"]:
        try:
            source_configs = _list_storage_configs(source_connect, source_instance_id, storage_type)
        except Exception:
            continue

        for config in source_configs:
            config_storage_type = config.get("StorageType", "")

            if config_storage_type == "KINESIS_STREAM":
                kinesis_config = config.get("KinesisStreamConfig", {})
                source_arn = kinesis_config.get("StreamArn", "")
                target_arn = kinesis_arn_mapping.get(source_arn)
                if target_arn:
                    try:
                        target_connect.associate_instance_storage_config(
                            InstanceId=replica_instance_id,
                            ResourceType=storage_type,
                            StorageConfig={
                                "StorageType": "KINESIS_STREAM",
                                "KinesisStreamConfig": {"StreamArn": target_arn},
                            },
                        )
                        results.append({
                            "storage_type": storage_type,
                            "action": "associated_kinesis",
                            "target_arn": target_arn,
                            "status": "success",
                        })
                    except Exception as exc:
                        error_msg = str(exc)
                        if "already" in error_msg.lower():
                            results.append({
                                "storage_type": storage_type,
                                "action": "already_configured",
                                "status": "success",
                            })
                        else:
                            results.append({
                                "storage_type": storage_type,
                                "action": "failed",
                                "status": "error",
                                "error": error_msg,
                            })

            elif config_storage_type == "KINESIS_FIREHOSE":
                firehose_config = config.get("KinesisFirehoseConfig", {})
                source_arn = firehose_config.get("FirehoseArn", "")
                target_arn = firehose_arn_mapping.get(source_arn)
                if target_arn:
                    try:
                        target_connect.associate_instance_storage_config(
                            InstanceId=replica_instance_id,
                            ResourceType=storage_type,
                            StorageConfig={
                                "StorageType": "KINESIS_FIREHOSE",
                                "KinesisFirehoseConfig": {"FirehoseArn": target_arn},
                            },
                        )
                        results.append({
                            "storage_type": storage_type,
                            "action": "associated_firehose",
                            "target_arn": target_arn,
                            "status": "success",
                        })
                    except Exception as exc:
                        error_msg = str(exc)
                        if "already" in error_msg.lower():
                            results.append({
                                "storage_type": storage_type,
                                "action": "already_configured",
                                "status": "success",
                            })
                        else:
                            results.append({
                                "storage_type": storage_type,
                                "action": "failed",
                                "status": "error",
                                "error": error_msg,
                            })

    # Process MEDIA_STREAMS (KVS) storage configs
    # KVS streams are created on-the-fly by Connect during calls using a prefix.
    # We replicate the storage config (prefix, retention, encryption) so the
    # DR instance creates KVS streams in the target region when calls happen.
    try:
        source_media_configs = _list_storage_configs(
            source_connect, source_instance_id, "MEDIA_STREAMS"
        )
    except Exception:
        logger.debug("No MEDIA_STREAMS configs on source instance")
        source_media_configs = []

    for config in source_media_configs:
        if config.get("StorageType") != "KINESIS_VIDEO_STREAM":
            continue

        kvs_config = config.get("KinesisVideoStreamConfig", {})
        source_prefix = kvs_config.get("Prefix", "")
        retention_hours = kvs_config.get("RetentionPeriodHours", 0)
        enc_config = kvs_config.get("EncryptionConfig")

        if not source_prefix:
            continue

        # Adapt the prefix for the target region by replacing the source
        # region identifier (e.g. "iad" → "pdx", "us-east-1" → "us-west-2").
        # Only replace when the region name appears as a delimited segment
        # (surrounded by hyphens, underscores, dots, or at string boundaries)
        # to avoid mangling names where the short name is part of a word.
        import re

        target_prefix = source_prefix
        _REGION_SHORT_NAMES = {
            "us-east-1": "iad",
            "us-west-2": "pdx",
            "eu-west-2": "lhr",
            "ap-southeast-2": "syd",
            "ap-northeast-1": "nrt",
            "eu-central-1": "fra",
            "ap-southeast-1": "sin",
            "ca-central-1": "yul",
            "af-south-1": "cpt",
            "ap-northeast-2": "icn",
        }
        source_short = _REGION_SHORT_NAMES.get(source_region, "")
        target_short = _REGION_SHORT_NAMES.get(target_region, "")

        # Try full region name replacement first (e.g. "us-east-1" → "us-west-2")
        if source_region in target_prefix:
            target_prefix = target_prefix.replace(source_region, target_region, 1)
        elif source_short and target_short:
            # Try short name replacement with word-boundary matching
            pattern = rf'(?<![a-zA-Z0-9]){re.escape(source_short)}(?![a-zA-Z0-9])'
            if re.search(pattern, target_prefix):
                target_prefix = re.sub(pattern, target_short, target_prefix, count=1)

        logger.info(
            "KVS prefix adaptation: '%s' → '%s' (source: %s, target: %s)",
            source_prefix, target_prefix, source_region, target_region,
        )

        # Build the KVS storage config for the replica
        new_kvs_config: dict[str, Any] = {
            "Prefix": target_prefix,
            "RetentionPeriodHours": retention_hours,
        }
        if enc_config:
            # Resolve the KMS key for the target region. The source key
            # (often alias/aws/kinesisvideo) may not exist in the target
            # region yet. ensure_kms_key_exists will bootstrap it if needed.
            from aws.kms_utils import ensure_kms_key_exists
            source_key_id = enc_config.get("KeyId", "alias/aws/kinesisvideo")
            target_key_id, _kms_info = ensure_kms_key_exists(source_key_id, target_region)
            if target_key_id:
                new_kvs_config["EncryptionConfig"] = {
                    "EncryptionType": enc_config.get("EncryptionType", "KMS"),
                    "KeyId": target_key_id,
                }
            else:
                logger.warning(
                    "KVS encryption config skipped — KMS key '%s' "
                    "cannot be resolved in %s. Media streams will use "
                    "default encryption. To enable KMS encryption, create "
                    "a KVS stream manually in %s to bootstrap the key, "
                    "then re-run the storage config replication.",
                    source_key_id, target_region, target_region,
                )

        try:
            target_connect.associate_instance_storage_config(
                InstanceId=replica_instance_id,
                ResourceType="MEDIA_STREAMS",
                StorageConfig={
                    "StorageType": "KINESIS_VIDEO_STREAM",
                    "KinesisVideoStreamConfig": new_kvs_config,
                },
            )
            results.append({
                "storage_type": "MEDIA_STREAMS",
                "action": "associated_kvs",
                "source_prefix": source_prefix,
                "target_prefix": target_prefix,
                "status": "success",
            })
            logger.info(
                "Associated MEDIA_STREAMS storage config on replica: prefix=%s",
                target_prefix,
            )
        except Exception as exc:
            error_msg = str(exc)
            if "already" in error_msg.lower() or "conflict" in error_msg.lower():
                results.append({
                    "storage_type": "MEDIA_STREAMS",
                    "action": "already_configured",
                    "target_prefix": target_prefix,
                    "status": "success",
                })
                logger.info(
                    "MEDIA_STREAMS storage config already exists on replica: prefix=%s",
                    target_prefix,
                )
            else:
                results.append({
                    "storage_type": "MEDIA_STREAMS",
                    "action": "failed",
                    "target_prefix": target_prefix,
                    "status": "error",
                    "error": error_msg,
                })
                logger.error(
                    "Failed to associate MEDIA_STREAMS storage config on replica: %s",
                    error_msg,
                )

    return results


def _list_storage_configs(
    connect_client: Any, instance_id: str, resource_type: str
) -> list[dict[str, Any]]:
    """List all storage configs for a given resource type with pagination."""
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
