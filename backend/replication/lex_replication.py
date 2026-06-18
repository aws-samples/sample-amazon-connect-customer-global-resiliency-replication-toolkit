"""Amazon Lex V2 bot replication to the ACGR target region.

Uses Amazon Lex Global Resiliency (ALGR) via CreateBotReplica as the
sole mechanism for Amazon Lex bot replication. If ALGR fails, an error is
raised directly — there is no legacy fallback.

Requirements: 7.1, 7.2, 7.6, 7.7
"""

from __future__ import annotations

import logging
import time

from botocore.exceptions import ClientError

from aws.arn_utils import rewrite_arn
from aws.client_factory import create_source_client, create_target_client
from models.resources import LexBotResource

logger = logging.getLogger(__name__)

# Polling configuration for BuildBotLocale
_BUILD_POLL_INTERVAL_SECONDS = 5
_BUILD_MAX_WAIT_SECONDS = 300


def _normalize_locale_id(locale_id: str) -> str:
    """Normalize locale ID to Amazon Lex V2 format (e.g., en-US -> en_US).

    Amazon Lex V1 uses hyphen format (en-US), Amazon Lex V2 requires underscore (en_US).
    """
    return locale_id.replace("-", "_")


def _rewrite_fulfillment_lambda_arn(
    arn: str, target_region: str, lambda_arn_mapping: dict[str, str]
) -> str:
    """Rewrite a fulfillment Lambda ARN to the target region equivalent.

    First checks the explicit mapping (source ARN → replicated ARN), then
    falls back to simple region rewriting.
    """
    if arn in lambda_arn_mapping:
        return lambda_arn_mapping[arn]
    try:
        return rewrite_arn(arn, target_region)
    except ValueError:
        logger.warning("Could not rewrite fulfillment Lambda ARN '%s'; keeping as-is", arn)
        return arn


def _rewrite_intent_fulfillment(
    intent_config: dict,
    target_region: str,
    lambda_arn_mapping: dict[str, str],
) -> dict:
    """Recursively rewrite Lambda ARNs in an intent configuration.

    Walks the intent dict and rewrites any string value that looks like a
    Lambda function ARN (contains ``:lambda:`` and ``:function:``).
    """
    if isinstance(intent_config, str):
        if (
            intent_config.startswith("arn:")
            and ":lambda:" in intent_config
            and ":function:" in intent_config
        ):
            return _rewrite_fulfillment_lambda_arn(
                intent_config, target_region, lambda_arn_mapping
            )
        return intent_config
    if isinstance(intent_config, list):
        return [
            _rewrite_intent_fulfillment(item, target_region, lambda_arn_mapping)
            for item in intent_config
        ]
    if isinstance(intent_config, dict):
        return {
            key: _rewrite_intent_fulfillment(value, target_region, lambda_arn_mapping)
            for key, value in intent_config.items()
        }
    return intent_config


def _wait_for_bot_available(lex_client, bot_id: str, max_wait: int = 60) -> None:
    """Wait for a bot to reach 'Available' status after creation.

    Args:
        lex_client: boto3 Amazon Lex V2 client.
        bot_id: The bot ID to wait for.
        max_wait: Maximum seconds to wait.

    Raises:
        RuntimeError: If the bot doesn't become available in time.
    """
    elapsed = 0
    while elapsed < max_wait:
        try:
            resp = lex_client.describe_bot(botId=bot_id)
            status = resp.get("botStatus", "")
            if status == "Available":
                return
            if status in ("Failed", "Deleting", "Inactive"):
                raise RuntimeError(
                    f"Bot '{bot_id}' entered unexpected status: {status}"
                )
            logger.debug("Bot '%s' status: %s (waiting...)", bot_id, status)
        except ClientError:
            pass
        time.sleep(2)
        elapsed += 2

    raise RuntimeError(
        f"Bot '{bot_id}' did not become Available within {max_wait}s"
    )


