"""KMS utility functions for cross-region encryption key resolution.

Handles bootstrapping AWS-managed KMS keys (alias/aws/*) in target regions
and resolving cross-region encryption configs for storage association.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from aws.client_factory import create_target_client

if TYPE_CHECKING:
    from models.resources import KmsInfo

logger = logging.getLogger(__name__)

# AWS-managed key aliases that may need bootstrapping in new regions.
# Each maps to the service that triggers auto-creation of the key.
_BOOTSTRAP_SERVICES = {
    "alias/aws/kinesisvideo": "kinesisvideo",
    "alias/aws/s3": "s3",
    "alias/aws/connect": "connect",
}


def _build_kms_info(
    key_type: str,
    key_alias_or_arn: str,
    action: str,
    message: str,
) -> dict:
    """Build a KmsInfo dict describing KMS handling for a resource."""
    return {
        "key_type": key_type,
        "key_alias_or_arn": key_alias_or_arn,
        "action": action,
        "message": message,
    }


def ensure_kms_key_exists(key_id: str, target_region: str) -> tuple[str, dict | None]:
    """Ensure a KMS key exists in the target region.

    For AWS-managed keys (alias/aws/*), checks if the alias exists and
    bootstraps it if needed. For customer-managed keys (CMK ARNs), detects
    cross-region issues and returns empty string with a warning.

    Args:
        key_id: The KMS key ID, alias, or ARN from the source config.
        target_region: The target AWS region.

    Returns:
        A tuple of (resolved_key_id, kms_info_dict). The key ID to use in
        the target region (empty string if unresolvable), and a dict
        describing the KMS handling action taken.
    """
    if not key_id:
        return "", _build_kms_info("NONE", "", "SKIPPED", "No KMS key configured")

    # Customer-managed key ARN (contains :key/ or starts with arn:)
    if key_id.startswith("arn:aws:kms:") or ":key/" in key_id:
        resolved = _resolve_cmk(key_id, target_region)
        if resolved:
            info = _build_kms_info(
                "CMK", key_id, "REUSED",
                f"Customer-managed key reused in {target_region}",
            )
        else:
            info = _build_kms_info(
                "CMK", key_id, "SKIPPED",
                f"Customer-managed key is region-specific and not available in {target_region}. "
                f"Encryption config was skipped for the replica. Create a matching CMK in "
                f"{target_region} and update the config manually.",
            )
        return resolved, info

    # AWS-managed alias (alias/aws/*)
    if key_id.startswith("alias/aws/"):
        resolved, action = _ensure_aws_managed_key(key_id, target_region)
        if resolved and action == "BOOTSTRAPPED":
            info = _build_kms_info(
                "AWS_MANAGED", key_id, "BOOTSTRAPPED",
                f"AWS-managed key bootstrapped in {target_region} by creating a temporary resource",
            )
        elif resolved:
            info = _build_kms_info(
                "AWS_MANAGED", key_id, "REUSED",
                f"AWS-managed key already exists in {target_region}",
            )
        else:
            info = _build_kms_info(
                "AWS_MANAGED", key_id, "SKIPPED",
                f"AWS-managed key could not be resolved in {target_region}. "
                f"Encryption config was skipped.",
            )
        return resolved, info

    # Plain alias or key ID — try to use as-is
    if key_id.startswith("alias/"):
        resolved = _check_alias_exists(key_id, target_region)
        if resolved:
            info = _build_kms_info(
                "CMK", key_id, "REUSED",
                f"Custom alias key found in {target_region}",
            )
        else:
            info = _build_kms_info(
                "CMK", key_id, "SKIPPED",
                f"Custom alias key not found in {target_region}. Encryption config was skipped.",
            )
        return resolved, info

    # Raw key ID — assume it's valid in the target region
    info = _build_kms_info(
        "CMK", key_id, "REUSED",
        f"Raw key ID assumed valid in {target_region}",
    )
    return key_id, info


def _check_alias_exists(alias: str, target_region: str) -> str:
    """Check if a KMS alias exists in the target region."""
    try:
        kms_client = create_target_client("kms", target_region)
        kms_client.describe_key(KeyId=alias)
        return alias
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "NotFoundException":
            logger.warning(
                "KMS alias '%s' not found in %s — skipping encryption",
                alias, target_region,
            )
            return ""
        # Other errors (throttling, etc.) — try to use it anyway
        logger.debug("KMS describe_key error for %s: %s", alias, exc)
        return alias


def _resolve_cmk(key_arn: str, target_region: str) -> str:
    """Resolve a customer-managed KMS key for cross-region use.

    CMK ARNs are region-specific. If the key is in a different region,
    we can't use it. Return empty string so the caller skips encryption
    or uses a default.
    """
    # Extract region from the ARN: arn:aws:kms:REGION:ACCOUNT:key/KEY-ID
    parts = key_arn.split(":")
    if len(parts) >= 4:
        key_region = parts[3]
        if key_region == target_region:
            # Same region — use as-is
            return key_arn

    logger.warning(
        "Customer-managed KMS key '%s' is region-specific and cannot be "
        "used in %s. Encryption config will be skipped for this resource. "
        "Create a matching CMK in %s and update the config manually.",
        key_arn, target_region, target_region,
    )
    return ""


def _ensure_aws_managed_key(alias: str, target_region: str) -> tuple[str, str]:
    """Ensure an AWS-managed KMS key exists in the target region.

    AWS-managed keys (alias/aws/*) are auto-created the first time the
    corresponding service is used in a region. If the key doesn't exist,
    we trigger its creation by making a minimal service call.

    Returns:
        A tuple of (resolved_key_id, action) where action is one of
        "REUSED", "BOOTSTRAPPED", or "SKIPPED".
    """
    try:
        kms_client = create_target_client("kms", target_region)
        kms_client.describe_key(KeyId=alias)
        logger.debug("KMS key '%s' exists in %s", alias, target_region)
        return alias, "REUSED"
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code != "NotFoundException":
            # Throttling or other transient error — try to use it anyway
            logger.debug("KMS describe_key error for %s: %s", alias, exc)
            return alias, "REUSED"

    # Key doesn't exist — try to bootstrap it
    logger.info(
        "KMS key '%s' not found in %s — attempting to bootstrap",
        alias, target_region,
    )

    if alias == "alias/aws/kinesisvideo":
        result = _bootstrap_kvs_kms_key(target_region)
        return (result, "BOOTSTRAPPED") if result else ("", "SKIPPED")
    elif alias == "alias/aws/s3":
        # alias/aws/s3 is created automatically in all regions where S3 is
        # available. If it's missing, it's likely a transient issue.
        logger.warning(
            "alias/aws/s3 not found in %s — this is unusual. "
            "Attempting to use it anyway.",
            target_region,
        )
        return alias, "REUSED"
    elif alias == "alias/aws/connect":
        # alias/aws/connect is created when Connect is first used in a region.
        # Since we're replicating TO this region, Connect should already be set up.
        logger.warning(
            "alias/aws/connect not found in %s — Connect may not be "
            "initialized in this region. Skipping encryption config.",
            target_region,
        )
        return "", "SKIPPED"
    else:
        # Unknown AWS-managed key — try to use it anyway
        logger.warning(
            "Unknown AWS-managed key '%s' not found in %s — trying anyway",
            alias, target_region,
        )
        return alias, "REUSED"


def _bootstrap_kvs_kms_key(target_region: str) -> str:
    """Bootstrap the alias/aws/kinesisvideo KMS key by creating a temp stream.

    The KVS AWS-managed key is only created when KVS is first used in a region.
    We create a temporary stream to trigger key creation, then clean it up.

    Returns:
        The alias string if successful, empty string if bootstrap fails.
    """
    temp_stream_name = "_acgr-kms-bootstrap-temp"
    kvs_client = create_target_client("kinesisvideo", target_region)

    try:
        kvs_client.create_stream(
            StreamName=temp_stream_name,
            DataRetentionInHours=1,
        )
        logger.info("Created temp KVS stream to bootstrap KMS key in %s", target_region)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceInUseException":
            # Stream already exists from a previous bootstrap attempt.
            # The key should exist now.
            logger.info(
                "Temp KVS stream already exists in %s — KMS key should be available",
                target_region,
            )
            _cleanup_temp_kvs_stream(kvs_client, temp_stream_name)
            return "alias/aws/kinesisvideo"
        logger.warning(
            "Failed to create temp KVS stream in %s for KMS bootstrap: %s",
            target_region, exc,
        )
        return ""
    except Exception as exc:
        logger.warning("KVS KMS bootstrap failed in %s: %s", target_region, exc)
        return ""

    # Clean up the temp stream
    _cleanup_temp_kvs_stream(kvs_client, temp_stream_name)

    # Verify the key now exists
    try:
        kms_client = create_target_client("kms", target_region)
        kms_client.describe_key(KeyId="alias/aws/kinesisvideo")
        logger.info("KMS key alias/aws/kinesisvideo now exists in %s", target_region)
        return "alias/aws/kinesisvideo"
    except Exception:
        # Key creation may take a moment — return the alias and hope for the best
        logger.info(
            "KMS key may still be propagating in %s — returning alias anyway",
            target_region,
        )
        return "alias/aws/kinesisvideo"


def _cleanup_temp_kvs_stream(kvs_client, stream_name: str) -> None:
    """Delete the temporary KVS stream used for KMS bootstrapping."""
    try:
        # List streams to find the ARN (delete_stream requires ARN)
        resp = kvs_client.list_streams(
            StreamNameCondition={
                "ComparisonOperator": "BEGINS_WITH",
                "ComparisonValue": stream_name,
            }
        )
        for stream_info in resp.get("StreamInfoList", []):
            if stream_info.get("StreamName") == stream_name:
                stream_arn = stream_info.get("StreamARN", "")
                if stream_arn:
                    kvs_client.delete_stream(StreamARN=stream_arn)
                    logger.info("Cleaned up temp KVS stream: %s", stream_name)
                return
        logger.debug("Temp KVS stream '%s' not found in list — may already be deleted", stream_name)
    except Exception:
        logger.debug("Could not clean up temp KVS stream '%s'", stream_name, exc_info=True)
