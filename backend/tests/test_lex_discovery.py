"""Unit tests for Lex bot discovery module."""

from unittest.mock import MagicMock, patch

import pytest

from discovery.lex_discovery import (
    _extract_from_dict,
    _extract_fulfillment_lambda_arns,
    _generate_resource_id,
    _get_v1_slot_types,
    discover_lex_bots,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
REGION = "us-west-2"
INSTANCE_ID = "abc-def-123"


def _lambda_arn(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"


def _bot_arn(bot_id: str) -> str:
    return f"arn:aws:lex:{REGION}:{ACCOUNT}:bot/{bot_id}"


def _make_bot_association(bot_id: str, alias_arn: str | None = None) -> dict:
    """Legacy/mock format: {"LexBot": {"LexBotId": ...}}."""
    assoc = {"LexBot": {"LexBotId": bot_id}}
    if alias_arn:
        assoc["LexBot"]["AliasArn"] = alias_arn
    return assoc


def _make_v2_bot_association(bot_id: str, alias_id: str = "TSTALIASID") -> dict:
    """Real AWS Connect ListBots V2 format: {"LexV2Bot": {"AliasArn": ...}}."""
    alias_arn = f"arn:aws:lex:{REGION}:{ACCOUNT}:bot-alias/{bot_id}/{alias_id}"
    return {"LexV2Bot": {"AliasArn": alias_arn}}


def _make_describe_bot_response(bot_id: str, bot_name: str) -> dict:
    return {
        "botId": bot_id,
        "botName": bot_name,
        "botArn": _bot_arn(bot_id),
        "botStatus": "Available",
        "roleArn": f"arn:aws:iam::{ACCOUNT}:role/lex-bot-role",
    }


def _make_alias_summary(alias_id: str, alias_name: str) -> dict:
    return {"botAliasId": alias_id, "botAliasName": alias_name, "botAliasStatus": "Available"}


def _make_locale_summary(locale_id: str) -> dict:
    return {"localeId": locale_id, "localeName": locale_id, "botLocaleStatus": "Built"}


def _make_intent_summary(intent_id: str, intent_name: str, lambda_arn: str | None = None) -> dict:
    intent = {"intentId": intent_id, "intentName": intent_name}
    if lambda_arn:
        intent["fulfillmentCodeHook"] = {"fulfillmentLambdaArn": lambda_arn}
    return intent


def _make_slot_type_summary(slot_type_id: str, slot_type_name: str) -> dict:
    return {"slotTypeId": slot_type_id, "slotTypeName": slot_type_name}


# ---------------------------------------------------------------------------
# Tests for helper functions
# ---------------------------------------------------------------------------


class TestGenerateResourceId:
    def test_deterministic(self):
        arn = _bot_arn("TESTBOT123")
        assert _generate_resource_id(arn) == _generate_resource_id(arn)

    def test_different_arns_produce_different_ids(self):
        assert _generate_resource_id(_bot_arn("bot-a")) != _generate_resource_id(_bot_arn("bot-b"))

    def test_returns_12_char_hex(self):
        rid = _generate_resource_id(_bot_arn("test"))
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestExtractFulfillmentLambdaArns:
    def test_no_intents(self):
        assert _extract_fulfillment_lambda_arns([]) == []

    def test_intent_with_lambda_arn(self):
        arn = _lambda_arn("my-fulfillment")
        intents = [_make_intent_summary("int-1", "OrderPizza", lambda_arn=arn)]
        result = _extract_fulfillment_lambda_arns(intents)
        assert result == [arn]

    def test_intent_without_lambda(self):
        intents = [_make_intent_summary("int-1", "Greeting")]
        result = _extract_fulfillment_lambda_arns(intents)
        assert result == []

    def test_deduplicates_same_lambda(self):
        arn = _lambda_arn("shared-func")
        intents = [
            _make_intent_summary("int-1", "Intent1", lambda_arn=arn),
            _make_intent_summary("int-2", "Intent2", lambda_arn=arn),
        ]
        result = _extract_fulfillment_lambda_arns(intents)
        assert result == [arn]

    def test_multiple_different_lambdas(self):
        arn1 = _lambda_arn("func-a")
        arn2 = _lambda_arn("func-b")
        intents = [
            _make_intent_summary("int-1", "Intent1", lambda_arn=arn1),
            _make_intent_summary("int-2", "Intent2", lambda_arn=arn2),
        ]
        result = _extract_fulfillment_lambda_arns(intents)
        assert set(result) == {arn1, arn2}

    def test_nested_lambda_arn_in_dict(self):
        arn = _lambda_arn("deep-func")
        intents = [{"nested": {"deep": {"lambdaArn": arn}}}]
        result = _extract_fulfillment_lambda_arns(intents)
        assert result == [arn]


class TestExtractFromDict:
    def test_string_lambda_arn(self):
        arns: list[str] = []
        seen: set[str] = set()
        _extract_from_dict(_lambda_arn("test"), arns, seen)
        assert arns == [_lambda_arn("test")]

    def test_non_lambda_arn_ignored(self):
        arns: list[str] = []
        seen: set[str] = set()
        _extract_from_dict("arn:aws:s3:::my-bucket", arns, seen)
        assert arns == []

    def test_plain_string_ignored(self):
        arns: list[str] = []
        seen: set[str] = set()
        _extract_from_dict("just a string", arns, seen)
        assert arns == []


# ---------------------------------------------------------------------------
# Tests for discover_lex_bots
# ---------------------------------------------------------------------------


class TestDiscoverLexBots:
    """Tests for the main discover_lex_bots orchestration."""

    def _setup_mocks(self, mock_create_client, mock_connect, mock_lex):
        """Set up mocks for V2-only tests. V1 returns empty, STS returns dummy account."""
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT}
        mock_v1 = MagicMock()

        def side_effect(service, region):
            if service == "connect":
                return mock_connect
            if service == "lexv2-models":
                return mock_lex
            if service == "lex-models":
                return mock_v1
            if service == "sts":
                return mock_sts
            raise ValueError(f"Unexpected service: {service}")

        mock_create_client.side_effect = side_effect

        # Make describe_intent fall back to summaries (simulates API not available)
        # This ensures _list_intents uses the summary data from list_intents
        if not mock_lex.describe_intent.side_effect:
            mock_lex.describe_intent.side_effect = Exception("Not mocked")

        # Make describe_bot_alias fall back to summaries (simulates API not available)
        # This ensures _list_bot_aliases uses the summary data from list_bot_aliases
        if not mock_lex.describe_bot_alias.side_effect:
            mock_lex.describe_bot_alias.side_effect = Exception("Not mocked")

        # Wrap list_bots so V1 calls return empty while V2 calls use the
        # return_value / side_effect already configured by the test.
        orig_rv = mock_connect.list_bots.return_value
        orig_se = mock_connect.list_bots.side_effect

        if orig_se is not None and not callable(orig_se):
            # side_effect is a list (for pagination tests) — convert to an iterator
            v2_iter = iter(orig_se)

            def _list_bots_dispatch(**kwargs):
                if kwargs.get("LexVersion") == "V1":
                    return {"LexBots": []}
                return next(v2_iter)
        else:
            def _list_bots_dispatch(**kwargs):
                if kwargs.get("LexVersion") == "V1":
                    return {"LexBots": []}
                if orig_se is not None:
                    return orig_se(**kwargs)
                return orig_rv

        mock_connect.list_bots.side_effect = _list_bots_dispatch

    @patch("discovery.lex_discovery.create_source_client")
    def test_no_bots(self, mock_create_client):
        """When Connect returns no bots, result should be empty."""
        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {"LexBots": []}
        mock_lex = MagicMock()
        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert bots == []
        assert lambda_arns == []
        # list_bots is called twice: once for V1, once for V2
        assert mock_connect.list_bots.call_count == 2

    @patch("discovery.lex_discovery.create_source_client")
    def test_single_bot_no_fulfillment(self, mock_create_client):
        """Discover a single bot with no fulfillment Lambda."""
        bot_id = "BOT123"
        bot_name = "OrderBot"

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [_make_bot_association(bot_id)]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = _make_describe_bot_response(bot_id, bot_name)
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [_make_alias_summary("TSTALIASID", "TestAlias")]
        }
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {
            "intentSummaries": [_make_intent_summary("int-1", "Greeting")]
        }
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        assert bots[0].name == bot_name
        assert bots[0].bot_id == bot_id
        assert bots[0].arn == _bot_arn(bot_id)
        assert bots[0].locales == ["en_US"]
        assert len(bots[0].intents) == 1
        assert bots[0].fulfillment_lambda_arns == []
        assert bots[0].dependencies == []
        assert lambda_arns == []

    @patch("discovery.lex_discovery.create_source_client")
    def test_bot_with_fulfillment_lambda(self, mock_create_client):
        """Bot with a fulfillment Lambda should extract the ARN."""
        bot_id = "BOT456"
        bot_name = "PaymentBot"
        fulfillment_arn = _lambda_arn("payment-handler")

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [_make_bot_association(bot_id)]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = _make_describe_bot_response(bot_id, bot_name)
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {
            "intentSummaries": [
                _make_intent_summary("int-1", "MakePayment", lambda_arn=fulfillment_arn)
            ]
        }
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        assert bots[0].fulfillment_lambda_arns == [fulfillment_arn]
        assert lambda_arns == [fulfillment_arn]
        # Bot should depend on the fulfillment Lambda
        expected_dep = _generate_resource_id(fulfillment_arn)
        assert expected_dep in bots[0].dependencies

    @patch("discovery.lex_discovery.create_source_client")
    def test_multiple_bots(self, mock_create_client):
        """Discover multiple bots with different configurations."""
        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [
                _make_bot_association("BOT-A"),
                _make_bot_association("BOT-B"),
            ]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.side_effect = [
            _make_describe_bot_response("BOT-A", "BotAlpha"),
            _make_describe_bot_response("BOT-B", "BotBeta"),
        ]
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {
            "intentSummaries": [_make_intent_summary("int-1", "Hello")]
        }
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 2
        assert {b.name for b in bots} == {"BotAlpha", "BotBeta"}

    @patch("discovery.lex_discovery.create_source_client")
    def test_describe_bot_failure_skips_bot(self, mock_create_client):
        """If DescribeBot fails, the bot should be skipped gracefully."""
        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [
                _make_bot_association("GOOD-BOT"),
                _make_bot_association("BAD-BOT"),
            ]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.side_effect = [
            _make_describe_bot_response("GOOD-BOT", "GoodBot"),
            RuntimeError("Access denied"),
        ]
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {"intentSummaries": []}
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        assert bots[0].name == "GoodBot"

    @patch("discovery.lex_discovery.create_source_client")
    def test_duplicate_bot_ids_deduplicated(self, mock_create_client):
        """If Connect returns the same bot ID twice, it should only be processed once."""
        bot_id = "DUP-BOT"

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [
                _make_bot_association(bot_id),
                _make_bot_association(bot_id),
            ]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = _make_describe_bot_response(bot_id, "DupBot")
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {"intentSummaries": []}
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        # DescribeBot should only be called once
        mock_lex.describe_bot.assert_called_once_with(botId=bot_id)

    @patch("discovery.lex_discovery.create_source_client")
    def test_multiple_locales(self, mock_create_client):
        """Bot with multiple locales should collect intents and slots from all."""
        bot_id = "MULTI-LOCALE"

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [_make_bot_association(bot_id)]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = _make_describe_bot_response(bot_id, "MultiLocaleBot")
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [
                _make_locale_summary("en_US"),
                _make_locale_summary("es_ES"),
            ]
        }
        # Return different intents for each locale
        mock_lex.list_intents.side_effect = [
            {"intentSummaries": [_make_intent_summary("int-en", "EnglishGreeting")]},
            {"intentSummaries": [_make_intent_summary("int-es", "SpanishGreeting")]},
        ]
        mock_lex.list_slot_types.side_effect = [
            {"slotTypeSummaries": [_make_slot_type_summary("st-en", "EnglishSlot")]},
            {"slotTypeSummaries": [_make_slot_type_summary("st-es", "SpanishSlot")]},
        ]

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        assert bots[0].locales == ["en_US", "es_ES"]
        assert len(bots[0].intents) == 2
        assert len(bots[0].slot_types) == 2

    @patch("discovery.lex_discovery.create_source_client")
    def test_pagination_of_bots(self, mock_create_client):
        """Connect ListBots pagination should be handled."""
        mock_connect = MagicMock()
        mock_connect.list_bots.side_effect = [
            {"LexBots": [_make_bot_association("BOT-1")], "NextToken": "token1"},
            {"LexBots": [_make_bot_association("BOT-2")]},
        ]

        mock_lex = MagicMock()
        mock_lex.describe_bot.side_effect = [
            _make_describe_bot_response("BOT-1", "Bot1"),
            _make_describe_bot_response("BOT-2", "Bot2"),
        ]
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {"intentSummaries": []}
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 2
        assert {b.name for b in bots} == {"Bot1", "Bot2"}

    @patch("discovery.lex_discovery.create_source_client")
    def test_shared_fulfillment_lambda_across_bots(self, mock_create_client):
        """Two bots sharing the same fulfillment Lambda should deduplicate in returned ARNs."""
        shared_arn = _lambda_arn("shared-handler")

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [
                _make_bot_association("BOT-X"),
                _make_bot_association("BOT-Y"),
            ]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.side_effect = [
            _make_describe_bot_response("BOT-X", "BotX"),
            _make_describe_bot_response("BOT-Y", "BotY"),
        ]
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {
            "intentSummaries": [
                _make_intent_summary("int-1", "DoSomething", lambda_arn=shared_arn)
            ]
        }
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 2
        # Both bots reference the same Lambda
        assert bots[0].fulfillment_lambda_arns == [shared_arn]
        assert bots[1].fulfillment_lambda_arns == [shared_arn]
        # But the returned list should have it only once
        assert lambda_arns == [shared_arn]

    @patch("discovery.lex_discovery.create_source_client")
    def test_config_summary_populated(self, mock_create_client):
        """Config summary should contain locale, intent, slot type, and alias counts."""
        bot_id = "SUMMARY-BOT"
        fulfillment_arn = _lambda_arn("summary-func")

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [_make_bot_association(bot_id)]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = _make_describe_bot_response(bot_id, "SummaryBot")
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [
                _make_alias_summary("alias-1", "Prod"),
                _make_alias_summary("alias-2", "Dev"),
            ]
        }
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {
            "intentSummaries": [
                _make_intent_summary("int-1", "Order", lambda_arn=fulfillment_arn),
                _make_intent_summary("int-2", "Cancel"),
            ]
        }
        mock_lex.list_slot_types.return_value = {
            "slotTypeSummaries": [_make_slot_type_summary("st-1", "PizzaType")]
        }

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, _ = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        summary = bots[0].config_summary
        assert summary["locales"] == "en_US"
        assert summary["intents"] == "2"
        assert summary["slot_types"] == "1"
        assert summary["aliases"] == "2"
        assert summary["fulfillment_lambdas"] == "1"

    @patch("discovery.lex_discovery.create_source_client")
    def test_v2_bot_association_format(self, mock_create_client):
        """Real AWS Connect ListBots V2 returns LexV2Bot with AliasArn."""
        bot_id = "YDJIL968WO"

        mock_connect = MagicMock()
        mock_connect.list_bots.return_value = {
            "LexBots": [_make_v2_bot_association(bot_id, "TSTALIASID")]
        }

        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = _make_describe_bot_response(bot_id, "RealV2Bot")
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [_make_alias_summary("TSTALIASID", "TestBotAlias")]
        }
        mock_lex.list_bot_locales.return_value = {
            "botLocaleSummaries": [_make_locale_summary("en_US")]
        }
        mock_lex.list_intents.return_value = {"intentSummaries": []}
        mock_lex.list_slot_types.return_value = {"slotTypeSummaries": []}

        self._setup_mocks(mock_create_client, mock_connect, mock_lex)

        bots, lambda_arns = discover_lex_bots(INSTANCE_ID, REGION)

        assert len(bots) == 1
        assert bots[0].bot_id == bot_id
        assert bots[0].name == "RealV2Bot"
        assert lambda_arns == []


