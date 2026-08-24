"""Associate replicated resources with the DR Connect instance.

After replication creates resources in the target region, this module:
1. Reads the source instance's storage configs to determine correct mappings
2. Checks which resources are already associated/enabled on the DR instance
3. Enables required instance attributes (data streaming, etc.)
4. Associates each replicated resource with the DR Connect instance

Covers: Lambda, Amazon Lex V2 bots, S3 (storage configs), Amazon Kinesis Data Streams,
Amazon Data Firehose, and Amazon Kinesis Video Streams (media streaming).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from botocore.exceptions import ClientError

from aws.client_factory import create_source_client, create_target_client
from models.enums import ReplicationStatus, ResourceType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source storage config reader — determines correct storage type mappings
# ---------------------------------------------------------------------------

def _read_source_storage_configs(
    source_region: str, instance_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Read all storage configs from the source Connect instance.

    Returns a dict keyed by resource type (CONTACT_TRACE_RECORDS, AGENT_EVENTS,
    CALL_RECORDINGS, etc.) with lists of storage config dicts.
    """
    source_connect = create_source_client("connect", source_region)
    all_types = [
        "CALL_RECORDINGS", "CHAT_TRANSCRIPTS", "SCHEDULED_REPORTS",
        "CONTACT_TRACE_RECORDS", "AGENT_EVENTS",
        "MEDIA_STREAMS", "ATTACHMENTS", "SCREEN_RECORDINGS",
    ]
    result: dict[str, list[dict[str, Any]]] = {}
    for storage_type in all_types:
        try:
            resp = source_connect.list_instance_storage_configs(
                InstanceId=instance_id, ResourceType=storage_type,
            )
            configs = resp.get("StorageConfigs", [])
            if configs:
                result[storage_type] = configs
        except ClientError:
            pass
    return result


def _build_source_mappings(
    source_configs: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]]:
    """Build mappings from source storage configs.

    Returns:
        s3_mapping: bucket_name -> list of storage types (e.g. CALL_RECORDINGS)
        kinesis_mapping: stream_arn -> list of storage types
        firehose_mapping: firehose_arn -> list of storage types
    """
    s3_mapping: dict[str, list[str]] = {}
    kinesis_mapping: dict[str, list[str]] = {}
    firehose_mapping: dict[str, list[str]] = {}

    for storage_type, configs in source_configs.items():
        for config in configs:
            st = config.get("StorageType", "")
            if st == "S3":
                bucket = config.get("S3Config", {}).get("BucketName", "")
                prefix = config.get("S3Config", {}).get("BucketPrefix", "")
                if bucket:
                    s3_mapping.setdefault(bucket, [])
                    if storage_type not in s3_mapping[bucket]:
                        s3_mapping[bucket].append(storage_type)
            elif st == "KINESIS_STREAM":
                arn = config.get("KinesisStreamConfig", {}).get("StreamArn", "")
                if arn:
                    kinesis_mapping.setdefault(arn, [])
                    if storage_type not in kinesis_mapping[arn]:
                        kinesis_mapping[arn].append(storage_type)
            elif st == "KINESIS_FIREHOSE":
                arn = config.get("KinesisFirehoseConfig", {}).get("FirehoseArn", "")
                if arn:
                    firehose_mapping.setdefault(arn, [])
                    if storage_type not in firehose_mapping[arn]:
                        firehose_mapping[arn].append(storage_type)

    return s3_mapping, kinesis_mapping, firehose_mapping


# ---------------------------------------------------------------------------
# Result merging
# ---------------------------------------------------------------------------

