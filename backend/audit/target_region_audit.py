"""Target region audit — checks which resources already exist in the DR region.

For each resource in the inventory, computes the expected target name
(source name + suffix) and calls the appropriate AWS list/describe API
to determine whether the resource already exists.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from aws.client_factory import create_client, create_target_client
from models.enums import ResourceType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ResourceAuditEntry(BaseModel):
    """Result of auditing a single resource in the target region."""

    resource_id: str
    exists_in_target: bool
    target_arn: str | None = None
    audit_error: str | None = None


class AuditResult(BaseModel):
    """Aggregated result of a target region audit."""

    entries: dict[str, ResourceAuditEntry] = {}
    already_replicated_count: int = 0
    missing_count: int = 0
    error_count: int = 0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _normalize_suffix(suffix: str) -> str:
    """Ensure the suffix starts with a hyphen if non-empty.

    Users enter a suffix like ``dr1`` but the expected target name should
    be ``source-name-dr1``, not ``source-namedr1``.

    .. deprecated:: Use compute_expected_target_name directly.
    """
    if suffix and not suffix.startswith("-"):
        return f"-{suffix}"
    return suffix


def compute_expected_target_name(source_name: str, suffix: str, resource_type: str | ResourceType | None = None, is_lex_codehook: bool = False) -> str:
    """Return the expected resource name in the target region.

    Resources that get a hardcoded '-dr' suffix:
    - S3 buckets (globally unique names)
    - DynamoDB tables
    - Kinesis Data Streams
    - Kinesis Firehose delivery streams
    - Kinesis Video Streams
    - Lambda functions that are Lex codehook/fulfillment only

    Resources that keep the original name:
    - Lambda functions associated with Connect
    - IAM roles (global)
    - Lex bots (ALGR handles naming)
    """
    if resource_type is not None:
        if isinstance(resource_type, str):
            try:
                resource_type = ResourceType(resource_type)
            except ValueError:
                pass
        # These resource types always get -dr suffix
        if resource_type in (
            ResourceType.S3_BUCKET,
            ResourceType.KINESIS_STREAM,
            ResourceType.KINESIS_FIREHOSE,
            ResourceType.KINESIS_VIDEO_STREAM,
        ):
            return source_name + "-dr"
        # Lambda: all Lambdas (including Lex codehook) keep the original name
        if resource_type == ResourceType.LAMBDA:
            return source_name
    # Legacy fallback: if suffix is provided and no resource_type, use old behavior
    if suffix and resource_type is None:
        return source_name + _normalize_suffix(suffix)
    return source_name


# ---------------------------------------------------------------------------
# Per-resource-type checkers
# ---------------------------------------------------------------------------


def _check_lambda_exists(
    target_region: str, expected_name: str
) -> tuple[bool, str | None]:
    """Check if a Lambda function exists in the target region."""
    try:
        client = create_target_client("lambda", target_region)
        response = client.get_function(FunctionName=expected_name)
        arn = response.get("Configuration", {}).get("FunctionArn")
        return (True, arn)
    except Exception:
        return (False, None)


def _check_lex_bot_exists(
    target_region: str, expected_name: str, lex_version: str = "V2"
) -> tuple[bool, str | None]:
    """Check if a Lex bot exists in the target region.

    Handles both V1 and V2 bots based on the lex_version parameter.
    """
    try:
        if lex_version == "V1":
            client = create_target_client("lex-models", target_region)
            response = client.get_bot(name=expected_name, versionOrAlias="$LATEST")
            # V1 bots don't return an ARN directly; construct one
            return (True, None)
        else:
            client = create_target_client("lexv2-models", target_region)
            # Use ListBots with a name filter to find the bot
            response = client.list_bots(
                filters=[
                    {
                        "name": "BotName",
                        "values": [expected_name],
                        "operator": "EQ",
                    }
                ],
                maxResults=10,
            )
            for bot in response.get("botSummaries", []):
                if bot.get("botName") == expected_name:
                    bot_id = bot.get("botId", "")
                    # Describe to get the full ARN
                    try:
                        desc = client.describe_bot(botId=bot_id)
                        return (True, desc.get("botArn"))
                    except Exception:
                        return (True, None)
            return (False, None)
    except Exception:
        return (False, None)



def _check_kinesis_stream_exists(
    target_region: str, expected_name: str
) -> tuple[bool, str | None]:
    """Check if a Kinesis stream exists in the target region."""
    try:
        client = create_target_client("kinesis", target_region)
        response = client.describe_stream_summary(StreamName=expected_name)
        arn = response.get("StreamDescriptionSummary", {}).get("StreamARN")
        return (True, arn)
    except Exception:
        return (False, None)


def _check_firehose_exists(
    target_region: str, expected_name: str
) -> tuple[bool, str | None]:
    """Check if a Firehose delivery stream exists in the target region."""
    try:
        client = create_target_client("firehose", target_region)
        response = client.describe_delivery_stream(
            DeliveryStreamName=expected_name
        )
        arn = (
            response.get("DeliveryStreamDescription", {})
            .get("DeliveryStreamARN")
        )
        return (True, arn)
    except Exception:
        return (False, None)


def _check_kvs_exists(
    target_region: str, expected_name: str
) -> tuple[bool, str | None]:
    """Check if a Kinesis Video Stream exists in the target region."""
    try:
        client = create_target_client("kinesisvideo", target_region)
        response = client.describe_stream(StreamName=expected_name)
        arn = response.get("StreamInfo", {}).get("StreamARN")
        return (True, arn)
    except Exception:
        return (False, None)


def _check_iam_role_exists(expected_name: str) -> tuple[bool, str | None]:
    """Check if an IAM role exists (IAM is global, no region needed)."""
    try:
        client = create_client("iam", "us-east-1")
        response = client.get_role(RoleName=expected_name)
        arn = response.get("Role", {}).get("Arn")
        return (True, arn)
    except Exception:
        return (False, None)


def _check_s3_bucket_exists(expected_name: str, target_region: str) -> tuple[bool, str | None]:
    """Check if an S3 bucket exists AND is in the target region.

    S3 bucket names are globally unique, so ``head_bucket`` alone is not
    enough — a bucket in us-east-1 would pass even when the target is
    us-west-2.  We therefore also call ``get_bucket_location`` and verify
    the bucket's actual region matches ``target_region``.
    """
    try:
        client = create_client("s3", "us-east-1")
        client.head_bucket(Bucket=expected_name)
        # Verify the bucket is actually in the target region
        loc_resp = client.get_bucket_location(Bucket=expected_name)
        # AWS returns None / "" for us-east-1
        bucket_region = loc_resp.get("LocationConstraint") or "us-east-1"
        if bucket_region != target_region:
            logger.info(
                "S3 bucket %s exists but is in %s, not target %s — treating as missing",
                expected_name,
                bucket_region,
                target_region,
            )
            return (False, None)
        arn = f"arn:aws:s3:::{expected_name}"
        return (True, arn)
    except Exception:
        return (False, None)


# ---------------------------------------------------------------------------
# Main audit function
# ---------------------------------------------------------------------------


def audit_target_region(
    inventory: dict[str, Any],
    target_region: str,
    resource_suffix: str = "",
) -> AuditResult:
    """Audit the target region and return which resources already exist.

    Iterates over every resource in the inventory, computes the expected
    target name, and calls the appropriate AWS checker to see if the
    resource exists. S3 buckets get a '-dr' suffix; all other resources
    use the same name as source.

    Args:
        inventory: Mapping of resource_id → ResourceBase (or dict with
            at least ``name``, ``resource_type``, and optionally
            ``config_summary``).
        target_region: The DR / target AWS region.
        resource_suffix: Legacy suffix param (ignored when resource_type is set).

    Returns:
        An ``AuditResult`` with per-resource entries and summary counts.
    """
    entries: dict[str, ResourceAuditEntry] = {}
    already_replicated = 0
    missing = 0
    errors = 0

    for resource_id, resource in inventory.items():
        # Support both Pydantic models and plain dicts
        if isinstance(resource, dict):
            name = resource.get("name", "")
            resource_type_raw = resource.get("resource_type", "")
            config_summary = resource.get("config_summary", {})
        else:
            name = resource.name
            resource_type_raw = resource.resource_type
            config_summary = resource.config_summary if hasattr(resource, "config_summary") else {}

        # Normalise resource_type to the enum
        if isinstance(resource_type_raw, str):
            try:
                resource_type = ResourceType(resource_type_raw)
            except ValueError:
                logger.warning(
                    "Unknown resource type '%s' for resource %s — skipping",
                    resource_type_raw,
                    resource_id,
                )
                entries[resource_id] = ResourceAuditEntry(
                    resource_id=resource_id,
                    exists_in_target=False,
                    audit_error=f"Unknown resource type: {resource_type_raw}",
                )
                errors += 1
                continue
        else:
            resource_type = resource_type_raw

        expected_name = compute_expected_target_name(
            name, resource_suffix, resource_type=resource_type,
            is_lex_codehook=getattr(resource, "is_lex_codehook", False),
        )

        try:
            # IAM roles are global — always exist in target (source role IS the target role)
            if resource_type == ResourceType.IAM_ROLE:
                # Use the source ARN directly; IAM is global so no region-specific check needed
                if isinstance(resource, dict):
                    source_arn = resource.get("arn", "")
                else:
                    source_arn = resource.arn if hasattr(resource, "arn") else ""
                exists, arn = True, source_arn or None
            elif resource_type == ResourceType.S3_BUCKET:
                exists, arn = _check_s3_bucket_exists(expected_name, target_region)
            elif resource_type == ResourceType.LEX_BOT:
                lex_version = config_summary.get("lex_version", "V2") if isinstance(config_summary, dict) else "V2"
                exists, arn = _check_lex_bot_exists(
                    target_region, expected_name, lex_version=lex_version
                )
            else:
                # Regional service — dispatch to the correct checker
                if resource_type == ResourceType.LAMBDA:
                    exists, arn = _check_lambda_exists(target_region, expected_name)
                elif resource_type == ResourceType.KINESIS_STREAM:
                    exists, arn = _check_kinesis_stream_exists(target_region, expected_name)
                elif resource_type == ResourceType.KINESIS_FIREHOSE:
                    exists, arn = _check_firehose_exists(target_region, expected_name)
                elif resource_type == ResourceType.KINESIS_VIDEO_STREAM:
                    exists, arn = _check_kvs_exists(target_region, expected_name)
                else:
                    logger.warning(
                        "No checker for resource type %s (resource %s)",
                        resource_type,
                        resource_id,
                    )
                    exists, arn = False, None

            entries[resource_id] = ResourceAuditEntry(
                resource_id=resource_id,
                exists_in_target=exists,
                target_arn=arn,
            )
            if exists:
                already_replicated += 1
            else:
                missing += 1

        except Exception as exc:
            logger.exception(
                "Audit checker failed for resource %s (%s): %s",
                resource_id,
                resource_type,
                exc,
            )
            entries[resource_id] = ResourceAuditEntry(
                resource_id=resource_id,
                exists_in_target=False,
                audit_error=str(exc),
            )
            errors += 1

    return AuditResult(
        entries=entries,
        already_replicated_count=already_replicated,
        missing_count=missing,
        error_count=errors,
    )
