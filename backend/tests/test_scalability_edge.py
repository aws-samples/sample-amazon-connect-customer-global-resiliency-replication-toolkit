"""Edge-case and scalability tests for the post-demo hardening:

- #5 configurable replica suffix (aws.naming)
- #2 batched/gated live association verification (api.routes._verify_associations_live)
- #1 repl_status overlay at scale (store round-trip)

Scalability is asserted structurally: the association verify makes at most one
List call PER RESOURCE TYPE regardless of how many resources there are, and is
fully gated (zero AWS calls once everything is terminal).
"""

import asyncio
import importlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from models.enums import ReplicationStatus, ResourceType
from models.resources import (
    ApprovedOriginResource,
    LambdaResource,
    S3BucketResource,
)
from models.session import Session


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _session(inventory) -> Session:
    return Session(
        session_id="sess-x",
        instance_arn="arn:aws:connect:us-east-1:123456789012:instance/abc123",
        instance_name="I",
        source_region="us-east-1",
        target_region="us-west-2",
        inventory=inventory,
        replication_jobs=[],
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _lambda(rid, replicated=True):
    r = LambdaResource(
        id=rid, name=f"fn-{rid}",
        arn=f"arn:aws:lambda:us-east-1:123456789012:function:fn-{rid}",
        runtime="python3.12", handler="i.h", memory_size=128, timeout=30,
        execution_role_arn="arn:aws:iam::123456789012:role/r",
    )
    if replicated:
        r.status = ReplicationStatus.REPLICATED
        r.replicated_arn = f"arn:aws:lambda:us-west-2:123456789012:function:fn-{rid}"
    return r


# ---------------------------------------------------------------------------
# #5 — configurable replica suffix
# ---------------------------------------------------------------------------

class TestReplicaSuffix:
    def _naming(self):
        import aws.naming as naming
        return importlib.reload(naming)

    def test_default_suffix(self, monkeypatch):
        monkeypatch.delenv("ACGR_REPLICA_SUFFIX", raising=False)
        n = self._naming()
        assert n.replica_suffix() == "-dr"
        assert n.target_replica_name("my-bucket") == "my-bucket-dr"

    def test_custom_suffix_with_hyphen(self, monkeypatch):
        monkeypatch.setenv("ACGR_REPLICA_SUFFIX", "-failover")
        n = self._naming()
        assert n.target_replica_name("b") == "b-failover"

    def test_custom_suffix_without_leading_hyphen(self, monkeypatch):
        monkeypatch.setenv("ACGR_REPLICA_SUFFIX", "dr2")
        n = self._naming()
        assert n.target_replica_name("b") == "b-dr2"

    def test_invalid_suffix_falls_back(self, monkeypatch):
        # Uppercase / illegal chars would produce invalid bucket names.
        monkeypatch.setenv("ACGR_REPLICA_SUFFIX", "BAD_$$")
        n = self._naming()
        assert n.replica_suffix() == "-dr"

    def test_empty_suffix_falls_back(self, monkeypatch):
        monkeypatch.setenv("ACGR_REPLICA_SUFFIX", "   ")
        n = self._naming()
        assert n.replica_suffix() == "-dr"


# ---------------------------------------------------------------------------
# #2 — live association verification: gating, batching, matching
# ---------------------------------------------------------------------------

class TestVerifyAssociationsLive:
    def test_gate_no_candidates_makes_zero_aws_calls(self):
        # All resources already terminal → must not even create a client.
        inv = {"l1": _lambda("l1")}
        assoc_map = {"fn-l1": {"status": "already_associated"}}
        with patch("api.routes.create_target_client") as mk:
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session(inv), assoc_map))
        assert out == {}
        mk.assert_not_called()

    def test_lambda_found_associated(self):
        inv = {"l1": _lambda("l1")}
        assoc_map: dict = {}  # not attempted
        connect = MagicMock()
        connect.list_lambda_functions.return_value = {
            "LambdaFunctions": ["arn:aws:lambda:us-west-2:123456789012:function:fn-l1"],
        }
        with patch("api.routes.create_target_client", return_value=connect):
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session(inv), assoc_map))
        assert out == {"fn-l1": "associated"}

    def test_lambda_not_associated(self):
        inv = {"l1": _lambda("l1")}
        connect = MagicMock()
        connect.list_lambda_functions.return_value = {"LambdaFunctions": []}
        with patch("api.routes.create_target_client", return_value=connect):
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session(inv), {}))
        assert out == {}

    def test_batching_one_call_regardless_of_count(self):
        # 100 lambda resources → exactly ONE list_lambda_functions call.
        inv = {f"l{i}": _lambda(f"l{i}") for i in range(100)}
        connect = MagicMock()
        connect.list_lambda_functions.return_value = {
            "LambdaFunctions": [r.replicated_arn for r in inv.values()],
        }
        with patch("api.routes.create_target_client", return_value=connect):
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session(inv), {}))
        assert len(out) == 100
        assert connect.list_lambda_functions.call_count == 1  # O(types), not O(resources)

    def test_approved_origin_match(self):
        origin = ApprovedOriginResource(
            id="o1", name="https://example.com",
            arn="arn:aws:connect:us-east-1:123456789012:instance/abc123",
        )
        origin.status = ReplicationStatus.REPLICATED
        origin.replicated_arn = "https://example.com"
        connect = MagicMock()
        connect.list_approved_origins.return_value = {"Origins": ["https://example.com"]}
        with patch("api.routes.create_target_client", return_value=connect):
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session({"o1": origin}), {}))
        assert out == {"https://example.com": "associated"}

    def test_s3_via_storage_config(self):
        s3 = S3BucketResource(
            id="s1", name="amazon-connect-abc",
            arn="arn:aws:s3:::amazon-connect-abc",
        )
        s3.status = ReplicationStatus.REPLICATED
        s3.replicated_arn = "arn:aws:s3:::amazon-connect-abc-dr"
        connect = MagicMock()
        def _lsc(InstanceId, ResourceType):
            if ResourceType == "CALL_RECORDINGS":
                return {"StorageConfigs": [{"StorageType": "S3", "S3Config": {"BucketName": "amazon-connect-abc-dr"}}]}
            return {"StorageConfigs": []}
        connect.list_instance_storage_configs.side_effect = _lsc
        with patch("api.routes.create_target_client", return_value=connect):
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session({"s1": s3}), {}))
        assert out == {"amazon-connect-abc": "associated"}

    def test_failure_is_non_fatal(self):
        inv = {"l1": _lambda("l1")}
        connect = MagicMock()
        connect.list_lambda_functions.side_effect = RuntimeError("boom")
        with patch("api.routes.create_target_client", return_value=connect):
            from api.routes import _verify_associations_live
            out = _run(_verify_associations_live(_session(inv), {}))
        assert out == {}  # swallowed, no crash


# ---------------------------------------------------------------------------
# #1 — repl_status overlay at scale
# ---------------------------------------------------------------------------

class TestReplStatusAtScale:
    def test_large_inventory_round_trip_and_overlay(self):
        from store.dynamodb_store import _serialize_session, _deserialize_session

        inv = {f"l{i}": _lambda(f"l{i}", replicated=False) for i in range(300)}
        for r in inv.values():
            r.status = ReplicationStatus.IN_PROGRESS
        s = _session(inv)
        item = _serialize_session(s)

        # Simulate 300 concurrent atomic updates flipping each to REPLICATED.
        for i in range(300):
            item["repl_status"][f"l{i}"] = {
                "status": "REPLICATED",
                "replicated_arn": f"arn:aws:lambda:us-west-2:123456789012:function:fn-l{i}",
                "error": None,
                "error_classification": None,
            }
        restored = _deserialize_session(item)
        assert len(restored.inventory) == 300
        assert all(r.status == ReplicationStatus.REPLICATED for r in restored.inventory.values())
