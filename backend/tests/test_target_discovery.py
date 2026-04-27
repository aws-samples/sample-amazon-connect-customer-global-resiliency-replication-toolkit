"""Tests for discovery.target_discovery module."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from discovery.target_discovery import (
    discover_target_resources,
    _discover_lex_bots,
    _discover_lambdas,
    _discover_kinesis_streams,
    _discover_firehose_streams,
    _discover_s3_buckets,
    _discover_kvs_config,
    _get_source_lambdas,
    _get_source_bots,
    _get_source_storage_configs,
    _get_target_associated_lambdas,
    _get_target_associated_bots,
    _get_target_storage_configs,
    _extract_name_from_arn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_error(code: str = "AccessDeniedException", msg: str = "denied"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "op")


# ---------------------------------------------------------------------------
# _extract_name_from_arn
# ---------------------------------------------------------------------------

class TestExtractNameFromArn:
    def test_lambda_arn(self):
        assert _extract_name_from_arn("arn:aws:lambda:us-east-1:123:function/my-func") == "my-func"

    def test_lex_alias_arn(self):
        assert _extract_name_from_arn("arn:aws:lex:us-east-1:123:bot-alias/BOT1/ALIAS1") == "ALIAS1"

    def test_plain_arn(self):
        assert _extract_name_from_arn("arn:aws:kinesis:us-east-1:123:stream:my-stream") == "my-stream"

    def test_short_string(self):
        assert _extract_name_from_arn("something") == "something"


# ---------------------------------------------------------------------------
# _get_source_lambdas
# ---------------------------------------------------------------------------

class TestGetSourceLambdas:
    @patch("discovery.target_discovery.create_source_client")
    def test_returns_lambda_arns(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.list_lambda_functions.return_value = {
            "LambdaFunctions": ["arn:aws:lambda:us-east-1:123:function:f1", "arn:aws:lambda:us-east-1:123:function:f2"],
        }
        result = _get_source_lambdas("us-east-1", "inst-1")
        assert len(result) == 2

    @patch("discovery.target_discovery.create_source_client")
    def test_handles_error(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.list_lambda_functions.side_effect = _client_error()
        result = _get_source_lambdas("us-east-1", "inst-1")
        assert result == []


# ---------------------------------------------------------------------------
# _get_source_bots
# ---------------------------------------------------------------------------

class TestGetSourceBots:
    @patch("discovery.target_discovery.create_source_client")
    def test_returns_bot_info(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.list_bots.return_value = {
            "LexBots": [
                {"LexV2Bot": {"AliasArn": "arn:aws:lex:us-east-1:123:bot-alias/BOT1/ALIAS1"}},
            ],
        }
        result = _get_source_bots("us-east-1", "inst-1")
        assert len(result) == 1
        assert result[0]["alias_arn"] == "arn:aws:lex:us-east-1:123:bot-alias/BOT1/ALIAS1"

    @patch("discovery.target_discovery.create_source_client")
    def test_handles_error(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.list_bots.side_effect = _client_error()
        result = _get_source_bots("us-east-1", "inst-1")
        assert result == []


# ---------------------------------------------------------------------------
# _get_source_storage_configs
# ---------------------------------------------------------------------------

class TestGetSourceStorageConfigs:
    @patch("discovery.target_discovery.create_source_client")
    def test_returns_configs(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client

        def list_configs(**kwargs):
            rt = kwargs["ResourceType"]
            if rt == "CONTACT_TRACE_RECORDS":
                return {"StorageConfigs": [{"StorageType": "KINESIS_FIREHOSE", "KinesisFirehoseConfig": {"FirehoseArn": "arn:firehose"}}]}
            return {"StorageConfigs": []}

        mock_client.list_instance_storage_configs.side_effect = list_configs
        result = _get_source_storage_configs("us-east-1", "inst-1")
        assert "CONTACT_TRACE_RECORDS" in result
        assert len(result["CONTACT_TRACE_RECORDS"]) == 1


# ---------------------------------------------------------------------------
# _get_target_associated_lambdas
# ---------------------------------------------------------------------------

class TestGetTargetAssociatedLambdas:
    @patch("discovery.target_discovery.create_target_client")
    def test_returns_set(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.list_lambda_functions.return_value = {
            "LambdaFunctions": ["arn:aws:lambda:us-west-2:123:function:f1"],
        }
        result = _get_target_associated_lambdas("us-west-2", "inst-1")
        assert "arn:aws:lambda:us-west-2:123:function:f1" in result


# ---------------------------------------------------------------------------
# _get_target_associated_bots
# ---------------------------------------------------------------------------

class TestGetTargetAssociatedBots:
    @patch("discovery.target_discovery.create_target_client")
    def test_returns_set(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.list_bots.return_value = {
            "LexBots": [
                {"LexV2Bot": {"AliasArn": "arn:aws:lex:us-west-2:123:bot-alias/BOT1/ALIAS1"}},
            ],
        }
        result = _get_target_associated_bots("us-west-2", "inst-1")
        assert "arn:aws:lex:us-west-2:123:bot-alias/BOT1/ALIAS1" in result


# ---------------------------------------------------------------------------
# _discover_lambdas
# ---------------------------------------------------------------------------

class TestDiscoverLambdas:
    @patch("discovery.target_discovery.create_target_client")
    def test_finds_matching_lambda(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Functions": [
                {"FunctionName": "my-func", "FunctionArn": "arn:aws:lambda:us-west-2:123:function:my-func"},
                {"FunctionName": "other-func", "FunctionArn": "arn:aws:lambda:us-west-2:123:function:other-func"},
            ]},
        ]

        source_lambdas = ["arn:aws:lambda:us-east-1:123:function:my-func"]
        target_associated: set[str] = set()

        result = _discover_lambdas("us-west-2", source_lambdas, target_associated)
        assert len(result) == 1
        assert result[0]["name"] == "my-func"
        assert result[0]["association_status"] == "not_associated"

    @patch("discovery.target_discovery.create_target_client")
    def test_marks_already_associated(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Functions": [
                {"FunctionName": "my-func", "FunctionArn": "arn:aws:lambda:us-west-2:123:function:my-func"},
            ]},
        ]

        source_lambdas = ["arn:aws:lambda:us-east-1:123:function:my-func"]
        target_associated = {"arn:aws:lambda:us-west-2:123:function:my-func"}

        result = _discover_lambdas("us-west-2", source_lambdas, target_associated)
        assert len(result) == 1
        assert result[0]["association_status"] == "already_associated"

    @patch("discovery.target_discovery.create_target_client")
    def test_no_source_lambdas(self, mock_create):
        result = _discover_lambdas("us-west-2", [], set())
        assert result == []

    @patch("discovery.target_discovery.create_target_client")
    def test_handles_error(self, mock_create):
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        paginator = MagicMock()
        mock_client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = _client_error()

        result = _discover_lambdas("us-west-2", ["arn:aws:lambda:us-east-1:123:function:f1"], set())
        assert result == []



# ---------------------------------------------------------------------------
# _discover_lex_bots
# ---------------------------------------------------------------------------

class TestDiscoverLexBots:
    @patch("discovery.target_discovery.create_target_client")
    def test_finds_matching_bot(self, mock_create_target):
        mock_lex = MagicMock()
        mock_sts = MagicMock()

        def create_target_side_effect(service, region):
            if service == "lexv2-models":
                return mock_lex
            if service == "sts":
                return mock_sts
            return MagicMock()

        mock_create_target.side_effect = create_target_side_effect
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        mock_lex.list_bots.return_value = {
            "botSummaries": [
                {"botId": "BOT1", "botName": "my-bot"},
                {"botId": "BOT_OTHER", "botName": "unrelated-bot"},
            ],
        }
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [
                {"botAliasId": "ALIAS1"},
            ],
        }
        mock_lex.describe_bot.return_value = {"botId": "BOT1"}

        source_bots = [{"alias_arn": "arn:aws:lex:us-east-1:123456789012:bot-alias/BOT1/SRCALIAS"}]
        target_associated: set[str] = set()

        result = _discover_lex_bots("us-west-2", source_bots, target_associated, "inst-1")
        assert len(result) == 1
        assert result[0]["name"] == "my-bot"
        assert result[0]["resource_type"] == "LEX_BOT"
        assert result[0]["association_status"] == "not_associated"

    @patch("discovery.target_discovery.create_target_client")
    def test_marks_already_associated(self, mock_create_target):
        mock_lex = MagicMock()
        mock_sts = MagicMock()

        def create_target_side_effect(service, region):
            if service == "lexv2-models":
                return mock_lex
            if service == "sts":
                return mock_sts
            return MagicMock()

        mock_create_target.side_effect = create_target_side_effect
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        mock_lex.list_bots.return_value = {
            "botSummaries": [{"botId": "BOT1", "botName": "my-bot"}],
        }
        mock_lex.list_bot_aliases.return_value = {
            "botAliasSummaries": [{"botAliasId": "ALIAS1"}],
        }
        mock_lex.describe_bot.return_value = {"botId": "BOT1"}

        source_bots = [{"alias_arn": "arn:aws:lex:us-east-1:123456789012:bot-alias/BOT1/SRCALIAS"}]
        target_associated = {"arn:aws:lex:us-west-2:123456789012:bot-alias/BOT1/ALIAS1"}

        result = _discover_lex_bots("us-west-2", source_bots, target_associated, "inst-1")
        assert len(result) == 1
        assert result[0]["association_status"] == "already_associated"

    def test_no_source_bots(self):
        result = _discover_lex_bots("us-west-2", [], set(), "inst-1")
        assert result == []

    @patch("discovery.target_discovery.create_target_client")
    def test_handles_error(self, mock_create_target):
        mock_lex = MagicMock()
        mock_create_target.return_value = mock_lex
        mock_lex.list_bots.side_effect = _client_error()

        source_bots = [{"alias_arn": "arn:aws:lex:us-east-1:123:bot-alias/BOT1/ALIAS1"}]
        result = _discover_lex_bots("us-west-2", source_bots, set(), "inst-1")
        assert result == []


# ---------------------------------------------------------------------------
# _discover_kinesis_streams
# ---------------------------------------------------------------------------

class TestDiscoverKinesisStreams:
    @patch("discovery.target_discovery.create_target_client")
    def test_finds_matching_stream(self, mock_create):
        mock_kinesis = MagicMock()
        mock_create.return_value = mock_kinesis
        mock_kinesis.list_streams.return_value = {"StreamNames": ["my-stream", "other-stream"]}
        mock_kinesis.describe_stream_summary.return_value = {
            "StreamDescriptionSummary": {"StreamARN": "arn:aws:kinesis:us-west-2:123:stream/my-stream"},
        }

        source_storage = {
            "AGENT_EVENTS": [{"StorageType": "KINESIS_STREAM", "KinesisStreamConfig": {"StreamArn": "arn:aws:kinesis:us-east-1:123:stream/my-stream"}}],
        }
        target_storage: dict = {}

        result = _discover_kinesis_streams("us-west-2", source_storage, target_storage)
        assert len(result) == 1
        assert result[0]["name"] == "my-stream"
        assert result[0]["association_status"] == "not_associated"

    @patch("discovery.target_discovery.create_target_client")
    def test_marks_already_associated(self, mock_create):
        mock_kinesis = MagicMock()
        mock_create.return_value = mock_kinesis
        mock_kinesis.list_streams.return_value = {"StreamNames": ["my-stream"]}
        mock_kinesis.describe_stream_summary.return_value = {
            "StreamDescriptionSummary": {"StreamARN": "arn:aws:kinesis:us-west-2:123:stream/my-stream"},
        }

        source_storage = {
            "AGENT_EVENTS": [{"StorageType": "KINESIS_STREAM", "KinesisStreamConfig": {"StreamArn": "arn:aws:kinesis:us-east-1:123:stream/my-stream"}}],
        }
        target_storage = {
            "AGENT_EVENTS": [{"StorageType": "KINESIS_STREAM", "KinesisStreamConfig": {"StreamArn": "arn:aws:kinesis:us-west-2:123:stream/my-stream"}}],
        }

        result = _discover_kinesis_streams("us-west-2", source_storage, target_storage)
        assert len(result) == 1
        assert result[0]["association_status"] == "already_associated"

    def test_no_source_streams(self):
        result = _discover_kinesis_streams("us-west-2", {}, {})
        assert result == []


# ---------------------------------------------------------------------------
# _discover_firehose_streams
# ---------------------------------------------------------------------------

class TestDiscoverFirehoseStreams:
    @patch("discovery.target_discovery.create_target_client")
    def test_finds_matching_firehose(self, mock_create):
        mock_firehose = MagicMock()
        mock_create.return_value = mock_firehose
        mock_firehose.list_delivery_streams.return_value = {"DeliveryStreamNames": ["my-firehose"]}
        mock_firehose.describe_delivery_stream.return_value = {
            "DeliveryStreamDescription": {"DeliveryStreamARN": "arn:aws:firehose:us-west-2:123:deliverystream/my-firehose"},
        }

        source_storage = {
            "CONTACT_TRACE_RECORDS": [{"StorageType": "KINESIS_FIREHOSE", "KinesisFirehoseConfig": {"FirehoseArn": "arn:aws:firehose:us-east-1:123:deliverystream/my-firehose"}}],
        }

        result = _discover_firehose_streams("us-west-2", source_storage, {})
        assert len(result) == 1
        assert result[0]["name"] == "my-firehose"
        assert result[0]["association_status"] == "not_associated"

    def test_no_source_firehoses(self):
        result = _discover_firehose_streams("us-west-2", {}, {})
        assert result == []


# ---------------------------------------------------------------------------
# _discover_s3_buckets
# ---------------------------------------------------------------------------

class TestDiscoverS3Buckets:
    @patch("discovery.target_discovery.create_target_client")
    def test_finds_matching_bucket(self, mock_create):
        mock_s3 = MagicMock()
        mock_create.return_value = mock_s3
        # head_bucket succeeds for the original bucket name
        mock_s3.head_bucket.return_value = {}

        source_storage = {
            "CALL_RECORDINGS": [{"StorageType": "S3", "S3Config": {"BucketName": "my-recordings-bucket"}}],
        }

        result = _discover_s3_buckets("us-east-1", "us-west-2", source_storage, {})
        assert len(result) == 1
        assert result[0]["name"] == "my-recordings-bucket"
        assert result[0]["association_status"] == "not_associated"

    @patch("discovery.target_discovery.create_target_client")
    def test_finds_dr_variant(self, mock_create):
        mock_s3 = MagicMock()
        mock_create.return_value = mock_s3

        # Original bucket doesn't exist, but -dr variant does
        def head_bucket(**kwargs):
            bucket = kwargs.get("Bucket", "")
            if bucket == "my-bucket-dr":
                return {}
            raise _client_error("404", "Not Found")

        mock_s3.head_bucket.side_effect = head_bucket

        source_storage = {
            "CALL_RECORDINGS": [{"StorageType": "S3", "S3Config": {"BucketName": "my-bucket"}}],
        }

        result = _discover_s3_buckets("us-east-1", "us-west-2", source_storage, {})
        assert len(result) == 1
        assert result[0]["name"] == "my-bucket-dr"

    @patch("discovery.target_discovery.create_target_client")
    def test_marks_already_associated(self, mock_create):
        mock_s3 = MagicMock()
        mock_create.return_value = mock_s3
        mock_s3.head_bucket.return_value = {}

        source_storage = {
            "CALL_RECORDINGS": [{"StorageType": "S3", "S3Config": {"BucketName": "my-bucket"}}],
        }
        target_storage = {
            "CALL_RECORDINGS": [{"StorageType": "S3", "S3Config": {"BucketName": "my-bucket"}}],
        }

        result = _discover_s3_buckets("us-east-1", "us-west-2", source_storage, target_storage)
        assert len(result) == 1
        assert result[0]["association_status"] == "already_associated"

    def test_no_source_buckets(self):
        result = _discover_s3_buckets("us-east-1", "us-west-2", {}, {})
        assert result == []


# ---------------------------------------------------------------------------
# _discover_kvs_config
# ---------------------------------------------------------------------------

class TestDiscoverKvsConfig:
    def test_finds_kvs_config(self):
        source_storage = {
            "MEDIA_STREAMS": [{"StorageType": "KINESIS_VIDEO_STREAM", "KinesisVideoStreamConfig": {"Prefix": "iad-connect", "RetentionPeriodHours": 24}}],
        }
        target_storage: dict = {}

        result = _discover_kvs_config("us-west-2", source_storage, target_storage, "inst-1")
        assert len(result) == 1
        assert result[0]["resource_type"] == "KINESIS_VIDEO_STREAM"
        assert result[0]["association_status"] == "not_associated"
        assert result[0]["kvs_prefix"] == "iad-connect"

    def test_marks_already_associated(self):
        source_storage = {
            "MEDIA_STREAMS": [{"StorageType": "KINESIS_VIDEO_STREAM", "KinesisVideoStreamConfig": {"Prefix": "iad-connect", "RetentionPeriodHours": 24}}],
        }
        target_storage = {
            "MEDIA_STREAMS": [{"StorageType": "KINESIS_VIDEO_STREAM", "KinesisVideoStreamConfig": {"Prefix": "pdx-connect"}}],
        }

        result = _discover_kvs_config("us-west-2", source_storage, target_storage, "inst-1")
        assert len(result) == 1
        assert result[0]["association_status"] == "already_associated"

    def test_no_source_kvs(self):
        result = _discover_kvs_config("us-west-2", {}, {}, "inst-1")
        assert result == []


# ---------------------------------------------------------------------------
# discover_target_resources (integration)
# ---------------------------------------------------------------------------

class TestDiscoverTargetResources:
    @patch("discovery.target_discovery._discover_kvs_config")
    @patch("discovery.target_discovery._discover_s3_buckets")
    @patch("discovery.target_discovery._discover_firehose_streams")
    @patch("discovery.target_discovery._discover_kinesis_streams")
    @patch("discovery.target_discovery._discover_lambdas")
    @patch("discovery.target_discovery._discover_lex_bots")
    @patch("discovery.target_discovery._get_target_storage_configs")
    @patch("discovery.target_discovery._get_target_associated_bots")
    @patch("discovery.target_discovery._get_target_associated_lambdas")
    @patch("discovery.target_discovery._get_source_storage_configs")
    @patch("discovery.target_discovery._get_source_bots")
    @patch("discovery.target_discovery._get_source_lambdas")
    def test_orchestrates_all_discovery(
        self, mock_src_lambdas, mock_src_bots, mock_src_storage,
        mock_tgt_lambdas, mock_tgt_bots, mock_tgt_storage,
        mock_disc_lex, mock_disc_lambda, mock_disc_kinesis,
        mock_disc_firehose, mock_disc_s3, mock_disc_kvs,
    ):
        mock_src_lambdas.return_value = []
        mock_src_bots.return_value = []
        mock_src_storage.return_value = {}
        mock_tgt_lambdas.return_value = set()
        mock_tgt_bots.return_value = set()
        mock_tgt_storage.return_value = {}

        mock_disc_lex.return_value = [{"name": "bot1", "resource_type": "LEX_BOT", "arn": "arn:lex", "association_status": "not_associated"}]
        mock_disc_lambda.return_value = [{"name": "func1", "resource_type": "LAMBDA", "arn": "arn:lambda", "association_status": "not_associated"}]
        mock_disc_kinesis.return_value = []
        mock_disc_firehose.return_value = []
        mock_disc_s3.return_value = []
        mock_disc_kvs.return_value = []

        result = discover_target_resources("inst-1", "us-east-1", "us-west-2")
        assert len(result) == 2
        assert result[0]["name"] == "bot1"
        assert result[1]["name"] == "func1"


# ---------------------------------------------------------------------------
# Route tests for /api/discover-target and /api/associate-discovered
# ---------------------------------------------------------------------------

class TestDiscoverTargetRoute:
    @patch("discovery.target_discovery.discover_target_resources")
    @patch("api.routes.resolve_target_region", return_value="us-west-2")
    @patch("api.routes.extract_region", return_value="us-east-1")
    @patch("api.routes.parse_arn", return_value={"service": "connect", "resource": "instance/inst-1", "partition": "aws", "region": "us-east-1", "account": "123456789012"})
    def test_discover_target_success(self, mock_parse, mock_extract, mock_resolve, mock_discover):
        """Test the discover-target endpoint returns discovered resources."""
        from fastapi.testclient import TestClient
        from api.routes import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        mock_discover.return_value = [
            {"name": "my-func", "resource_type": "LAMBDA", "arn": "arn:lambda", "source_match": "my-func", "association_status": "not_associated"},
        ]

        resp = client.post("/api/discover-target", json={"instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalDiscovered"] == 1
        assert data["totalNotAssociated"] == 1
        assert data["totalAlreadyAssociated"] == 0
        assert len(data["resources"]) == 1

    def test_discover_target_invalid_arn(self):
        from fastapi.testclient import TestClient
        from api.routes import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.post("/api/discover-target", json={"instanceArn": "not-an-arn"})
        assert resp.status_code == 400


class TestAssociateDiscoveredRoute:
    @patch("api.routes.create_target_client")
    @patch("api.routes.resolve_target_region", return_value="us-west-2")
    @patch("api.routes.extract_region", return_value="us-east-1")
    @patch("api.routes.parse_arn", return_value={"service": "connect", "resource": "instance/inst-1", "partition": "aws", "region": "us-east-1", "account": "123456789012"})
    def test_associate_discovered_lambda(self, mock_parse, mock_extract, mock_resolve, mock_create_target):
        from fastapi.testclient import TestClient
        from api.routes import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        mock_connect = MagicMock()
        mock_create_target.return_value = mock_connect
        mock_connect.list_lambda_functions.return_value = {"LambdaFunctions": []}
        mock_connect.list_bots.return_value = {"LexBots": []}
        mock_connect.list_instance_storage_configs.return_value = {"StorageConfigs": []}
        mock_connect.describe_instance_attribute.return_value = {"Attribute": {"Value": "true"}}

        with patch("association.resource_association._enable_instance_attributes", return_value=[]), \
             patch("association.resource_association._list_associated_lambdas", return_value=set()), \
             patch("association.resource_association._list_associated_bots", return_value=set()), \
             patch("association.resource_association._read_source_storage_configs", return_value={}), \
             patch("association.resource_association._build_source_mappings", return_value=({}, {}, {})), \
             patch("association.resource_association._ensure_lambda_connect_permission"):
            mock_connect.associate_lambda_function.return_value = {}

            resp = client.post("/api/associate-discovered", json={
                "instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1",
                "resources": [
                    {"name": "my-func", "resource_type": "LAMBDA", "arn": "arn:aws:lambda:us-west-2:123456789012:function:my-func"},
                ],
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["totalAssociated"] == 1

    def test_associate_discovered_no_resources(self):
        from fastapi.testclient import TestClient
        from api.routes import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.post("/api/associate-discovered", json={
            "instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1",
            "resources": [],
        })
        assert resp.status_code == 400
