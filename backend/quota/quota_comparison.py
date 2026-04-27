"""Service Quota comparison across ACGR region pairs.

Queries AWS Service Quotas API for both source and target regions,
checks if the Connect instance has ACGR (Global Resiliency) enabled,
and returns a comparison table with discrepancies highlighted.

Covers quotas for: Amazon Connect, Lambda, Lex V2, DynamoDB, Kinesis,
Kinesis Firehose, Kinesis Video Streams, S3, and IAM.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from aws.client_factory import create_client, create_source_client, create_target_client

logger = logging.getLogger(__name__)


# Service codes and their display names + key quota codes
_SERVICE_QUOTAS: list[dict[str, Any]] = [
    {
        "service_code": "connect",
        "display_name": "Amazon Connect",
        "quotas": [
            ("L-AA17A6B9", "Amazon Connect instance count"),
            ("L-9A46857E", "Users per instance"),
            ("L-8F812903", "Phone numbers per instance"),
            ("L-19A87C94", "Queues per instance"),
            ("L-D3E7BE26", "Routing profiles per instance"),
            ("L-22922690", "Contact flows per instance"),
            ("L-D945C9A8", "Agent status per instance"),
            ("L-68BBE2E8", "Quick connects per instance"),
            ("L-20CD02F7", "Hours of operation per instance"),
            ("L-0865B754", "Prompts per instance"),
            ("L-F325A715", "Security profiles per instance"),
            ("L-12AB7C57", "Concurrent active calls per instance"),
            ("L-D4BA6F6E", "Concurrent active chats per instance"),
            ("L-60553137", "Concurrent active tasks per instance"),
            ("L-E3D2F503", "AWS Lambda functions per instance"),
            ("L-B93A6612", "Amazon Lex bots per instance"),
            ("L-CCEA7427", "Amazon Lex V2 bot aliases per instance"),
            ("L-19755C7E", "Flow modules per instance"),
            ("L-D68AAAE4", "User hierarchy groups per instance"),
            ("L-516BC0EB", "Queues per routing profile per instance"),
        ],
    },
    {
        "service_code": "lambda",
        "display_name": "AWS Lambda",
        "quotas": [
            ("L-B99A9384", "Concurrent executions"),
            ("L-2ACBD22F", "Function and layer storage"),
        ],
    },
    {
        "service_code": "lex",
        "display_name": "Amazon Lex V2",
        "quotas": [
            ("L-36FA8BD2", "Bots per account (V2)"),
            ("L-BCD96794", "Versions per bot (V2)"),
            ("L-3D56827F", "Custom slot types per bot locale (V2)"),
            ("L-311093B9", "Slots per intent (V2)"),
            ("L-ED50DA7C", "Sample utterances per intent (V2)"),
            ("L-DA28F59B", "Bot channel associations per bot alias (V2)"),
        ],
    },
    {
        "service_code": "dynamodb",
        "display_name": "Amazon DynamoDB",
        "quotas": [
            ("L-F98FE922", "Maximum number of tables"),
            ("L-34F6A552", "Account-level read throughput limit (Provisioned mode)"),
            ("L-34F8CCC8", "Account-level write throughput limit (Provisioned mode)"),
        ],
    },
    {
        "service_code": "kinesis",
        "display_name": "Amazon Kinesis Data Streams",
        "quotas": [
            ("L-0918CF54", "Shards per Region"),
        ],
    },
    {
        "service_code": "firehose",
        "display_name": "Amazon Kinesis Data Firehose",
        "quotas": [
            ("L-14BB0BE7", "Delivery streams per Region"),
        ],
    },
    {
        "service_code": "kinesisvideo",
        "display_name": "Amazon Kinesis Video Streams",
        "quotas": [
            ("L-F06528A6", "Number of video streams"),
            ("L-B7F419CA", "Number of signaling channels"),
        ],
    },
    {
        "service_code": "s3",
        "display_name": "Amazon S3",
        "quotas": [
            ("L-DC2B2D3D", "General purpose buckets"),
        ],
    },
]


def _get_quota_value(
    client: Any, service_code: str, quota_code: str
) -> dict[str, Any]:
    """Get a single service quota value. Tries applied quota first, falls back to default."""
    try:
        resp = client.get_service_quota(
            ServiceCode=service_code, QuotaCode=quota_code
        )
        q = resp.get("Quota", {})
        return {
            "value": q.get("Value"),
            "quota_name": q.get("QuotaName", ""),
            "adjustable": q.get("Adjustable", False),
            "is_applied": True,
        }
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchResourceException":
            # No applied quota — get the default
            try:
                resp = client.get_aws_default_service_quota(
                    ServiceCode=service_code, QuotaCode=quota_code
                )
                q = resp.get("Quota", {})
                return {
                    "value": q.get("Value"),
                    "quota_name": q.get("QuotaName", ""),
                    "adjustable": q.get("Adjustable", False),
                    "is_applied": False,
                }
            except Exception:
                return {"value": None, "quota_name": "", "error": "Default quota not found"}
        return {"value": None, "quota_name": "", "error": str(exc)}
    except Exception as exc:
        return {"value": None, "quota_name": "", "error": str(exc)}


def _check_acgr_enabled(instance_arn: str, source_region: str) -> dict[str, Any]:
    """Check if the Connect instance has ACGR (Global Resiliency) enabled.

    Looks for Traffic Distribution Groups associated with the instance.
    If a TDG exists, ACGR is enabled.
    """
    try:
        connect_client = create_source_client("connect", source_region)

        # Parse instance ID from ARN
        parts = instance_arn.split("/")
        instance_id = parts[-1] if len(parts) >= 2 else instance_arn

        # Describe the instance to get basic info
        desc_resp = connect_client.describe_instance(InstanceId=instance_id)
        instance = desc_resp.get("Instance", {})
        instance_status = instance.get("InstanceStatus", "")

        # Check for TDGs
        tdg_list = []
        try:
            tdg_resp = connect_client.list_traffic_distribution_groups(
                InstanceId=instance_id, MaxResults=10
            )
            tdg_list = tdg_resp.get("TrafficDistributionGroupSummaryList", [])
        except ClientError:
            pass

        acgr_enabled = len(tdg_list) > 0
        target_region = None
        tdg_info = None

        if acgr_enabled and tdg_list:
            tdg = tdg_list[0]
            tdg_arn = tdg.get("Arn", "")
            tdg_name = tdg.get("Name", "")
            tdg_status = tdg.get("Status", "")

            # Try to determine the target region from the TDG
            try:
                tdg_desc = connect_client.describe_traffic_distribution_group(
                    TrafficDistributionGroupId=tdg_arn
                )
                tdg_detail = tdg_desc.get("TrafficDistributionGroup", {})
                # The TDG ARN region is the source; the replica is in the paired region
                tdg_region = tdg_arn.split(":")[3] if ":" in tdg_arn else ""
                # Common ACGR pairs
                _ACGR_PAIRS = {
                    "us-east-1": "us-west-2",
                    "us-west-2": "us-east-1",
                    "eu-west-2": "eu-central-1",
                    "eu-central-1": "eu-west-2",
                    "ap-southeast-1": "ap-northeast-1",
                    "ap-northeast-1": "ap-southeast-1",
                    "ap-southeast-2": "ap-northeast-2",
                    "ap-northeast-2": "ap-southeast-2",
                    "ca-central-1": "ca-west-1",
                    "ca-west-1": "ca-central-1",
                }
                target_region = _ACGR_PAIRS.get(source_region)
                tdg_info = {
                    "name": tdg_name,
                    "arn": tdg_arn,
                    "status": tdg_status,
                }
            except Exception:
                pass

        return {
            "instance_id": instance_id,
            "instance_status": instance_status,
            "acgr_enabled": acgr_enabled,
            "target_region": target_region,
            "tdg": tdg_info,
        }
    except ClientError as exc:
        return {
            "instance_id": "",
            "instance_status": "ERROR",
            "acgr_enabled": False,
            "error": str(exc),
        }


def compare_quotas(
    instance_arn: str,
    source_region: str,
    target_region: str | None = None,
) -> dict[str, Any]:
    """Compare service quotas between source and target ACGR regions.

    1. Validates the instance and checks ACGR status
    2. Determines the target region (from ACGR config or user-provided)
    3. Queries Service Quotas API for both regions
    4. Returns a comparison table with discrepancies

    Args:
        instance_arn: The Connect instance ARN.
        source_region: The source AWS region.
        target_region: Optional target region override. If None, auto-detected from ACGR.

    Returns:
        A dict with acgr_status, comparison table, and discrepancies.
    """
    # Step 1: Check ACGR status
    acgr_status = _check_acgr_enabled(instance_arn, source_region)

    # Step 2: Determine target region
    effective_target = target_region or acgr_status.get("target_region")
    if not effective_target:
        return {
            "acgr_status": acgr_status,
            "error": "Could not determine target region. ACGR may not be enabled, "
                     "or the region pair is not recognized. Please provide a target region.",
            "comparison": [],
            "discrepancies": [],
        }

    # Step 3: Query quotas for both regions
    source_sq = create_client("service-quotas", source_region)
    target_sq = create_client("service-quotas", effective_target)

    comparison: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []

    for svc in _SERVICE_QUOTAS:
        service_code = svc["service_code"]
        display_name = svc["display_name"]

        for quota_code, fallback_name in svc["quotas"]:
            source_info = _get_quota_value(source_sq, service_code, quota_code)
            target_info = _get_quota_value(target_sq, service_code, quota_code)

            source_val = source_info.get("value")
            target_val = target_info.get("value")
            quota_name = source_info.get("quota_name") or target_info.get("quota_name") or fallback_name

            # Determine discrepancy
            discrepancy = None
            if source_val is not None and target_val is not None:
                if source_val != target_val:
                    if target_val < source_val:
                        discrepancy = (
                            f"Target region ({effective_target}) has a lower limit "
                            f"({target_val:,.0f}) than source ({source_val:,.0f}). "
                            f"This may cause failures during DR failover if usage "
                            f"exceeds the target limit. Request a quota increase "
                            f"in {effective_target}."
                        )
                    else:
                        discrepancy = (
                            f"Target region ({effective_target}) has a higher limit "
                            f"({target_val:,.0f}) than source ({source_val:,.0f}). "
                            f"No action needed."
                        )

            entry = {
                "service": display_name,
                "quota_name": quota_name,
                "quota_code": quota_code,
                "source_region": source_region,
                "source_value": source_val,
                "source_applied": source_info.get("is_applied", False),
                "target_region": effective_target,
                "target_value": target_val,
                "target_applied": target_info.get("is_applied", False),
                "adjustable": source_info.get("adjustable", False),
                "match": source_val == target_val if source_val is not None and target_val is not None else None,
                "discrepancy": discrepancy,
                "source_error": source_info.get("error"),
                "target_error": target_info.get("error"),
            }
            comparison.append(entry)

            if discrepancy and target_val is not None and source_val is not None and target_val < source_val:
                discrepancies.append(entry)

    return {
        "acgr_status": acgr_status,
        "source_region": source_region,
        "target_region": effective_target,
        "comparison": comparison,
        "discrepancies": discrepancies,
        "total_quotas_checked": len(comparison),
        "total_discrepancies": len(discrepancies),
    }
