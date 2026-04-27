"""Unit tests for the replication orchestrator and replication API routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from models.enums import ReplicationStatus, ResourceType
from models.resources import (
    IAMRoleResource,
    KinesisStreamResource,
    KinesisVideoResource,
    LambdaResource,
    LexBotResource,
    ResourceBase,
)
from models.session import ReplicationJob, ReplicationProgress, Session
from replication.orchestrator import (
    _compute_progress,
    _replicate_single_resource,
    retry_resource,
    run_replication,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(resources: dict[str, ResourceBase] | None = None) -> Session:
    """Create a minimal session for testing."""
    return Session(
        session_id=str(uuid.uuid4()),
        instance_arn="arn:aws:connect:us-west-2:123456789012:instance/test",
        instance_name="TestInstance",
        source_region="us-west-2",
        target_region="us-east-1",
        inventory=resources or {},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_iam_role(rid: str = "role-1", name: str = "test-role") -> IAMRoleResource:
    return IAMRoleResource(
        id=rid,
        name=name,
        arn=f"arn:aws:iam::123456789012:role/{name}",
        assume_role_policy={"Version": "2012-10-17"},
        inline_policies=[],
        attached_policies=[],
    )


def _make_lambda(
    rid: str = "lambda-1",
    name: str = "test-func",
    deps: list[str] | None = None,
    role_arn: str = "arn:aws:iam::123456789012:role/test-role",
) -> LambdaResource:
    return LambdaResource(
        id=rid,
        name=name,
        arn=f"arn:aws:lambda:us-west-2:123456789012:function:{name}",
        runtime="python3.12",
        handler="index.handler",
        memory_size=128,
        timeout=30,
        execution_role_arn=role_arn,
        dependencies=deps or [],
    )


def _make_lex(
    rid: str = "lex-1",
    name: str = "test-bot",
    deps: list[str] | None = None,
) -> LexBotResource:
    return LexBotResource(
        id=rid,
        name=name,
        arn=f"arn:aws:lex:us-west-2:123456789012:bot/{name}",
        bot_id="bot-123",
        dependencies=deps or [],
    )


def _make_kinesis_stream(
    rid: str = "ks-1",
    name: str = "test-stream",
    deps: list[str] | None = None,
) -> KinesisStreamResource:
    return KinesisStreamResource(
        id=rid,
        name=name,
        arn=f"arn:aws:kinesis:us-west-2:123456789012:stream/{name}",
        shard_count=1,
        retention_period=24,
        stream_mode="PROVISIONED",
        dependencies=deps or [],
    )


def _make_kvs(
    rid: str = "kvs-1",
    name: str = "test-kvs",
    deps: list[str] | None = None,
) -> KinesisVideoResource:
    return KinesisVideoResource(
        id=rid,
        name=name,
        arn=f"arn:aws:kinesisvideo:us-west-2:123456789012:stream/{name}",
        data_retention_in_hours=24,
        dependencies=deps or [],
    )


# ---------------------------------------------------------------------------
# Tests: _compute_progress
# ---------------------------------------------------------------------------

class TestComputeProgress:
    def test_all_replicated(self):
        r1 = _make_iam_role("r1")
        r1.status = ReplicationStatus.REPLICATED
        r2 = _make_lambda("r2")
        r2.status = ReplicationStatus.REPLICATED
        progress = _compute_progress({"r1": r1, "r2": r2})
        assert progress.total == 2
        assert progress.completed == 2
        assert progress.failed == 0
        assert progress.blocked == 0

    def test_mixed_statuses(self):
        r1 = _make_iam_role("r1")
        r1.status = ReplicationStatus.REPLICATED
        r2 = _make_lambda("r2")
        r2.status = ReplicationStatus.FAILED
        r3 = _make_lex("r3")
        r3.status = ReplicationStatus.BLOCKED
        progress = _compute_progress({"r1": r1, "r2": r2, "r3": r3})
        assert progress.total == 3
        assert progress.completed == 1
        assert progress.failed == 1
        assert progress.blocked == 1

    def test_empty(self):
        progress = _compute_progress({})
        assert progress.total == 0
        assert progress.completed == 0


# ---------------------------------------------------------------------------
# Tests: run_replication
# ---------------------------------------------------------------------------

class TestRunReplication:
    """Tests for the run_replication orchestrator function."""

    @patch("replication.orchestrator.replicate_iam_role")
    def test_single_iam_role_success(self, mock_replicate_iam):
        """IAM roles are global — they should be auto-marked REPLICATED without calling replicate_iam_role."""
        role = _make_iam_role("role-1")
        session = _make_session({"role-1": role})

        job = run_replication(session, ["role-1"])

        assert job.status == "COMPLETED"
        assert job.progress.total == 1
        assert job.progress.completed == 1
        assert job.progress.failed == 0
        assert session.inventory["role-1"].status == ReplicationStatus.REPLICATED
        # IAM is global — replicated_arn should be the same as the original ARN
        assert session.inventory["role-1"].replicated_arn == role.arn
        # replicate_iam_role should NOT be called since IAM roles are auto-skipped
        mock_replicate_iam.assert_not_called()

    @patch("replication.orchestrator.replicate_lambda_function")
    @patch("replication.orchestrator.replicate_iam_role")
    def test_dependency_order_iam_before_lambda(self, mock_iam, mock_lambda):
        """IAM roles are auto-skipped (global), Lambda should still get the role ARN mapping."""
        mock_lambda.return_value = "arn:aws:lambda:us-east-1:123456789012:function:test-func"

        role = _make_iam_role("role-1")
        func = _make_lambda("lambda-1", deps=["role-1"])
        session = _make_session({"role-1": role, "lambda-1": func})

        job = run_replication(session, ["role-1", "lambda-1"])

        assert job.status == "COMPLETED"
        assert job.progress.completed == 2
        # IAM should NOT be called (auto-skipped as global)
        mock_iam.assert_not_called()
        mock_lambda.assert_called_once()
        # Lambda should receive the role ARN mapping (same ARN since IAM is global)
        call_kwargs = mock_lambda.call_args
        assert call_kwargs.kwargs.get("role_arn_mapping") == {
            role.arn: role.arn
        }

    @patch("replication.orchestrator.replicate_iam_role")
    def test_failure_marks_resource_failed(self, mock_iam):
        """IAM roles are auto-skipped (global), so test failure with a different resource type."""
        # IAM roles can't fail anymore since they're auto-skipped.
        # This test now verifies that IAM roles are auto-marked REPLICATED.
        role = _make_iam_role("role-1")
        session = _make_session({"role-1": role})

        job = run_replication(session, ["role-1"])

        assert job.status == "COMPLETED"
        assert job.progress.completed == 1
        assert session.inventory["role-1"].status == ReplicationStatus.REPLICATED
        mock_iam.assert_not_called()

    @patch("replication.orchestrator.replicate_lambda_function")
    def test_failure_cascades_to_blocked(self, mock_lambda):
        """If a dependency fails, dependents should be blocked."""
        mock_lambda.side_effect = RuntimeError("Lambda error")

        func = _make_lambda("lambda-1")
        bot = _make_lex("lex-1", deps=["lambda-1"])
        session = _make_session({"lambda-1": func, "lex-1": bot})

        job = run_replication(session, ["lambda-1", "lex-1"])

        assert job.status == "FAILED"
        assert job.progress.failed == 1
        assert job.progress.blocked == 1
        assert session.inventory["lambda-1"].status == ReplicationStatus.FAILED
        assert session.inventory["lex-1"].status == ReplicationStatus.BLOCKED


    @patch("replication.orchestrator.replicate_lex_bot")
    @patch("replication.orchestrator.replicate_lambda_function")
    @patch("replication.orchestrator.replicate_iam_role")
    def test_full_chain_iam_lambda_lex(self, mock_iam, mock_lambda, mock_lex):
        mock_lambda.return_value = "arn:aws:lambda:us-east-1:123456789012:function:f"
        mock_lex.return_value = "arn:aws:lex:us-east-1:123456789012:bot/b"

        role = _make_iam_role("role-1")
        func = _make_lambda("lambda-1", deps=["role-1"])
        bot = _make_lex("lex-1", deps=["lambda-1"])
        session = _make_session({
            "role-1": role,
            "lambda-1": func,
            "lex-1": bot,
        })

        job = run_replication(session, ["role-1", "lambda-1", "lex-1"])

        assert job.status == "COMPLETED"
        assert job.progress.completed == 3
        # IAM is auto-skipped (global)
        mock_iam.assert_not_called()
        # Lex should receive the lambda ARN mapping
        lex_call_kwargs = mock_lex.call_args
        assert func.arn in lex_call_kwargs.kwargs.get("lambda_arn_mapping", {})

    @patch("replication.orchestrator.replicate_lex_bot")
    @patch("replication.orchestrator.replicate_lambda_function")
    def test_transitive_blocking(self, mock_lambda, mock_lex):
        """If Lambda fails, Lex (which depends on Lambda) is also blocked."""
        mock_lambda.side_effect = RuntimeError("Lambda error")

        func = _make_lambda("lambda-1")
        bot = _make_lex("lex-1", deps=["lambda-1"])
        session = _make_session({
            "lambda-1": func,
            "lex-1": bot,
        })

        job = run_replication(session, ["lambda-1", "lex-1"])

        assert job.progress.failed == 1
        assert job.progress.blocked == 1
        assert session.inventory["lambda-1"].status == ReplicationStatus.FAILED
        assert session.inventory["lex-1"].status == ReplicationStatus.BLOCKED

    @patch("replication.orchestrator.replicate_kinesis_stream")
    @patch("replication.orchestrator.replicate_kvs_stream")
    def test_independent_resources_all_succeed(self, mock_kvs, mock_kinesis):
        mock_kinesis.return_value = "arn:aws:kinesis:us-east-1:123456789012:stream/s"
        mock_kvs.return_value = "arn:aws:kinesisvideo:us-east-1:123456789012:stream/v"

        ks = _make_kinesis_stream("ks-1")
        kvs = _make_kvs("kvs-1")
        session = _make_session({"ks-1": ks, "kvs-1": kvs})

        job = run_replication(session, ["ks-1", "kvs-1"])

        assert job.status == "COMPLETED"
        assert job.progress.completed == 2

    @patch("replication.orchestrator.replicate_kinesis_stream")
    @patch("replication.orchestrator.replicate_lambda_function")
    def test_one_failure_doesnt_block_independent(self, mock_lambda, mock_kinesis):
        """If Lambda fails, an independent Kinesis stream should still succeed."""
        mock_lambda.side_effect = RuntimeError("Lambda error")
        mock_kinesis.return_value = "arn:aws:kinesis:us-east-1:123456789012:stream/s"

        func = _make_lambda("lambda-1")
        ks = _make_kinesis_stream("ks-1")  # no dependency on lambda
        session = _make_session({"lambda-1": func, "ks-1": ks})

        job = run_replication(session, ["lambda-1", "ks-1"])

        assert job.progress.failed == 1
        assert job.progress.completed == 1
        assert session.inventory["lambda-1"].status == ReplicationStatus.FAILED
        assert session.inventory["ks-1"].status == ReplicationStatus.REPLICATED

    def test_empty_resource_ids(self):
        session = _make_session({})
        job = run_replication(session, [])
        assert job.status == "COMPLETED"
        assert job.progress.total == 0

    def test_nonexistent_resource_ids_ignored(self):
        session = _make_session({})
        job = run_replication(session, ["nonexistent-id"])
        assert job.status == "COMPLETED"
        assert job.progress.total == 0

    @patch("replication.orchestrator.replicate_iam_role")
    def test_job_appended_to_session(self, mock_iam):
        mock_iam.return_value = "arn:aws:iam::123456789012:role/r"
        role = _make_iam_role("role-1")
        session = _make_session({"role-1": role})

        job = run_replication(session, ["role-1"])

        assert len(session.replication_jobs) == 1
        assert session.replication_jobs[0].job_id == job.job_id

    @patch("replication.orchestrator.replicate_iam_role")
    def test_job_has_execution_order(self, mock_iam):
        mock_iam.return_value = "arn:aws:iam::123456789012:role/r"
        role = _make_iam_role("role-1")
        session = _make_session({"role-1": role})

        job = run_replication(session, ["role-1"])

        assert "role-1" in job.execution_order


# ---------------------------------------------------------------------------
# Tests: retry_resource
# ---------------------------------------------------------------------------

class TestRetryResource:
    """Tests for the retry_resource function."""

    @patch("replication.orchestrator.replicate_iam_role")
    def test_retry_failed_resource_succeeds(self, mock_iam):
        """IAM roles auto-succeed on retry since they're global."""
        role = _make_iam_role("role-1")
        role.status = ReplicationStatus.FAILED
        role.error = "Previous error"
        session = _make_session({"role-1": role})

        job = ReplicationJob(
            job_id="job-1",
            status="FAILED",
            selected_resource_ids=["role-1"],
            execution_order=["role-1"],
            progress=ReplicationProgress(total=1, completed=0, failed=1, blocked=0),
            started_at=datetime.now(timezone.utc),
        )
        session.replication_jobs.append(job)

        result = retry_resource(session, "job-1", "role-1")

        assert result.status == ReplicationStatus.REPLICATED
        # IAM is global — replicated_arn is the same as original
        assert result.replicated_arn == role.arn
        assert result.error is None
        # replicate_iam_role should NOT be called
        mock_iam.assert_not_called()

    @patch("replication.orchestrator.replicate_lambda_function")
    def test_retry_failed_resource_fails_again(self, mock_lambda):
        mock_lambda.side_effect = RuntimeError("Still failing")
        func = _make_lambda("lambda-1")
        func.status = ReplicationStatus.FAILED
        session = _make_session({"lambda-1": func})

        job = ReplicationJob(
            job_id="job-1",
            status="FAILED",
            selected_resource_ids=["lambda-1"],
            execution_order=["lambda-1"],
            progress=ReplicationProgress(total=1, completed=0, failed=1, blocked=0),
            started_at=datetime.now(timezone.utc),
        )
        session.replication_jobs.append(job)

        result = retry_resource(session, "job-1", "lambda-1")

        assert result.status == ReplicationStatus.FAILED
        assert "Still failing" in result.error

    def test_retry_nonexistent_job_raises(self):
        session = _make_session({})
        with pytest.raises(ValueError, match="job not found"):
            retry_resource(session, "nonexistent-job", "role-1")

    def test_retry_nonexistent_resource_raises(self):
        session = _make_session({})
        job = ReplicationJob(
            job_id="job-1",
            status="FAILED",
            selected_resource_ids=[],
            execution_order=[],
            progress=ReplicationProgress(total=0, completed=0, failed=0, blocked=0),
            started_at=datetime.now(timezone.utc),
        )
        session.replication_jobs.append(job)

        with pytest.raises(ValueError, match="Resource not found"):
            retry_resource(session, "job-1", "nonexistent")

    def test_retry_replicated_resource_raises(self):
        role = _make_iam_role("role-1")
        role.status = ReplicationStatus.REPLICATED
        session = _make_session({"role-1": role})

        job = ReplicationJob(
            job_id="job-1",
            status="COMPLETED",
            selected_resource_ids=["role-1"],
            execution_order=["role-1"],
            progress=ReplicationProgress(total=1, completed=1, failed=0, blocked=0),
            started_at=datetime.now(timezone.utc),
        )
        session.replication_jobs.append(job)

        with pytest.raises(ValueError, match="not in FAILED or BLOCKED"):
            retry_resource(session, "job-1", "role-1")

    @patch("replication.orchestrator.replicate_lambda_function")
    @patch("replication.orchestrator.replicate_iam_role")
    def test_retry_unblocks_dependent(self, mock_iam, mock_lambda):
        """When a failed IAM role is retried, it auto-succeeds (global) and unblocks Lambda."""
        mock_lambda.return_value = "arn:aws:lambda:us-east-1:123456789012:function:f"

        role = _make_iam_role("role-1")
        role.status = ReplicationStatus.FAILED
        func = _make_lambda("lambda-1", deps=["role-1"])
        func.status = ReplicationStatus.BLOCKED
        session = _make_session({"role-1": role, "lambda-1": func})

        job = ReplicationJob(
            job_id="job-1",
            status="FAILED",
            selected_resource_ids=["role-1", "lambda-1"],
            execution_order=["role-1", "lambda-1"],
            progress=ReplicationProgress(total=2, completed=0, failed=1, blocked=1),
            started_at=datetime.now(timezone.utc),
        )
        session.replication_jobs.append(job)

        retry_resource(session, "job-1", "role-1")

        assert session.inventory["role-1"].status == ReplicationStatus.REPLICATED
        assert session.inventory["lambda-1"].status == ReplicationStatus.REPLICATED
        assert job.progress.completed == 2
        assert job.progress.failed == 0
        assert job.progress.blocked == 0
        # IAM should NOT be called (auto-skipped)
        mock_iam.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: API routes
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from main import app

