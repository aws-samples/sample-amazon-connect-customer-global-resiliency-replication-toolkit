"""Amazon Lex bot discovery for Amazon Connect ACGR Resource Replicator.

Discovers all Amazon Lex V1 and V2 bots associated with a Connect instance, their aliases,
locales, intents, and slot types. Extracts fulfillment Lambda ARNs from intent
configurations for cross-discovery.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from aws.client_factory import create_source_client
from models.enums import ResourceType
from models.resources import LexBotResource

logger = logging.getLogger(__name__)


def _generate_resource_id(arn: str) -> str:
    """Generate a deterministic resource ID from an ARN using a SHA-256 hash prefix."""
    return hashlib.sha256(arn.encode()).hexdigest()[:12]


def _get_associated_bots(connect_client: Any, instance_id: str) -> list[dict[str, Any]]:
    """Call Connect ListBots to get all Amazon Lex V2 bot associations for the instance.

    Handles pagination via NextToken.

    Returns:
        A list of bot association dicts, each containing LexBot info with
        AliasArn and LexBotId.
    """
    bots: list[dict[str, Any]] = []
    params: dict[str, Any] = {"InstanceId": instance_id, "LexVersion": "V2"}

    while True:
        response = connect_client.list_bots(**params)
        bots.extend(response.get("LexBots", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token

    return bots


def _describe_bot(lex_client: Any, bot_id: str) -> dict[str, Any] | None:
    """Call Amazon Lex V2 DescribeBot to retrieve full bot configuration.

    Returns None if the bot cannot be retrieved.
    """
    try:
        return lex_client.describe_bot(botId=bot_id)
    except Exception:
        logger.exception("Failed to describe Lex bot: %s", bot_id)
        return None


def _list_bot_aliases(lex_client: Any, bot_id: str) -> list[dict[str, Any]]:
    """Call Amazon Lex V2 ListBotAliases to retrieve all aliases for a bot.

    Handles pagination via nextToken. After listing, calls DescribeBotAlias
    for each alias to get full configuration including botAliasLocaleSettings
    with codeHookSpecification Lambda ARNs.
    """
    summaries: list[dict[str, Any]] = []
    params: dict[str, Any] = {"botId": bot_id, "maxResults": 1000}

    try:
        while True:
            response = lex_client.list_bot_aliases(**params)
            summaries.extend(response.get("botAliasSummaries", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception:
        logger.exception("Failed to list aliases for bot: %s", bot_id)

    # Enrich each alias summary with full details (includes Lambda ARNs
    # in botAliasLocaleSettings -> codeHookSpecification)
    aliases: list[dict[str, Any]] = []
    for summary in summaries:
        alias_id = summary.get("botAliasId")
        if not alias_id:
            aliases.append(summary)
            continue
        try:
            detail = lex_client.describe_bot_alias(
                botAliasId=alias_id,
                botId=bot_id,
            )
            detail.pop("ResponseMetadata", None)
            aliases.append(detail)
        except Exception:
            logger.debug(
                "Failed to describe alias %s for bot %s; using summary",
                alias_id, bot_id,
            )
            aliases.append(summary)

    return aliases


def _list_bot_locales(lex_client: Any, bot_id: str, bot_version: str = "DRAFT") -> list[dict[str, Any]]:
    """Call Amazon Lex V2 ListBotLocales to retrieve all locales for a bot.

    Handles pagination via nextToken.
    """
    locales: list[dict[str, Any]] = []
    params: dict[str, Any] = {"botId": bot_id, "botVersion": bot_version, "maxResults": 1000}

    try:
        while True:
            response = lex_client.list_bot_locales(**params)
            locales.extend(response.get("botLocaleSummaries", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception:
        logger.exception("Failed to list locales for bot: %s", bot_id)

    return locales


def _list_intents(
    lex_client: Any, bot_id: str, bot_version: str, locale_id: str
) -> list[dict[str, Any]]:
    """Call Amazon Lex V2 ListIntents to retrieve all intents for a bot locale.

    Handles pagination via nextToken. After listing, calls DescribeIntent
    for each intent to get full configuration including Lambda ARNs.
    """
    summaries: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "botId": bot_id,
        "botVersion": bot_version,
        "localeId": locale_id,
        "maxResults": 1000,
    }

    try:
        while True:
            response = lex_client.list_intents(**params)
            summaries.extend(response.get("intentSummaries", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception:
        logger.exception(
            "Failed to list intents for bot %s, locale %s", bot_id, locale_id
        )
        return summaries

    # Enrich each intent summary with full details (includes Lambda ARNs)
    intents: list[dict[str, Any]] = []
    for summary in summaries:
        intent_id = summary.get("intentId")
        if not intent_id:
            intents.append(summary)
            continue
        try:
            detail = lex_client.describe_intent(
                intentId=intent_id,
                botId=bot_id,
                botVersion=bot_version,
                localeId=locale_id,
            )
            # Remove ResponseMetadata to keep data clean
            detail.pop("ResponseMetadata", None)
            intents.append(detail)
        except Exception:
            logger.debug(
                "Failed to describe intent %s for bot %s; using summary",
                intent_id, bot_id,
            )
            intents.append(summary)

    return intents


def _list_slot_types(
    lex_client: Any, bot_id: str, bot_version: str, locale_id: str
) -> list[dict[str, Any]]:
    """Call Amazon Lex V2 ListSlotTypes to retrieve all slot types for a bot locale.

    Handles pagination via nextToken.
    """
    slot_types: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "botId": bot_id,
        "botVersion": bot_version,
        "localeId": locale_id,
        "maxResults": 1000,
    }

    try:
        while True:
            response = lex_client.list_slot_types(**params)
            slot_types.extend(response.get("slotTypeSummaries", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception:
        logger.exception(
            "Failed to list slot types for bot %s, locale %s", bot_id, locale_id
        )

    return slot_types


def _extract_fulfillment_lambda_arns(intents: list[dict[str, Any]]) -> list[str]:
    """Extract Lambda function ARNs from intent fulfillment and code hook configurations.

    Scans each intent summary for fulfillment or dialogCodeHook Lambda references.
    The intent summaries from ListIntents may not contain full fulfillment details,
    so we look for common patterns in the intent data.

    Returns:
        A deduplicated list of Lambda function ARNs found in intent configurations.
    """
    lambda_arns: list[str] = []
    seen: set[str] = set()

    for intent in intents:
        # Check various locations where Lambda ARNs may appear
        _extract_from_dict(intent, lambda_arns, seen)

    return lambda_arns


def _classify_v2_lambda_roles(
    intents: list[dict[str, Any]], aliases: list[dict[str, Any]]
) -> dict[str, set[str]]:
    """Classify Lambda ARNs by their role for V2 bots.

    - ARNs found in intent data are classified as "fulfillment"
    - ARNs found in alias botAliasLocaleSettings (codeHookSpecification) are "codehook"
    - ARNs found in both get both roles

    Returns a dict mapping Lambda ARN -> set of roles.
    """
    roles: dict[str, set[str]] = {}

    # Intent-sourced Lambdas → fulfillment
    intent_arns: list[str] = []
    seen: set[str] = set()
    for intent in intents:
        _extract_from_dict(intent, intent_arns, seen)
    for arn in intent_arns:
        roles.setdefault(arn, set()).add("fulfillment")

    # Alias-sourced Lambdas → codehook
    alias_arns: list[str] = []
    alias_seen: set[str] = set()
    for alias in aliases:
        _extract_from_dict(alias, alias_arns, alias_seen)
    for arn in alias_arns:
        roles.setdefault(arn, set()).add("codehook")

    return roles


def _extract_from_dict(data: Any, lambda_arns: list[str], seen: set[str]) -> None:
    """Recursively search a dict/list structure for Lambda ARN values.

    Looks for string values that match the Lambda function ARN pattern.
    """
    if isinstance(data, str):
        if data.startswith("arn:") and ":lambda:" in data and ":function:" in data:
            if data not in seen:
                seen.add(data)
                lambda_arns.append(data)
    elif isinstance(data, dict):
        for value in data.values():
            _extract_from_dict(value, lambda_arns, seen)
    elif isinstance(data, list):
        for item in data:
            _extract_from_dict(item, lambda_arns, seen)


# ---------------------------------------------------------------------------
# Amazon Lex V1 discovery helpers
# ---------------------------------------------------------------------------

def _get_associated_v1_bots(connect_client: Any, instance_id: str) -> list[dict[str, Any]]:
    """Call Connect ListBots with LexVersion=V1 to get all Amazon Lex V1 bot associations.

    Handles pagination via NextToken.

    Returns:
        A list of bot association dicts, each containing LexBot info with
        Name and LexRegion.
    """
    bots: list[dict[str, Any]] = []
    params: dict[str, Any] = {"InstanceId": instance_id, "LexVersion": "V1"}

    while True:
        response = connect_client.list_bots(**params)
        bots.extend(response.get("LexBots", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        params["NextToken"] = next_token

    return bots


def _get_v1_bot(lex_v1_client: Any, bot_name: str) -> dict[str, Any] | None:
    """Call Amazon Lex V1 GetBot to retrieve full bot configuration.

    Returns None if the bot cannot be retrieved.
    """
    try:
        return lex_v1_client.get_bot(name=bot_name, versionOrAlias="$LATEST")
    except Exception:
        logger.exception("Failed to get Lex V1 bot: %s", bot_name)
        return None


def _get_v1_bot_aliases(lex_v1_client: Any, bot_name: str) -> list[dict[str, Any]]:
    """Call Amazon Lex V1 GetBotAliases to retrieve all aliases for a bot.

    Handles pagination via nextToken.
    """
    aliases: list[dict[str, Any]] = []
    params: dict[str, Any] = {"botName": bot_name}

    try:
        while True:
            response = lex_v1_client.get_bot_aliases(**params)
            aliases.extend(response.get("BotAliases", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception:
        logger.exception("Failed to get aliases for V1 bot: %s", bot_name)

    return aliases


def _get_v1_intents(lex_v1_client: Any, bot_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract intent details from a Amazon Lex V1 GetBot response.

    The V1 GetBot response includes intent references (name + version).
    We call GetIntent for each to get full details including fulfillment Lambda ARNs.
    """
    intent_details: list[dict[str, Any]] = []
    intent_refs = bot_response.get("intents", [])

    for ref in intent_refs:
        intent_name = ref.get("intentName")
        intent_version = ref.get("intentVersion", "$LATEST")
        if not intent_name:
            continue
        try:
            intent = lex_v1_client.get_intent(name=intent_name, version=intent_version)
            intent_details.append(intent)
        except Exception:
            logger.exception("Failed to get V1 intent: %s (version %s)", intent_name, intent_version)

    return intent_details


