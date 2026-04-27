"""Unit tests for session models and session store abstraction."""

import asyncio
import os
from datetime import datetime, timezone

import pytest

from models.enums import ReplicationStatus, ResourceType
from models.resources import LambdaResource, ResourceBase
from models.session import ReplicationJob, ReplicationProgress, Session
from store.factory import create_session_store
from store.memory_store import InMemorySessionStore


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_session(session_id: str = "sess-1", **overrides) -> Session:
    defaults = dict(
        session_id=session_id,
        instance_arn="arn:aws:connect:us-west-2:123456789012:instance/abc",
        instance_name="TestInstance",
        source_region="us-west-2",
        target_region="us-east-1",
        inventory={},
        replication_jobs=[],
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Session(**defaults)


def _make_lambda_resource(rid: str = "r-1") -> LambdaResource:
    return LambdaResource(
        id=rid,
        name="my-func",
        arn=f"arn:aws:lambda:us-west-2:123456789012:function:{rid}",
        runtime="python3.12",
        handler="index.handler",
        memory_size=128,
        timeout=30,
        execution_role_arn="arn:aws:iam::123456789012:role/role1",
    )


# ---------------------------------------------------------------------------
# Session model tests
# ---------------------------------------------------------------------------

class TestSessionModel:
    def test_create_empty_session(self):
        s = _make_session()
        assert s.session_id == "sess-1"
        assert s.inventory == {}
        assert s.replication_jobs == []

    def test_session_with_inventory(self):
        res = _make_lambda_resource("r-1")
        s = _make_session(inventory={"r-1": res})
        assert "r-1" in s.inventory
        assert s.inventory["r-1"].name == "my-func"

    def test_session_with_replication_job(self):
        job = ReplicationJob(
            job_id="job-1",
            status="IN_PROGRESS",
            selected_resource_ids=["r-1"],
            execution_order=["r-1"],
            progress=ReplicationProgress(total=1, completed=0, failed=0, blocked=0),
            started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        s = _make_session(replication_jobs=[job])
        assert len(s.replication_jobs) == 1
        assert s.replication_jobs[0].job_id == "job-1"

    def test_session_serialization_roundtrip(self):
        res = _make_lambda_resource("r-1")
        job = ReplicationJob(
            job_id="job-1",
            status="COMPLETED",
            selected_resource_ids=["r-1"],
            execution_order=["r-1"],
            progress=ReplicationProgress(total=1, completed=1, failed=0, blocked=0),
            started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            completed_at=datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc),
        )
        s = _make_session(inventory={"r-1": res}, replication_jobs=[job])
        data = s.model_dump(mode="json")
        restored = Session.model_validate(data)
        assert restored.session_id == s.session_id
        assert restored.inventory["r-1"].name == "my-func"
        assert restored.replication_jobs[0].status == "COMPLETED"


# ---------------------------------------------------------------------------
# InMemorySessionStore tests
# ---------------------------------------------------------------------------

class TestInMemorySessionStore:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_save_and_get(self):
        store = InMemorySessionStore()
        s = _make_session("s1")
        self._run(store.save_session(s))
        got = self._run(store.get_session("s1"))
        assert got is not None
        assert got.session_id == "s1"

    def test_get_missing_returns_none(self):
        store = InMemorySessionStore()
        assert self._run(store.get_session("nope")) is None

    def test_delete(self):
        store = InMemorySessionStore()
        s = _make_session("s1")
        self._run(store.save_session(s))
        self._run(store.delete_session("s1"))
        assert self._run(store.get_session("s1")) is None

    def test_delete_missing_is_noop(self):
        store = InMemorySessionStore()
        # Should not raise
        self._run(store.delete_session("nope"))

    def test_overwrite_session(self):
        store = InMemorySessionStore()
        s1 = _make_session("s1", instance_name="First")
        self._run(store.save_session(s1))
        s2 = _make_session("s1", instance_name="Second")
        self._run(store.save_session(s2))
        got = self._run(store.get_session("s1"))
        assert got.instance_name == "Second"

    def test_roundtrip_with_resources(self):
        store = InMemorySessionStore()
        res = _make_lambda_resource("r-1")
        s = _make_session("s1", inventory={"r-1": res})
        self._run(store.save_session(s))
        got = self._run(store.get_session("s1"))
        assert "r-1" in got.inventory
        assert got.inventory["r-1"].runtime == "python3.12"


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------

class TestSessionStoreFactory:
    def test_local_mode_returns_memory_store(self):
        os.environ["DEPLOYMENT_MODE"] = "local"
        try:
            store = create_session_store()
            assert isinstance(store, InMemorySessionStore)
        finally:
            os.environ.pop("DEPLOYMENT_MODE", None)

    def test_default_mode_returns_memory_store(self):
        os.environ.pop("DEPLOYMENT_MODE", None)
        store = create_session_store()
        assert isinstance(store, InMemorySessionStore)

    def test_lambda_mode_returns_dynamodb_store(self):
        from store.dynamodb_store import DynamoDBSessionStore
        os.environ["DEPLOYMENT_MODE"] = "lambda"
        try:
            store = create_session_store()
            assert isinstance(store, DynamoDBSessionStore)
        finally:
            os.environ.pop("DEPLOYMENT_MODE", None)

    def test_case_insensitive_lambda(self):
        from store.dynamodb_store import DynamoDBSessionStore
        os.environ["DEPLOYMENT_MODE"] = "Lambda"
        try:
            store = create_session_store()
            assert isinstance(store, DynamoDBSessionStore)
        finally:
            os.environ.pop("DEPLOYMENT_MODE", None)
