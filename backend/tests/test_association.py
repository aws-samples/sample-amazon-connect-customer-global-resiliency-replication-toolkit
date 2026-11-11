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
    _list_source_bot_alias_arns,
    _replica_alias_statuses,
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
    """CONTACTFLOW_LOGS always on; CONTACT_LENS / EARLY_MEDIA / MESSAGE_STREAMING
    are replicated by ACGR, so the tool only reports a difference and never writes them."""

    MIRRORED = ("CONTACT_LENS", "EARLY_MEDIA", "MESSAGE_STREAMING")

    @staticmethod
    def _client(values: dict[str, str] | str):
        c = MagicMock()
        if isinstance(values, str):
            c.describe_instance_attribute.return_value = {"Attribute": {"Value": values}}
        else:
            c.describe_instance_attribute.side_effect = (
                lambda InstanceId, AttributeType: {"Attribute": {"Value": values.get(AttributeType, "false")}})
        return c

    def _run(self, target, source=None, source_region="us-east-1"):
        with patch("association.resource_association.create_source_client", return_value=source):
            return _enable_instance_attributes(target, INSTANCE_ID, source_region=source_region,
                                               source_instance_id="src-id")

    @staticmethod
    def _updated(client):
        return {c.kwargs["AttributeType"] for c in client.update_instance_attribute.call_args_list}

    def test_contactflow_logs_always_enabled_even_without_source(self):
        target = self._client("false")
        results = _enable_instance_attributes(target, INSTANCE_ID)
        assert self._updated(target) == {"CONTACTFLOW_LOGS"}
        # ACGR-replicated attributes are reported as not compared, never written
        assert sum(r["status"] == "skipped" for r in results) == 3

    def test_never_writes_an_attribute_acgr_replicates(self):
        target = self._client("false")
        source = self._client({"CONTACT_LENS": "true", "EARLY_MEDIA": "false", "MESSAGE_STREAMING": "true"})
        results = self._run(target, source)
        assert self._updated(target) == {"CONTACTFLOW_LOGS"}
        differs = {r["resource"] for r in results if "differs" in r.get("message", "")}
        assert differs == {"Instance Attribute: Contact Lens", "Instance Attribute: Message Streaming"}

    def test_no_write_and_no_warning_when_replica_matches(self):
        target = self._client("true")
        source = self._client("true")
        results = self._run(target, source)
        target.update_instance_attribute.assert_not_called()
        assert len(results) == 4 and all(r["status"] == "already_enabled" for r in results)
        assert not any("differs" in r.get("message", "") for r in results)

    def test_only_valid_attribute_types_are_used(self):
        import boto3
        valid = set(boto3.client("connect", region_name="us-east-1").meta.service_model
                    .operation_model("UpdateInstanceAttribute").input_shape
                    .members["AttributeType"].enum)
        target = self._client("false")
        source = self._client("true")
        self._run(target, source)
        assert self._updated(target) == {"CONTACTFLOW_LOGS"}
        used = {c.kwargs["AttributeType"] for c in target.describe_instance_attribute.call_args_list}
        used |= {c.kwargs["AttributeType"] for c in source.describe_instance_attribute.call_args_list}
        assert used == {"CONTACTFLOW_LOGS", *self.MIRRORED}
        assert used <= valid

    def test_handles_update_error(self):
        target = self._client("false")
        target.update_instance_attribute.side_effect = _client_error("InternalServiceException")
        results = self._run(target, self._client("true"))
        # only CONTACTFLOW_LOGS is ever written, so only it can fail
        assert [r["resource"] for r in results if r["status"] == "error"] == [
            "Instance Attribute: Contact Flow Logs"]

    def test_skips_unsupported_attributes(self):
        target = MagicMock()
        target.describe_instance_attribute.side_effect = _client_error("InvalidParameterException")
        results = self._run(target, self._client("true"))
        target.update_instance_attribute.assert_not_called()
        assert all(r["status"] == "skipped" for r in results)


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

    def test_already_associated_still_gets_connect_invoke_permission(self):
        mock_client = MagicMock()
        resource = _make_resource("fn1", ResourceType.LAMBDA, "arn:lambda:fn1")
        with patch("association.resource_association._ensure_lambda_connect_permission") as perm:
            result = _associate_lambda(mock_client, INSTANCE_ID, resource, {"arn:lambda:fn1"})
        assert result["status"] == "already_associated"
        perm.assert_called_once_with("arn:lambda:fn1", INSTANCE_ID)

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
# Lex bot association (ALGR)
#
# Amazon Lex Global Resiliency already replicates the bot, every version and every
# alias (same IDs) and the replica cannot be modified. The tool therefore associates
# the alias(es) the SOURCE Connect instance uses, with the Region swapped, once ALGR
# reports them Available, and creates nothing on either bot.
# ---------------------------------------------------------------------------

