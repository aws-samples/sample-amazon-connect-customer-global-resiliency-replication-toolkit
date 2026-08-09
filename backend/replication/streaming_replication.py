"""Streaming resource replication to the ACGR target region.

Replicates three types of streaming resources:
- Amazon Kinesis Data Streams: CreateStream with same shard count, retention, encryption
- Amazon Data Firehose delivery streams: CreateDeliveryStream with same destination/buffering
- Amazon Kinesis Video Streams: CreateStream with same retention and encryption

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from aws.client_factory import create_target_client
from aws.naming import target_replica_name
from models.resources import (
    KinesisFirehoseResource,
    KinesisStreamResource,
    KinesisVideoResource,
)

logger = logging.getLogger(__name__)


def replicate_kinesis_stream(stream: KinesisStreamResource, target_region: str, resource_tags: dict[str, str] | None = None) -> str:
    """Replicate a Kinesis Data Stream to the ACGR target region.

    Creates an equivalent stream with the same:
    - Stream name and shard count
    - Retention period
    - Encryption configuration
    - Stream mode (PROVISIONED or ON_DEMAND)

    Args:
        stream: The discovered Kinesis Data Stream resource.
        target_region: The ACGR target region.

    Returns:
        The ARN of the newly created stream in the target region.

    Raises:
        ValueError: If the stream already exists in the target region.
        PermissionError: If the caller lacks Kinesis permissions.
        RuntimeError: For any other AWS API error during replication.
    """
    kinesis_client = create_target_client("kinesis", target_region)

    logger.info(
        "Creating Kinesis Data Stream '%s' in %s", stream.name, target_region
    )

    target_stream_name = target_replica_name(stream.name)
    create_params: dict = {
        "StreamName": target_stream_name,
        "StreamModeDetails": {"StreamMode": stream.stream_mode},
    }

    # Shard count is required for PROVISIONED mode
    if stream.stream_mode == "PROVISIONED":
        create_params["ShardCount"] = stream.shard_count

    already_exists = False

    try:
        kinesis_client.create_stream(**create_params)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ResourceInUseException":
            # Stream already exists — reuse it
            logger.info(
                "Kinesis Data Stream '%s' already exists in %s, reusing",
                target_stream_name,
                target_region,
            )
            already_exists = True
        else:
            _handle_kinesis_error(exc, stream.name)

    # Only set retention/encryption for newly created streams
    if not already_exists:
        # Set retention period if non-default (default is 24 hours)
        if stream.retention_period and stream.retention_period != 24:
            try:
                kinesis_client.increase_stream_retention_period(
                    StreamName=target_stream_name,
                    RetentionPeriodHours=stream.retention_period,
                )
            except ClientError as exc:
                logger.warning(
                    "Failed to set retention period for stream '%s': %s",
                    stream.name,
                    exc,
                )

        # Enable encryption if configured
        if stream.encryption_type and stream.encryption_type != "NONE":
            try:
                kinesis_client.start_stream_encryption(
                    StreamName=target_stream_name,
                    EncryptionType=stream.encryption_type,
                    KeyId="alias/aws/kinesis",
                )
            except ClientError as exc:
                logger.warning(
                    "Failed to enable encryption for stream '%s': %s",
                    stream.name,
                    exc,
                )

    # Describe the stream to get the ARN
    try:
        describe_resp = kinesis_client.describe_stream(StreamName=target_stream_name)
        stream_arn = describe_resp["StreamDescription"]["StreamARN"]

        # Apply tags
        if resource_tags:
            try:
                kinesis_client.add_tags_to_stream(
                    StreamARN=stream_arn,
                    Tags=resource_tags,
                )
                logger.info("Applied %d tag(s) to Kinesis stream '%s'", len(resource_tags), target_stream_name)
            except Exception:
                logger.warning("Failed to apply tags to Kinesis stream '%s'", target_stream_name, exc_info=True)

        logger.info(
            "Successfully %s Kinesis Data Stream '%s' → %s",
            "reused" if already_exists else "replicated",
            stream.name,
            stream_arn,
        )
        return stream_arn
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        raise RuntimeError(
            f"Stream '{stream.name}' was created but failed to describe: "
            f"[{error_code}] {error_msg}"
        ) from exc


def replicate_firehose_stream(
    stream: KinesisFirehoseResource,
    target_region: str,
    s3_bucket_name: str | None = None,
    resource_tags: dict[str, str] | None = None,
    role_arn_mapping: dict[str, str] | None = None,
) -> str:
    """Replicate a Amazon Data Firehose delivery stream to the ACGR target region.

    Creates an equivalent delivery stream with the same:
    - Delivery stream name and destination type
    - S3 destination configuration (with user-provided bucket name)
    - Buffering hints (interval and size)

    When the source stream has an S3 destination, the caller should provide
    ``s3_bucket_name`` for the target region bucket.  If not provided, the
    original bucket name from the source configuration is used as-is.

    Args:
        stream: The discovered Amazon Data Firehose resource.
        target_region: The ACGR target region.
        s3_bucket_name: Optional S3 bucket name in the target region for the
            delivery destination.  If ``None``, the original bucket name from
            the source configuration is reused.

    Returns:
        The ARN of the newly created delivery stream in the target region.

    Raises:
        ValueError: If the delivery stream already exists in the target region.
        PermissionError: If the caller lacks Firehose permissions.
        RuntimeError: For any other AWS API error during replication.
    """
    firehose_client = create_target_client("firehose", target_region)

    logger.info(
        "Creating Kinesis Firehose delivery stream '%s' in %s",
        stream.name,
        target_region,
    )

    target_name = target_replica_name(stream.name)
    create_params: dict = {
        "DeliveryStreamName": target_name,
        "DeliveryStreamType": "DirectPut",
    }

    # Build S3 destination configuration
    if stream.destination_type == "ExtendedS3" or stream.s3_destination:
        s3_config = _build_s3_destination_config(stream, s3_bucket_name, role_arn_mapping=role_arn_mapping)
        create_params["ExtendedS3DestinationConfiguration"] = s3_config

    # Add tags
    if resource_tags:
        create_params["Tags"] = [{"Key": k, "Value": v} for k, v in resource_tags.items()]

    try:
        response = firehose_client.create_delivery_stream(**create_params)
        stream_arn = response["DeliveryStreamARN"]
        logger.info(
            "Successfully replicated Firehose delivery stream '%s' → %s",
            stream.name,
            stream_arn,
        )
        return stream_arn
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]

        if error_code == "ResourceInUseException":
            # Delivery stream already exists — look up its ARN and return it
            logger.info(
                "Firehose delivery stream '%s' already exists in %s, reusing",
                target_name,
                target_region,
            )
            try:
                desc_resp = firehose_client.describe_delivery_stream(
                    DeliveryStreamName=target_name
                )
                existing_arn = desc_resp["DeliveryStreamDescription"]["DeliveryStreamARN"]
                logger.info(
                    "Reusing existing Firehose delivery stream '%s' → %s",
                    target_name,
                    existing_arn,
                )
                return existing_arn
            except ClientError as desc_exc:
                raise RuntimeError(
                    f"Firehose delivery stream '{target_name}' already exists but "
                    f"failed to describe: {desc_exc}"
                ) from desc_exc
        if error_code in ("AccessDeniedException", "AccessDenied"):
            raise PermissionError(
                f"Permission denied creating Firehose delivery stream "
                f"'{stream.name}': {error_msg}"
            ) from exc
        raise RuntimeError(
            f"Failed to create Firehose delivery stream '{stream.name}': "
            f"[{error_code}] {error_msg}"
        ) from exc


def _build_s3_destination_config(
    stream: KinesisFirehoseResource,
    s3_bucket_name: str | None,
    role_arn_mapping: dict[str, str] | None = None,
) -> dict:
    """Build the ExtendedS3DestinationConfiguration for CreateDeliveryStream.

    Args:
        stream: The source Firehose resource.
        s3_bucket_name: Optional override bucket name for the target region.
        role_arn_mapping: Optional mapping of source IAM role ARN → replicated role ARN.

    Returns:
        A dict suitable for the ExtendedS3DestinationConfiguration parameter.
    """
    s3_dest = stream.s3_destination or {}
    role_arn_mapping = role_arn_mapping or {}

    # Determine the bucket ARN
    if s3_bucket_name:
        bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
    else:
        bucket_arn = s3_dest.get("BucketARN", "")

    # Map the RoleARN to the replicated role if available
    source_role_arn = s3_dest.get("RoleARN", "")
    target_role_arn = role_arn_mapping.get(source_role_arn, source_role_arn)

    config: dict = {
        "RoleARN": target_role_arn,
        "BucketARN": bucket_arn,
    }

    # Prefix
    if s3_dest.get("Prefix"):
        config["Prefix"] = s3_dest["Prefix"]

    # Buffering hints
    if stream.buffering_hints:
        config["BufferingHints"] = {
            "SizeInMBs": stream.buffering_hints.get("SizeInMBs", 5),
            "IntervalInSeconds": stream.buffering_hints.get("IntervalInSeconds", 300),
        }
    elif s3_dest.get("BufferingHints"):
        config["BufferingHints"] = {
            "SizeInMBs": s3_dest["BufferingHints"].get("SizeInMBs", 5),
            "IntervalInSeconds": s3_dest["BufferingHints"].get(
                "IntervalInSeconds", 300
            ),
        }

    # Compression
    if s3_dest.get("CompressionFormat"):
        config["CompressionFormat"] = s3_dest["CompressionFormat"]

    return config



def replicate_kvs_stream(stream: KinesisVideoResource, target_region: str, resource_tags: dict[str, str] | None = None) -> str:
    """Handle KVS replication for the ACGR target region.

    Amazon Connect does NOT create physical Amazon Kinesis Video Streams during
    ACGR replication. Instead, Connect creates KVS streams on-the-fly
    during calls using the MEDIA_STREAMS storage config prefix. Therefore,
    we never create physical KVS streams — we only need to enable the
    MEDIA_STREAMS storage config on the replica instance, which happens
    during the Associate step.

    For ALL KVS resources (both synthetic config-based and physical streams
    discovered via prefix listing), we return a synthetic target ARN and
    defer the actual work to the association step where the storage config
    (prefix, retention, encryption) is applied to the DR Connect instance.

    Args:
        stream: The discovered Kinesis Video Stream resource.
        target_region: The ACGR target region.

    Returns:
        A synthetic ARN for the target region. The real enablement happens
        during the Associate step via _associate_kvs_stream.
    """
    # For ALL KVS resources — whether synthetic config-based or physical
    # streams — we do NOT create physical streams. Connect creates them
    # on-the-fly during calls. We just need the MEDIA_STREAMS storage
    # config enabled on the replica, which happens at association time.
    if "/config/media-streams/" in stream.arn:
        # Synthetic config-based ARN — replace ONLY the region field (index 3)
        # using positional replacement to avoid the .replace() bug where
        # the region string appears in other parts of the ARN.
        parts = stream.arn.split(":")
        if len(parts) >= 5:
            parts[3] = target_region
            synthetic_target_arn = ":".join(parts)
        else:
            synthetic_target_arn = stream.arn
    else:
        # Physical KVS stream discovered via prefix listing — convert to
        # a config-based synthetic ARN since we don't need to create it.
        # Extract account from the real ARN for the synthetic one.
        parts = stream.arn.split(":")
        account = parts[4] if len(parts) > 4 else ""
        synthetic_target_arn = (
            f"arn:aws:kinesisvideo:{target_region}:{account}"
            f":config/media-streams/{stream.name}"
        )

    logger.info(
        "KVS resource '%s' — no physical stream creation needed. "
        "Connect creates KVS streams on-the-fly during calls. "
        "MEDIA_STREAMS storage config will be applied during association. "
        "Synthetic ARN: %s",
        stream.name, synthetic_target_arn,
    )
    return synthetic_target_arn



def _handle_kinesis_error(exc: ClientError, stream_name: str) -> None:
    """Handle ClientError from Kinesis CreateStream.

    Raises:
        ValueError: If the stream already exists.
        PermissionError: If access is denied.
        RuntimeError: For other AWS errors.
    """
    error_code = exc.response["Error"]["Code"]
    error_msg = exc.response["Error"]["Message"]

    if error_code == "ResourceInUseException":
        raise ValueError(
            f"Kinesis Data Stream '{stream_name}' already exists "
            f"in the target region"
        ) from exc
    if error_code in ("AccessDeniedException", "AccessDenied"):
        raise PermissionError(
            f"Permission denied creating Kinesis Data Stream "
            f"'{stream_name}': {error_msg}"
        ) from exc
    raise RuntimeError(
        f"Failed to create Kinesis Data Stream '{stream_name}': "
        f"[{error_code}] {error_msg}"
    ) from exc