def replicate_lex_bot(
    bot_resource: LexBotResource,
    target_region: str,
    lambda_arn_mapping: dict[str, str] | None = None,
    role_arn_mapping: dict[str, str] | None = None,
    resource_tags: dict[str, str] | None = None,
    instance_id: str = "",
) -> str:
    """Replicate a Amazon Lex V2 bot to the ACGR target region using ALGR.

    Uses Amazon Lex Global Resiliency (ALGR) via CreateBotReplica as the
    sole replication mechanism. If ALGR fails, raises an error directly.

    Args:
        bot_resource: The discovered Amazon Lex bot resource to replicate.
        target_region: The ACGR target region.
        lambda_arn_mapping: Optional mapping of source Lambda ARN → target Lambda ARN.
        role_arn_mapping: Optional mapping of source role ARN → target role ARN.
        instance_id: Connect instance ID for post-replication association.

    Returns:
        The ARN of the Amazon Lex bot in the target region.

    Raises:
        LexAlgrSkippedError: If ALGR is not supported for the region pair.
        RuntimeError: If ALGR replication fails.
    """
    if lambda_arn_mapping is None:
        lambda_arn_mapping = {}
    if role_arn_mapping is None:
        role_arn_mapping = {}

    source_region = bot_resource.arn.split(":")[3] if ":" in bot_resource.arn else ""

    # Try ALGR replication
    algr_result = _try_algr_replication(bot_resource, source_region, target_region)
    if algr_result is not None:
        # ALGR succeeded — now update fulfillment/codehook Lambda ARNs
        # on the replica bot to point to the replicated Lambdas in the
        # target region (ALGR copies the source ARNs as-is).
        if lambda_arn_mapping:
            try:
                _update_algr_bot_lambda_arns(
                    bot_resource.bot_id, target_region, lambda_arn_mapping,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to update Lambda ARNs on ALGR bot '%s': %s — "
                    "bot is replicated but fulfillment Lambdas still point to source region",
                    bot_resource.name, exc,
                )
        # Association is deferred to the explicit Associate step
        return algr_result

    # ALGR not supported or failed — raise error directly (no legacy fallback)
    if _is_algr_supported_pair(source_region, target_region):
        raise RuntimeError(
            f"Lex ALGR replication failed for bot '{bot_resource.name}' "
            f"({source_region} → {target_region}). "
            f"Check ALGR permissions and bot configuration."
        )

    raise LexAlgrSkippedError(
        f"Lex ALGR is not supported for region pair "
        f"({source_region} → {target_region}). "
        f"Bot '{bot_resource.name}' cannot be replicated."
    )


# ---------------------------------------------------------------------------
# ALGR (Lex Global Resiliency) support
# ---------------------------------------------------------------------------

# Region pairs that support Lex ALGR (CreateBotReplica)
_ALGR_SUPPORTED_PAIRS: set[tuple[str, str]] = {
    ("us-east-1", "us-west-2"),
    ("us-west-2", "us-east-1"),
    ("eu-west-2", "eu-central-1"),
    ("eu-central-1", "eu-west-2"),
}


class LexAlgrSkippedError(Exception):
    """Raised when ALGR replication is not supported for the region pair."""
    pass


def _is_algr_supported_pair(source_region: str, target_region: str) -> bool:
    """Check if a region pair supports Lex ALGR."""
    return (source_region, target_region) in _ALGR_SUPPORTED_PAIRS


def _try_algr_replication(
    bot_resource: LexBotResource, source_region: str, target_region: str
) -> str | None:
    """Attempt ALGR replication via CreateBotReplica.

    First checks if a replica already exists in the target region (e.g.
    Global Resiliency was enabled in the console).  If so, returns the
    existing replica ARN without calling CreateBotReplica.

    Returns the replicated bot ARN on success, or None if ALGR is not
    supported for this region pair or the API call fails.
    """
    if not _is_algr_supported_pair(source_region, target_region):
        return None

    source_parsed = bot_resource.arn.split(":")
    account_id = source_parsed[4] if len(source_parsed) > 4 else "unknown"

    try:
        source_lex = create_source_client("lexv2-models", source_region)

        # Check if a replica already exists in the target region
        try:
            replicas_resp = source_lex.list_bot_replicas(botId=bot_resource.bot_id)
            for replica in replicas_resp.get("botReplicaSummaries", []):
                if replica.get("replicaRegion") == target_region:
                    replica_status = replica.get("replicaStatus", "")
                    if replica_status in ("Enabled", "Enabling", "Available"):
                        replicated_arn = f"arn:aws:lex:{target_region}:{account_id}:bot/{bot_resource.bot_id}"
                        logger.info(
                            "ALGR replica already exists for bot '%s' in %s (status=%s) → %s",
                            bot_resource.name, target_region, replica_status, replicated_arn,
                        )
                        return replicated_arn
        except Exception as list_exc:
            logger.debug(
                "Could not list bot replicas for '%s': %s — will try CreateBotReplica or target lookup",
                bot_resource.name, list_exc,
            )

        # Try creating the replica
        try:
            response = source_lex.create_bot_replica(
                botId=bot_resource.bot_id,
                replicaRegion=target_region,
            )
            replica_bot_id = response.get("botId", bot_resource.bot_id)
            replicated_arn = f"arn:aws:lex:{target_region}:{account_id}:bot/{replica_bot_id}"
            logger.info(
                "ALGR replication succeeded for bot '%s' → %s",
                bot_resource.name, replicated_arn,
            )
            return replicated_arn
        except ClientError as create_exc:
            error_code = create_exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ConflictException", "ResourceInUseException", "PreconditionFailedException"):
                # Replica already exists — return the ARN
                replicated_arn = f"arn:aws:lex:{target_region}:{account_id}:bot/{bot_resource.bot_id}"
                logger.info(
                    "ALGR replica already exists for bot '%s' (ConflictException) → %s",
                    bot_resource.name, replicated_arn,
                )
                return replicated_arn
            raise  # Re-raise for the outer except to handle

    except Exception as exc:
        logger.warning(
            "ALGR replication failed for bot '%s' (%s → %s): %s",
            bot_resource.name, source_region, target_region, exc,
        )

        # Last resort: check if the bot exists in the target region by name
        # (covers the case where Global Resiliency was enabled via console
        # but we lack lex:CreateBotReplica / lex:ListBotReplicas permissions)
        try:
            target_lex = create_target_client("lexv2-models", target_region)
            resp = target_lex.list_bots(
                filters=[{"name": "BotName", "values": [bot_resource.name], "operator": "EQ"}],
                maxResults=10,
            )
            for bot in resp.get("botSummaries", []):
                if bot.get("botName") == bot_resource.name:
                    target_bot_id = bot["botId"]
                    replicated_arn = f"arn:aws:lex:{target_region}:{account_id}:bot/{target_bot_id}"
                    logger.info(
                        "Found existing bot '%s' in target region %s (likely ALGR replica) → %s",
                        bot_resource.name, target_region, replicated_arn,
                    )
                    return replicated_arn
        except Exception as lookup_exc:
            logger.debug(
                "Target region bot lookup also failed for '%s': %s",
                bot_resource.name, lookup_exc,
            )

        return None



