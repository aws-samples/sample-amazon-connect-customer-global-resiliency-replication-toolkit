"""Unit tests for the resource association module."""

from unittest.mock import MagicMock, patch, call
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from association.resource_association import (
    associate_resources,
    _enable_instance_attributes,
    _list_associated_lambdas,
    _list_associated_bots,
    _associate_lambda,
    _associate_lex_bot,
    _associate_kinesis_stream,
    _associate_firehose,
    _associate_kvs_stream,
    _associate_s3_bucket,
    _check_storage_config_exists,
    _get_bot_alias_arn,
    _find_latest_bot_version,
    _create_alias_on_source_bot,
    _read_source_storage_configs,
    _build_source_mappings,
)
from models.enums import ReplicationStatus, ResourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INSTANCE_ARN = "arn:aws:connect:us-east-1:123456789012:instance/abc-def-123"
INSTANCE_ID = "abc-def-123"
TARGET_REGION = "us-east-1"
SOURCE_REGION = "us-east-1"


def _make_resource(name, rtype, replicated_arn=None, status=ReplicationStatus.REPLICATED, arn=None):
    return SimpleNamespace(
        name=name,
        resource_type=rtype,
        replicated_arn=replicated_arn,
        status=status,
        arn=arn or replicated_arn or "",
        storage_types=[],
    )


def _make_session(resources=None, instance_arn=INSTANCE_ARN, target_region=TARGET_REGION, source_region=SOURCE_REGION):
    inv = {}
    for i, r in enumerate(resources or []):
        inv[f"r{i}"] = r
    return SimpleNamespace(
        instance_arn=instance_arn,
        target_region=target_region,
        source_region=source_region,
        inventory=inv,
    )


def _client_error(code="ResourceNotFoundException", msg="Not found"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "op")


# ---------------------------------------------------------------------------
# _enable_instance_attributes
# ---------------------------------------------------------------------------

class TestEnableInstanceAttributes:
    def test_all_already_enabled(self):
        mock_client = MagicMock()
        mock_client.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "true"}
        }
        results = _enable_instance_attributes(mock_client, INSTANCE_ID)
        assert len(results) == 3
        assert all(r["status"] == "already_enabled" for r in results)

    def test_enables_disabled_attributes(self):
        mock_client = MagicMock()
        mock_client.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "false"}
        }
        results = _enable_instance_attributes(mock_client, INSTANCE_ID)
        assert len(results) == 3
        assert all(r["status"] == "enabled" for r in results)
        assert mock_client.update_instance_attribute.call_count == 3

    def test_handles_update_error(self):
        mock_client = MagicMock()
        mock_client.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "false"}
        }
        mock_client.update_instance_attribute.side_effect = _client_error("InternalServiceException")
        results = _enable_instance_attributes(mock_client, INSTANCE_ID)
        assert all(r["status"] == "error" for r in results)

    def test_skips_unsupported_attributes(self):
        mock_client = MagicMock()
        mock_client.describe_instance_attribute.side_effect = _client_error("InvalidParameterException")
        results = _enable_instance_attributes(mock_client, INSTANCE_ID)
        assert len(results) == 0

    @patch("association.resource_association.create_source_client")
    def test_mirrors_lex_attrs_from_source(self, mock_create_src):
        """Lex attributes (BOT_MANAGEMENT, ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS,
        MESSAGE_STREAMING) are mirrored from source when enabled."""
        mock_target = MagicMock()
        mock_source = MagicMock()
        mock_create_src.return_value = mock_source

        # Source: all Lex attrs enabled
        mock_source.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "true"}
        }
        # Target: always-enable attrs already on, Lex attrs off
        def _target_describe(InstanceId, AttributeType):
            if AttributeType in ("CONTACTFLOW_LOGS", "CONTACT_LENS", "EARLY_MEDIA"):
                return {"Attribute": {"Value": "true"}}
            return {"Attribute": {"Value": "false"}}

        mock_target.describe_instance_attribute.side_effect = _target_describe

        results = _enable_instance_attributes(
            mock_target, INSTANCE_ID,
            source_region="us-east-1", source_instance_id="src-id",
        )
        # 3 always-enable (already_enabled) + 3 Lex mirrored (enabled)
        assert len(results) == 6
        lex_results = [r for r in results if r["status"] == "enabled"]
        assert len(lex_results) == 3
        lex_names = {r["resource"] for r in lex_results}
        assert "Instance Attribute: Lex Bot Management" in lex_names
        assert "Instance Attribute: Bot Analytics and Transcripts" in lex_names
        assert "Instance Attribute: Message Streaming" in lex_names

    @patch("association.resource_association.create_source_client")
    def test_skips_lex_attrs_when_source_disabled(self, mock_create_src):
        """Lex attributes are skipped when not enabled on source."""
        mock_target = MagicMock()
        mock_source = MagicMock()
        mock_create_src.return_value = mock_source

        mock_source.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "false"}
        }
        mock_target.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "true"}
        }

        results = _enable_instance_attributes(
            mock_target, INSTANCE_ID,
            source_region="us-east-1", source_instance_id="src-id",
        )
        # 3 always-enable (already_enabled) + 3 Lex skipped
        assert len(results) == 6
        skipped = [r for r in results if r["status"] == "skipped"]
        assert len(skipped) == 3

    @patch("association.resource_association.create_source_client")
    def test_correct_lex_attribute_types_used(self, mock_create_src):
        """Verify the exact API attribute types passed to describe/update."""
        mock_target = MagicMock()
        mock_source = MagicMock()
        mock_create_src.return_value = mock_source

        mock_source.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "true"}
        }
        mock_target.describe_instance_attribute.return_value = {
            "Attribute": {"Value": "false"}
        }

        _enable_instance_attributes(
            mock_target, INSTANCE_ID,
            source_region="us-east-1", source_instance_id="src-id",
        )

        # Collect all AttributeType values passed to update on target
        update_calls = mock_target.update_instance_attribute.call_args_list
        updated_types = {c.kwargs.get("AttributeType", c[1].get("AttributeType", ""))
                         if c.kwargs else c[1]["AttributeType"]
                         for c in update_calls}
        # Should include the 3 always-enable + 3 correct Lex types
        for expected in ("BOT_MANAGEMENT", "ENABLE_BOT_ANALYTICS_AND_TRANSCRIPTS", "MESSAGE_STREAMING"):
            assert expected in updated_types, f"{expected} not in update calls"