LEX_SRC = "eu-west-2"
LEX_TGT = "eu-central-1"
LEX_ACCT = "123456789012"
LEX_BOT = "BOTID12345"
LEX_BOT_ARN_TGT = f"arn:aws:lex:{LEX_TGT}:{LEX_ACCT}:bot/{LEX_BOT}"


def _alias_arn(region, alias_id):
    return f"arn:aws:lex:{region}:{LEX_ACCT}:bot-alias/{LEX_BOT}/{alias_id}"


class TestAssociateLexBot:
    """_associate_lex_bot: which alias, when, and that it writes nothing to either bot."""

    def _run(self, source_aliases, statuses, existing=None, connect=None, bot_exists=True,
             source_region=LEX_SRC, source_error=None):
        connect = connect or MagicMock()
        lex = MagicMock()
        if not bot_exists:
            lex.describe_bot.side_effect = _client_error("ResourceNotFoundException")
        resource = _make_resource("order-bot", ResourceType.LEX_BOT, LEX_BOT_ARN_TGT)
        list_patch = (
            patch("association.resource_association._list_source_bot_alias_arns",
                  side_effect=source_error)
            if source_error else
            patch("association.resource_association._list_source_bot_alias_arns",
                  return_value=source_aliases)
        )
        with list_patch, \
             patch("association.resource_association.create_source_client"), \
             patch("association.resource_association._replica_alias_statuses", return_value=statuses), \
             patch("association.resource_association._ensure_lex_connect_permission") as perm, \
             patch("association.resource_association.create_target_client", return_value=lex):
            result = _associate_lex_bot(connect, INSTANCE_ID, resource, existing or set(),
                                        LEX_TGT, source_region=source_region)
        return result, connect, lex, perm

    @staticmethod
    def _assert_no_bot_writes(lex):
        for name in ("create_bot_alias", "create_bot_version", "build_bot_locale",
                     "update_bot_alias", "update_intent", "create_bot"):
            getattr(lex, name).assert_not_called()

    def test_associates_the_alias_the_source_uses_with_region_swapped(self):
        result, connect, lex, perm = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"PRODALIAS1": "Available"})
        assert result["status"] == "associated"
        assert result["replicated_arn"] == _alias_arn(LEX_TGT, "PRODALIAS1")
        connect.associate_bot.assert_called_once_with(
            InstanceId=INSTANCE_ID, LexV2Bot={"AliasArn": _alias_arn(LEX_TGT, "PRODALIAS1")})
        perm.assert_called_once()
        self._assert_no_bot_writes(lex)

    def test_picks_the_source_alias_not_the_first_replica_alias(self):
        """Replica also has DEVALIAS01; the source instance uses PRODALIAS1."""
        result, connect, lex, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")],
            {"DEVALIAS01": "Available", "PRODALIAS1": "Available", "TSTALIASID": "Available"})
        assert result["replicated_arn"].endswith("/PRODALIAS1")
        assert connect.associate_bot.call_count == 1

    def test_associates_every_alias_the_source_uses(self):
        result, connect, _, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1"), _alias_arn(LEX_SRC, "DEVALIAS01")],
            {"PRODALIAS1": "Available", "DEVALIAS01": "Available"})
        assert result["status"] == "associated"
        assert connect.associate_bot.call_count == 2

    def test_already_associated_is_not_touched(self):
        result, connect, lex, perm = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"PRODALIAS1": "Available"},
            existing={_alias_arn(LEX_TGT, "PRODALIAS1")})
        assert result["status"] == "already_associated"
        connect.associate_bot.assert_not_called()
        perm.assert_not_called()
        self._assert_no_bot_writes(lex)

    @pytest.mark.parametrize("state", ["Creating", "Updating"])
    def test_pending_while_alias_replica_is_not_available(self, state):
        result, connect, lex, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"PRODALIAS1": state})
        assert result["status"] == "pending"
        assert result["retryable"] is True
        assert state in result["message"]
        connect.associate_bot.assert_not_called()
        self._assert_no_bot_writes(lex)

    def test_pending_when_alias_not_listed_yet_but_other_aliases_are(self):
        result, connect, lex, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"TSTALIASID": "Available"})
        assert result["status"] == "pending"
        connect.associate_bot.assert_not_called()
        self._assert_no_bot_writes(lex)

    def test_pending_when_nothing_listed_but_bot_exists(self):
        result, connect, _, _ = self._run([_alias_arn(LEX_SRC, "PRODALIAS1")], {}, bot_exists=True)
        assert result["status"] == "pending"
        connect.associate_bot.assert_not_called()

    def test_error_when_nothing_listed_and_bot_missing_in_dr_region(self):
        result, connect, _, _ = self._run([_alias_arn(LEX_SRC, "PRODALIAS1")], {}, bot_exists=False)
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()
        connect.associate_bot.assert_not_called()

    def test_error_when_alias_replica_failed(self):
        result, connect, _, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"PRODALIAS1": "Failed"})
        assert result["status"] == "error"
        assert "Failed" in result["error"]
        connect.associate_bot.assert_not_called()

    def test_error_when_source_instance_has_no_alias_of_this_bot(self):
        result, connect, lex, _ = self._run([], {"TSTALIASID": "Available"})
        assert result["status"] == "error"
        assert "source" in result["error"].lower()
        connect.associate_bot.assert_not_called()
        self._assert_no_bot_writes(lex)

    def test_error_without_source_region(self):
        result, connect, _, _ = self._run([], {}, source_region=None)
        assert result["status"] == "error"
        connect.associate_bot.assert_not_called()

    def test_error_when_source_associations_cannot_be_read(self):
        result, connect, _, _ = self._run([], {}, source_error=_client_error("AccessDeniedException"))
        assert result["status"] == "error"
        assert "source" in result["error"].lower()
        connect.associate_bot.assert_not_called()

    def test_conflict_from_connect_counts_as_already_associated(self):
        connect = MagicMock()
        connect.associate_bot.side_effect = _client_error(
            "ResourceConflictException", "Bot already associated")
        result, _, _, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"PRODALIAS1": "Available"}, connect=connect)
        assert result["status"] == "already_associated"

    def test_other_connect_error_is_reported(self):
        connect = MagicMock()
        connect.associate_bot.side_effect = _client_error("InternalServiceException", "boom")
        result, _, _, _ = self._run(
            [_alias_arn(LEX_SRC, "PRODALIAS1")], {"PRODALIAS1": "Available"}, connect=connect)
        assert result["status"] == "error"

    def test_error_wins_over_pending_and_done(self):
        result, connect, _, _ = self._run(
            [_alias_arn(LEX_SRC, "A"), _alias_arn(LEX_SRC, "B"), _alias_arn(LEX_SRC, "C")],
            {"A": "Available", "B": "Creating", "C": "Failed"})
        assert result["status"] == "error"


