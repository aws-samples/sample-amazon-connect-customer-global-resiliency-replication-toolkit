"""Unit tests for Lex V2 bot replication (ALGR-only)."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from models.resources import LexBotResource
from replication.lex_replication import (
    replicate_lex_bot,
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

    @patch("replication.lex_replication.create_target_client")
    @patch("replication.lex_replication.create_source_client")
    @patch("replication.lex_replication._try_algr_replication")
    def test_algr_success_leaves_the_replica_bot_alone(self, mock_algr, mock_src, mock_tgt):
        """ALGR keeps the replica in sync and the replica cannot be modified: after
        CreateBotReplica the tool makes no further Lex calls, even with a Lambda mapping."""
        expected_arn = f"arn:aws:lex:{TARGET_REGION}:{ACCOUNT_ID}:bot/BOTID123"
        mock_algr.return_value = expected_arn
        bot = _make_lex_bot()
        mapping = {"arn:aws:lambda:us-west-2:123:function:fn": "arn:aws:lambda:us-east-1:123:function:fn"}
        result = replicate_lex_bot(bot, TARGET_REGION, lambda_arn_mapping=mapping)
        assert result == expected_arn
        mock_src.assert_not_called()
        mock_tgt.assert_not_called()

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

    @pytest.mark.parametrize("name", [
        "_build_bot_locale", "_update_algr_bot_lambda_arns",
        "_rewrite_intent_fulfillment", "_rewrite_fulfillment_lambda_arn",
        "_associate_lex_bot_with_connect", "_wait_for_bot_available", "_normalize_locale_id",
    ])
    def test_replica_editing_helpers_are_removed(self, name):
        """ALGR keeps the replica in sync and the replica cannot be modified."""
        import replication.lex_replication as lr
        assert not hasattr(lr, name)

    def test_create_bot_alias_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _create_bot_alias  # noqa: F401

    def test_map_v1_slot_type_to_v2_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _map_v1_slot_type_to_v2  # noqa: F401

    def test_get_existing_bot_id_not_importable(self):
        with pytest.raises(ImportError):
            from replication.lex_replication import _get_existing_bot_id  # noqa: F401


class TestLexV1NotReplicatedByAlgr:
    def test_v1_bot_is_skipped_without_calling_algr(self):
        bot = LexBotResource(
            id="v1-1", name="OrderBot",
            arn=f"arn:aws:lex:{SOURCE_REGION}:{ACCOUNT_ID}:bot:OrderBot",
            bot_id="OrderBot", config_summary={"lex_version": "V1"},
        )
        with patch("replication.lex_replication._try_algr_replication") as algr:
            with pytest.raises(LexAlgrSkippedError, match="Lex V1"):
                replicate_lex_bot(bot, TARGET_REGION)
        algr.assert_not_called()