# ---------------------------------------------------------------------------
# _list_associated_lambdas / _list_associated_bots
# ---------------------------------------------------------------------------

class TestListAssociatedLambdas:
    def test_returns_arns(self):
        mock_client = MagicMock()
        mock_client.list_lambda_functions.return_value = {
            "LambdaFunctions": ["arn:aws:lambda:us-east-1:123:function:fn1"],
        }
        result = _list_associated_lambdas(mock_client, INSTANCE_ID)
        assert "arn:aws:lambda:us-east-1:123:function:fn1" in result

    def test_paginates(self):
        mock_client = MagicMock()
        mock_client.list_lambda_functions.side_effect = [
            {"LambdaFunctions": ["arn1"], "NextToken": "tok"},
            {"LambdaFunctions": ["arn2"]},
        ]
        result = _list_associated_lambdas(mock_client, INSTANCE_ID)
        assert result == {"arn1", "arn2"}

    def test_handles_error(self):
        mock_client = MagicMock()
        mock_client.list_lambda_functions.side_effect = _client_error()
        result = _list_associated_lambdas(mock_client, INSTANCE_ID)
        assert result == set()


class TestListAssociatedBots:
    def test_returns_alias_arns(self):
        mock_client = MagicMock()
        mock_client.list_bots.return_value = {
            "LexBots": [{"LexV2Bot": {"AliasArn": "arn:aws:lex:us-east-1:123:bot-alias/BOT/ALIAS"}}],
        }
        result = _list_associated_bots(mock_client, INSTANCE_ID)
        assert "arn:aws:lex:us-east-1:123:bot-alias/BOT/ALIAS" in result

    def test_handles_error(self):
        mock_client = MagicMock()
        mock_client.list_bots.side_effect = _client_error()
        result = _list_associated_bots(mock_client, INSTANCE_ID)
        assert result == set()


# ---------------------------------------------------------------------------
# _associate_lambda
# ---------------------------------------------------------------------------

class TestAssociateLambda:
    def test_already_associated(self):
        mock_client = MagicMock()
        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1")
        result = _associate_lambda(mock_client, INSTANCE_ID, resource, {"arn:lambda:fn1"})
        assert result["status"] == "already_associated"
        mock_client.associate_lambda_function.assert_not_called()

    def test_associates_successfully(self):
        mock_client = MagicMock()
        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1")
        result = _associate_lambda(mock_client, INSTANCE_ID, resource, set())
        assert result["status"] == "associated"
        mock_client.associate_lambda_function.assert_called_once()

    def test_handles_duplicate_error(self):
        mock_client = MagicMock()
        mock_client.associate_lambda_function.side_effect = _client_error(
            "ResourceConflictException", "already associated"
        )
        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1")
        result = _associate_lambda(mock_client, INSTANCE_ID, resource, set())
        assert result["status"] == "already_associated"

    def test_handles_error(self):
        mock_client = MagicMock()
        mock_client.associate_lambda_function.side_effect = _client_error(
            "InternalServiceException", "boom"
        )
        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1")
        result = _associate_lambda(mock_client, INSTANCE_ID, resource, set())
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# _associate_lex_bot
# ---------------------------------------------------------------------------

class TestAssociateLexBot:
    @patch("association.resource_association._get_bot_alias_arn", return_value="arn:lex:alias")
    def test_associates_successfully(self, mock_alias):
        mock_client = MagicMock()
        resource = _make_resource("bot1", ResourceType.LEX_BOT, "arn:lex:bot1")
        result = _associate_lex_bot(mock_client, INSTANCE_ID, resource, set(), TARGET_REGION)
        assert result["status"] == "associated"

    @patch("association.resource_association._get_bot_alias_arn", return_value="arn:lex:alias")
    def test_already_associated(self, mock_alias):
        mock_client = MagicMock()
        resource = _make_resource("bot1", ResourceType.LEX_BOT, "arn:lex:bot1")
        result = _associate_lex_bot(mock_client, INSTANCE_ID, resource, {"arn:lex:alias"}, TARGET_REGION)
        assert result["status"] == "already_associated"

    @patch("association.resource_association._get_bot_alias_arn", return_value=None)
    def test_returns_error_when_no_alias(self, mock_alias):
        mock_client = MagicMock()
        resource = _make_resource("bot1", ResourceType.LEX_BOT, "arn:lex:bot1")
        result = _associate_lex_bot(mock_client, INSTANCE_ID, resource, set(), TARGET_REGION)
        assert result["status"] == "error"
        assert "bot alias ARN" in result["error"].lower() or "bot-alias" in result["error"].lower()
        mock_client.associate_bot.assert_not_called()


# ---------------------------------------------------------------------------
# _get_bot_alias_arn
# ---------------------------------------------------------------------------

class TestGetBotAliasArn:
    @patch("association.resource_association.create_target_client")
    def test_returns_alias_arn(self, mock_create):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [
                {"botAliasId": "TSTALIASID"},
                {"botAliasId": "REAL_ALIAS"},
            ]
        }
        mock_create.return_value = mock_lex
        result = _get_bot_alias_arn("arn:aws:lex:us-east-1:123:bot/BOTID", TARGET_REGION)
        assert result is not None
        assert "REAL_ALIAS" in result
        assert "BOTID" in result

    @patch("association.resource_association.create_target_client")
    def test_returns_none_when_no_aliases(self, mock_create):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {"botAliasSummaries": []}
        mock_create.return_value = mock_lex
        result = _get_bot_alias_arn("arn:aws:lex:us-east-1:123:bot/BOTID", TARGET_REGION)
        assert result is None

    def test_returns_none_for_bad_arn(self):
        result = _get_bot_alias_arn("bad-arn", TARGET_REGION)
        assert result is None


# ---------------------------------------------------------------------------
# _find_latest_bot_version
# ---------------------------------------------------------------------------

class TestFindLatestBotVersion:
    def test_returns_highest_numeric_version(self):
        mock_lex = MagicMock()
        mock_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [
                {"botVersion": "3"},
                {"botVersion": "2"},
                {"botVersion": "DRAFT"},
                {"botVersion": "1"},
            ]
        }
        result = _find_latest_bot_version(mock_lex, "BOTID")
        assert result == "3"

    def test_returns_none_when_only_draft(self):
        mock_lex = MagicMock()
        mock_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [{"botVersion": "DRAFT"}]
        }
        result = _find_latest_bot_version(mock_lex, "BOTID")
        assert result is None

    def test_returns_none_on_empty(self):
        mock_lex = MagicMock()
        mock_lex.list_bot_versions.return_value = {"botVersionSummaries": []}
        result = _find_latest_bot_version(mock_lex, "BOTID")
        assert result is None

    def test_handles_api_error(self):
        mock_lex = MagicMock()
        mock_lex.list_bot_versions.side_effect = Exception("API error")
        result = _find_latest_bot_version(mock_lex, "BOTID")
        assert result is None