api_client = TestClient(app)

VALID_CONNECT_ARN = "arn:aws:connect:us-west-2:123456789012:instance/abc-def-123"


class TestReplicateRoute:
    """Tests for POST /api/replicate."""

    @patch("api.routes.run_discovery")
    def test_replicate_success(self, mock_discover):
        """End-to-end: discover → replicate → poll status until complete (IAM auto-skipped)."""
        import time
        from models.inventory import ResourceInventory

        role = _make_iam_role("role-1")
        inv = ResourceInventory()
        inv.add_resource(role)
        mock_discover.return_value = ("sess-repl", inv)

        # Step 1: Discover to create session
        resp = api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})
        assert resp.status_code == 200
        session_id = resp.json()["sessionId"]

        # Step 2: Replicate (returns immediately with IN_PROGRESS)
        resp = api_client.post("/api/replicate", json={
            "sessionId": session_id,
            "resourceIds": ["role-1"],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "IN_PROGRESS"
        job_id = body["jobId"]

        # Step 3: Poll status until complete (background thread)
        for _ in range(20):
            time.sleep(0.1)
            status_resp = api_client.get(f"/api/replicate/{job_id}/status")
            if status_resp.status_code == 200 and status_resp.json()["status"] != "IN_PROGRESS":
                break
        status_body = status_resp.json()
        assert status_body["status"] == "COMPLETED"
        assert status_body["progress"]["completed"] == 1
        assert status_body["progress"]["failed"] == 0

    def test_replicate_missing_session(self):
        resp = api_client.post("/api/replicate", json={
            "sessionId": "nonexistent",
            "resourceIds": ["r1"],
        })
        assert resp.status_code == 404

    @patch("api.routes.run_discovery")
    def test_replicate_empty_resource_ids(self, mock_discover):
        from models.inventory import ResourceInventory

        inv = ResourceInventory()
        mock_discover.return_value = ("sess-empty", inv)
        api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        resp = api_client.post("/api/replicate", json={
            "sessionId": "sess-empty",
            "resourceIds": [],
        })
        assert resp.status_code == 422

    @patch("api.routes.run_discovery")
    def test_replicate_invalid_resource_ids(self, mock_discover):
        from models.inventory import ResourceInventory

        role = _make_iam_role("role-1")
        inv = ResourceInventory()
        inv.add_resource(role)
        mock_discover.return_value = ("sess-inv", inv)
        api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        resp = api_client.post("/api/replicate", json={
            "sessionId": "sess-inv",
            "resourceIds": ["nonexistent-id"],
        })
        assert resp.status_code == 400
        assert "not found in inventory" in resp.json()["detail"]


class TestReplicationStatusRoute:
    """Tests for GET /api/replicate/{job_id}/status."""

    @patch("api.routes.run_discovery")
    def test_get_status_after_replication(self, mock_discover):
        import time
        from models.inventory import ResourceInventory

        role = _make_iam_role("role-1")
        inv = ResourceInventory()
        inv.add_resource(role)
        mock_discover.return_value = ("sess-status", inv)

        api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})
        repl_resp = api_client.post("/api/replicate", json={
            "sessionId": "sess-status",
            "resourceIds": ["role-1"],
        })
        job_id = repl_resp.json()["jobId"]

        # Wait for background thread to complete
        for _ in range(20):
            time.sleep(0.1)
            resp = api_client.get(f"/api/replicate/{job_id}/status")
            if resp.status_code == 200 and resp.json()["status"] != "IN_PROGRESS":
                break

        assert resp.status_code == 200
        body = resp.json()
        assert body["jobId"] == job_id
        assert body["status"] == "COMPLETED"
        assert len(body["resources"]) == 1

    def test_get_status_nonexistent_job(self):
        resp = api_client.get("/api/replicate/nonexistent-job/status")
        assert resp.status_code == 404


