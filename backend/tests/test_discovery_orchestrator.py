"""Unit tests for the discovery orchestrator and new API routes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from typing import Any

import pytest

from discovery.orchestrator import (
    _deduplicate_resources,
    run_discovery,
)
from models.enums import ReplicationStatus, ResourceType
from models.inventory import ResourceInventory
from models.resources import (
    IAMRoleResource,
    KinesisStreamResource,
    KinesisVideoResource,
    LambdaResource,
    LexBotResource,
    ResourceBase,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lambda(arn: str, name: str = "func", rid: str | None = None) -> LambdaResource:
    return LambdaResource(
        id=rid or arn[-12:],
        name=name,
        arn=arn,
        runtime="python3.12",
        handler="index.handler",
        memory_size=128,
        timeout=30,
        execution_role_arn="arn:aws:iam::123456789012:role/role1",
    )


def _make_iam_role(arn: str, name: str = "role", rid: str | None = None) -> IAMRoleResource:
    return IAMRoleResource(
        id=rid or arn[-12:],
        name=name,
        arn=arn,
        assume_role_policy={"Version": "2012-10-17"},
        inline_policies=[
            {
                "PolicyName": "inline1",
                "PolicyDocument": {
                    "Statement": [
                        {"Resource": "arn:aws:dynamodb:us-west-2:123456789012:table/MyTable"}
                    ]
                },
            }
        ],
    )


def _make_lex(arn: str, name: str = "bot", rid: str | None = None) -> LexBotResource:
    return LexBotResource(
        id=rid or arn[-12:],
        name=name,
        arn=arn,
        bot_id="bot-123",
    )


def _make_kinesis_stream(arn: str, name: str = "stream", rid: str | None = None) -> KinesisStreamResource:
    return KinesisStreamResource(
        id=rid or arn[-12:],
        name=name,
        arn=arn,
        shard_count=1,
        retention_period=24,
        stream_mode="PROVISIONED",
    )


def _make_kvs(arn: str, name: str = "kvs", rid: str | None = None) -> KinesisVideoResource:
    return KinesisVideoResource(
        id=rid or arn[-12:],
        name=name,
        arn=arn,
        data_retention_in_hours=24,
    )


# ---------------------------------------------------------------------------
# Tests: _deduplicate_resources
# ---------------------------------------------------------------------------

class TestDeduplicateResources:
    def test_removes_duplicate_arns(self):
        r1 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:func1", rid="id1")
        r2 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:func1", rid="id2")
        result = _deduplicate_resources([r1, r2])
        assert len(result) == 1
        assert result[0].id == "id1"

    def test_keeps_unique_arns(self):
        r1 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:func1", rid="id1")
        r2 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:func2", rid="id2")
        result = _deduplicate_resources([r1, r2])
        assert len(result) == 2

    def test_empty_list(self):
        assert _deduplicate_resources([]) == []

    def test_preserves_order(self):
        r1 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:a", rid="id1")
        r2 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:b", rid="id2")
        r3 = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:c", rid="id3")
        result = _deduplicate_resources([r1, r2, r3])
        assert [r.id for r in result] == ["id1", "id2", "id3"]


# ---------------------------------------------------------------------------
# Tests: run_discovery (with mocked discovery modules)
# ---------------------------------------------------------------------------

class TestRunDiscovery:
    """Tests for the run_discovery orchestrator function."""

    @patch("discovery.orchestrator.discover_kvs_resources")
    @patch("discovery.orchestrator.discover_streaming_resources")
    @patch("discovery.orchestrator.discover_lex_bots")
    @patch("discovery.orchestrator.discover_lambda_functions")
    def test_returns_session_id_and_inventory(
        self,
        mock_lambda,
        mock_lex,
        mock_streaming,
        mock_kvs,
    ):
        lam = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:f1", rid="lam1")
        role = _make_iam_role("arn:aws:iam::123456789012:role/r1", rid="role1")

        mock_lambda.return_value = ([lam], [role], [], [])
        mock_lex.return_value = ([], [])
        mock_streaming.return_value = []
        mock_kvs.return_value = ([], [])

        session_id, inventory = run_discovery("inst-1", "us-west-2")

        assert isinstance(session_id, str)
        assert len(session_id) > 0
        assert isinstance(inventory, ResourceInventory)
        assert len(inventory) == 2  # lambda + iam role

    @patch("discovery.orchestrator.discover_kvs_resources")
    @patch("discovery.orchestrator.discover_streaming_resources")
    @patch("discovery.orchestrator.discover_lex_bots")
    @patch("discovery.orchestrator.discover_lambda_functions")
    def test_deduplicates_resources_by_arn(
        self,
        mock_lambda,
        mock_lex,
        mock_streaming,
        mock_kvs,
    ):
        # Same ARN returned by both lambda and streaming
        stream = _make_kinesis_stream(
            "arn:aws:kinesis:us-west-2:123456789012:stream/ctr-stream", rid="s1"
        )
        stream_dup = _make_kinesis_stream(
            "arn:aws:kinesis:us-west-2:123456789012:stream/ctr-stream", rid="s2"
        )

        mock_lambda.return_value = ([], [], [], [])
        mock_lex.return_value = ([], [])
        mock_streaming.return_value = [stream, stream_dup]
        mock_kvs.return_value = ([], [])

        _, inventory = run_discovery("inst-1", "us-west-2")

        assert len(inventory) == 1

    @patch("discovery.orchestrator.discover_kvs_resources")
    @patch("discovery.orchestrator.discover_streaming_resources")
    @patch("discovery.orchestrator.discover_lex_bots")
    @patch("discovery.orchestrator.discover_lambda_functions")
    def test_all_resource_types_included(
        self,
        mock_lambda,
        mock_lex,
        mock_streaming,
        mock_kvs,
    ):
        lam = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:f1", rid="lam1")
        role = _make_iam_role("arn:aws:iam::123456789012:role/r1", rid="role1")
        lex = _make_lex("arn:aws:lex:us-west-2:123456789012:bot/b1", rid="lex1")
        stream = _make_kinesis_stream(
            "arn:aws:kinesis:us-west-2:123456789012:stream/s1", rid="str1"
        )
        kvs = _make_kvs(
            "arn:aws:kinesisvideo:us-west-2:123456789012:stream/v1", rid="kvs1"
        )

        mock_lambda.return_value = ([lam], [role], [], [])
        mock_lex.return_value = ([lex], [])
        mock_streaming.return_value = [stream]
        mock_kvs.return_value = ([kvs], [])

        _, inventory = run_discovery("inst-1", "us-west-2")

        assert len(inventory) == 5
        types = {r.resource_type for r in inventory.get_all()}
        assert ResourceType.LAMBDA in types
        assert ResourceType.IAM_ROLE in types
        assert ResourceType.LEX_BOT in types
        assert ResourceType.KINESIS_STREAM in types
        assert ResourceType.KINESIS_VIDEO_STREAM in types

    @patch("discovery.orchestrator.discover_kvs_resources")
    @patch("discovery.orchestrator.discover_streaming_resources")
    @patch("discovery.orchestrator.discover_lex_bots")
    @patch("discovery.orchestrator.discover_lambda_functions")
    def test_empty_discovery_returns_empty_inventory(
        self,
        mock_lambda,
        mock_lex,
        mock_streaming,
        mock_kvs,
    ):
        mock_lambda.return_value = ([], [], [], [])
        mock_lex.return_value = ([], [])
        mock_streaming.return_value = []
        mock_kvs.return_value = ([], [])

        session_id, inventory = run_discovery("inst-1", "us-west-2")

        assert len(inventory) == 0
        assert isinstance(session_id, str)


# ---------------------------------------------------------------------------
# Tests: API routes (POST /api/discover, GET /api/inventory/{session_id})
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from main import app

api_client = TestClient(app)

VALID_CONNECT_ARN = "arn:aws:connect:us-west-2:123456789012:instance/abc-def-123"


class TestDiscoverRoute:
    """Tests for POST /api/discover."""

    @patch("api.routes.run_discovery")
    def test_discover_returns_session_and_inventory(self, mock_run):
        lam = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:f1", rid="lam1")
        inv = ResourceInventory()
        inv.add_resource(lam)
        mock_run.return_value = ("sess-123", inv)

        resp = api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "sess-123"
        assert len(body["inventory"]) == 1
        assert body["inventory"][0]["arn"] == lam.arn

    @patch("api.routes.run_discovery")
    def test_discover_empty_inventory(self, mock_run):
        inv = ResourceInventory()
        mock_run.return_value = ("sess-empty", inv)

        resp = api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "sess-empty"
        assert body["inventory"] == []

    def test_discover_invalid_arn(self):
        resp = api_client.post("/api/discover", json={"instanceArn": "not-an-arn"})
        assert resp.status_code == 422

    def test_discover_non_connect_arn(self):
        arn = "arn:aws:lambda:us-west-2:123456789012:function:my-func"
        resp = api_client.post("/api/discover", json={"instanceArn": arn})
        assert resp.status_code == 400

    def test_discover_unsupported_region(self):
        arn = "arn:aws:connect:ap-southeast-1:123456789012:instance/inst-1"
        resp = api_client.post("/api/discover", json={"instanceArn": arn})
        assert resp.status_code == 400

    def test_discover_not_instance_resource(self):
        arn = "arn:aws:connect:us-west-2:123456789012:contact-flow/flow-1"
        resp = api_client.post("/api/discover", json={"instanceArn": arn})
        assert resp.status_code == 400

    @patch("api.routes.run_discovery")
    def test_discover_handles_exception(self, mock_run):
        mock_run.side_effect = RuntimeError("AWS error")

        resp = api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        assert resp.status_code == 500
        assert "Discovery failed" in resp.json()["detail"]


class TestGetInventoryRoute:
    """Tests for GET /api/inventory/{session_id}."""

    @patch("api.routes.run_discovery")
    def test_get_inventory_after_discover(self, mock_run):
        lam = _make_lambda("arn:aws:lambda:us-west-2:123456789012:function:f1", rid="lam1")
        inv = ResourceInventory()
        inv.add_resource(lam)
        mock_run.return_value = ("sess-get-test", inv)

        # First, run discovery to create the session
        api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        # Then, retrieve the inventory
        resp = api_client.get("/api/inventory/sess-get-test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["sessionId"] == "sess-get-test"
        assert body["sourceRegion"] == "us-west-2"
        assert body["targetRegion"] == "us-east-1"
        assert len(body["inventory"]) == 1

    def test_get_inventory_not_found(self):
        resp = api_client.get("/api/inventory/nonexistent-session")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