# ---------------------------------------------------------------------------
# _get_bot_alias_arn — ALGR replica scenarios
# ---------------------------------------------------------------------------

class TestGetBotAliasArnAlgrScenarios:
    BOT_ARN = "arn:aws:lex:us-west-2:123456789012:bot/YDJIL968WO"
    SOURCE_REGION = "us-east-1"
    TARGET_REGION = "us-west-2"

    @patch("association.resource_association.create_target_client")
    def test_creates_alias_on_replica_when_only_tstaliasid(self, mock_create):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [{"botAliasId": "TSTALIASID"}]
        }
        mock_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [{"botVersion": "1"}]
        }
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_lex.create_bot_alias.return_value = {"botAliasId": "NEW_ALIAS_ID"}
        mock_create.return_value = mock_lex

        result = _get_bot_alias_arn(self.BOT_ARN, self.TARGET_REGION)
        assert result is not None
        assert "NEW_ALIAS_ID" in result
        assert "YDJIL968WO" in result
        mock_lex.create_bot_alias.assert_called_once()

    @patch("association.resource_association._create_alias_on_source_bot")
    @patch("association.resource_association.create_target_client")
    def test_falls_back_to_source_bot_on_validation_error(self, mock_create, mock_source_alias):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [{"botAliasId": "TSTALIASID"}]
        }
        mock_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [{"botVersion": "1"}]
        }
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_lex.create_bot_alias.side_effect = _client_error(
            "ValidationException", "Bot is a replica and cannot be modified"
        )
        mock_create.return_value = mock_lex
        mock_source_alias.return_value = "arn:aws:lex:us-west-2:123456789012:bot-alias/YDJIL968WO/SOURCE_ALIAS"

        result = _get_bot_alias_arn(
            self.BOT_ARN, self.TARGET_REGION, source_region=self.SOURCE_REGION
        )
        assert result is not None
        assert "SOURCE_ALIAS" in result
        mock_source_alias.assert_called_once_with(
            "YDJIL968WO", self.SOURCE_REGION, self.TARGET_REGION,
            "arn:aws:lex:us-west-2:123456789012:bot-alias",
        )

    @patch("association.resource_association._create_alias_on_source_bot")
    @patch("association.resource_association.create_target_client")
    def test_falls_back_to_source_on_any_error(self, mock_create, mock_source_alias):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [{"botAliasId": "TSTALIASID"}]
        }
        mock_lex.list_bot_versions.return_value = {"botVersionSummaries": []}
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_lex.create_bot_alias.side_effect = _client_error(
            "InternalServerException", "Something went wrong"
        )
        mock_create.return_value = mock_lex
        mock_source_alias.return_value = "arn:aws:lex:us-west-2:123456789012:bot-alias/YDJIL968WO/FROM_SOURCE"

        result = _get_bot_alias_arn(
            self.BOT_ARN, self.TARGET_REGION, source_region=self.SOURCE_REGION
        )
        assert result is not None
        assert "FROM_SOURCE" in result

    @patch("association.resource_association.create_target_client")
    def test_conflict_finds_existing_alias(self, mock_create):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.side_effect = [
            {"botAliasSummaries": [{"botAliasId": "TSTALIASID"}]},
            {"botAliasSummaries": [
                {"botAliasId": "TSTALIASID"},
                {"botAliasId": "EXISTING_ALIAS"},
            ]},
        ]
        mock_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [{"botVersion": "1"}]
        }
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_lex.create_bot_alias.side_effect = _client_error(
            "ConflictException", "Alias already exists"
        )
        mock_create.return_value = mock_lex

        result = _get_bot_alias_arn(self.BOT_ARN, self.TARGET_REGION)
        assert result is not None
        assert "EXISTING_ALIAS" in result

    @patch("association.resource_association._create_alias_on_source_bot")
    @patch("association.resource_association.create_target_client")
    def test_returns_none_when_no_source_region_and_replica_fails(self, mock_create, mock_source_alias):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [{"botAliasId": "TSTALIASID"}]
        }
        mock_lex.list_bot_versions.return_value = {"botVersionSummaries": []}
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_lex.create_bot_alias.side_effect = _client_error(
            "ValidationException", "Immutable replica"
        )
        mock_create.return_value = mock_lex

        result = _get_bot_alias_arn(self.BOT_ARN, self.TARGET_REGION, source_region=None)
        assert result is None
        mock_source_alias.assert_not_called()

    @patch("association.resource_association._create_alias_on_source_bot")
    @patch("association.resource_association.create_target_client")
    def test_returns_none_when_source_fallback_also_fails(self, mock_create, mock_source_alias):
        mock_lex = MagicMock()
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [{"botAliasId": "TSTALIASID"}]
        }
        mock_lex.list_bot_versions.return_value = {"botVersionSummaries": []}
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_lex.create_bot_alias.side_effect = _client_error(
            "ValidationException", "Immutable"
        )
        mock_create.return_value = mock_lex
        mock_source_alias.return_value = None

        result = _get_bot_alias_arn(
            self.BOT_ARN, self.TARGET_REGION, source_region=self.SOURCE_REGION
        )
        assert result is None


# ---------------------------------------------------------------------------
# _create_alias_on_source_bot
# ---------------------------------------------------------------------------