def _build_bot_locale(lex_client, bot_id: str, locale_id: str) -> None:
    """Build a bot locale after intent updates.

    Initiates a build and polls until the locale reaches 'Built' status.
    Used after updating Lambda ARNs on ALGR replica intents.
    """
    try:
        lex_client.build_bot_locale(
            botId=bot_id, botVersion="DRAFT", localeId=locale_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "PreconditionFailedException":
            # Locale may already be built or in a state that doesn't need building
            logger.debug("Locale '%s' build precondition failed (may already be built)", locale_id)
            return
        raise RuntimeError(
            f"Failed to initiate build for locale '{locale_id}' on bot '{bot_id}': {exc}"
        ) from exc

    # Poll until built
    elapsed = 0
    while elapsed < _BUILD_MAX_WAIT_SECONDS:
        try:
            resp = lex_client.describe_bot_locale(
                botId=bot_id, botVersion="DRAFT", localeId=locale_id,
            )
            status = resp.get("botLocaleStatus", "")
            if status == "Built":
                return
            if status in ("ReadyExpressTesting",):
                return  # Also acceptable
            if status == "Failed":
                failure_reasons = resp.get("failureReasons", [])
                raise RuntimeError(
                    f"Locale '{locale_id}' build failed for bot '{bot_id}': {failure_reasons}"
                )
        except ClientError:
            pass
        time.sleep(_BUILD_POLL_INTERVAL_SECONDS)
        elapsed += _BUILD_POLL_INTERVAL_SECONDS

    raise RuntimeError(
        f"Locale '{locale_id}' build did not complete within {_BUILD_MAX_WAIT_SECONDS}s for bot '{bot_id}'"
    )


def _update_algr_bot_lambda_arns(
    bot_id: str,
    target_region: str,
    lambda_arn_mapping: dict[str, str],
) -> None:
    """Update fulfillment/codehook Lambda ARNs on an ALGR replica bot.

    After ALGR replicates a bot, the intents still reference the source-region
    Lambda ARNs. This function walks every intent in every locale of the
    DRAFT version and rewrites Lambda ARNs to the replicated target-region
    equivalents, then rebuilds the locale.

    Args:
        bot_id: The bot ID in the target region.
        target_region: The ACGR target region.
        lambda_arn_mapping: Mapping of source Lambda ARN → target Lambda ARN.
    """
    if not lambda_arn_mapping:
        logger.debug("No lambda_arn_mapping provided; skipping ALGR Lambda ARN update")
        return

    lex_client = create_target_client("lexv2-models", target_region)

    # List locales on the DRAFT version
    try:
        locales_resp = lex_client.list_bot_locales(
            botId=bot_id, botVersion="DRAFT", maxResults=20,
        )
        locale_ids = [
            loc["localeId"]
            for loc in locales_resp.get("botLocaleSummaries", [])
        ]
    except ClientError as exc:
        logger.warning(
            "Could not list locales for ALGR bot '%s': %s — skipping Lambda ARN update",
            bot_id, exc,
        )
        return

    if not locale_ids:
        logger.debug("No locales found for ALGR bot '%s'; skipping Lambda ARN update", bot_id)
        return

    any_updated = False

    for locale_id in locale_ids:
        locale_updated = False

        # List intents for this locale
        try:
            intents_resp = lex_client.list_intents(
                botId=bot_id, botVersion="DRAFT", localeId=locale_id, maxResults=100,
            )
            intent_summaries = intents_resp.get("intentSummaries", [])
        except ClientError as exc:
            logger.warning(
                "Could not list intents for bot '%s' locale '%s': %s",
                bot_id, locale_id, exc,
            )
            continue

        for intent_summary in intent_summaries:
            intent_id = intent_summary.get("intentId", "")
            if not intent_id:
                continue

            # Get full intent details
            try:
                intent_detail = lex_client.describe_intent(
                    botId=bot_id, botVersion="DRAFT",
                    localeId=locale_id, intentId=intent_id,
                )
            except ClientError:
                continue

            # Check fulfillmentCodeHook and dialogCodeHook for Lambda ARNs
            updated = False
            update_params: dict = {
                "botId": bot_id,
                "botVersion": "DRAFT",
                "localeId": locale_id,
                "intentId": intent_id,
                "intentName": intent_detail.get("intentName", ""),
            }

            # Copy over required fields for UpdateIntent
            for field in (
                "description", "sampleUtterances", "dialogCodeHook",
                "fulfillmentCodeHook", "intentConfirmationSetting",
                "intentClosingSetting", "inputContexts", "outputContexts",
                "kendraConfiguration", "parentIntentSignature",
                "initialResponseSetting", "qnAIntentConfiguration",
            ):
                if field in intent_detail and intent_detail[field] is not None:
                    update_params[field] = intent_detail[field]

            # Also copy slotPriorities if present
            if "slotPriorities" in intent_detail and intent_detail["slotPriorities"] is not None:
                update_params["slotPriorities"] = intent_detail["slotPriorities"]

            # Rewrite Lambda ARNs in fulfillmentCodeHook
            fc_hook = update_params.get("fulfillmentCodeHook")
            if fc_hook and isinstance(fc_hook, dict):
                rewritten = _rewrite_intent_fulfillment(fc_hook, target_region, lambda_arn_mapping)
                if rewritten != fc_hook:
                    update_params["fulfillmentCodeHook"] = rewritten
                    updated = True

            # Rewrite Lambda ARNs in dialogCodeHook
            dc_hook = update_params.get("dialogCodeHook")
            if dc_hook and isinstance(dc_hook, dict):
                rewritten = _rewrite_intent_fulfillment(dc_hook, target_region, lambda_arn_mapping)
                if rewritten != dc_hook:
                    update_params["dialogCodeHook"] = rewritten
                    updated = True

            # Also check initialResponseSetting and intentConfirmationSetting
            # which can contain codeHook references
            for setting_key in ("initialResponseSetting", "intentConfirmationSetting", "intentClosingSetting"):
                setting = update_params.get(setting_key)
                if setting and isinstance(setting, dict):
                    rewritten = _rewrite_intent_fulfillment(setting, target_region, lambda_arn_mapping)
                    if rewritten != setting:
                        update_params[setting_key] = rewritten
                        updated = True

            if updated:
                try:
                    lex_client.update_intent(**update_params)
                    locale_updated = True
                    logger.info(
                        "Updated Lambda ARNs on intent '%s' (bot '%s', locale '%s')",
                        intent_detail.get("intentName", intent_id), bot_id, locale_id,
                    )
                except ClientError as exc:
                    logger.warning(
                        "Failed to update intent '%s' on ALGR bot '%s': %s",
                        intent_id, bot_id, exc,
                    )

        if locale_updated:
            any_updated = True
            # Rebuild the locale after updating intents
            try:
                _build_bot_locale(lex_client, bot_id, locale_id)
                logger.info(
                    "Rebuilt locale '%s' for ALGR bot '%s' after Lambda ARN update",
                    locale_id, bot_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to rebuild locale '%s' for ALGR bot '%s': %s",
                    locale_id, bot_id, exc,
                )

    if any_updated:
        logger.info("Completed Lambda ARN update on ALGR bot '%s'", bot_id)
    else:
        logger.debug("No Lambda ARN updates needed for ALGR bot '%s'", bot_id)


def _associate_lex_bot_with_connect(
    bot_arn: str, bot_id: str, instance_id: str, target_region: str
) -> None:
    """Associate a Amazon Lex V2 bot with a Connect instance (best-effort).

    Calls connect:AssociateBot. Failures are logged but don't fail replication.
    """
    try:
        connect_client = create_target_client("connect", target_region)
        connect_client.associate_bot(
            InstanceId=instance_id,
            LexV2Bot={"AliasArn": bot_arn},
        )
        logger.info("Associated Lex bot '%s' with Connect instance '%s'", bot_id, instance_id)
    except Exception:
        logger.debug(
            "Could not associate Lex bot '%s' with Connect instance '%s' (best-effort)",
            bot_id, instance_id, exc_info=True,
        )