# ---------------------------------------------------------------------------
# _get_v1_slot_types
# ---------------------------------------------------------------------------


class TestGetV1SlotTypes:
    def test_discovers_custom_slot_types(self):
        mock_client = MagicMock()
        mock_client.get_slot_types.return_value = {
            "slotTypes": [
                {"name": "RoomTypeValues"},
                {"name": "CarTypeValues"},
                {"name": "AMAZON.NUMBER"},  # Should be skipped
            ]
        }

        # New implementation extracts slot types from bot intents
        bot_response = {
            "name": "TestBot",
            "intents": [
                {"intentName": "BookHotel", "intentVersion": "$LATEST"},
                {"intentName": "BookCar", "intentVersion": "$LATEST"},
            ],
        }
        mock_client.get_intent.side_effect = [
            {
                "name": "BookHotel",
                "slots": [
                    {"name": "RoomType", "slotType": "RoomTypeValues"},
                    {"name": "Nights", "slotType": "AMAZON.NUMBER"},
                ],
            },
            {
                "name": "BookCar",
                "slots": [
                    {"name": "CarType", "slotType": "CarTypeValues"},
                ],
            },
        ]
        mock_client.get_slot_type.side_effect = [
            {
                "name": "CarTypeValues",
                "description": "Car types",
                "enumerationValues": [
                    {"value": "sedan"},
                ],
            },
            {
                "name": "RoomTypeValues",
                "description": "Room types",
                "enumerationValues": [
                    {"value": "king", "synonyms": ["large"]},
                    {"value": "queen"},
                ],
            },
        ]

        result = _get_v1_slot_types(mock_client, bot_response)

        assert len(result) == 2
        # Results are sorted alphabetically
        assert result[0]["slotTypeName"] == "CarTypeValues"
        assert result[1]["slotTypeName"] == "RoomTypeValues"
        assert len(result[1]["slotTypeValues"]) == 2
        assert result[1]["slotTypeValues"][0]["sampleValue"]["value"] == "king"
        assert result[1]["slotTypeValues"][0]["synonyms"] == [{"value": "large"}]

    def test_returns_empty_when_no_custom_types(self):
        mock_client = MagicMock()
        # Bot with only built-in slot types
        bot_response = {
            "name": "TestBot",
            "intents": [
                {"intentName": "SimpleIntent", "intentVersion": "$LATEST"},
            ],
        }
        mock_client.get_intent.return_value = {
            "name": "SimpleIntent",
            "slots": [
                {"name": "Count", "slotType": "AMAZON.NUMBER"},
            ],
        }

        result = _get_v1_slot_types(mock_client, bot_response)
        assert result == []

    def test_skips_individual_slot_type_on_failure(self):
        mock_client = MagicMock()
        bot_response = {
            "name": "TestBot",
            "intents": [
                {"intentName": "TestIntent", "intentVersion": "$LATEST"},
            ],
        }
        mock_client.get_intent.return_value = {
            "name": "TestIntent",
            "slots": [
                {"name": "SlotA", "slotType": "TypeA"},
                {"name": "SlotB", "slotType": "TypeB"},
            ],
        }
        mock_client.get_slot_type.side_effect = [
            Exception("Failed"),
            {"name": "TypeB", "description": "", "enumerationValues": [{"value": "x"}]},
        ]

        result = _get_v1_slot_types(mock_client, bot_response)
        assert len(result) == 1
        assert result[0]["slotTypeName"] == "TypeB"