class TestCreateAliasOnSourceBot:
    BOT_ID = "YDJIL968WO"
    SOURCE_REGION = "us-east-1"
    TARGET_REGION = "us-west-2"
    ALIAS_ARN_PREFIX = "arn:aws:lex:us-west-2:123456789012:bot-alias"

    @patch("time.sleep", return_value=None)
    @patch("association.resource_association.create_target_client")
    def test_creates_alias_with_existing_version(self, mock_create, mock_sleep):
        mock_source_lex = MagicMock()
        mock_target_lex = MagicMock()

        mock_source_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [{"botVersion": "1"}]
        }
        mock_source_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_source_lex.create_bot_alias.return_value = {"botAliasId": "NEW_ALIAS"}

        mock_target_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [
                {"botAliasId": "TSTALIASID"},
                {"botAliasId": "NEW_ALIAS"},
            ]
        }

        def create_client_side_effect(service, region):
            if region == self.SOURCE_REGION:
                return mock_source_lex
            return mock_target_lex

        mock_create.side_effect = create_client_side_effect

        result = _create_alias_on_source_bot(
            self.BOT_ID, self.SOURCE_REGION, self.TARGET_REGION, self.ALIAS_ARN_PREFIX
        )
        assert result is not None
        assert "NEW_ALIAS" in result
        assert self.BOT_ID in result
        mock_source_lex.create_bot_alias.assert_called_once()

    @patch("time.sleep", return_value=None)
    @patch("association.resource_association.create_target_client")
    def test_handles_alias_already_exists_on_source(self, mock_create, mock_sleep):
        mock_source_lex = MagicMock()
        mock_target_lex = MagicMock()

        mock_source_lex.list_bot_versions.return_value = {
            "botVersionSummaries": [{"botVersion": "1"}]
        }
        mock_source_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_source_lex.create_bot_alias.side_effect = _client_error(
            "ConflictException", "Alias already exists"
        )
        mock_source_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [
                {"botAliasId": "TSTALIASID"},
                {"botAliasId": "EXISTING_SRC_ALIAS"},
            ]
        }

        mock_target_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [
                {"botAliasId": "TSTALIASID"},
                {"botAliasId": "EXISTING_SRC_ALIAS"},
            ]
        }

        def create_client_side_effect(service, region):
            if region == self.SOURCE_REGION:
                return mock_source_lex
            return mock_target_lex

        mock_create.side_effect = create_client_side_effect

        result = _create_alias_on_source_bot(
            self.BOT_ID, self.SOURCE_REGION, self.TARGET_REGION, self.ALIAS_ARN_PREFIX
        )
        assert result is not None
        assert "EXISTING_SRC_ALIAS" in result

    @patch("time.sleep", return_value=None)
    @patch("association.resource_association.create_target_client")
    def test_returns_none_on_total_failure(self, mock_create, mock_sleep):
        mock_create.side_effect = Exception("Cannot create client")

        result = _create_alias_on_source_bot(
            self.BOT_ID, self.SOURCE_REGION, self.TARGET_REGION, self.ALIAS_ARN_PREFIX
        )
        assert result is None


# ---------------------------------------------------------------------------
# _associate_lex_bot — source_region passthrough
# ---------------------------------------------------------------------------

class TestAssociateLexBotSourceRegion:
    @patch("association.resource_association._get_bot_alias_arn")
    def test_passes_source_region(self, mock_alias):
        mock_alias.return_value = "arn:aws:lex:us-west-2:123:bot-alias/BOT/ALIAS"
        mock_client = MagicMock()
        resource = _make_resource("bot1", ResourceType.LEX_BOT, "arn:aws:lex:us-west-2:123:bot/BOT",
                                  arn="arn:aws:lex:us-east-1:123:bot/BOT")

        _associate_lex_bot(
            mock_client, INSTANCE_ID, resource, set(), "us-west-2",
            source_region="us-east-1",
        )
        mock_alias.assert_called_once_with(
            "arn:aws:lex:us-west-2:123:bot/BOT", "us-west-2",
            source_region="us-east-1",
            source_bot_arn="arn:aws:lex:us-east-1:123:bot/BOT",
        )

    @patch("association.resource_association._get_bot_alias_arn", return_value=None)
    def test_error_message_when_alias_resolution_fails(self, mock_alias):
        mock_client = MagicMock()
        resource = _make_resource("my-bot", ResourceType.LEX_BOT, "arn:aws:lex:us-west-2:123:bot/BOT")
        result = _associate_lex_bot(
            mock_client, INSTANCE_ID, resource, set(), "us-west-2",
            source_region="us-east-1",
        )
        assert result["status"] == "error"
        assert "my-bot" in result["error"]
        mock_client.associate_bot.assert_not_called()

    @patch("association.resource_association._get_bot_alias_arn", return_value="arn:aws:lex:us-west-2:123:bot-alias/BOT/ALIAS")
    def test_handles_associate_bot_error(self, mock_alias):
        mock_client = MagicMock()
        mock_client.associate_bot.side_effect = _client_error(
            "InternalServiceException", "Service error"
        )
        resource = _make_resource("bot1", ResourceType.LEX_BOT, "arn:aws:lex:us-west-2:123:bot/BOT")
        result = _associate_lex_bot(
            mock_client, INSTANCE_ID, resource, set(), "us-west-2",
            source_region="us-east-1",
        )
        assert result["status"] == "error"

    @patch("association.resource_association._get_bot_alias_arn", return_value="arn:aws:lex:us-west-2:123:bot-alias/BOT/ALIAS")
    def test_handles_already_associated_error(self, mock_alias):
        mock_client = MagicMock()
        mock_client.associate_bot.side_effect = _client_error(
            "ResourceConflictException", "Bot already associated"
        )
        resource = _make_resource("bot1", ResourceType.LEX_BOT, "arn:aws:lex:us-west-2:123:bot/BOT")
        result = _associate_lex_bot(
            mock_client, INSTANCE_ID, resource, set(), "us-west-2",
        )
        assert result["status"] == "already_associated"


# ---------------------------------------------------------------------------
# _associate_kinesis_stream (new signature with kinesis_type_map)
# ---------------------------------------------------------------------------

