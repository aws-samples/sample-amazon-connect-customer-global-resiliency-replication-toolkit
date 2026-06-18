"""Discover existing resources in the target region for association.

Scans the target region for resources that were replicated manually or
outside this tool (e.g. via ALGR, manual creation, or other automation).
Cross-references against the source instance's associated resources to
find matches by name, naming convention, or bot ID.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from aws.client_factory import create_source_client, create_target_client

logger = logging.getLogger(__name__)


def discover_target_resources(
    instance_id: str,
    source_region: str,
    target_region: str,
) -> list[dict[str, Any]]:
    """Discover resources in the target region that can be associated.

    Scans for Amazon Lex bots, AWS Lambda functions, Kinesis streams, Firehose streams,
    S3 buckets, and KVS configs. Cross-references against source instance
    resources to identify matches.

    Returns a list of discovered resource dicts with:
        name, resource_type, arn, source_match (name of matched source resource),
        association_status ("not_associated" or "already_associated")
    """
    results: list[dict[str, Any]] = []

    # Get source instance's associated resources for cross-referencing
    source_lambdas = _get_source_lambdas(source_region, instance_id)
    source_bots = _get_source_bots(source_region, instance_id)
    source_storage = _get_source_storage_configs(source_region, instance_id)

    # Get target instance's currently associated resources for dedup
    target_lambdas = _get_target_associated_lambdas(target_region, instance_id)
    target_bots = _get_target_associated_bots(target_region, instance_id)
    target_storage = _get_target_storage_configs(target_region, instance_id)

    # Discover Amazon Lex bots
    lex_results = _discover_lex_bots(
        target_region, source_bots, target_bots, instance_id
    )
    results.extend(lex_results)

    # Discover AWS Lambda functions
    lambda_results = _discover_lambdas(
        target_region, source_lambdas, target_lambdas
    )
    results.extend(lambda_results)

    # Discover Kinesis streams
    kinesis_results = _discover_kinesis_streams(
        target_region, source_storage, target_storage
    )
    results.extend(kinesis_results)

    # Discover Firehose streams
    firehose_results = _discover_firehose_streams(
        target_region, source_storage, target_storage
    )
    results.extend(firehose_results)

    # Discover S3 buckets
    s3_results = _discover_s3_buckets(
        source_region, target_region, source_storage, target_storage
    )
    results.extend(s3_results)

    # Discover KVS (media streams) config
    kvs_results = _discover_kvs_config(
        target_region, source_storage, target_storage, instance_id
    )
    results.extend(kvs_results)

    return results


# ---------------------------------------------------------------------------
# Source resource readers
# ---------------------------------------------------------------------------

def _get_source_lambdas(region: str, instance_id: str) -> list[str]:
    """Get Lambda ARNs associated with the source instance."""
    arns: list[str] = []
    try:
        client = create_source_client("connect", region)
        params: dict = {"InstanceId": instance_id, "MaxResults": 25}
        while True:
            resp = client.list_lambda_functions(**params)
            arns.extend(resp.get("LambdaFunctions", []))
            nt = resp.get("NextToken")
            if not nt:
                break
            params["NextToken"] = nt
    except ClientError:
        logger.debug("Could not list source Lambda functions", exc_info=True)
    return arns


def _get_source_bots(region: str, instance_id: str) -> list[dict[str, Any]]:
    """Get Lex bot info associated with the source instance."""
    bots: list[dict[str, Any]] = []
    try:
        client = create_source_client("connect", region)
        params: dict = {"InstanceId": instance_id, "MaxResults": 25, "LexVersion": "V2"}
        while True:
            resp = client.list_bots(**params)
            for b in resp.get("LexBots", []):
                v2 = b.get("LexV2Bot", {})
                alias_arn = v2.get("AliasArn", "")
                if alias_arn:
                    bots.append({"alias_arn": alias_arn})
            nt = resp.get("NextToken")
            if not nt:
                break
            params["NextToken"] = nt
    except ClientError:
        logger.debug("Could not list source bots", exc_info=True)
    return bots


def _get_source_storage_configs(
    region: str, instance_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Get all storage configs from the source instance."""
    result: dict[str, list[dict[str, Any]]] = {}
    all_types = [
        "CALL_RECORDINGS", "CHAT_TRANSCRIPTS", "SCHEDULED_REPORTS",
        "CONTACT_TRACE_RECORDS", "AGENT_EVENTS", "MEDIA_STREAMS",
        "ATTACHMENTS", "SCREEN_RECORDINGS",
    ]
    try:
        client = create_source_client("connect", region)
        for st in all_types:
            try:
                resp = client.list_instance_storage_configs(
                    InstanceId=instance_id, ResourceType=st,
                )
                configs = resp.get("StorageConfigs", [])
                if configs:
                    result[st] = configs
            except ClientError:
                pass
    except Exception:
        logger.debug("Could not read source storage configs", exc_info=True)
    return result