def _get_v1_slot_types(lex_v1_client: Any, bot_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover custom slot types used by a specific Amazon Lex V1 bot.

    Extracts slot type names from the bot's intents, then fetches details
    (enumeration values) for each custom (non-AMAZON.*) type.
    Returns V2-compatible dicts.
    """
    # Collect custom slot type names actually used by this bot's intents
    used_slot_types: set[str] = set()
    for intent_ref in bot_response.get("intents", []):
        intent_name = intent_ref.get("intentName", "")
        intent_version = intent_ref.get("intentVersion", "$LATEST")
        if not intent_name:
            continue
        try:
            intent_detail = lex_v1_client.get_intent(
                name=intent_name, version=intent_version
            )
            for slot in intent_detail.get("slots", []):
                slot_type = slot.get("slotType", "")
                if slot_type and not slot_type.startswith("AMAZON."):
                    used_slot_types.add(slot_type)
        except Exception:
            logger.warning(
                "Failed to get V1 intent '%s' for slot type extraction",
                intent_name,
            )

    logger.info(
        "V1 bot '%s' uses %d custom slot types: %s",
        bot_response.get("name", "?"),
        len(used_slot_types),
        sorted(used_slot_types),
    )

    slot_types: list[dict[str, Any]] = []
    for st_name in sorted(used_slot_types):
        try:
            detail = lex_v1_client.get_slot_type(
                name=st_name, version="$LATEST"
            )
            values = detail.get("enumerationValues", [])
            v2_values: list[dict[str, Any]] = []
            for val in values:
                v2_val: dict[str, Any] = {
                    "sampleValue": {"value": val.get("value", "")}
                }
                synonyms = val.get("synonyms", [])
                if synonyms:
                    v2_val["synonyms"] = [{"value": s} for s in synonyms]
                v2_values.append(v2_val)
            slot_types.append({
                "slotTypeName": st_name,
                "description": detail.get("description", ""),
                "slotTypeValues": v2_values,
                "valueSelectionSetting": {
                    "resolutionStrategy": "TopResolution",
                },
            })
        except Exception:
            logger.warning(
                "Failed to get V1 slot type details for '%s'", st_name
            )

    return slot_types


def _classify_v1_lambda_roles(intents: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Classify Lambda ARNs by their role (fulfillment / codehook) in V1 intents.

    Returns a dict mapping Lambda ARN -> set of roles ("fulfillment", "codehook").
    """
    roles: dict[str, set[str]] = {}

    for intent in intents:
        # fulfillmentActivity -> codeHook -> uri
        fulfillment = intent.get("fulfillmentActivity", {})
        code_hook = fulfillment.get("codeHook", {})
        uri = code_hook.get("uri", "")
        if uri and uri.startswith("arn:") and ":lambda:" in uri:
            roles.setdefault(uri, set()).add("fulfillment")

        # dialogCodeHook -> uri
        dialog_hook = intent.get("dialogCodeHook", {})
        dialog_uri = dialog_hook.get("uri", "")
        if dialog_uri and dialog_uri.startswith("arn:") and ":lambda:" in dialog_uri:
            roles.setdefault(dialog_uri, set()).add("codehook")

    return roles


def _extract_v1_fulfillment_lambda_arns(intents: list[dict[str, Any]]) -> list[str]:
    """Extract Lambda function ARNs from Amazon Lex V1 intent fulfillment configurations.

    V1 intents have a fulfillmentActivity.codeHook.uri field that contains
    the Lambda function ARN, and a dialogCodeHook.uri field.
    """
    return list(_classify_v1_lambda_roles(intents).keys())


def _build_v1_lex_bot_resource(
    bot_name: str,
    bot_response: dict[str, Any],
    aliases: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    fulfillment_lambda_arns: list[str],
    source_region: str,
    account_id: str,
    slot_types: list[dict[str, Any]] | None = None,
    lambda_roles: dict[str, set[str]] | None = None,
) -> LexBotResource:
    """Build a LexBotResource from a Lex V1 GetBot response."""
    # V1 bots use name-based ARNs: arn:aws:lex:<region>:<account>:bot:<name>
    bot_arn = f"arn:aws:lex:{source_region}:{account_id}:bot:{bot_name}"
    locale = bot_response.get("locale", "en-US")

    config_summary = {
        "lex_version": "V1",
        "locales": locale,
        "intents": str(len(intents)),
        "aliases": str(len(aliases)),
        "status": bot_response.get("status", "UNKNOWN"),
    }
    if fulfillment_lambda_arns:
        config_summary["fulfillment_lambdas"] = str(len(fulfillment_lambda_arns))

    # Store lambda role mapping: resource_id -> "fulfillment", "codehook", or "fulfillment, codehook"
    if lambda_roles:
        role_map: dict[str, str] = {}
        for arn, roles in lambda_roles.items():
            rid = _generate_resource_id(arn)
            role_map[rid] = ", ".join(sorted(roles))
        config_summary["lambda_roles"] = json.dumps(role_map)

    resource_id = _generate_resource_id(bot_arn)
    dependencies = [_generate_resource_id(arn) for arn in fulfillment_lambda_arns]

    return LexBotResource(
        id=resource_id,
        name=bot_name,
        arn=bot_arn,
        bot_id=bot_name,  # V1 bots use name as identifier
        locales=[locale],
        intents=intents,
        slot_types=slot_types or [],
        fulfillment_lambda_arns=fulfillment_lambda_arns,
        config_summary=config_summary,
        dependencies=dependencies,
    )


def _build_lex_bot_resource(
    bot_id: str,
    bot_response: dict[str, Any],
    aliases: list[dict[str, Any]],
    locales: list[str],
    intents: list[dict[str, Any]],
    slot_types: list[dict[str, Any]],
    fulfillment_lambda_arns: list[str],
    source_region: str = "unknown",
    account_id: str = "unknown",
    lambda_roles: dict[str, set[str]] | None = None,
) -> LexBotResource:
    """Build a LexBotResource from DescribeBot response and related data."""
    bot_name = bot_response.get("botName", "")
    bot_arn = bot_response.get(
        "botArn",
        f"arn:aws:lex:{source_region}:{account_id}:bot/{bot_id}",
    )

    config_summary = {
        "lex_version": "V2",
        "locales": ", ".join(locales) if locales else "none",
        "intents": str(len(intents)),
        "slot_types": str(len(slot_types)),
        "aliases": str(len(aliases)),
    }
    if fulfillment_lambda_arns:
        config_summary["fulfillment_lambdas"] = str(len(fulfillment_lambda_arns))

    # Store lambda role mapping: resource_id -> "codehook", "fulfillment", or "codehook, fulfillment"
    if lambda_roles:
        role_map: dict[str, str] = {}
        for arn, roles in lambda_roles.items():
            rid = _generate_resource_id(arn)
            role_map[rid] = ", ".join(sorted(roles))
        config_summary["lambda_roles"] = json.dumps(role_map)

    resource_id = _generate_resource_id(bot_arn)

    # Dependencies: Amazon Lex bots depend on their fulfillment AWS Lambda functions
    dependencies = [_generate_resource_id(arn) for arn in fulfillment_lambda_arns]

    return LexBotResource(
        id=resource_id,
        name=bot_name,
        arn=bot_arn,
        bot_id=bot_id,
        locales=locales,
        intents=intents,
        slot_types=slot_types,
        fulfillment_lambda_arns=fulfillment_lambda_arns,
        config_summary=config_summary,
        dependencies=dependencies,
    )


def discover_lex_bots(
    instance_id: str, source_region: str
) -> tuple[list[LexBotResource], list[str]]:
    """Discover all Amazon Lex V1 and V2 bots associated with a Connect instance.

    This function:
    1. Calls Connect ListBots (V1) to get Amazon Lex V1 bot associations
    2. For each V1 bot, calls GetBot, GetBotAliases, GetIntent
    3. Calls Connect ListBots (V2) to get Amazon Lex V2 bot associations
    4. For each V2 bot, calls DescribeBot, ListBotAliases, ListBotLocales
    5. For each V2 locale, calls ListIntents and ListSlotTypes
    6. Extracts fulfillment Lambda ARNs from intent configurations

    Args:
        instance_id: The Connect instance ID.
        source_region: The AWS region of the Connect instance.

    Returns:
        A tuple of:
            - lex_resources: List of discovered LexBotResource objects
            - fulfillment_lambda_arns: List of Lambda ARNs referenced for
              fulfillment (for cross-discovery with Lambda discovery)
    """
    connect_client = create_source_client("connect", source_region)
    lex_v2_client = create_source_client("lexv2-models", source_region)

    lex_resources: list[LexBotResource] = []
    all_fulfillment_lambda_arns: list[str] = []
    seen_lambda_arns: set[str] = set()

    # ---------------------------------------------------------------
    # Amazon Lex V1 discovery
    # ---------------------------------------------------------------
    v1_associations = _get_associated_v1_bots(connect_client, instance_id)
    logger.info(
        "Found %d Lex V1 bot associations for instance %s",
        len(v1_associations),
        instance_id,
    )

    if v1_associations:
        lex_v1_client = create_source_client("lex-models", source_region)

        # Get account ID for ARN construction (from STS)
        try:
            sts_client = create_source_client("sts", source_region)
            account_id = sts_client.get_caller_identity().get("Account", "unknown")
        except Exception:
            logger.exception("Failed to get account ID from STS")
            account_id = "unknown"

        seen_v1_names: set[str] = set()

        for association in v1_associations:
            lex_bot = association.get("LexBot", {})
            bot_name = lex_bot.get("Name")
            if not bot_name or bot_name in seen_v1_names:
                continue
            seen_v1_names.add(bot_name)

            # Get full bot configuration
            bot_response = _get_v1_bot(lex_v1_client, bot_name)
            if bot_response is None:
                logger.warning("Skipping V1 Lex bot (could not retrieve): %s", bot_name)
                continue

            # Get aliases
            aliases = _get_v1_bot_aliases(lex_v1_client, bot_name)

            # Get intent details (includes fulfillment Lambda ARNs)
            intents = _get_v1_intents(lex_v1_client, bot_response)

            # Extract fulfillment Lambda ARNs
            fulfillment_arns = _extract_v1_fulfillment_lambda_arns(intents)

            # Classify Lambda roles (fulfillment vs codehook)
            v1_lambda_roles = _classify_v1_lambda_roles(intents)

            # Track unique Lambda ARNs
            for arn in fulfillment_arns:
                if arn not in seen_lambda_arns:
                    seen_lambda_arns.add(arn)
                    all_fulfillment_lambda_arns.append(arn)

            # Discover V1 custom slot types
            v1_slot_types = _get_v1_slot_types(lex_v1_client, bot_response)

            # Build the resource
            lex_resource = _build_v1_lex_bot_resource(
                bot_name=bot_name,
                bot_response=bot_response,
                aliases=aliases,
                intents=intents,
                fulfillment_lambda_arns=fulfillment_arns,
                source_region=source_region,
                account_id=account_id,
                slot_types=v1_slot_types,
                lambda_roles=v1_lambda_roles,
            )
            lex_resources.append(lex_resource)

    # ---------------------------------------------------------------
    # Amazon Lex V2 discovery
    # ---------------------------------------------------------------
    v2_associations = _get_associated_bots(connect_client, instance_id)
    logger.info(
        "Found %d Lex V2 bot associations for instance %s",
        len(v2_associations),
        instance_id,
    )

    # Get account ID for ARN construction if not already available (from V1 path)
    if not v1_associations:
        try:
            sts_client = create_source_client("sts", source_region)
            account_id = sts_client.get_caller_identity().get("Account", "unknown")
        except Exception:
            logger.exception("Failed to get account ID from STS")
            account_id = "unknown"

    seen_bot_ids: set[str] = set()

    for association in v2_associations:
        # Connect ListBots V2 returns either:
        #   {"LexV2Bot": {"AliasArn": "arn:aws:lex:region:acct:bot-alias/BOT_ID/ALIAS_ID"}}
        # or the older/mock format:
        #   {"LexBot": {"LexBotId": "BOT_ID", ...}}
        lex_v2_bot = association.get("LexV2Bot", {})
        lex_bot = association.get("LexBot", {})

        bot_id = None
        if lex_v2_bot:
            alias_arn = lex_v2_bot.get("AliasArn", "")
            # Extract bot ID from AliasArn format:
            #   arn:aws:lex:region:acct:bot-alias/BOT_ID/ALIAS_ID
            # The resource portion after the last ':' is bot-alias/BOT_ID/ALIAS_ID
            if "bot-alias/" in alias_arn:
                resource_part = alias_arn.split(":")[-1]  # "bot-alias/BOT_ID/ALIAS_ID"
                segments = resource_part.split("/")
                if len(segments) >= 2:
                    bot_id = segments[1]
            if not bot_id:
                bot_id = lex_v2_bot.get("LexBotId")
        if not bot_id:
            bot_id = lex_bot.get("LexBotId") or association.get("LexBotId")

        if not bot_id or bot_id in seen_bot_ids:
            continue
        seen_bot_ids.add(bot_id)

        # Get full bot configuration
        bot_response = _describe_bot(lex_v2_client, bot_id)
        if bot_response is None:
            logger.warning("Skipping Lex V2 bot (could not retrieve): %s", bot_id)
            continue

        # Get all aliases
        aliases = _list_bot_aliases(lex_v2_client, bot_id)

        # Get all locales
        locale_summaries = _list_bot_locales(lex_v2_client, bot_id)
        locale_ids = [loc.get("localeId", "") for loc in locale_summaries]

        # Get intents and slot types for each locale
        all_intents: list[dict[str, Any]] = []
        all_slot_types: list[dict[str, Any]] = []

        for locale_id in locale_ids:
            if not locale_id:
                continue
            intents = _list_intents(lex_v2_client, bot_id, "DRAFT", locale_id)
            all_intents.extend(intents)

            slot_types = _list_slot_types(lex_v2_client, bot_id, "DRAFT", locale_id)
            all_slot_types.extend(slot_types)

        # Extract fulfillment Lambda ARNs
        fulfillment_arns = _extract_fulfillment_lambda_arns(all_intents)

        # Also check aliases for Lambda ARNs
        alias_lambda_arns = _extract_fulfillment_lambda_arns(aliases)
        for arn in alias_lambda_arns:
            if arn not in {a for a in fulfillment_arns}:
                fulfillment_arns.append(arn)

        # Classify Lambda roles (fulfillment vs codehook)
        v2_lambda_roles = _classify_v2_lambda_roles(all_intents, aliases)

        # Track unique Lambda ARNs across all bots
        for arn in fulfillment_arns:
            if arn not in seen_lambda_arns:
                seen_lambda_arns.add(arn)
                all_fulfillment_lambda_arns.append(arn)

        # Build the Amazon Lex bot resource
        lex_resource = _build_lex_bot_resource(
            bot_id=bot_id,
            bot_response=bot_response,
            aliases=aliases,
            locales=locale_ids,
            intents=all_intents,
            slot_types=all_slot_types,
            fulfillment_lambda_arns=fulfillment_arns,
            source_region=source_region,
            account_id=account_id,
            lambda_roles=v2_lambda_roles,
        )
        lex_resources.append(lex_resource)

    logger.info(
        "Lex discovery complete: %d bots (%d V1, %d V2), %d fulfillment Lambda ARNs",
        len(lex_resources),
        len(v1_associations),
        len(v2_associations),
        len(all_fulfillment_lambda_arns),
    )

    return lex_resources, all_fulfillment_lambda_arns