class TestAssociateKinesisStream:
    @patch("association.resource_association._check_storage_config_exists", return_value=True)
    def test_already_associated(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("stream1", ResourceType.KINESIS_STREAM, "arn:kinesis:stream1",
                                  arn="arn:kinesis:us-east-1:123:stream/stream1")
        result = _associate_kinesis_stream(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "already_associated"

    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_associates_successfully(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("stream1", ResourceType.KINESIS_STREAM, "arn:kinesis:stream1",
                                  arn="arn:kinesis:us-east-1:123:stream/stream1")
        result = _associate_kinesis_stream(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "associated"
        mock_client.associate_instance_storage_config.assert_called_once()

    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_uses_source_mapping_for_storage_type(self, mock_check):
        """Kinesis stream should use the storage type from source config mapping."""
        mock_client = MagicMock()
        source_arn = "arn:aws:kinesis:us-east-1:123:stream/agent-stream"
        resource = _make_resource("agent-stream", ResourceType.KINESIS_STREAM,
                                  "arn:aws:kinesis:us-west-2:123:stream/agent-stream",
                                  arn=source_arn)
        kinesis_type_map = {source_arn: ["AGENT_EVENTS"]}
        result = _associate_kinesis_stream(
            mock_client, INSTANCE_ID, resource,
            kinesis_type_map=kinesis_type_map, source_region="us-east-1",
        )
        assert result["status"] == "associated"
        call_args = mock_client.associate_instance_storage_config.call_args
        assert call_args[1]["ResourceType"] == "AGENT_EVENTS"

    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_handles_conflict(self, mock_check):
        mock_client = MagicMock()
        mock_client.associate_instance_storage_config.side_effect = _client_error(
            "ResourceConflictException", "already exists"
        )
        resource = _make_resource("stream1", ResourceType.KINESIS_STREAM, "arn:kinesis:stream1",
                                  arn="arn:kinesis:us-east-1:123:stream/stream1")
        result = _associate_kinesis_stream(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "already_associated"


# ---------------------------------------------------------------------------
# _associate_firehose (new signature with firehose_type_map)
# ---------------------------------------------------------------------------

class TestAssociateFirehose:
    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_associates_successfully(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("fh1", ResourceType.KINESIS_FIREHOSE, "arn:firehose:fh1",
                                  arn="arn:firehose:us-east-1:123:deliverystream/fh1")
        result = _associate_firehose(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "associated"

    @patch("association.resource_association._check_storage_config_exists", return_value=True)
    def test_already_associated(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("fh1", ResourceType.KINESIS_FIREHOSE, "arn:firehose:fh1",
                                  arn="arn:firehose:us-east-1:123:deliverystream/fh1")
        result = _associate_firehose(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "already_associated"

    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_uses_source_mapping_for_storage_type(self, mock_check):
        mock_client = MagicMock()
        source_arn = "arn:aws:firehose:us-east-1:123:deliverystream/ctr-firehose"
        resource = _make_resource("ctr-firehose", ResourceType.KINESIS_FIREHOSE,
                                  "arn:aws:firehose:us-west-2:123:deliverystream/ctr-firehose",
                                  arn=source_arn)
        firehose_type_map = {source_arn: ["CONTACT_TRACE_RECORDS"]}
        result = _associate_firehose(
            mock_client, INSTANCE_ID, resource,
            firehose_type_map=firehose_type_map, source_region="us-east-1",
        )
        assert result["status"] == "associated"
        call_args = mock_client.associate_instance_storage_config.call_args
        assert call_args[1]["ResourceType"] == "CONTACT_TRACE_RECORDS"


# ---------------------------------------------------------------------------
# _associate_kvs_stream
# ---------------------------------------------------------------------------

class TestAssociateKvsStream:
    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_associates_successfully(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("kvs1", ResourceType.KINESIS_VIDEO_STREAM, "arn:kvs:kvs1")
        result = _associate_kvs_stream(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "associated"
        assert "kvs1" in result["message"]

    @patch("association.resource_association._check_storage_config_exists", return_value=True)
    def test_already_configured(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("kvs1", ResourceType.KINESIS_VIDEO_STREAM, "arn:kvs:kvs1")
        result = _associate_kvs_stream(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "already_associated"


# ---------------------------------------------------------------------------
# _associate_s3_bucket (now returns list, uses source mappings)
# ---------------------------------------------------------------------------

class TestAssociateS3Bucket:
    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_associates_for_all_source_storage_types(self, mock_check):
        """S3 bucket should be associated for all storage types from source mapping."""
        mock_client = MagicMock()
        resource = _make_resource("my-bucket-dr", ResourceType.S3_BUCKET, "arn:aws:s3:::my-bucket-dr",
                                  arn="arn:aws:s3:::my-bucket")
        s3_type_map = {"my-bucket": ["CALL_RECORDINGS", "CHAT_TRANSCRIPTS", "SCHEDULED_REPORTS"]}
        results = _associate_s3_bucket(mock_client, INSTANCE_ID, resource, s3_type_map=s3_type_map)
        assert len(results) == 3
        assert all(r["status"] == "associated" for r in results)
        assert mock_client.associate_instance_storage_config.call_count == 3

    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_defaults_to_call_recordings(self, mock_check):
        """Without source mapping, defaults to CALL_RECORDINGS."""
        mock_client = MagicMock()
        resource = _make_resource("my-bucket", ResourceType.S3_BUCKET, "arn:aws:s3:::my-bucket")
        results = _associate_s3_bucket(mock_client, INSTANCE_ID, resource)
        assert len(results) == 1
        assert results[0]["status"] == "associated"
        assert "CALL_RECORDINGS" in results[0]["message"]

    @patch("association.resource_association._check_storage_config_exists", return_value=True)
    def test_already_associated(self, mock_check):
        mock_client = MagicMock()
        resource = _make_resource("my-bucket", ResourceType.S3_BUCKET, "arn:aws:s3:::my-bucket")
        results = _associate_s3_bucket(mock_client, INSTANCE_ID, resource)
        assert len(results) == 1
        assert results[0]["status"] == "already_associated"

    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_uses_resource_storage_types_as_fallback(self, mock_check):
        """Falls back to resource.storage_types when no source mapping."""
        mock_client = MagicMock()
        resource = _make_resource("my-bucket", ResourceType.S3_BUCKET, "arn:aws:s3:::my-bucket")
        resource.storage_types = ["CALL_RECORDINGS", "CHAT_TRANSCRIPTS"]
        results = _associate_s3_bucket(mock_client, INSTANCE_ID, resource)
        assert len(results) == 2
        assert mock_client.associate_instance_storage_config.call_count == 2


# ---------------------------------------------------------------------------
# _check_storage_config_exists
# ---------------------------------------------------------------------------

class TestCheckStorageConfigExists:
    def test_finds_kinesis_stream(self):
        mock_client = MagicMock()
        mock_client.list_instance_storage_configs.return_value = {
            "StorageConfigs": [{
                "StorageType": "KINESIS_STREAM",
                "KinesisStreamConfig": {"StreamArn": "arn:stream:1"},
            }]
        }
        assert _check_storage_config_exists(
            mock_client, INSTANCE_ID, "CONTACT_TRACE_RECORDS", "KINESIS_STREAM", "arn:stream:1"
        ) is True

    def test_no_match(self):
        mock_client = MagicMock()
        mock_client.list_instance_storage_configs.return_value = {"StorageConfigs": []}
        assert _check_storage_config_exists(
            mock_client, INSTANCE_ID, "CONTACT_TRACE_RECORDS", "KINESIS_STREAM", "arn:stream:1"
        ) is False

    def test_kvs_any_config_matches(self):
        mock_client = MagicMock()
        mock_client.list_instance_storage_configs.return_value = {
            "StorageConfigs": [{"StorageType": "KINESIS_VIDEO_STREAM"}]
        }
        assert _check_storage_config_exists(
            mock_client, INSTANCE_ID, "MEDIA_STREAMS", "KINESIS_VIDEO_STREAM", None
        ) is True

    def test_s3_bucket_match(self):
        mock_client = MagicMock()
        mock_client.list_instance_storage_configs.return_value = {
            "StorageConfigs": [{
                "StorageType": "S3",
                "S3Config": {"BucketName": "my-bucket"},
            }]
        }
        assert _check_storage_config_exists(
            mock_client, INSTANCE_ID, "CALL_RECORDINGS", "S3", None, "my-bucket"
        ) is True

    def test_handles_api_error(self):
        mock_client = MagicMock()
        mock_client.list_instance_storage_configs.side_effect = _client_error()
        assert _check_storage_config_exists(
            mock_client, INSTANCE_ID, "CONTACT_TRACE_RECORDS", "KINESIS_STREAM", "arn:x"
        ) is False


# ---------------------------------------------------------------------------
# _build_source_mappings
# ---------------------------------------------------------------------------

class TestBuildSourceMappings:
    def test_builds_all_mappings(self):
        source_configs = {
            "CALL_RECORDINGS": [{"StorageType": "S3", "S3Config": {"BucketName": "my-bucket", "BucketPrefix": "cr"}}],
            "CHAT_TRANSCRIPTS": [{"StorageType": "S3", "S3Config": {"BucketName": "my-bucket", "BucketPrefix": "ct"}}],
            "CONTACT_TRACE_RECORDS": [{"StorageType": "KINESIS_FIREHOSE", "KinesisFirehoseConfig": {"FirehoseArn": "arn:fh:1"}}],
            "AGENT_EVENTS": [{"StorageType": "KINESIS_STREAM", "KinesisStreamConfig": {"StreamArn": "arn:ks:1"}}],
        }
        s3_map, kinesis_map, firehose_map = _build_source_mappings(source_configs)
        assert s3_map == {"my-bucket": ["CALL_RECORDINGS", "CHAT_TRANSCRIPTS"]}
        assert kinesis_map == {"arn:ks:1": ["AGENT_EVENTS"]}
        assert firehose_map == {"arn:fh:1": ["CONTACT_TRACE_RECORDS"]}

    def test_empty_configs(self):
        s3_map, kinesis_map, firehose_map = _build_source_mappings({})
        assert s3_map == {}
        assert kinesis_map == {}
        assert firehose_map == {}


# ---------------------------------------------------------------------------
# associate_resources (integration-level)
# ---------------------------------------------------------------------------

class TestAssociateResources:
    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._enable_instance_attributes", return_value=[])
    @patch("association.resource_association._list_associated_lambdas", return_value=set())
    @patch("association.resource_association._list_associated_bots", return_value=set())
    def test_associates_lambda(self, mock_bots, mock_lambdas, mock_attrs, mock_create, mock_source):
        mock_connect = MagicMock()
        mock_connect.describe_instance.return_value = {
            "Instance": {"InstanceStatus": "ACTIVE"}
        }
        mock_create.return_value = mock_connect

        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:aws:lambda:us-east-1:123:function:fn1")
        session = _make_session([resource])

        results = associate_resources(session)
        assert any(r["status"] == "associated" and r["resource"] == "fn1" for r in results)

    @patch("association.resource_association.create_target_client")
    def test_invalid_instance_arn(self, mock_create):
        session = _make_session(instance_arn="not-an-arn")
        results = associate_resources(session)
        assert len(results) == 1
        assert results[0]["status"] == "error"

    @patch("association.resource_association.create_target_client")
    def test_instance_not_active(self, mock_create):
        mock_connect = MagicMock()
        mock_connect.describe_instance.return_value = {
            "Instance": {"InstanceStatus": "CREATION_IN_PROGRESS"}
        }
        mock_create.return_value = mock_connect

        session = _make_session()
        results = associate_resources(session)
        assert len(results) == 1
        assert "not ACTIVE" in results[0]["error"]

    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    def test_skips_non_replicated_resources(self, mock_create, mock_source):
        mock_connect = MagicMock()
        mock_connect.describe_instance.return_value = {
            "Instance": {"InstanceStatus": "ACTIVE"}
        }
        mock_create.return_value = mock_connect

        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1", status=ReplicationStatus.FAILED)
        session = _make_session([resource])

        with patch("association.resource_association._enable_instance_attributes", return_value=[]), \
             patch("association.resource_association._list_associated_lambdas", return_value=set()), \
             patch("association.resource_association._list_associated_bots", return_value=set()):
            results = associate_resources(session)

        assert not any(r.get("resource") == "fn1" for r in results)


# ---------------------------------------------------------------------------
# _associate_lex_bot — pending status for ALGR alias not yet replicated
# ---------------------------------------------------------------------------

class TestAssociateLexBotPendingStatus:
    """Tests for the 'pending' status when bot exists but alias is not yet replicated."""

    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._get_bot_alias_arn", return_value=None)
    def test_returns_pending_when_bot_exists_but_no_alias(self, mock_alias, mock_create):
        """When alias resolution returns None but bot exists in target, status should be 'pending'."""
        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_create.return_value = mock_lex

        mock_client = MagicMock()
        resource = _make_resource(
            "my-bot", ResourceType.LEX_BOT,
            "arn:aws:lex:us-west-2:123:bot/BOTID",
        )
        result = _associate_lex_bot(
            mock_client, INSTANCE_ID, resource, set(), "us-west-2",
            source_region="us-east-1",
        )
        assert result["status"] == "pending"
        assert result["retryable"] is True
        assert "replicating" in result["message"].lower() or "retry" in result["message"].lower()
        mock_client.associate_bot.assert_not_called()

    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._get_bot_alias_arn", return_value=None)
    def test_returns_error_when_bot_does_not_exist(self, mock_alias, mock_create):
        """When alias resolution returns None and bot doesn't exist, status should be 'error'."""
        mock_lex = MagicMock()
        mock_lex.describe_bot.side_effect = _client_error("ResourceNotFoundException")
        mock_create.return_value = mock_lex

        mock_client = MagicMock()
        resource = _make_resource(
            "my-bot", ResourceType.LEX_BOT,
            "arn:aws:lex:us-west-2:123:bot/BOTID",
        )
        result = _associate_lex_bot(
            mock_client, INSTANCE_ID, resource, set(), "us-west-2",
            source_region="us-east-1",
        )
        assert result["status"] == "error"
        assert "bot alias" in result["error"].lower() or "bot-alias" in result["error"].lower()


# ---------------------------------------------------------------------------
# associate_single_resource
# ---------------------------------------------------------------------------

class TestAssociateSingleResource:
    """Tests for the per-resource retry function."""

    def _make_full_session(self, resources=None):
        """Create a session with proper structure for associate_single_resource."""
        inv = {}
        for r in (resources or []):
            inv[r.name] = r  # use name as ID for simplicity
        return SimpleNamespace(
            instance_arn=INSTANCE_ARN,
            target_region=TARGET_REGION,
            source_region=SOURCE_REGION,
            inventory=inv,
        )

    def test_resource_not_found(self):
        session = self._make_full_session([])
        from association.resource_association import associate_single_resource
        result = associate_single_resource(session, "nonexistent")
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    def test_resource_not_replicated(self):
        resource = _make_resource("fn1", ResourceType.LAMBDA, None, status=ReplicationStatus.FAILED)
        session = self._make_full_session([resource])
        from association.resource_association import associate_single_resource
        result = associate_single_resource(session, "fn1")
        assert result["status"] == "error"
        assert "not replicated" in result["error"].lower()

    def test_resource_no_replicated_arn(self):
        resource = _make_resource("fn1", ResourceType.LAMBDA, None, status=ReplicationStatus.REPLICATED)
        resource.replicated_arn = None
        session = self._make_full_session([resource])
        from association.resource_association import associate_single_resource
        result = associate_single_resource(session, "fn1")
        assert result["status"] == "error"
        assert "no replicated arn" in result["error"].lower()

    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._list_associated_lambdas", return_value=set())
    def test_associates_lambda_successfully(self, mock_lambdas, mock_create, mock_source):
        mock_connect = MagicMock()
        mock_create.return_value = mock_connect

        resource = _make_resource(
            "fn1", ResourceType.LAMBDA,
            "arn:aws:lambda:us-east-1:123456789012:function:fn1",
            status=ReplicationStatus.REPLICATED,
        )
        session = self._make_full_session([resource])
        from association.resource_association import associate_single_resource
        result = associate_single_resource(session, "fn1")
        assert result["status"] == "associated"

    def test_invalid_instance_arn(self):
        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1", status=ReplicationStatus.REPLICATED)
        session = SimpleNamespace(
            instance_arn="not-an-arn",
            target_region=TARGET_REGION,
            source_region=SOURCE_REGION,
            inventory={"fn1": resource},
        )
        from association.resource_association import associate_single_resource
        result = associate_single_resource(session, "fn1")
        assert result["status"] == "error"

    def test_unsupported_resource_type(self):
        resource = _make_resource("unknown", "UNKNOWN_TYPE", "arn:unknown:x", status=ReplicationStatus.REPLICATED)
        session = self._make_full_session([resource])
        from association.resource_association import associate_single_resource
        with patch("association.resource_association._read_source_storage_configs", return_value={}), \
             patch("association.resource_association.create_target_client", return_value=MagicMock()):
            result = associate_single_resource(session, "unknown")
        assert result["status"] == "error"
        assert "unsupported" in result["error"].lower()


# ---------------------------------------------------------------------------
# _resolve_lex_bot_arn — resolves missing replicated_arn for Lex bots
# ---------------------------------------------------------------------------

class TestResolveLexBotArn:
    """Tests for resolving Lex bot ARN when replicated_arn is missing."""

    @patch("association.resource_association.create_target_client")
    def test_resolves_bot_arn_in_target_region(self, mock_create):
        from association.resource_association import _resolve_lex_bot_arn
        mock_lex = MagicMock()
        mock_lex.describe_bot.return_value = {"botName": "my-bot"}
        mock_create.return_value = mock_lex

        resource = _make_resource(
            "my-bot", ResourceType.LEX_BOT, None,
            arn="arn:aws:lex:us-east-1:123456789012:bot/BOTID123",
        )
        result = _resolve_lex_bot_arn(resource, "us-west-2", "us-east-1")
        assert result is not None
        assert "us-west-2" in result
        assert "BOTID123" in result

    @patch("association.resource_association.create_target_client")
    def test_returns_none_when_bot_not_found(self, mock_create):
        from association.resource_association import _resolve_lex_bot_arn
        mock_lex = MagicMock()
        mock_lex.describe_bot.side_effect = _client_error("ResourceNotFoundException")
        mock_create.return_value = mock_lex

        resource = _make_resource(
            "my-bot", ResourceType.LEX_BOT, None,
            arn="arn:aws:lex:us-east-1:123456789012:bot/BOTID123",
        )
        result = _resolve_lex_bot_arn(resource, "us-west-2", "us-east-1")
        assert result is None

    def test_returns_none_when_no_source_arn(self):
        from association.resource_association import _resolve_lex_bot_arn
        resource = _make_resource("my-bot", ResourceType.LEX_BOT, None, arn="")
        result = _resolve_lex_bot_arn(resource, "us-west-2", "us-east-1")
        assert result is None

    def test_returns_none_for_bad_arn(self):
        from association.resource_association import _resolve_lex_bot_arn
        resource = _make_resource("my-bot", ResourceType.LEX_BOT, None, arn="bad-arn")
        result = _resolve_lex_bot_arn(resource, "us-west-2", "us-east-1")
        assert result is None


# ---------------------------------------------------------------------------
# associate_resources — Lex bot with missing replicated_arn
# ---------------------------------------------------------------------------

class TestAssociateResourcesLexBotMissingArn:
    """Tests that associate_resources resolves Lex bot ARN when replicated_arn is None."""

    @patch("association.resource_association._replicate_approved_origins", return_value=[])
    @patch("association.resource_association._resolve_lex_bot_arn")
    @patch("association.resource_association._get_bot_alias_arn", return_value="arn:aws:lex:us-west-2:123:bot-alias/BOTID/ALIAS")
    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._enable_instance_attributes", return_value=[])
    @patch("association.resource_association._list_associated_lambdas", return_value=set())
    @patch("association.resource_association._list_associated_bots", return_value=set())
    def test_resolves_and_associates_lex_bot(
        self, mock_bots, mock_lambdas, mock_attrs, mock_create, mock_source,
        mock_alias, mock_resolve, mock_origins,
    ):
        mock_connect = MagicMock()
        mock_connect.describe_instance.return_value = {
            "Instance": {"InstanceStatus": "ACTIVE"}
        }
        mock_create.return_value = mock_connect
        mock_resolve.return_value = "arn:aws:lex:us-west-2:123:bot/BOTID"

        resource = _make_resource(
            "my-bot", ResourceType.LEX_BOT, None,
            status=ReplicationStatus.REPLICATED,
            arn="arn:aws:lex:us-east-1:123:bot/BOTID",
        )
        resource.replicated_arn = None
        session = _make_session([resource], target_region="us-west-2")

        results = associate_resources(session)
        bot_results = [r for r in results if r.get("resource") == "my-bot"]
        assert len(bot_results) == 1
        assert bot_results[0]["status"] in ("associated", "already_associated")
        mock_resolve.assert_called_once()

    @patch("association.resource_association._replicate_approved_origins", return_value=[])
    @patch("association.resource_association._resolve_lex_bot_arn", return_value=None)
    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._enable_instance_attributes", return_value=[])
    @patch("association.resource_association._list_associated_lambdas", return_value=set())
    @patch("association.resource_association._list_associated_bots", return_value=set())
    def test_skips_when_resolve_fails(
        self, mock_bots, mock_lambdas, mock_attrs, mock_create, mock_source,
        mock_resolve, mock_origins,
    ):
        mock_connect = MagicMock()
        mock_connect.describe_instance.return_value = {
            "Instance": {"InstanceStatus": "ACTIVE"}
        }
        mock_create.return_value = mock_connect

        resource = _make_resource(
            "my-bot", ResourceType.LEX_BOT, None,
            status=ReplicationStatus.REPLICATED,
            arn="arn:aws:lex:us-east-1:123:bot/BOTID",
        )
        resource.replicated_arn = None
        session = _make_session([resource], target_region="us-west-2")

        results = associate_resources(session)
        bot_results = [r for r in results if r.get("resource") == "my-bot"]
        assert len(bot_results) == 0  # skipped, not errored


# ---------------------------------------------------------------------------
# _wait_for_firehose_active
# ---------------------------------------------------------------------------

class TestWaitForFirehoseActive:
    """Tests for the Firehose ACTIVE wait logic."""

    @patch("time.sleep", return_value=None)
    @patch("association.resource_association.create_target_client")
    def test_returns_active_immediately(self, mock_create, mock_sleep):
        from association.resource_association import _wait_for_firehose_active
        mock_fh = MagicMock()
        mock_fh.describe_delivery_stream.return_value = {
            "DeliveryStreamDescription": {"DeliveryStreamStatus": "ACTIVE"}
        }
        mock_create.return_value = mock_fh
        result = _wait_for_firehose_active(
            "arn:aws:firehose:us-west-2:123:deliverystream/my-stream", "us-west-2", max_wait=10
        )
        assert result == "ACTIVE"
        mock_sleep.assert_not_called()

    @patch("time.sleep", return_value=None)
    @patch("association.resource_association.create_target_client")
    def test_waits_then_returns_active(self, mock_create, mock_sleep):
        from association.resource_association import _wait_for_firehose_active
        mock_fh = MagicMock()
        mock_fh.describe_delivery_stream.side_effect = [
            {"DeliveryStreamDescription": {"DeliveryStreamStatus": "CREATING"}},
            {"DeliveryStreamDescription": {"DeliveryStreamStatus": "ACTIVE"}},
        ]
        mock_create.return_value = mock_fh
        result = _wait_for_firehose_active(
            "arn:aws:firehose:us-west-2:123:deliverystream/my-stream", "us-west-2", max_wait=15
        )
        assert result == "ACTIVE"

    @patch("time.sleep", return_value=None)
    @patch("association.resource_association.create_target_client")
    def test_returns_none_on_describe_error(self, mock_create, mock_sleep):
        from association.resource_association import _wait_for_firehose_active
        mock_fh = MagicMock()
        mock_fh.describe_delivery_stream.side_effect = _client_error("ResourceNotFoundException")
        mock_create.return_value = mock_fh
        result = _wait_for_firehose_active(
            "arn:aws:firehose:us-west-2:123:deliverystream/my-stream", "us-west-2", max_wait=10
        )
        assert result is None

    def test_returns_none_for_bad_arn(self):
        from association.resource_association import _wait_for_firehose_active
        result = _wait_for_firehose_active("bad-arn", "us-west-2", max_wait=5)
        assert result is None


# ---------------------------------------------------------------------------
# _associate_firehose — waits for ACTIVE
# ---------------------------------------------------------------------------

class TestAssociateFirehoseWaitsForActive:
    """Tests that _associate_firehose waits for the stream to become ACTIVE."""

    @patch("association.resource_association._wait_for_firehose_active", return_value="CREATING")
    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_returns_retryable_error_when_not_active(self, mock_check, mock_wait):
        mock_client = MagicMock()
        resource = _make_resource(
            "fh1", ResourceType.KINESIS_FIREHOSE,
            "arn:aws:firehose:us-west-2:123:deliverystream/fh1",
            arn="arn:aws:firehose:us-east-1:123:deliverystream/fh1",
        )
        result = _associate_firehose(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "error"
        assert result.get("retryable") is True
        assert "not ACTIVE" in result["error"]
        mock_client.associate_instance_storage_config.assert_not_called()

    @patch("association.resource_association._wait_for_firehose_active", return_value="ACTIVE")
    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_associates_when_active(self, mock_check, mock_wait):
        mock_client = MagicMock()
        resource = _make_resource(
            "fh1", ResourceType.KINESIS_FIREHOSE,
            "arn:aws:firehose:us-west-2:123:deliverystream/fh1",
            arn="arn:aws:firehose:us-east-1:123:deliverystream/fh1",
        )
        result = _associate_firehose(mock_client, INSTANCE_ID, resource)
        assert result["status"] == "associated"
        mock_client.associate_instance_storage_config.assert_called_once()