class TestListSourceBotAliasArns:
    def test_returns_only_aliases_of_the_requested_bot_and_follows_pages(self):
        connect = MagicMock()
        connect.list_bots.side_effect = [
            {"LexBots": [{"LexV2Bot": {"AliasArn": _alias_arn(LEX_SRC, "PROD")}},
                         {"LexV2Bot": {"AliasArn": f"arn:aws:lex:{LEX_SRC}:{LEX_ACCT}:bot-alias/OTHERBOT01/X"}}],
             "NextToken": "t1"},
            {"LexBots": [{"LexV2Bot": {"AliasArn": _alias_arn(LEX_SRC, "DEV")}}]},
        ]
        assert _list_source_bot_alias_arns(connect, INSTANCE_ID, LEX_BOT) == [
            _alias_arn(LEX_SRC, "PROD"), _alias_arn(LEX_SRC, "DEV")]
        assert connect.list_bots.call_args_list[0].kwargs["LexVersion"] == "V2"

    def test_empty(self):
        connect = MagicMock()
        connect.list_bots.return_value = {"LexBots": []}
        assert _list_source_bot_alias_arns(connect, INSTANCE_ID, LEX_BOT) == []


class TestReplicaAliasStatuses:
    def test_uses_list_bot_alias_replicas_on_the_source_region(self):
        src = MagicMock()
        src.list_bot_alias_replicas.side_effect = [
            {"botAliasReplicaSummaries": [{"botAliasId": "PROD", "botAliasReplicationStatus": "Available"}],
             "nextToken": "n"},
            {"botAliasReplicaSummaries": [{"botAliasId": "DEV", "botAliasReplicationStatus": "Creating"}]},
        ]
        tgt = MagicMock()
        tgt.list_bot_aliases.return_value = {"botAliasSummaries": [{"botAliasId": "PROD"}, {"botAliasId": "DEV"}]}
        with patch("association.resource_association.create_source_client", return_value=src) as mk, \
             patch("association.resource_association.create_target_client", return_value=tgt):
            result = _replica_alias_statuses(LEX_BOT, LEX_SRC, LEX_TGT)
        # ALGR's own status wins over "the replica lists it"
        assert result == {"PROD": "Available", "DEV": "Creating"}
        mk.assert_called_once_with("lexv2-models", LEX_SRC)
        assert src.list_bot_alias_replicas.call_args_list[0].kwargs["replicaRegion"] == LEX_TGT

    def test_falls_back_to_listing_replica_aliases_when_the_call_is_not_allowed(self):
        src = MagicMock()
        src.list_bot_alias_replicas.side_effect = _client_error("AccessDeniedException")
        tgt = MagicMock()
        tgt.list_bot_aliases.return_value = {"botAliasSummaries": [{"botAliasId": "PROD"}]}
        with patch("association.resource_association.create_source_client", return_value=src), \
             patch("association.resource_association.create_target_client", return_value=tgt):
            assert _replica_alias_statuses(LEX_BOT, LEX_SRC, LEX_TGT) == {"PROD": "Available"}

    def test_alias_listed_only_on_the_replica_such_as_tstaliasid_is_available(self):
        src = MagicMock()
        src.list_bot_alias_replicas.return_value = {"botAliasReplicaSummaries": []}
        tgt = MagicMock()
        tgt.list_bot_aliases.return_value = {"botAliasSummaries": [{"botAliasId": "TSTALIASID"}]}
        with patch("association.resource_association.create_source_client", return_value=src), \
             patch("association.resource_association.create_target_client", return_value=tgt):
            assert _replica_alias_statuses(LEX_BOT, LEX_SRC, LEX_TGT) == {"TSTALIASID": "Available"}

    def test_empty_when_both_fail(self):
        src = MagicMock()
        src.list_bot_alias_replicas.side_effect = _client_error("AccessDeniedException")
        tgt = MagicMock()
        tgt.list_bot_aliases.side_effect = _client_error("AccessDeniedException")
        with patch("association.resource_association.create_source_client", return_value=src), \
             patch("association.resource_association.create_target_client", return_value=tgt):
            assert _replica_alias_statuses(LEX_BOT, LEX_SRC, LEX_TGT) == {}


