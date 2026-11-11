"""Amazon Lex V2 bot replication to the ACGR target region.

Uses Amazon Lex Global Resiliency (ALGR) via CreateBotReplica as the
sole mechanism for Amazon Lex bot replication. If ALGR fails, an error is
raised directly — there is no legacy fallback.

Requirements: 7.1, 7.2, 7.6, 7.7
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from aws.client_factory import create_source_client, create_target_client
from models.resources import LexBotResource

logger = logging.getLogger(__name__)

class LexAlgrInProgressError(Exception):
    """Raised when an ALGR replica has been created/exists but is not yet Enabled.

    This signals that replication is genuinely still in progress (not failed and
    NOT complete). The bot must NOT be reported as REPLICATED yet, because the
    replica is still enabling asynchronously. The resource is marked IN_PROGRESS
    (carrying the replica ARN) and each session-status poll re-checks the live
    replica status, flipping it to REPLICATED once it reaches "Enabled".
    """

    def __init__(self, message: str, replicated_arn: str | None = None) -> None:
        super().__init__(message)
        self.replicated_arn = replicated_arn


def _get_replica_status(source_lex, bot_id: str, target_region: str) -> str | None:
    """Return the ALGR replica status for a bot in the target region, or None.

    Values: Enabling | Enabled | Deleting | Failed (None if no replica exists).
    """
    try:
        resp = source_lex.list_bot_replicas(botId=bot_id)
    except Exception as exc:
        logger.debug("list_bot_replicas failed for '%s': %s", bot_id, exc)
        return None
    for replica in resp.get("botReplicaSummaries", []):
        if replica.get("replicaRegion") == target_region:
            # The Lex V2 ListBotReplicas response field is "botReplicaStatus"
            # (values: Enabling | Enabled | Deleting | Failed), NOT "replicaStatus".
            return replica.get("botReplicaStatus", "")
    return None


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
        lambda_arn_mapping, role_arn_mapping, resource_tags, instance_id:
            Not used. Kept so the orchestrator's common call signature still
            works. ALGR copies the bot as is (nothing to rewrite or tag on the
            read-only replica) and association is a separate step.

    Returns:
        The ARN of the Amazon Lex bot in the target region.

    Raises:
        LexAlgrSkippedError: If ALGR is not supported for the region pair, or the
            bot is a Lex V1 bot (ALGR is V2 only).
        RuntimeError: If ALGR replication fails.
    """
    source_region = bot_resource.arn.split(":")[3] if ":" in bot_resource.arn else ""

    # ALGR (CreateBotReplica) is Lex V2 only. A V1 bot's "bot_id" is its name,
    # so calling ALGR with it would fail and the name-based fallback below could
    # wrongly pick an unrelated V2 bot with the same name.
    if (bot_resource.config_summary or {}).get("lex_version") == "V1":
        raise LexAlgrSkippedError(
            f"Bot '{bot_resource.name}' is an Amazon Lex V1 (classic) bot. Lex Global "
            f"Resiliency works with Lex V2 bots only, so it is not replicated. Migrate the "
            f"bot to Lex V2, or create and maintain it in {target_region} yourself."
        )

    # Try ALGR replication
    algr_result = _try_algr_replication(bot_resource, source_region, target_region)
    if algr_result is not None:
        # Nothing more to do on the bot itself. ALGR keeps the replica in sync
        # (bot, versions and aliases, same IDs), the replica cannot be modified,
        # and the code hook resolves to the Lambda with the same name in the
        # target Region, which the Lambda replication step creates.
        # Association is deferred to the explicit Associate step.
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

        replicated_arn = f"arn:aws:lex:{target_region}:{account_id}:bot/{bot_resource.bot_id}"

        # Check whether a replica already exists in the target region.
        try:
            replica_status = _get_replica_status(source_lex, bot_resource.bot_id, target_region)
        except Exception as list_exc:
            logger.debug("Could not list bot replicas for '%s': %s", bot_resource.name, list_exc)
            replica_status = None

        if replica_status == "Enabled":
            logger.info(
                "ALGR replica already Enabled for bot '%s' in %s → %s",
                bot_resource.name, target_region, replicated_arn,
            )
            return replicated_arn
        if replica_status is not None:
            # Replica exists but is still enabling — report in-progress. We do
            # NOT block; each status poll re-checks and flips to REPLICATED.
            raise LexAlgrInProgressError(
                f"ALGR replica for bot '{bot_resource.name}' is enabling in {target_region} "
                f"(asynchronous — refresh to check status).",
                replicated_arn=replicated_arn,
            )

        # No replica yet — create it. CreateBotReplica returns immediately; the
        # replica then enables asynchronously. We do NOT block waiting for it.
        try:
            source_lex.create_bot_replica(
                botId=bot_resource.bot_id,
                replicaRegion=target_region,
            )
        except ClientError as create_exc:
            error_code = create_exc.response.get("Error", {}).get("Code", "")
            if error_code not in ("ConflictException", "ResourceInUseException", "PreconditionFailedException"):
                raise  # genuine error → outer except handles/falls back
            # Replica already exists — it may already be Enabled.
            if _get_replica_status(source_lex, bot_resource.bot_id, target_region) == "Enabled":
                return replicated_arn

        logger.info(
            "ALGR replica for bot '%s' → %s (enabling asynchronously)",
            bot_resource.name, replicated_arn,
        )
        raise LexAlgrInProgressError(
            f"ALGR replica creation started for bot '{bot_resource.name}' in {target_region} "
            f"(asynchronous — refresh to check status).",
            replicated_arn=replicated_arn,
        )

    except LexAlgrInProgressError:
        # Genuinely still replicating — propagate so it is NOT reported as
        # REPLICATED and is not masked by the target-name fallback below.
        raise
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