# ---------------------------------------------------------------------------
# Target associated resource readers
# ---------------------------------------------------------------------------

def _get_target_associated_lambdas(region: str, instance_id: str) -> set[str]:
    arns: set[str] = set()
    try:
        client = create_target_client("connect", region)
        params: dict = {"InstanceId": instance_id, "MaxResults": 25}
        while True:
            resp = client.list_lambda_functions(**params)
            for arn in resp.get("LambdaFunctions", []):
                arns.add(arn)
            nt = resp.get("NextToken")
            if not nt:
                break
            params["NextToken"] = nt
    except ClientError:
        logger.debug("Could not list target Lambda functions", exc_info=True)
    return arns


def _get_target_associated_bots(region: str, instance_id: str) -> set[str]:
    arns: set[str] = set()
    try:
        client = create_target_client("connect", region)
        params: dict = {"InstanceId": instance_id, "MaxResults": 25, "LexVersion": "V2"}
        while True:
            resp = client.list_bots(**params)
            for b in resp.get("LexBots", []):
                v2 = b.get("LexV2Bot", {})
                alias_arn = v2.get("AliasArn", "")
                if alias_arn:
                    arns.add(alias_arn)
            nt = resp.get("NextToken")
            if not nt:
                break
            params["NextToken"] = nt
    except ClientError:
        logger.debug("Could not list target bots", exc_info=True)
    return arns