class TestNoAliasOrBotCreation:
    """The tool must not create or edit Lex versions, aliases or bots (ALGR does that)."""

    @pytest.mark.parametrize("name", ["_get_bot_alias_arn", "_create_alias_on_source_bot",
                                      "_find_latest_bot_version"])
    def test_removed(self, name):
        import association.resource_association as ra
        assert not hasattr(ra, name)

    def test_module_has_no_lex_write_calls(self):
        import inspect
        import association.resource_association as ra
        src = inspect.getsource(ra)
        for call in ("create_bot_alias(", "create_bot_version(", "build_bot_locale(",
                     "update_bot_alias(", "update_intent("):
            assert call not in src


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

    @patch("association.resource_association._wait_for_firehose_active", return_value="ACTIVE")
    @patch("association.resource_association._check_storage_config_exists", return_value=False)
    def test_uses_source_mapping_for_storage_type(self, mock_check, mock_wait):
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

    @patch("association.resource_association._resolve_lex_bot_arn")
    @patch("association.resource_association._associate_lex_bot", return_value={"resource": "my-bot", "resource_type": "LEX_BOT", "status": "associated", "replicated_arn": "arn:aws:lex:us-west-2:123:bot-alias/BOTID/ALIAS"})
    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._enable_instance_attributes", return_value=[])
    @patch("association.resource_association._list_associated_lambdas", return_value=set())
    @patch("association.resource_association._list_associated_bots", return_value=set())
    def test_resolves_and_associates_lex_bot(
        self, mock_bots, mock_lambdas, mock_attrs, mock_create, mock_source,
        mock_alias, mock_resolve,
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

    @patch("association.resource_association._resolve_lex_bot_arn", return_value=None)
    @patch("association.resource_association._read_source_storage_configs", return_value={})
    @patch("association.resource_association.create_target_client")
    @patch("association.resource_association._enable_instance_attributes", return_value=[])
    @patch("association.resource_association._list_associated_lambdas", return_value=set())
    @patch("association.resource_association._list_associated_bots", return_value=set())
    def test_skips_when_resolve_fails(
        self, mock_bots, mock_lambdas, mock_attrs, mock_create, mock_source,
        mock_resolve,
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