class TestRetryRoute:
    """Tests for POST /api/replicate/{job_id}/retry/{resource_id}."""

    @patch("replication.orchestrator.replicate_lambda_function")
    @patch("api.routes.run_discovery")
    def test_retry_failed_resource(self, mock_discover, mock_lambda):
        import time
        from models.inventory import ResourceInventory

        func = _make_lambda("lambda-1")
        inv = ResourceInventory()
        inv.add_resource(func)
        mock_discover.return_value = ("sess-retry", inv)

        # First call fails, second succeeds
        mock_lambda.side_effect = [
            RuntimeError("First attempt failed"),
            "arn:aws:lambda:us-east-1:123456789012:function:retried",
        ]

        api_client.post("/api/discover", json={"instanceArn": VALID_CONNECT_ARN})

        # Replicate (will fail in background)
        repl_resp = api_client.post("/api/replicate", json={
            "sessionId": "sess-retry",
            "resourceIds": ["lambda-1"],
        })
        job_id = repl_resp.json()["jobId"]
        assert repl_resp.json()["status"] == "IN_PROGRESS"

        # Wait for background thread to complete
        for _ in range(20):
            time.sleep(0.1)
            status_resp = api_client.get(f"/api/replicate/{job_id}/status")
            if status_resp.status_code == 200 and status_resp.json()["status"] != "IN_PROGRESS":
                break
        assert status_resp.json()["status"] == "FAILED"

        # Retry
        resp = api_client.post(f"/api/replicate/{job_id}/retry/lambda-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "REPLICATED"
        assert body["replicatedArn"] == "arn:aws:lambda:us-east-1:123456789012:function:retried"

    def test_retry_nonexistent_job(self):
        resp = api_client.post("/api/replicate/nonexistent/retry/role-1")
        assert resp.status_code == 404
