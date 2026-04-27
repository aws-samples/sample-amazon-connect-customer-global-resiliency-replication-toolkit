"""Unit tests for Lex V2 bot replication (ALGR-only)."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from models.resources import LexBotResource
from replication.lex_replication import (
    replicate_lex_bot,
    _rewrite_fulfillment_lambda_arn,
    _rewrite_intent_fulfillment,
    LexAlgrSkippedError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_REGION = "us-east-1"
SOURCE_REGION = "us-west-2"
ACCOUNT_ID = "123456789012"


def _make_lex_bot(
    name: str = "my-bot",
    bot_id: str = "BOTID123",
    locales: list[str] | None = None,
    intents: list[dict] | None = None,
    slot_types: list[dict] | None = None,
    fulfillment_lambda_arns: list[str] | None = None,
) -> LexBotResource:
    return LexBotResource(
        id="lex-1",
        name=name,
        arn=f"arn:aws:lex:{SOURCE_REGION}:{ACCOUNT_ID}:bot/{bot_id}",
        bot_id=bot_id,
        locales=locales or ["en_US"],
        intents=intents or [],
        slot_types=slot_types or [],
        fulfillment_lambda_arns=fulfillment_lambda_arns or [],
    )


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# _rewrite_fulfillment_lambda_arn
# ---------------------------------------------------------------------------


class TestRewriteFulfillmentLambdaArn:
    def test_uses_mapping_when_present(self):
        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:my-func"
        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"
        mapping = {source_arn: target_arn}
        result = _rewrite_fulfillment_lambda_arn(source_arn, TARGET_REGION, mapping)
        assert result == target_arn

    def test_falls_back_to_region_rewrite(self):
        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:my-func"
        result = _rewrite_fulfillment_lambda_arn(source_arn, TARGET_REGION, {})
        assert result == f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:my-func"

    def test_keeps_invalid_arn_as_is(self):
        result = _rewrite_fulfillment_lambda_arn("arn:bad", TARGET_REGION, {})
        assert result == "arn:bad"


# ---------------------------------------------------------------------------
# _rewrite_intent_fulfillment
# ---------------------------------------------------------------------------


class TestRewriteIntentFulfillment:
    def test_rewrites_lambda_arn_in_nested_dict(self):
        intent = {
            "intentName": "OrderPizza",
            "fulfillmentCodeHook": {
                "uri": f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:order-handler"
            },
        }
        result = _rewrite_intent_fulfillment(intent, TARGET_REGION, {})
        assert result["fulfillmentCodeHook"]["uri"] == (
            f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:order-handler"
        )

    def test_rewrites_lambda_arn_in_list(self):
        data = [f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:fn1"]
        result = _rewrite_intent_fulfillment(data, TARGET_REGION, {})
        assert result == [f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:fn1"]

    def test_leaves_non_lambda_arn_unchanged(self):
        intent = {"resource": f"arn:aws:dynamodb:{SOURCE_REGION}:{ACCOUNT_ID}:table/t"}
        result = _rewrite_intent_fulfillment(intent, TARGET_REGION, {})
        # DynamoDB ARN should NOT be rewritten (only Lambda ARNs)
        assert intent["resource"] == result["resource"]

    def test_leaves_plain_strings_unchanged(self):
        assert _rewrite_intent_fulfillment("hello", TARGET_REGION, {}) == "hello"

    def test_preserves_non_string_values(self):
        assert _rewrite_intent_fulfillment(42, TARGET_REGION, {}) == 42
        assert _rewrite_intent_fulfillment(True, TARGET_REGION, {}) is True

    def test_uses_mapping_for_rewrite(self):
        source_arn = f"arn:aws:lambda:{SOURCE_REGION}:{ACCOUNT_ID}:function:fn"
        target_arn = f"arn:aws:lambda:{TARGET_REGION}:{ACCOUNT_ID}:function:fn-replicated"
        mapping = {source_arn: target_arn}
        intent = {"uri": source_arn}
        result = _rewrite_intent_fulfillment(intent, TARGET_REGION, mapping)
        assert result["uri"] == target_arn


# ---------------------------------------------------------------------------
# replicate_lex_bot — ALGR-only behavior
# ---------------------------------------------------------------------------


class TestReplicateLexBotAlgr:
    """Tests for replicate_lex_bot using ALGR exclusively (no legacy fallback)."""

    @patch("replication.lex_replication._try_algr_replication")
    def test_algr_success_returns_arn(self, mock_algr):
        expected_arn = f"arn:aws:lex:{TARGET_REGION}:{ACCOUNT_ID}:bot/BOTID123"
        mock_algr.return_value = expected_arn
        bot = _make_lex_bot()
        result = replicate_lex_bot(bot, TARGET_REGION)
        assert result == expected_arn

    @patch("replication.lex_replication._update_algr_bot_lambda_arns")
    @patch("replication.lex_replication._try_algr_replication")
    def test_algr_success_updates_lambda_arns(self, mock_algr, mock_update):
        expected_arn = f"arn:aws:lex:{TARGET_REGION}:{ACCOUNT_ID}:bot/BOTID123"
        mock_algr.return_value = expected_arn
        bot = _make_lex_bot()
        mapping = {"arn:aws:lambda:us-west-2:123:function:fn": "arn:aws:lambda:us-east-1:123:function:fn"}
        result = replicate_lex_bot(bot, TARGET_REGION, lambda_arn_mapping=mapping)
        assert result == expected_arn
        mock_update.assert_called_once_with("BOTID123", TARGET_REGION, mapping)

    @patch("replication.lex_replication._is_algr_supported_pair", return_value=True)
    @patch("replication.lex_replication._try_algr_replication", return_value=None)
    def test_algr_failure_in_supported_pair_raises_runtime_error(self, mock_algr, mock_pair):
        """When ALGR fails for a supported pair, raise RuntimeError (no fallback)."""
        bot = _make_lex_bot()
        with pytest.raises(RuntimeError, match="ALGR replication failed"):
            replicate_lex_bot(bot, TARGET_REGION)

    @patch("replication.lex_replication._is_algr_supported_pair", return_value=False)
    @patch("replication.lex_replication._try_algr_replication", return_value=None)
    def test_unsupported_pair_raises_skipped_error(self, mock_algr, mock_pair):
        """When ALGR is not supported for the pair, raise LexAlgrSkippedError."""
        bot = _make_lex_bot()
        with pytest.raises(LexAlgrSkippedError, match="not supported"):
            replicate_lex_bot(bot, TARGET_REGION)

    @patch("replication.lex_replication._update_algr_bot_lambda_arns")
    @patch("replication.lex_replication._try_algr_replication")
    def test_algr_lambda_update_failure_still_returns_arn(self, mock_algr, mock_update):
        """Lambda ARN update failure is non-fatal — bot ARN is still returned."""
        expected_arn = f"arn:aws:lex:{TARGET_REGION}:{ACCOUNT_ID}:bot/BOTID123"
        mock_algr.return_value = expected_arn
        mock_update.side_effect = Exception("update failed")
        bot = _make_lex_bot()
        mapping = {"arn:aws:lambda:us-west-2:123:function:fn": "arn:aws:lambda:us-east-1:123:function:fn"}
        result = replicate_lex_bot(bot, TARGET_REGION, lambda_arn_mapping=mapping)
        assert result == expected_arn

    def test_replicate_lex_bot_has_no_lex_fallback_enabled_param(self):
        """Verify the lex_fallback_enabled parameter has been removed."""
        import inspect
        sig = inspect.signature(replicate_lex_bot)
        assert "lex_fallback_enabled" not in sig.parameters

    def test_lex_algr_skipped_error_exists(self):
        """Verify LexAlgrSkippedError is still importable."""
        assert issubclass(LexAlgrSkippedError, Exception)


# ---------------------------------------------------------------------------
# Verify legacy functions are removed
# ---------------------------------------------------------------------------


class TestLegacyFunctionsRemoved:
    """Verify that legacy bot recreation functions are no longer importable."""

    def test_create_bot_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_bot  # noqa: F401

    def test_create_bot_locale_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_bot_locale  # noqa: F401

    def test_create_slot_types_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_slot_types  # noqa: F401

    def test_create_intents_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_intents  # noqa: F401

    def test_create_intent_slots_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_intent_slots  # noqa: F401

    def test_build_bot_locale_is_internal_only(self):
        """_build_bot_locale still exists but only for ALGR locale rebuilds."""
        from replication.lex_replication import _build_bot_locale
        assert callable(_build_bot_locale)

    def test_create_bot_alias_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_bot_alias  # noqa: F401

    def test_map_v1_slot_type_to_v2_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _map_v1_slot_type_to_v2  # noqa: F401

    def test_get_existing_bot_id_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _get_existing_bot_id  # noqa: F401
