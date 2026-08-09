"""End-to-end edge-case and scalability tests for the post-demo fixes.

Focus areas:
  * #1 concurrency — parallel per-resource status writes must not clobber
  * DynamoDB 400KB item limit with the new uncompressed repl_status map
  * cleanup — IN_PROGRESS resources with a replica ARN are cleaned; IAM never is
  * pagination — >1 page of associated bots must still be found
  * idempotency / resilience of naming and status overlay
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock

from models.enums import ReplicationStatus, ResourceType
from models.resources import LambdaResource, S3BucketResource
from models.session import Session
from store.dynamodb_store import _deserialize_session, _serialize_session
from store.memory_store import InMemorySessionStore


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _session(inventory: dict, sid: str = "s-1") -> Session:
    return Session(
        session_id=sid,
        instance_arn="arn:aws:connect:us-east-1:111122223333:instance/abc",
        instance_name="inst",
        source_region="us-east-1",
        target_region="us-west-2",
        inventory=inventory,
        replication_jobs=[],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _lambda(rid: str) -> LambdaResource:
    return LambdaResource(
        id=rid,
        name=f"fn-{rid}",
        arn=f"arn:aws:lambda:us-east-1:111122223333:function:fn-{rid}",
        runtime="python3.12",
        handler="i.h",
        memory_size=128,
        timeout=30,
        execution_role_arn="arn:aws:iam::111122223333:role/r",
    )


# ---------------------------------------------------------------------------
# #1 Concurrency — the original clobber bug
# ---------------------------------------------------------------------------

class TestConcurrentStatusWrites:
    def test_parallel_updates_do_not_clobber_each_other(self):
        """N concurrent per-resource updates must ALL persist.

        This is the regression test for the Step Functions clobber: with the
        old whole-session save_session() path, concurrent writers overwrote
        each other and only the last one survived.
        """
        store = InMemorySessionStore()
        inv = {f"r{i}": _lambda(f"r{i}") for i in range(50)}
        for r in inv.values():
            r.status = ReplicationStatus.IN_PROGRESS
        _run(store.save_session(_session(inv)))

        def worker(i: int):
            return _run(store.update_resource_status(
                "s-1", f"r{i}", "REPLICATED",
                replicated_arn=f"arn:aws:lambda:us-west-2:111122223333:function:fn-r{i}",
            ))

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(worker, range(50)))

        got = _run(store.get_session("s-1"))
        statuses = [r.status for r in got.inventory.values()]
        assert all(s == ReplicationStatus.REPLICATED for s in statuses), (
            f"clobber detected: {sum(1 for s in statuses if s != ReplicationStatus.REPLICATED)} lost"
        )

    def test_repl_status_map_uses_targeted_update_expression(self):
        """The DynamoDB path must target a single map key, never rewrite the item."""
        from store.dynamodb_store import DynamoDBSessionStore

        store = DynamoDBSessionStore.__new__(DynamoDBSessionStore)
        table = MagicMock()
        store._table = table
        store._table_name = "t"

        _run(store.update_resource_status("s-1", "r-1", "REPLICATED", replicated_arn="arn:x"))

        assert table.update_item.called
        kwargs = table.update_item.call_args.kwargs
        assert "repl_status.#rid" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeNames"]["#rid"] == "r-1"
        # Critically: no put_item (whole-item overwrite) anywhere.
        assert not table.put_item.called


# ---------------------------------------------------------------------------
# Scalability — DynamoDB 400KB item limit with the new repl_status map
# ---------------------------------------------------------------------------

class TestItemSizeAtScale:
    def test_large_inventory_item_stays_under_dynamodb_limit(self):
        """500 resources must still serialize well under the 400KB item cap."""
        inv = {}
        for i in range(500):
            r = _lambda(f"r{i}")
            r.status = ReplicationStatus.REPLICATED
            r.replicated_arn = f"arn:aws:lambda:us-west-2:111122223333:function:fn-r{i}"
            inv[f"r{i}"] = r

        item = _serialize_session(_session(inv))
        size = len(json.dumps(item, default=str).encode())
        assert size < 400 * 1024, f"item too large: {size} bytes"
        # repl_status is uncompressed by design (needs atomic key updates), so
        # assert it stays a modest fraction of the budget.
        repl_size = len(json.dumps(item["repl_status"], default=str).encode())
        assert repl_size < 200 * 1024, f"repl_status too large: {repl_size}"

    def test_overlay_applies_to_every_resource_at_scale(self):
        inv = {}
        for i in range(300):
            r = _lambda(f"r{i}")
            r.status = ReplicationStatus.IN_PROGRESS
            inv[f"r{i}"] = r
        item = _serialize_session(_session(inv))
        for i in range(300):
            item["repl_status"][f"r{i}"] = {
                "status": "REPLICATED",
                "replicated_arn": f"arn:aws:lambda:us-west-2:111122223333:function:fn-r{i}",
                "error": None,
                "error_classification": None,
            }
        restored = _deserialize_session(item)
        assert all(
            r.status == ReplicationStatus.REPLICATED for r in restored.inventory.values()
        )

    def test_corrupt_repl_status_entry_does_not_break_read(self):
        """A malformed map entry must not blow up deserialization."""
        r = _lambda("r1")
        r.status = ReplicationStatus.REPLICATED
        item = _serialize_session(_session({"r1": r}))
        item["repl_status"]["r1"] = {"status": "NOT_A_REAL_STATUS"}
        item["repl_status"]["ghost"] = {"status": "REPLICATED"}  # unknown id
        item["repl_status"]["bad"] = "not-a-dict"
        restored = _deserialize_session(item)
        # Falls back to the inventory's own status rather than raising.
        assert restored.inventory["r1"].status == ReplicationStatus.REPLICATED


# ---------------------------------------------------------------------------
# #3 Cleanup edge cases
# ---------------------------------------------------------------------------

class TestCleanupSelection:
    def test_in_progress_with_replica_arn_is_cleaned(self):
        """A Lex replica still enabling must not be orphaned by cleanup."""
        from cleanup.resource_cleanup import cleanup_replicated_resources

        r = _lambda("r1")
        r.status = ReplicationStatus.IN_PROGRESS
        r.replicated_arn = "arn:aws:lambda:us-west-2:111122223333:function:fn-r1"

        called = {}

        def fake_delete(FunctionName):  # noqa: N803
            called["arn"] = FunctionName
            return {}

        import cleanup.resource_cleanup as cr

        orig = cr.create_target_client
        cr.create_target_client = lambda svc, region: MagicMock(
            delete_function=fake_delete
        )
        try:
            res = cleanup_replicated_resources({"r1": r}, "us-west-2")
        finally:
            cr.create_target_client = orig
        assert res.deleted_count == 1
        assert called["arn"].endswith("fn-r1")

    def test_iam_role_never_deleted(self):
        from models.resources import IAMRoleResource
        from cleanup.resource_cleanup import cleanup_replicated_resources

        role = IAMRoleResource(
            id="role1",
            name="my-role",
            arn="arn:aws:iam::111122223333:role/my-role",
            assume_role_policy={},
        )
        role.status = ReplicationStatus.REPLICATED
        role.replicated_arn = role.arn
        res = cleanup_replicated_resources({"role1": role}, "us-west-2")
        assert res.deleted_count == 0

    def test_not_replicated_without_arn_is_skipped(self):
        from cleanup.resource_cleanup import cleanup_replicated_resources

        r = _lambda("r1")
        r.status = ReplicationStatus.NOT_REPLICATED
        r.replicated_arn = None
        res = cleanup_replicated_resources({"r1": r}, "us-west-2")
        assert res.deleted_count == 0


# ---------------------------------------------------------------------------
# Pagination — cleanup must find bots beyond the first page
# ---------------------------------------------------------------------------

class TestLexDisassociatePagination:
    def test_bot_on_second_page_is_found(self):
        from cleanup.resource_cleanup import _disassociate_lex_bot

        connect = MagicMock()
        connect.list_bots.side_effect = [
            {"LexBots": [{"LexV2Bot": {"AliasArn": "arn:aws:lex:us-west-2:1:bot-alias/OTHER/AL"}}],
             "NextToken": "t1"},
            {"LexBots": [{"LexV2Bot": {"AliasArn": "arn:aws:lex:us-west-2:1:bot-alias/TARGETBOT/AL"}}]},
        ]
        _disassociate_lex_bot(
            connect, "inst", "arn:aws:lex:us-west-2:1:bot/TARGETBOT", "us-west-2",
        )
        assert connect.list_bots.call_count == 2
        assert connect.disassociate_bot.called

    def test_absent_bot_is_not_an_error(self):
        from cleanup.resource_cleanup import _disassociate_lex_bot

        connect = MagicMock()
        connect.list_bots.return_value = {"LexBots": []}
        _disassociate_lex_bot(connect, "inst", "arn:aws:lex:us-west-2:1:bot/NOPE", "us-west-2")
        assert not connect.disassociate_bot.called


# ---------------------------------------------------------------------------
# Naming edge cases
# ---------------------------------------------------------------------------

class TestNamingEdges:
    def test_s3_name_stays_within_63_char_limit(self, monkeypatch):
        from aws.naming import target_replica_name

        monkeypatch.delenv("ACGR_REPLICA_SUFFIX", raising=False)
        # 60-char source name + "-dr" = 63, the S3 maximum.
        name = "a" * 60
        assert len(target_replica_name(name)) == 63

    def test_suffix_injection_is_rejected(self, monkeypatch):
        from aws.naming import replica_suffix

        for bad in ["../evil", "UPPER", "has space", "sl/ash", "a" * 40]:
            monkeypatch.setenv("ACGR_REPLICA_SUFFIX", bad)
            assert replica_suffix() == "-dr", f"unsafe suffix accepted: {bad}"

    def test_s3_resource_uses_configured_suffix(self, monkeypatch):
        from aws.naming import target_replica_name

        monkeypatch.setenv("ACGR_REPLICA_SUFFIX", "-failover")
        assert target_replica_name("my-bucket") == "my-bucket-failover"


# ---------------------------------------------------------------------------
# Status live-check resilience
# ---------------------------------------------------------------------------

class TestLiveCheckResilience:
    def test_missing_target_resource_returns_none(self):
        from api.routes import _live_check_target_resource
        import api.routes as routes

        s3 = S3BucketResource(
            id="s1", name="bkt",
            arn="arn:aws:s3:::bkt",
        )
        orig = routes.create_target_client
        routes.create_target_client = lambda svc, region: MagicMock(
            head_bucket=MagicMock(side_effect=RuntimeError("404"))
        )
        try:
            assert _live_check_target_resource(s3, "us-west-2") is None
        finally:
            routes.create_target_client = orig

    def test_client_creation_failure_is_swallowed(self):
        from api.routes import _live_check_target_resource
        import api.routes as routes

        orig = routes.create_target_client
        routes.create_target_client = lambda svc, region: (_ for _ in ()).throw(
            RuntimeError("no creds")
        )
        try:
            assert _live_check_target_resource(_lambda("r1"), "us-west-2") is None
        finally:
            routes.create_target_client = orig