def _get_target_storage_configs(
    region: str, instance_id: str
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    all_types = [
        "CALL_RECORDINGS", "CHAT_TRANSCRIPTS", "SCHEDULED_REPORTS",
        "CONTACT_TRACE_RECORDS", "AGENT_EVENTS", "MEDIA_STREAMS",
        "ATTACHMENTS", "SCREEN_RECORDINGS",
    ]
    try:
        client = create_target_client("connect", region)
        for st in all_types:
            try:
                resp = client.list_instance_storage_configs(
                    InstanceId=instance_id, ResourceType=st,
                )
                configs = resp.get("StorageConfigs", [])
                if configs:
                    result[st] = configs
            except ClientError:
                pass
    except Exception:
        logger.debug("Could not read target storage configs", exc_info=True)
    return result


# ---------------------------------------------------------------------------
# Amazon Lex bot discovery
# ---------------------------------------------------------------------------

def _discover_lex_bots(
    target_region: str,
    source_bots: list[dict[str, Any]],
    target_associated: set[str],
    instance_id: str,
) -> list[dict[str, Any]]:
    """Discover Lex bots in the target region that match source bots."""
    results: list[dict[str, Any]] = []

    # Extract source bot IDs from alias ARNs
    source_bot_ids: dict[str, str] = {}  # bot_id -> source alias ARN
    for bot in source_bots:
        alias_arn = bot.get("alias_arn", "")
        # ARN format: arn:aws:lex:region:account:bot-alias/BOT_ID/ALIAS_ID
        parts = alias_arn.split("/")
        if len(parts) >= 2:
            bot_id = parts[-2] if len(parts) >= 3 else parts[-1]
            source_bot_ids[bot_id] = alias_arn

    if not source_bot_ids:
        return results

    try:
        lex_client = create_target_client("lexv2-models", target_region)
        resp = lex_client.list_bots(maxResults=50)
        for bot_summary in resp.get("botSummaries", []):
            bot_id = bot_summary.get("botId", "")
            bot_name = bot_summary.get("botName", "")

            if bot_id not in source_bot_ids:
                continue

            # Bot exists in target — find its alias ARN
            alias_arn = _get_best_alias_arn(lex_client, bot_id, target_region)
            if not alias_arn:
                continue

            is_associated = alias_arn in target_associated
            source_alias = source_bot_ids[bot_id]

            results.append({
                "name": bot_name,
                "resource_type": "LEX_BOT",
                "arn": alias_arn,
                "source_match": _extract_name_from_arn(source_alias),
                "source_arn": source_alias,
                "association_status": "already_associated" if is_associated else "not_associated",
            })
    except ClientError:
        logger.debug("Could not list Lex bots in target region", exc_info=True)

    return results


def _get_best_alias_arn(lex_client, bot_id: str, target_region: str) -> str | None:
    """Get the best (non-test) alias ARN for a bot."""
    try:
        resp = lex_client.list_bot_aliases(botId=bot_id, maxResults=10)
        test_alias = None
        for alias in resp.get("botAliasSummaries", []):
            alias_id = alias.get("botAliasId", "")
            if not alias_id:
                continue
            if alias_id != "TSTALIASID":
                account = _get_account_id(target_region)
                return f"arn:aws:lex:{target_region}:{account}:bot-alias/{bot_id}/{alias_id}"
            else:
                test_alias = alias_id

        if test_alias:
            account = _get_account_id(target_region)
            return f"arn:aws:lex:{target_region}:{account}:bot-alias/{bot_id}/{test_alias}"
    except ClientError:
        pass
    return None


def _get_account_id(region: str) -> str:
    """Get the AWS account ID via STS."""
    try:
        sts = create_target_client("sts", region)
        return sts.get_caller_identity()["Account"]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Lambda discovery
# ---------------------------------------------------------------------------

def _discover_lambdas(
    target_region: str,
    source_lambdas: list[str],
    target_associated: set[str],
) -> list[dict[str, Any]]:
    """Discover Lambda functions in the target region matching source functions."""
    results: list[dict[str, Any]] = []

    # Extract function names from source ARNs
    source_names: dict[str, str] = {}  # name -> source ARN
    for arn in source_lambdas:
        parts = arn.split(":")
        if len(parts) >= 7:
            name = parts[6].split("/")[-1]
            source_names[name] = arn

    if not source_names:
        return results

    try:
        lambda_client = create_target_client("lambda", target_region)
        paginator = lambda_client.get_paginator("list_functions")
        for page in paginator.paginate(MaxItems=200):
            for func in page.get("Functions", []):
                func_name = func.get("FunctionName", "")
                func_arn = func.get("FunctionArn", "")

                if func_name in source_names:
                    is_associated = func_arn in target_associated
                    results.append({
                        "name": func_name,
                        "resource_type": "LAMBDA",
                        "arn": func_arn,
                        "source_match": func_name,
                        "source_arn": source_names[func_name],
                        "association_status": "already_associated" if is_associated else "not_associated",
                    })
    except ClientError:
        logger.debug("Could not list Lambda functions in target region", exc_info=True)

    return results


# ---------------------------------------------------------------------------
# Kinesis stream discovery
# ---------------------------------------------------------------------------

def _discover_kinesis_streams(
    target_region: str,
    source_storage: dict[str, list[dict[str, Any]]],
    target_storage: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Discover Kinesis streams in the target region matching source storage configs."""
    results: list[dict[str, Any]] = []

    # Extract source Kinesis stream names
    source_streams: dict[str, str] = {}  # stream_name -> storage_type
    for storage_type, configs in source_storage.items():
        for cfg in configs:
            if cfg.get("StorageType") == "KINESIS_STREAM":
                arn = cfg.get("KinesisStreamConfig", {}).get("StreamArn", "")
                if arn:
                    name = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]
                    source_streams[name] = storage_type

    if not source_streams:
        return results

    # Get already-associated stream ARNs in target
    target_stream_arns: set[str] = set()
    for configs in target_storage.values():
        for cfg in configs:
            if cfg.get("StorageType") == "KINESIS_STREAM":
                arn = cfg.get("KinesisStreamConfig", {}).get("StreamArn", "")
                if arn:
                    target_stream_arns.add(arn)

    try:
        kinesis_client = create_target_client("kinesis", target_region)
        resp = kinesis_client.list_streams(Limit=100)
        for stream_name in resp.get("StreamNames", []):
            if stream_name in source_streams:
                # Get full ARN
                try:
                    desc = kinesis_client.describe_stream_summary(StreamName=stream_name)
                    stream_arn = desc.get("StreamDescriptionSummary", {}).get("StreamARN", "")
                except ClientError:
                    stream_arn = ""

                is_associated = stream_arn in target_stream_arns if stream_arn else False
                results.append({
                    "name": stream_name,
                    "resource_type": "KINESIS_STREAM",
                    "arn": stream_arn or f"arn:aws:kinesis:{target_region}:*:stream/{stream_name}",
                    "source_match": stream_name,
                    "source_arn": "",
                    "storage_type": source_streams[stream_name],
                    "association_status": "already_associated" if is_associated else "not_associated",
                })
    except ClientError:
        logger.debug("Could not list Kinesis streams in target region", exc_info=True)

    return results


# ---------------------------------------------------------------------------
# Firehose discovery
# ---------------------------------------------------------------------------

def _discover_firehose_streams(
    target_region: str,
    source_storage: dict[str, list[dict[str, Any]]],
    target_storage: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Discover Firehose delivery streams in the target region."""
    results: list[dict[str, Any]] = []

    source_firehoses: dict[str, str] = {}  # stream_name -> storage_type
    for storage_type, configs in source_storage.items():
        for cfg in configs:
            if cfg.get("StorageType") == "KINESIS_FIREHOSE":
                arn = cfg.get("KinesisFirehoseConfig", {}).get("FirehoseArn", "")
                if arn:
                    name = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]
                    source_firehoses[name] = storage_type

    if not source_firehoses:
        return results

    target_firehose_arns: set[str] = set()
    for configs in target_storage.values():
        for cfg in configs:
            if cfg.get("StorageType") == "KINESIS_FIREHOSE":
                arn = cfg.get("KinesisFirehoseConfig", {}).get("FirehoseArn", "")
                if arn:
                    target_firehose_arns.add(arn)

    try:
        firehose_client = create_target_client("firehose", target_region)
        resp = firehose_client.list_delivery_streams(Limit=100)
        for stream_name in resp.get("DeliveryStreamNames", []):
            if stream_name in source_firehoses:
                try:
                    desc = firehose_client.describe_delivery_stream(
                        DeliveryStreamName=stream_name
                    )
                    stream_arn = desc.get("DeliveryStreamDescription", {}).get(
                        "DeliveryStreamARN", ""
                    )
                except ClientError:
                    stream_arn = ""

                is_associated = stream_arn in target_firehose_arns if stream_arn else False
                results.append({
                    "name": stream_name,
                    "resource_type": "KINESIS_FIREHOSE",
                    "arn": stream_arn or f"arn:aws:firehose:{target_region}:*:deliverystream/{stream_name}",
                    "source_match": stream_name,
                    "source_arn": "",
                    "storage_type": source_firehoses[stream_name],
                    "association_status": "already_associated" if is_associated else "not_associated",
                })
    except ClientError:
        logger.debug("Could not list Firehose streams in target region", exc_info=True)

    return results


# ---------------------------------------------------------------------------
# S3 bucket discovery
# ---------------------------------------------------------------------------

def _discover_s3_buckets(
    source_region: str,
    target_region: str,
    source_storage: dict[str, list[dict[str, Any]]],
    target_storage: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Discover S3 buckets that match source storage config buckets."""
    results: list[dict[str, Any]] = []

    source_buckets: dict[str, list[str]] = {}  # bucket_name -> [storage_types]
    for storage_type, configs in source_storage.items():
        for cfg in configs:
            if cfg.get("StorageType") == "S3":
                bucket = cfg.get("S3Config", {}).get("BucketName", "")
                if bucket:
                    source_buckets.setdefault(bucket, []).append(storage_type)

    if not source_buckets:
        return results

    target_bucket_names: set[str] = set()
    for configs in target_storage.values():
        for cfg in configs:
            if cfg.get("StorageType") == "S3":
                bucket = cfg.get("S3Config", {}).get("BucketName", "")
                if bucket:
                    target_bucket_names.add(bucket)

    # Check if source buckets exist (S3 is global)
    try:
        s3_client = create_target_client("s3", target_region)
        for source_bucket, storage_types in source_buckets.items():
            # Try to find a DR bucket with similar name
            # Common patterns: bucket-name-dr, bucket-name-us-west-2
            dr_candidates = [
                source_bucket,
                f"{source_bucket}-dr",
                f"{source_bucket}-{target_region}",
            ]
            # Also try replacing source region in bucket name
            dr_candidates.append(source_bucket.replace(source_region, target_region))

            for candidate in dr_candidates:
                try:
                    s3_client.head_bucket(Bucket=candidate)
                    is_associated = candidate in target_bucket_names
                    results.append({
                        "name": candidate,
                        "resource_type": "S3_BUCKET",
                        "arn": f"arn:aws:s3:::{candidate}",
                        "source_match": source_bucket,
                        "source_arn": f"arn:aws:s3:::{source_bucket}",
                        "storage_types": storage_types,
                        "association_status": "already_associated" if is_associated else "not_associated",
                    })
                    break  # Found a match, stop checking candidates
                except ClientError:
                    continue
    except Exception:
        logger.debug("Could not discover S3 buckets", exc_info=True)

    return results


# ---------------------------------------------------------------------------
# KVS (media streams) discovery
# ---------------------------------------------------------------------------

def _discover_kvs_config(
    target_region: str,
    source_storage: dict[str, list[dict[str, Any]]],
    target_storage: dict[str, list[dict[str, Any]]],
    instance_id: str,
) -> list[dict[str, Any]]:
    """Check if MEDIA_STREAMS storage config exists in source but not target."""
    results: list[dict[str, Any]] = []

    source_kvs = source_storage.get("MEDIA_STREAMS", [])
    target_kvs = target_storage.get("MEDIA_STREAMS", [])

    if not source_kvs:
        return results

    for cfg in source_kvs:
        if cfg.get("StorageType") != "KINESIS_VIDEO_STREAM":
            continue
        kvs_config = cfg.get("KinesisVideoStreamConfig", {})
        prefix = kvs_config.get("Prefix", "")
        retention = kvs_config.get("RetentionPeriodHours", 0)

        is_associated = len(target_kvs) > 0
        results.append({
            "name": f"KVS: {prefix}",
            "resource_type": "KINESIS_VIDEO_STREAM",
            "arn": f"kvs-config:{target_region}:{prefix}",
            "source_match": prefix,
            "source_arn": "",
            "kvs_prefix": prefix,
            "retention_hours": retention,
            "association_status": "already_associated" if is_associated else "not_associated",
        })

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_name_from_arn(arn: str) -> str:
    """Extract a human-readable name from an ARN."""
    if "/" in arn:
        return arn.split("/")[-1]
    parts = arn.split(":")
    if len(parts) >= 7:
        return parts[6]
    return arn