def merge_association_results(
    existing: list[dict[str, Any]] | None,
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge a fresh association run into the previously persisted results.

    An association run only produces results for resources it actually
    attempts — resources that are not yet REPLICATED at click-time are
    skipped. Overwriting wholesale would then wipe the status of resources
    that were successfully associated in an earlier run (making them look
    "not attempted"). Instead we keep prior results for any resource this
    run did not touch, and let the new run's results win for those it did.

    Keyed by (resource, resource_type). All entries from ``new`` are kept
    (S3 buckets legitimately produce one entry per storage type).
    """
    if not existing:
        return new
    new_keys = {(r.get("resource"), r.get("resource_type")) for r in new}
    merged = [
        r for r in existing
        if (r.get("resource"), r.get("resource_type")) not in new_keys
    ]
    merged.extend(new)
    return merged


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def associate_resources(
    session: Any,
) -> list[dict[str, Any]]:
    """Associate all replicated resources with the DR Connect instance.

    Reads the source instance's storage configs to determine the correct
    storage type for each resource, then associates them on the DR instance.

    Args:
        session: The current session with inventory and instance info.

    Returns:
        A list of result dicts, one per resource attempted.
    """
    from aws.arn_utils import parse_arn

    results: list[dict[str, Any]] = []

    # Parse instance info
    try:
        parsed = parse_arn(session.instance_arn)
    except ValueError:
        return [{"resource": "instance", "status": "error",
                 "error": f"Invalid instance ARN: {session.instance_arn}"}]

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        return [{"resource": "instance", "status": "error",
                 "error": "ARN is not a Connect instance"}]

    instance_id = resource_part.split("/", 1)[1]
    target_region = session.target_region
    source_region = session.source_region

    # Verify the replica instance exists and is active
    try:
        target_connect = create_target_client("connect", target_region)
        resp = target_connect.describe_instance(InstanceId=instance_id)
        instance_status = resp.get("Instance", {}).get("InstanceStatus", "")
        if instance_status != "ACTIVE":
            return [{"resource": "instance", "status": "error",
                     "error": f"DR instance is not ACTIVE (status: {instance_status})"}]
    except ClientError as exc:
        return [{"resource": "instance", "status": "error",
                 "error": f"Cannot reach DR instance: {exc}"}]

    # Step 1: Read source storage configs to determine correct mappings
    source_configs = _read_source_storage_configs(source_region, instance_id)
    s3_type_map, kinesis_type_map, firehose_type_map = _build_source_mappings(source_configs)
    logger.info(
        "Source storage config mappings: S3=%s, Kinesis=%s, Firehose=%s",
        s3_type_map, kinesis_type_map, firehose_type_map,
    )

    # Step 2: Enable instance attributes as needed
    attr_results = _enable_instance_attributes(
        target_connect, instance_id,
        source_region=source_region,
        source_instance_id=instance_id,
    )
    results.extend(attr_results)

    # Step 3: Get currently associated resources for dedup
    existing_lambdas = _list_associated_lambdas(target_connect, instance_id)
    existing_bots = _list_associated_bots(target_connect, instance_id)

    # Step 4: Associate each replicated resource by type
    for rid, resource in session.inventory.items():
        if resource.status != ReplicationStatus.REPLICATED:
            continue
        if not resource.replicated_arn:
            # For Amazon Lex bots that are REPLICATED (e.g. ALGR replica exists) but
            # were not in the selected replication job, try to resolve the ARN
            # on-the-fly so association can proceed.
            if resource.resource_type == ResourceType.LEX_BOT:
                resolved = _resolve_lex_bot_arn(resource, target_region, source_region)
                if resolved:
                    resource.replicated_arn = resolved
                    logger.info(
                        "Resolved missing replicated_arn for Lex bot '%s': %s",
                        resource.name, resolved,
                    )
                else:
                    logger.warning(
                        "Lex bot '%s' is REPLICATED but has no replicated_arn and "
                        "could not be resolved — skipping association",
                        resource.name,
                    )
                    continue
            else:
                continue

        rtype = resource.resource_type

        if rtype == ResourceType.LAMBDA:
            # Skip Lex codehook/fulfillment lambdas — they are invoked by
            # the Amazon Lex bot, not directly by Connect contact flows.
            if getattr(resource, 'is_lex_codehook', False):
                results.append({
                    "resource": resource.name,
                    "resource_type": "LAMBDA",
                    "replicated_arn": resource.replicated_arn,
                    "status": "skipped",
                    "message": "Lex codehook Lambda — invoked by Lex bot, not Connect",
                })
                continue
            result = _associate_lambda(
                target_connect, instance_id, resource, existing_lambdas
            )
            results.append(result)

        elif rtype == ResourceType.LEX_BOT:
            result = _associate_lex_bot(
                target_connect, instance_id, resource, existing_bots, target_region,
                source_region=source_region,
            )
            results.append(result)

        elif rtype == ResourceType.KINESIS_STREAM:
            result = _associate_kinesis_stream(
                target_connect, instance_id, resource,
                kinesis_type_map=kinesis_type_map,
                source_region=source_region,
            )
            results.append(result)

        elif rtype == ResourceType.KINESIS_FIREHOSE:
            result = _associate_firehose(
                target_connect, instance_id, resource,
                firehose_type_map=firehose_type_map,
                source_region=source_region,
            )
            results.append(result)

        elif rtype == ResourceType.KINESIS_VIDEO_STREAM:
            result = _associate_kvs_stream(
                target_connect, instance_id, resource
            )
            results.append(result)

        elif rtype == ResourceType.S3_BUCKET:
            sub_results = _associate_s3_bucket(
                target_connect, instance_id, resource,
                s3_type_map=s3_type_map,
                source_configs=source_configs,
            )
            results.extend(sub_results)

    # Step 5: Replicate approved origins from source to replica
    origin_results = _replicate_approved_origins(
        source_region, target_region, instance_id
    )
    results.extend(origin_results)

    # Step 6: Replicate Wisdom/Q in Connect domains
    try:
        from association.wisdom_replication import replicate_wisdom_domains
        wisdom_results = replicate_wisdom_domains(
            source_region, target_region, instance_id,
        )
        results.extend(wisdom_results)
    except Exception as exc:
        logger.warning("Wisdom replication failed: %s", exc, exc_info=True)
        results.append({
            "resource": "wisdom_domains",
            "resource_type": "WISDOM_ASSISTANT",
            "status": "error",
            "error": f"Wisdom replication failed: {exc}",
        })

    return results


def associate_single_resource(
    session: Any,
    resource_id: str,
) -> dict[str, Any]:
    """Associate a single replicated resource with the DR Connect instance.

    Used for per-resource retry from the Session Status page.

    Args:
        session: The current session with inventory and instance info.
        resource_id: The ID of the resource to associate.

    Returns:
        A result dict for the single resource.
    """
    from aws.arn_utils import parse_arn

    resource = session.inventory.get(resource_id)
    if resource is None:
        return {"resource": resource_id, "status": "error", "error": "Resource not found in session"}
    if resource.status != ReplicationStatus.REPLICATED:
        return {"resource": resource.name, "status": "error", "error": f"Resource is not replicated (status: {resource.status})"}
    if not resource.replicated_arn:
        # For Amazon Lex bots, try to resolve the ARN on-the-fly
        if resource.resource_type == ResourceType.LEX_BOT:
            resolved = _resolve_lex_bot_arn(resource, session.target_region, session.source_region)
            if resolved:
                resource.replicated_arn = resolved
            else:
                return {"resource": resource.name, "status": "error", "error": "No replicated ARN available and could not resolve Lex bot in target region"}
        else:
            return {"resource": resource.name, "status": "error", "error": "No replicated ARN available"}

    try:
        parsed = parse_arn(session.instance_arn)
    except ValueError:
        return {"resource": resource.name, "status": "error", "error": f"Invalid instance ARN: {session.instance_arn}"}

    resource_part = parsed["resource"]
    if not resource_part.startswith("instance/"):
        return {"resource": resource.name, "status": "error", "error": "ARN is not a Connect instance"}

    instance_id = resource_part.split("/", 1)[1]
    target_region = session.target_region
    source_region = session.source_region

    try:
        target_connect = create_target_client("connect", target_region)
    except Exception as exc:
        return {"resource": resource.name, "status": "error", "error": f"Cannot create Connect client: {exc}"}

    source_configs = _read_source_storage_configs(source_region, instance_id)
    s3_type_map, kinesis_type_map, firehose_type_map = _build_source_mappings(source_configs)

    rtype = resource.resource_type

    if rtype == ResourceType.LAMBDA:
        existing_lambdas = _list_associated_lambdas(target_connect, instance_id)
        return _associate_lambda(target_connect, instance_id, resource, existing_lambdas)
    elif rtype == ResourceType.LEX_BOT:
        existing_bots = _list_associated_bots(target_connect, instance_id)
        return _associate_lex_bot(target_connect, instance_id, resource, existing_bots, target_region, source_region=source_region)
    elif rtype == ResourceType.KINESIS_STREAM:
        return _associate_kinesis_stream(target_connect, instance_id, resource, kinesis_type_map=kinesis_type_map, source_region=source_region)
    elif rtype == ResourceType.KINESIS_FIREHOSE:
        return _associate_firehose(target_connect, instance_id, resource, firehose_type_map=firehose_type_map, source_region=source_region)
    elif rtype == ResourceType.KINESIS_VIDEO_STREAM:
        return _associate_kvs_stream(target_connect, instance_id, resource)
    elif rtype == ResourceType.S3_BUCKET:
        sub_results = _associate_s3_bucket(target_connect, instance_id, resource, s3_type_map=s3_type_map, source_configs=source_configs)
        return sub_results[0] if sub_results else {"resource": resource.name, "status": "error", "error": "No S3 association result"}
    else:
        return {"resource": resource.name, "status": "error", "error": f"Unsupported resource type: {rtype}"}


# ---------------------------------------------------------------------------
# Instance attributes
# ---------------------------------------------------------------------------

def _enable_instance_attributes(
    connect_client, instance_id: str,
    source_region: str | None = None,
    source_instance_id: str | None = None,
) -> list[dict[str, Any]]:
    """Enable required instance attributes on the DR Connect instance.

    Always-enabled attributes (CONTACTFLOW_LOGS, CONTACT_LENS, EARLY_MEDIA)
    are enabled unconditionally.  Lex-related attributes are mirrored from
    the source instance: if enabled on source they are enabled on the
    replica, otherwise they are left untouched.
    """
    results: list[dict[str, Any]] = []

    # --- Always-enable attributes -------------------------------------------
    always_enable = [
        ("CONTACTFLOW_LOGS", "Contact Flow Logs"),
        ("CONTACT_LENS", "Contact Lens"),
        ("EARLY_MEDIA", "Early Media"),
    ]

    for attr_type, display_name in always_enable:
        try:
            resp = connect_client.describe_instance_attribute(
                InstanceId=instance_id,
                AttributeType=attr_type,
            )
            current_value = resp.get("Attribute", {}).get("Value", "false")

            if current_value.lower() == "true":
                results.append({
                    "resource": f"Instance Attribute: {display_name}",
                    "resource_type": "INSTANCE_ATTRIBUTE",
                    "status": "already_enabled",
                    "message": f"{display_name} is already enabled",
                })
            else:
                try:
                    connect_client.update_instance_attribute(
                        InstanceId=instance_id,
                        AttributeType=attr_type,
                        Value="true",
                    )
                    results.append({
                        "resource": f"Instance Attribute: {display_name}",
                        "resource_type": "INSTANCE_ATTRIBUTE",
                        "status": "enabled",
                        "message": f"{display_name} has been enabled",
                    })
                    logger.info("Enabled %s on instance %s", attr_type, instance_id)
                except ClientError as exc:
                    results.append({
                        "resource": f"Instance Attribute: {display_name}",
                        "resource_type": "INSTANCE_ATTRIBUTE",
                        "status": "error",
                        "error": str(exc),
                    })
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("InvalidParameterException", "InvalidRequestException"):
                logger.debug("Attribute %s not supported on this instance", attr_type)
            else:
                results.append({
                    "resource": f"Instance Attribute: {display_name}",
                    "resource_type": "INSTANCE_ATTRIBUTE",
                    "status": "error",
                    "error": str(exc),
                })

    # --- Mirror Lex-related attributes from source --------------------------
    # The three Lex-related instance attributes visible in the Connect console
    # under "Amazon Lex Bots" settings:
    #   1. Enable Lex Bot Management → BOT_MANAGEMENT
    #   2. Enable Bot Analytics and Transcripts → ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS
    #   3. Enable message streaming → MESSAGE_STREAMING
    lex_mirror_attrs = [
        ("BOT_MANAGEMENT", "Lex Bot Management"),
        ("ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS", "Bot Analytics and Transcripts"),
        ("MESSAGE_STREAMING", "Message Streaming"),
    ]

    src_instance_id = source_instance_id or instance_id
    if source_region:
        try:
            source_connect = create_source_client("connect", source_region)
        except Exception:
            logger.warning("Could not create source client for region %s — skipping Lex attribute mirroring", source_region)
            source_connect = None
    else:
        source_connect = None

    for attr_type, display_name in lex_mirror_attrs:
        # Read source value
        source_enabled = False
        if source_connect:
            try:
                src_resp = source_connect.describe_instance_attribute(
                    InstanceId=src_instance_id,
                    AttributeType=attr_type,
                )
                source_enabled = src_resp.get("Attribute", {}).get("Value", "false").lower() == "true"
            except ClientError:
                logger.debug("Attribute %s not available on source instance", attr_type)
                continue
            except Exception:
                logger.debug("Could not read attribute %s from source", attr_type)
                continue
        else:
            # No source client — skip mirroring
            continue

        if not source_enabled:
            results.append({
                "resource": f"Instance Attribute: {display_name}",
                "resource_type": "INSTANCE_ATTRIBUTE",
                "status": "skipped",
                "message": f"{display_name} is not enabled on source — skipping",
            })
            continue

        # Source is enabled — ensure replica matches
        try:
            tgt_resp = connect_client.describe_instance_attribute(
                InstanceId=instance_id,
                AttributeType=attr_type,
            )
            tgt_value = tgt_resp.get("Attribute", {}).get("Value", "false")
            if tgt_value.lower() == "true":
                results.append({
                    "resource": f"Instance Attribute: {display_name}",
                    "resource_type": "INSTANCE_ATTRIBUTE",
                    "status": "already_enabled",
                    "message": f"{display_name} already enabled on replica (mirrored from source)",
                })
            else:
                connect_client.update_instance_attribute(
                    InstanceId=instance_id,
                    AttributeType=attr_type,
                    Value="true",
                )
                results.append({
                    "resource": f"Instance Attribute: {display_name}",
                    "resource_type": "INSTANCE_ATTRIBUTE",
                    "status": "enabled",
                    "message": f"{display_name} enabled on replica (mirrored from source)",
                })
                logger.info("Mirrored %s from source to replica instance %s", attr_type, instance_id)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("InvalidParameterException", "InvalidRequestException"):
                logger.debug("Attribute %s not supported on replica instance", attr_type)
            else:
                results.append({
                    "resource": f"Instance Attribute: {display_name}",
                    "resource_type": "INSTANCE_ATTRIBUTE",
                    "status": "error",
                    "error": str(exc),
                })

    return results


# ---------------------------------------------------------------------------
# Helpers — list what is already associated on the target instance
# ---------------------------------------------------------------------------

def _list_associated_lambdas(connect_client, instance_id: str) -> set[str]:
    """List Lambda function ARNs currently associated with the instance."""
    arns: set[str] = set()
    try:
        params: dict = {"InstanceId": instance_id, "MaxResults": 25}
        while True:
            resp = connect_client.list_lambda_functions(**params)
            for arn in resp.get("LambdaFunctions", []):
                arns.add(arn)
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except ClientError:
        logger.debug("Could not list associated Lambda functions", exc_info=True)
    return arns


def _list_associated_bots(connect_client, instance_id: str) -> set[str]:
    """List Lex bot alias ARNs currently associated with the instance."""
    arns: set[str] = set()
    try:
        params: dict = {"InstanceId": instance_id, "MaxResults": 25, "LexVersion": "V2"}
        while True:
            resp = connect_client.list_bots(**params)
            for bot_summary in resp.get("LexBots", []):
                v2 = bot_summary.get("LexV2Bot", {})
                alias_arn = v2.get("AliasArn", "")
                if alias_arn:
                    arns.add(alias_arn)
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except ClientError:
        logger.debug("Could not list associated bots", exc_info=True)
    return arns


# ---------------------------------------------------------------------------
# Lambda association
# ---------------------------------------------------------------------------

def _associate_lambda(
    connect_client, instance_id: str, resource: Any, existing: set[str]
) -> dict[str, Any]:
    """Associate a replicated Lambda function with the DR Connect instance."""
    func_arn = resource.replicated_arn

    if func_arn in existing:
        return {
            "resource": resource.name,
            "resource_type": "LAMBDA",
            "replicated_arn": func_arn,
            "status": "already_associated",
            "message": "Lambda function is already associated with the DR instance",
        }

    # Ensure the Lambda has a resource-based policy allowing Connect to invoke it
    _ensure_lambda_connect_permission(func_arn, instance_id)

    last_error = ""
    for attempt in range(2):
        try:
            if attempt > 0:
                time.sleep(5)  # nosemgrep: arbitrary-sleep — retry backoff (max 1 retry / 5s) before Lambda association reattempt
            connect_client.associate_lambda_function(
                InstanceId=instance_id,
                FunctionArn=func_arn,
            )
            return {
                "resource": resource.name,
                "resource_type": "LAMBDA",
                "replicated_arn": func_arn,
                "status": "associated",
                "message": "Lambda function associated with DR instance",
            }
        except ClientError as exc:
            error_msg = str(exc)
            if "already" in error_msg.lower() or "duplicate" in error_msg.lower():
                return {
                    "resource": resource.name,
                    "resource_type": "LAMBDA",
                    "replicated_arn": func_arn,
                    "status": "already_associated",
                    "message": "Lambda function was already associated",
                }
            last_error = error_msg

    return {
        "resource": resource.name,
        "resource_type": "LAMBDA",
        "replicated_arn": func_arn,
        "status": "error",
        "error": last_error,
    }


def _ensure_lambda_connect_permission(func_arn: str, instance_id: str) -> None:
    """Ensure the Lambda function has a resource-based policy allowing Connect to invoke it."""
    try:
        arn_parts = func_arn.split(":")
        if len(arn_parts) < 5:
            return
        lambda_region = arn_parts[3]
        account_id = arn_parts[4]

        lambda_client = create_target_client("lambda", lambda_region)
        connect_instance_arn = f"arn:aws:connect:{lambda_region}:{account_id}:instance/{instance_id}"
        statement_id = f"connect-{instance_id}"

        # Check if permission already exists
        try:
            resp = lambda_client.get_policy(FunctionName=func_arn)
            policy = json.loads(resp.get("Policy", "{}"))
            for stmt in policy.get("Statement", []):
                if stmt.get("Sid") == statement_id:
                    logger.debug("Connect invoke permission already exists on '%s'", func_arn)
                    return
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                pass
            else:
                logger.debug("Could not check Lambda policy: %s", e)

        lambda_client.add_permission(
            FunctionName=func_arn,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal="connect.amazonaws.com",
            SourceAccount=account_id,
            SourceArn=connect_instance_arn,
        )
        logger.info("Added Connect invoke permission to Lambda '%s'", func_arn)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceConflictException":
            logger.debug("Connect permission already exists on Lambda '%s'", func_arn)
        else:
            logger.warning("Failed to add Connect permission to Lambda '%s': %s", func_arn, exc)
    except Exception:
        logger.warning("Failed to add Connect permission to Lambda '%s'", func_arn, exc_info=True)


# ---------------------------------------------------------------------------
# Amazon Lex bot association
# ---------------------------------------------------------------------------

def _ensure_lex_connect_permission(alias_arn: str, instance_id: str, target_region: str) -> None:
    """Ensure the Lex bot alias has a resource policy allowing Connect to invoke it."""
    try:
        arn_parts = alias_arn.split(":")
        if len(arn_parts) < 5:
            return
        account_id = arn_parts[4]

        lex_client = create_target_client("lexv2-models", target_region)
        connect_instance_arn = f"arn:aws:connect:{target_region}:{account_id}:instance/{instance_id}"

        # Check if a policy already exists
        try:
            resp = lex_client.describe_resource_policy(resourceArn=alias_arn)
            existing_policy = json.loads(resp.get("policy", "{}"))
            for stmt in existing_policy.get("Statement", []):
                principal = stmt.get("Principal", {})
                svc = principal.get("Service", "") if isinstance(principal, dict) else ""
                if svc == "connect.amazonaws.com":
                    logger.debug("Connect permission already exists on Lex alias '%s'", alias_arn)
                    return
            # Add Connect statement to existing policy
            existing_policy.setdefault("Statement", []).append({
                "Sid": f"connect-{instance_id}",
                "Effect": "Allow",
                "Principal": {"Service": "connect.amazonaws.com"},
                "Action": [
                    "lex:RecognizeText",
                    "lex:StartConversation",
                    "lex:RecognizeMessageAsync",
                ],
                "Resource": alias_arn,
                "Condition": {
                    "StringEquals": {"AWS:SourceAccount": account_id},
                    "ArnEquals": {"AWS:SourceArn": connect_instance_arn},
                },
            })
            lex_client.update_resource_policy(
                resourceArn=alias_arn,
                policy=json.dumps(existing_policy),
                expectedRevisionId=resp.get("revisionId"),
            )
            logger.info("Updated resource policy on Lex alias '%s'", alias_arn)
            return
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                pass
            else:
                logger.debug("Could not check Lex resource policy: %s", e)

        # Create a new resource policy
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": f"connect-{instance_id}",
                "Effect": "Allow",
                "Principal": {"Service": "connect.amazonaws.com"},
                "Action": [
                    "lex:RecognizeText",
                    "lex:StartConversation",
                    "lex:RecognizeMessageAsync",
                ],
                "Resource": alias_arn,
                "Condition": {
                    "StringEquals": {"AWS:SourceAccount": account_id},
                    "ArnEquals": {"AWS:SourceArn": connect_instance_arn},
                },
            }],
        }
        lex_client.create_resource_policy(
            resourceArn=alias_arn,
            policy=json.dumps(policy),
        )
        logger.info("Created resource policy on Lex alias '%s'", alias_arn)
    except ClientError as exc:
        logger.warning("Failed to set Lex resource policy on '%s': %s", alias_arn, exc)
    except Exception:
        logger.warning("Failed to set Lex resource policy on '%s'", alias_arn, exc_info=True)


def _associate_lex_bot(
    connect_client, instance_id: str, resource: Any,
    existing: set[str], target_region: str, source_region: str | None = None,
) -> dict[str, Any]:
    """Associate a replicated Lex V2 bot with the DR Connect instance."""
    bot_arn = resource.replicated_arn

    alias_arn = _get_bot_alias_arn(
        bot_arn, target_region,
        source_region=source_region,
        source_bot_arn=getattr(resource, 'arn', None),
    )
    if not alias_arn:
        # Check if the bot itself exists in the target region (ALGR replica present
        # but alias not yet replicated — this is an async process that can take ~80 min)
        bot_exists = False
        try:
            parts = bot_arn.split("/")
            if len(parts) >= 2:
                bot_id = parts[-1]
                lex_client = create_target_client("lexv2-models", target_region)
                lex_client.describe_bot(botId=bot_id)
                bot_exists = True
        except Exception:
            pass

        if bot_exists:
            return {
                "resource": resource.name,
                "resource_type": "LEX_BOT",
                "replicated_arn": bot_arn,
                "status": "pending",
                "message": "Bot alias is still replicating via ALGR. "
                           "Retry association in a few minutes.",
                "retryable": True,
            }
        return {
            "resource": resource.name,
            "resource_type": "LEX_BOT",
            "replicated_arn": bot_arn,
            "status": "error",
            "error": f"Could not resolve a bot alias ARN for bot {resource.name}. "
                     f"Connect requires a bot-alias ARN.",
        }

    if alias_arn in existing:
        return {
            "resource": resource.name,
            "resource_type": "LEX_BOT",
            "replicated_arn": alias_arn,
            "status": "already_associated",
            "message": "Lex bot is already associated with the DR instance",
        }

    _ensure_lex_connect_permission(alias_arn, instance_id, target_region)

    try:
        connect_client.associate_bot(
            InstanceId=instance_id,
            LexV2Bot={"AliasArn": alias_arn},
        )
        return {
            "resource": resource.name,
            "resource_type": "LEX_BOT",
            "replicated_arn": alias_arn,
            "status": "associated",
            "message": "Lex bot associated with DR instance",
        }
    except ClientError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "duplicate" in error_msg.lower():
            return {
                "resource": resource.name,
                "resource_type": "LEX_BOT",
                "replicated_arn": alias_arn,
                "status": "already_associated",
                "message": "Lex bot was already associated",
            }
        return {
            "resource": resource.name,
            "resource_type": "LEX_BOT",
            "replicated_arn": alias_arn,
            "status": "error",
            "error": error_msg,
        }


# ---------------------------------------------------------------------------
# Amazon Lex bot alias resolution helpers
# ---------------------------------------------------------------------------

def _find_latest_bot_version(lex_client, bot_id: str) -> str | None:
    """Find the latest numeric (built) bot version."""
    try:
        resp = lex_client.list_bot_versions(
            botId=bot_id, maxResults=20,
            sortBy={"attribute": "BotVersion", "order": "Descending"},
        )
        for v in resp.get("botVersionSummaries", []):
            version = v.get("botVersion", "")
            if version and version != "DRAFT" and version.isdigit():
                return version
    except Exception:
        logger.debug("Could not list bot versions for %s", bot_id, exc_info=True)
    return None


def _create_alias_on_source_bot(
    bot_id: str, source_region: str, target_region: str, target_alias_arn_prefix: str,
) -> str | None:
    """Create a bot version and alias on the source bot, then wait for replication."""
    try:
        source_lex = create_target_client("lexv2-models", source_region)

        # Find or create a built version
        bot_version = _find_latest_bot_version(source_lex, bot_id)
        if not bot_version:
            logger.info("No built version for source bot '%s' — building one", bot_id)
            try:
                locales_resp = source_lex.list_bot_locales(
                    botId=bot_id, botVersion="DRAFT", maxResults=10,
                )
                locale_ids = [
                    loc["localeId"]
                    for loc in locales_resp.get("botLocaleSummaries", [])
                ]
                if not locale_ids:
                    locale_ids = ["en_US"]

                for locale_id in locale_ids:
                    try:
                        source_lex.build_bot_locale(
                            botId=bot_id, botVersion="DRAFT", localeId=locale_id,
                        )
                    except ClientError:
                        logger.debug("Build locale %s may already be built", locale_id)

                # Wait for locale build
                for _ in range(12):
                    time.sleep(5)  # nosemgrep: arbitrary-sleep — bounded polling loop (max 12 iterations / 60s) awaiting Lex locale build
                    locales_resp = source_lex.list_bot_locales(
                        botId=bot_id, botVersion="DRAFT", maxResults=10,
                    )
                    all_built = all(
                        loc.get("botLocaleStatus") in ("Built", "ReadyExpressTesting")
                        for loc in locales_resp.get("botLocaleSummaries", [])
                    )
                    if all_built:
                        break

                version_resp = source_lex.create_bot_version(
                    botId=bot_id,
                    botVersionLocaleSpecification={
                        locale_id: {"sourceBotVersion": "DRAFT"}
                        for locale_id in locale_ids
                    },
                    description="Auto-created version for Connect association",
                )
                bot_version = version_resp.get("botVersion")

                # Wait for version to become Available
                for _ in range(12):
                    time.sleep(5)  # nosemgrep: arbitrary-sleep — bounded polling loop (max 12 iterations / 60s) awaiting Lex bot version Available
                    ver_resp = source_lex.describe_bot_version(
                        botId=bot_id, botVersion=bot_version,
                    )
                    if ver_resp.get("botStatus") == "Available":
                        break
            except Exception as build_exc:
                logger.warning("Failed to build bot version for '%s': %s", bot_id, build_exc)
                return None

        if not bot_version:
            return None

        # Create alias on source bot
        bot_desc = source_lex.describe_bot(botId=bot_id)
        bot_name = bot_desc.get("botName", bot_id)
        alias_name = f"{bot_name}-connect"

        try:
            create_resp = source_lex.create_bot_alias(
                botId=bot_id,
                botAliasName=alias_name,
                botVersion=bot_version,
                description=f"Alias for Connect association of replicated bot {bot_name}",
            )
            new_alias_id = create_resp.get("botAliasId", "")
        except ClientError as alias_exc:
            error_code = alias_exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ConflictException", "PreconditionFailedException"):
                resp = source_lex.list_bot_aliases(botId=bot_id, maxResults=10)
                new_alias_id = ""
                for alias in resp.get("botAliasSummaries", []):
                    aid = alias.get("botAliasId", "")
                    if aid and aid != "TSTALIASID":
                        new_alias_id = aid
                        break
            else:
                logger.warning("Failed to create alias on source bot '%s': %s", bot_id, alias_exc)
                return None

        if not new_alias_id:
            return None

        # Wait for alias to replicate to target region
        target_lex = create_target_client("lexv2-models", target_region)
        for attempt in range(12):
            time.sleep(5)  # nosemgrep: arbitrary-sleep — bounded polling loop (max 12 iterations / 60s) awaiting alias replication to target region
            try:
                replica_resp = target_lex.list_bot_aliases(botId=bot_id, maxResults=10)
                for alias in replica_resp.get("botAliasSummaries", []):
                    alias_id = alias.get("botAliasId", "")
                    if alias_id and alias_id != "TSTALIASID":
                        return f"{target_alias_arn_prefix}/{bot_id}/{alias_id}"
            except Exception:
                pass

        # Return anyway — it should replicate shortly
        return f"{target_alias_arn_prefix}/{bot_id}/{new_alias_id}"

    except Exception as exc:
        logger.warning("Failed to create alias on source bot '%s': %s", bot_id, exc)
        return None


def _get_bot_alias_arn(
    bot_arn: str, target_region: str,
    source_region: str | None = None,
    source_bot_arn: str | None = None,
) -> str | None:
    """Get the best alias ARN for a Amazon Lex V2 bot.

    Prefers non-DRAFT aliases. For ALGR replicas that only have TSTALIASID,
    creates a proper alias on the source bot.
    """
    try:
        parts = bot_arn.split("/")
        if len(parts) < 2:
            return None
        bot_id = parts[-1]

        colon_parts = bot_arn.split(":")
        if len(colon_parts) < 6:
            return None
        arn_base = ":".join(colon_parts[:5])
        alias_arn_prefix = f"{arn_base}:bot-alias"

        lex_client = create_target_client("lexv2-models", target_region)
        resp = lex_client.list_bot_aliases(botId=bot_id, maxResults=10)

        # Prefer non-test aliases
        has_test_alias = False
        for alias in resp.get("botAliasSummaries", []):
            alias_id = alias.get("botAliasId", "")
            if not alias_id:
                continue
            if alias_id != "TSTALIASID":
                return f"{alias_arn_prefix}/{bot_id}/{alias_id}"
            else:
                has_test_alias = True

        # Only TSTALIASID — create a proper alias
        if has_test_alias:
            logger.info("Bot '%s' only has TSTALIASID — creating a proper alias", bot_id)
            try:
                bot_version = _find_latest_bot_version(lex_client, bot_id)
                bot_desc = lex_client.describe_bot(botId=bot_id)
                bot_name = bot_desc.get("botName", bot_id)
                alias_name = f"{bot_name}-connect"

                create_alias_params: dict = {
                    "botId": bot_id,
                    "botAliasName": alias_name,
                    "description": f"Alias for Connect association of replicated bot {bot_name}",
                }
                if bot_version:
                    create_alias_params["botVersion"] = bot_version

                create_resp = lex_client.create_bot_alias(**create_alias_params)
                new_alias_id = create_resp.get("botAliasId", "")
                if new_alias_id:
                    return f"{alias_arn_prefix}/{bot_id}/{new_alias_id}"
            except ClientError as alias_exc:
                error_code = alias_exc.response.get("Error", {}).get("Code", "")
                if error_code in ("ConflictException", "PreconditionFailedException"):
                    retry_resp = lex_client.list_bot_aliases(botId=bot_id, maxResults=10)
                    for alias in retry_resp.get("botAliasSummaries", []):
                        alias_id = alias.get("botAliasId", "")
                        if alias_id and alias_id != "TSTALIASID":
                            return f"{alias_arn_prefix}/{bot_id}/{alias_id}"

                # Fall back to creating alias on source bot
                if source_region:
                    result = _create_alias_on_source_bot(
                        bot_id, source_region, target_region, alias_arn_prefix,
                    )
                    if result:
                        return result

            logger.warning("Could not create a usable alias for bot '%s'", bot_id)
            return None

        return None
    except Exception:
        logger.warning("Could not list bot aliases for %s", bot_arn, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Storage config association — uses source mappings for correct types
# ---------------------------------------------------------------------------

def _resolve_lex_bot_arn(resource: Any, target_region: str, source_region: str) -> str | None:
    """Resolve a Amazon Lex bot ARN in the target region when replicated_arn is missing.

    This handles the case where a Amazon Lex bot was marked REPLICATED during discovery
    (e.g. ALGR replica already exists) but was not selected for the replication
    job, so the orchestrator never set its replicated_arn.
    """
    source_arn = getattr(resource, 'arn', '') or ''
    if not source_arn:
        return None

    try:
        parts = source_arn.split("/")
        if len(parts) < 2:
            return None
        bot_id = parts[-1]

        # Try to describe the bot in the target region
        lex_client = create_target_client("lexv2-models", target_region)
        lex_client.describe_bot(botId=bot_id)

        # Bot exists — build the target ARN
        colon_parts = source_arn.split(":")
        if len(colon_parts) >= 5:
            colon_parts[3] = target_region
            return ":".join(colon_parts)
    except ClientError:
        pass
    except Exception:
        logger.debug("Could not resolve Lex bot ARN for '%s'", resource.name, exc_info=True)
    return None


def _resolve_target_arn(source_arn: str, target_region: str) -> str:
    """Convert a source-region ARN to the equivalent target-region ARN."""
    parts = source_arn.split(":")
    if len(parts) >= 5:
        parts[3] = target_region
    return ":".join(parts)


def _associate_kinesis_stream(
    connect_client, instance_id: str, resource: Any,
    kinesis_type_map: dict[str, list[str]] | None = None,
    source_region: str | None = None,
) -> dict[str, Any]:
    """Associate a replicated Kinesis Data Stream with the DR Connect instance.

    Uses the source storage config mapping to determine the correct storage
    type (CONTACT_TRACE_RECORDS or AGENT_EVENTS).
    """
    stream_arn = resource.replicated_arn

    # Determine which storage type this stream should be associated with
    storage_type = "AGENT_EVENTS"  # safe default for Kinesis streams
    if kinesis_type_map and source_region:
        # Look up the source ARN to find the correct storage type
        source_arn = resource.arn
        types = kinesis_type_map.get(source_arn, [])
        if types:
            storage_type = types[0]

    # Check if already configured
    already = _check_storage_config_exists(
        connect_client, instance_id, storage_type, "KINESIS_STREAM", stream_arn
    )
    if already:
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_STREAM",
            "replicated_arn": stream_arn,
            "status": "already_associated",
            "message": f"Kinesis stream already configured for {storage_type}",
        }

    try:
        connect_client.associate_instance_storage_config(
            InstanceId=instance_id,
            ResourceType=storage_type,
            StorageConfig={
                "StorageType": "KINESIS_STREAM",
                "KinesisStreamConfig": {"StreamArn": stream_arn},
            },
        )
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_STREAM",
            "replicated_arn": stream_arn,
            "status": "associated",
            "message": f"Kinesis stream associated for {storage_type}",
        }
    except ClientError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "conflict" in error_msg.lower():
            return {
                "resource": resource.name,
                "resource_type": "KINESIS_STREAM",
                "replicated_arn": stream_arn,
                "status": "already_associated",
                "message": f"Kinesis stream storage config already exists for {storage_type}",
            }
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_STREAM",
            "replicated_arn": stream_arn,
            "status": "error",
            "error": error_msg,
        }


def _wait_for_firehose_active(firehose_arn: str, target_region: str, max_wait: int = 60) -> str | None:
    """Wait for a Firehose delivery stream to become ACTIVE.

    Returns the stream status if ACTIVE, or the last observed status if timeout.
    Returns None if the stream cannot be described.
    """
    # Extract stream name from ARN: arn:aws:firehose:REGION:ACCOUNT:deliverystream/NAME
    parts = firehose_arn.split("/")
    if len(parts) < 2:
        return None
    stream_name = parts[-1]

    try:
        firehose_client = create_target_client("firehose", target_region)
    except Exception:
        return None

    waited = 0
    interval = 5
    while waited < max_wait:
        try:
            resp = firehose_client.describe_delivery_stream(
                DeliveryStreamName=stream_name
            )
            status = resp.get("DeliveryStreamDescription", {}).get(
                "DeliveryStreamStatus", ""
            )
            if status == "ACTIVE":
                return "ACTIVE"
            logger.info(
                "Firehose '%s' status is %s, waiting... (%ds/%ds)",
                stream_name, status, waited, max_wait,
            )
        except ClientError:
            logger.debug("Could not describe Firehose '%s'", stream_name, exc_info=True)
            return None
        time.sleep(interval)  # nosemgrep: arbitrary-sleep — bounded polling loop awaiting Firehose ACTIVE status (max_wait capped)
        waited += interval

    return status if status else None


def _associate_firehose(
    connect_client, instance_id: str, resource: Any,
    firehose_type_map: dict[str, list[str]] | None = None,
    source_region: str | None = None,
) -> dict[str, Any]:
    """Associate a replicated Amazon Data Firehose with the DR Connect instance.

    Uses the source storage config mapping to determine the correct storage type.
    Waits for the Firehose to become ACTIVE before attempting association.
    """
    firehose_arn = resource.replicated_arn

    # Determine which storage type this firehose should be associated with
    storage_type = "CONTACT_TRACE_RECORDS"  # default for Firehose
    if firehose_type_map and source_region:
        source_arn = resource.arn
        types = firehose_type_map.get(source_arn, [])
        if types:
            storage_type = types[0]

    # Check if already configured
    already = _check_storage_config_exists(
        connect_client, instance_id, storage_type, "KINESIS_FIREHOSE", firehose_arn
    )
    if already:
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_FIREHOSE",
            "replicated_arn": firehose_arn,
            "status": "already_associated",
            "message": f"Firehose already configured for {storage_type}",
        }

    # Wait for the Firehose to become ACTIVE before associating.
    # Newly created streams may still be in CREATING state.
    target_region = None
    if firehose_arn and ":" in firehose_arn:
        arn_parts = firehose_arn.split(":")
        if len(arn_parts) > 3:
            target_region = arn_parts[3]

    if target_region:
        stream_status = _wait_for_firehose_active(firehose_arn, target_region)
        if stream_status and stream_status != "ACTIVE":
            return {
                "resource": resource.name,
                "resource_type": "KINESIS_FIREHOSE",
                "replicated_arn": firehose_arn,
                "status": "error",
                "error": f"Firehose stream is not ACTIVE (status: {stream_status}). "
                         f"Retry association once the stream is ready.",
                "retryable": True,
            }

    try:
        connect_client.associate_instance_storage_config(
            InstanceId=instance_id,
            ResourceType=storage_type,
            StorageConfig={
                "StorageType": "KINESIS_FIREHOSE",
                "KinesisFirehoseConfig": {"FirehoseArn": firehose_arn},
            },
        )
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_FIREHOSE",
            "replicated_arn": firehose_arn,
            "status": "associated",
            "message": f"Firehose associated for {storage_type}",
        }
    except ClientError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "conflict" in error_msg.lower():
            return {
                "resource": resource.name,
                "resource_type": "KINESIS_FIREHOSE",
                "replicated_arn": firehose_arn,
                "status": "already_associated",
                "message": f"Firehose storage config already exists for {storage_type}",
            }
        # If the error is about the stream not being active, mark as retryable
        if "not active" in error_msg.lower():
            return {
                "resource": resource.name,
                "resource_type": "KINESIS_FIREHOSE",
                "replicated_arn": firehose_arn,
                "status": "error",
                "error": error_msg,
                "retryable": True,
            }
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_FIREHOSE",
            "replicated_arn": firehose_arn,
            "status": "error",
            "error": error_msg,
        }



def _ensure_kvs_kms_key(target_region: str) -> str:
    """Ensure the alias/aws/kinesisvideo KMS key exists in the target region.

    Delegates to the shared kms_utils module which handles bootstrapping
    AWS-managed keys by creating a temporary KVS stream if needed.
    """
    from aws.kms_utils import ensure_kms_key_exists
    key_id, _kms_info = ensure_kms_key_exists("alias/aws/kinesisvideo", target_region)
    return key_id


def _associate_kvs_stream(
    connect_client, instance_id: str, resource: Any
) -> dict[str, Any]:
    """Associate a replicated KVS stream with the DR Connect instance.

    For synthetic config-based KVS resources (MEDIA_STREAMS storage configs),
    adapts the prefix for the target region (e.g. iad → pdx) and applies the
    storage config to the DR instance.
    """
    stream_arn = resource.replicated_arn
    kvs_prefix = resource.name
    retention_hours = getattr(resource, "data_retention_in_hours", 0)

    already = _check_storage_config_exists(
        connect_client, instance_id, "MEDIA_STREAMS", "KINESIS_VIDEO_STREAM", None
    )
    if already:
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_VIDEO_STREAM",
            "replicated_arn": stream_arn,
            "status": "already_associated",
            "message": "Media streaming (KVS) is already configured on the DR instance",
        }

    # Adapt the prefix for the target region by replacing region short names.
    # Only replace when the short name appears as a delimited segment
    # (surrounded by hyphens, underscores, dots, or at string boundaries)
    # to avoid mangling names like "connect-acgr-saml-demo-iad-contact-"
    # where "iad" is part of the instance alias, not a region indicator.
    import re

    target_prefix = kvs_prefix
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
    target_region = None
    if stream_arn and ":" in stream_arn:
        parts = stream_arn.split(":")
        if len(parts) > 3:
            target_region = parts[3]

    if target_region:
        target_short = _REGION_SHORT_NAMES.get(target_region)
        if target_short:
            # Try full region name replacement first (e.g. "us-east-1" → "us-west-2")
            for region, short in _REGION_SHORT_NAMES.items():
                if region != target_region and region in kvs_prefix:
                    target_prefix = kvs_prefix.replace(region, target_region, 1)
                    break
            else:
                # Try short name replacement with word-boundary matching
                # Match only when surrounded by delimiters (-, _, .) or at
                # string start/end to avoid replacing inside words.
                for region, short in _REGION_SHORT_NAMES.items():
                    if region == target_region:
                        continue
                    pattern = rf'(?<![a-zA-Z0-9]){re.escape(short)}(?![a-zA-Z0-9])'
                    if re.search(pattern, kvs_prefix):
                        target_prefix = re.sub(pattern, target_short, kvs_prefix, count=1)
                        break

    logger.info(
        "KVS prefix adaptation: '%s' → '%s' (target region: %s)",
        kvs_prefix, target_prefix, target_region,
    )

    # Ensure the KMS key exists in the target region before associating
    kms_key_id = ""
    kms_warning = ""
    if target_region:
        kms_key_id = _ensure_kvs_kms_key(target_region)
        if not kms_key_id:
            kms_warning = (
                f"KMS key 'alias/aws/kinesisvideo' could not be resolved in "
                f"{target_region}. Media streams will use default encryption. "
                f"To use KMS encryption, create a KVS stream manually in "
                f"{target_region} to bootstrap the key, then retry association."
            )
            logger.warning(kms_warning)

    # Build the storage config — include encryption only if KMS key is available
    kvs_config: dict = {
        "Prefix": target_prefix,
        "RetentionPeriodHours": retention_hours,
    }
    if kms_key_id:
        kvs_config["EncryptionConfig"] = {
            "EncryptionType": "KMS",
            "KeyId": kms_key_id,
        }

    try:
        connect_client.associate_instance_storage_config(
            InstanceId=instance_id,
            ResourceType="MEDIA_STREAMS",
            StorageConfig={
                "StorageType": "KINESIS_VIDEO_STREAM",
                "KinesisVideoStreamConfig": kvs_config,
            },
        )
        message = f"KVS media streaming configured with prefix '{target_prefix}'"
        if kms_warning:
            message += f" | Warning: {kms_warning}"
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_VIDEO_STREAM",
            "replicated_arn": stream_arn,
            "status": "associated",
            "message": message,
        }
    except ClientError as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "conflict" in error_msg.lower():
            return {
                "resource": resource.name,
                "resource_type": "KINESIS_VIDEO_STREAM",
                "replicated_arn": stream_arn,
                "status": "already_associated",
                "message": "KVS media streaming config already exists",
            }
        # If KMS key not found, provide a helpful error with bootstrap instructions
        if "kms" in error_msg.lower() and ("not found" in error_msg.lower() or "invalid" in error_msg.lower()):
            return {
                "resource": resource.name,
                "resource_type": "KINESIS_VIDEO_STREAM",
                "replicated_arn": stream_arn,
                "status": "error",
                "error": f"KMS key issue in {target_region}: {error_msg}. "
                         f"The tool attempted to bootstrap 'alias/aws/kinesisvideo' "
                         f"but it may not have propagated yet. Try: 1) Wait 1-2 minutes "
                         f"and retry association, or 2) Create a KVS stream manually in "
                         f"{target_region} via the AWS console to bootstrap the key.",
                "retryable": True,
            }
        return {
            "resource": resource.name,
            "resource_type": "KINESIS_VIDEO_STREAM",
            "replicated_arn": stream_arn,
            "status": "error",
            "error": error_msg,
        }



def _associate_s3_bucket(
    connect_client, instance_id: str, resource: Any,
    s3_type_map: dict[str, list[str]] | None = None,
    source_configs: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Associate a replicated S3 bucket with the DR Connect instance.

    Associates the bucket for ALL storage types it was used for on the source
    instance (e.g. CALL_RECORDINGS, CHAT_TRANSCRIPTS, SCHEDULED_REPORTS).
    Returns a list of results — one per storage type.
    """
    results: list[dict[str, Any]] = []
    bucket_arn = resource.replicated_arn
    bucket_name = bucket_arn.split(":::")[-1] if ":::" in bucket_arn else resource.name

    # Determine which storage types this bucket should be associated with
    # Priority: source config mapping > resource.storage_types > default
    storage_types_to_associate: list[str] = []

    # Get the source bucket name from the resource's original ARN
    source_bucket_name = resource.arn.split(":::")[-1] if ":::" in resource.arn else resource.name

    if s3_type_map and source_bucket_name in s3_type_map:
        storage_types_to_associate = s3_type_map[source_bucket_name]
    elif hasattr(resource, 'storage_types') and resource.storage_types:
        # Filter out non-S3 storage types like FIREHOSE_DESTINATION
        storage_types_to_associate = [
            st for st in resource.storage_types
            if st in ("CALL_RECORDINGS", "CHAT_TRANSCRIPTS", "SCHEDULED_REPORTS",
                      "ATTACHMENTS", "SCREEN_RECORDINGS")
        ]

    if not storage_types_to_associate:
        storage_types_to_associate = ["CALL_RECORDINGS"]

    for storage_type in storage_types_to_associate:
        # Check if already configured
        already = _check_storage_config_exists(
            connect_client, instance_id, storage_type, "S3", None, bucket_name
        )
        if already:
            results.append({
                "resource": resource.name,
                "resource_type": "S3_BUCKET",
                "replicated_arn": bucket_arn,
                "status": "already_associated",
                "message": f"S3 bucket already configured for {storage_type}",
            })
            continue

        # Get the prefix from the source config if available
        bucket_prefix = f"connect/{storage_type}"
        if source_configs and storage_type in source_configs:
            for cfg in source_configs[storage_type]:
                if cfg.get("StorageType") == "S3":
                    src_prefix = cfg.get("S3Config", {}).get("BucketPrefix", "")
                    if src_prefix:
                        bucket_prefix = src_prefix
                        break

        try:
            connect_client.associate_instance_storage_config(
                InstanceId=instance_id,
                ResourceType=storage_type,
                StorageConfig={
                    "StorageType": "S3",
                    "S3Config": {
                        "BucketName": bucket_name,
                        "BucketPrefix": bucket_prefix,
                    },
                },
            )
            results.append({
                "resource": resource.name,
                "resource_type": "S3_BUCKET",
                "replicated_arn": bucket_arn,
                "status": "associated",
                "message": f"S3 bucket associated for {storage_type}",
            })
        except ClientError as exc:
            error_msg = str(exc)
            if "already" in error_msg.lower() or "conflict" in error_msg.lower():
                results.append({
                    "resource": resource.name,
                    "resource_type": "S3_BUCKET",
                    "replicated_arn": bucket_arn,
                    "status": "already_associated",
                    "message": f"S3 storage config already exists for {storage_type}",
                })
            else:
                results.append({
                    "resource": resource.name,
                    "resource_type": "S3_BUCKET",
                    "replicated_arn": bucket_arn,
                    "status": "error",
                    "error": f"{storage_type}: {error_msg}",
                })

    return results


# ---------------------------------------------------------------------------
# Storage config existence check
# ---------------------------------------------------------------------------

def _check_storage_config_exists(
    connect_client, instance_id: str, resource_type: str,
    storage_type: str, target_arn: str | None,
    bucket_name: str | None = None,
) -> bool:
    """Check if a storage config already exists for the given type."""
    try:
        resp = connect_client.list_instance_storage_configs(
            InstanceId=instance_id, ResourceType=resource_type,
        )
        for config in resp.get("StorageConfigs", []):
            if config.get("StorageType") != storage_type:
                continue
            if storage_type == "KINESIS_STREAM" and target_arn:
                if config.get("KinesisStreamConfig", {}).get("StreamArn") == target_arn:
                    return True
            elif storage_type == "KINESIS_FIREHOSE" and target_arn:
                if config.get("KinesisFirehoseConfig", {}).get("FirehoseArn") == target_arn:
                    return True
            elif storage_type == "KINESIS_VIDEO_STREAM":
                return True
            elif storage_type == "S3" and bucket_name:
                if config.get("S3Config", {}).get("BucketName") == bucket_name:
                    return True
            elif storage_type == "S3" and not bucket_name:
                return True
    except ClientError:
        pass
    return False


# ---------------------------------------------------------------------------
# Approved origins replication
# ---------------------------------------------------------------------------

def _replicate_approved_origins(
    source_region: str, target_region: str, instance_id: str,
) -> list[dict[str, Any]]:
    """Replicate approved origins from the source Connect instance to the replica.

    Reads all approved origins from the source instance and adds any missing
    ones to the target (replica) instance.
    """
    results: list[dict[str, Any]] = []

    try:
        source_connect = create_source_client("connect", source_region)
        source_origins: list[str] = []
        params: dict = {"InstanceId": instance_id, "MaxResults": 25}
        while True:
            resp = source_connect.list_approved_origins(**params)
            source_origins.extend(resp.get("Origins", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except ClientError as exc:
        results.append({
            "resource": "approved_origins",
            "resource_type": "APPROVED_ORIGIN",
            "status": "error",
            "error": f"Failed to list source approved origins: {exc}",
        })
        return results

    if not source_origins:
        logger.info("No approved origins found on source instance")
        return results

    # Get existing origins on the replica
    try:
        target_connect = create_target_client("connect", target_region)
        target_origins: list[str] = []
        params = {"InstanceId": instance_id, "MaxResults": 25}
        while True:
            resp = target_connect.list_approved_origins(**params)
            target_origins.extend(resp.get("Origins", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token
    except ClientError:
        target_origins = []

    existing_set = set(target_origins)
    added = 0
    skipped = 0

    for origin in source_origins:
        if origin in existing_set:
            skipped += 1
            continue
        try:
            target_connect.associate_approved_origin(
                InstanceId=instance_id, Origin=origin,
            )
            added += 1
        except ClientError as exc:
            error_msg = str(exc)
            if "already" in error_msg.lower() or "duplicate" in error_msg.lower():
                skipped += 1
            else:
                results.append({
                    "resource": origin,
                    "resource_type": "APPROVED_ORIGIN",
                    "status": "error",
                    "error": error_msg,
                })

    if added > 0 or skipped > 0:
        results.append({
            "resource": "approved_origins",
            "resource_type": "APPROVED_ORIGIN",
            "status": "associated",
            "message": f"Approved origins: {added} added, {skipped} already existed "
                       f"(out of {len(source_origins)} source origins)",
        })

    return results
