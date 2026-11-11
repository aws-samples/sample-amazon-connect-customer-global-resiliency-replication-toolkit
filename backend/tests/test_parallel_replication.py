"""Tests for parallel replication (concurrency > 1) in the orchestrator."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from replication.orchestrator import run_replication, _compute_dependency_levels
from models.enums import ReplicationStatus, ResourceType
from models.resources import ResourceBase
from models.session import Session


def _make_resource(name, rtype, deps=None):
    return ResourceBase(
        id=f"id-{name}",
        name=name,
        arn=f"arn:aws:test:us-east-1:123456789012:{name}",
        resource_type=rtype,
        dependencies=deps or [],
    )


def _make_session(resources):
    now = datetime.now(timezone.utc)
    inventory = {r.id: r for r in resources}
    return Session(
        session_id="test-session",
        instance_arn="arn:aws:connect:us-east-1:123456789012:instance/test-id",
        instance_name="test",
        source_region="us-east-1",
        target_region="us-west-2",
        inventory=inventory,
        created_at=now,
        updated_at=now,
    )


class TestComputeDependencyLevels:
    """Tests for _compute_dependency_levels."""

    def test_independent_resources_single_level(self):
        graph = {"a": [], "b": [], "c": []}
        order = ["a", "b", "c"]
        levels = _compute_dependency_levels(graph, order)
        assert len(levels) == 1
        assert set(levels[0]) == {"a", "b", "c"}

    def test_linear_dependency_chain(self):
        # a → b → c
        graph = {"a": ["b"], "b": ["c"], "c": []}
        order = ["a", "b", "c"]
        levels = _compute_dependency_levels(graph, order)
        assert len(levels) == 3
        assert levels[0] == ["a"]
        assert levels[1] == ["b"]
        assert levels[2] == ["c"]

    def test_diamond_dependency(self):
        # a → b, a → c, b → d, c → d
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        order = ["a", "b", "c", "d"]
        levels = _compute_dependency_levels(graph, order)
        assert len(levels) == 3
        assert levels[0] == ["a"]
        assert set(levels[1]) == {"b", "c"}
        assert levels[2] == ["d"]

    def test_empty_graph(self):
        levels = _compute_dependency_levels({}, [])
        assert levels == []


class TestParallelReplication:
    """Tests for run_replication with concurrency > 1."""

    @patch("replication.orchestrator._replicate_single_resource")
    @patch("replication.orchestrator.replicate_lambda_layers")
    def test_concurrent_replication_completes_all(self, mock_layers, mock_replicate):
        mock_layers.return_value = {}
        mock_replicate.return_value = "arn:aws:test:us-west-2:123456789012:replicated"

        r1 = _make_resource("fn1", ResourceType.LAMBDA)
        r2 = _make_resource("fn2", ResourceType.LAMBDA)
        r3 = _make_resource("fn3", ResourceType.LAMBDA)
        session = _make_session([r1, r2, r3])

        job = run_replication(
            session, [r1.id, r2.id, r3.id], concurrency=3
        )

        assert job.progress.completed == 3
        assert job.progress.failed == 0
        assert job.status == "COMPLETED"

    @patch("replication.orchestrator._replicate_single_resource")
    @patch("replication.orchestrator.replicate_lambda_layers")
    def test_sequential_replication_still_works(self, mock_layers, mock_replicate):
        mock_layers.return_value = {}
        mock_replicate.return_value = "arn:aws:test:us-west-2:123456789012:replicated"

        r1 = _make_resource("fn1", ResourceType.LAMBDA)
        r2 = _make_resource("fn2", ResourceType.LAMBDA)
        session = _make_session([r1, r2])

        job = run_replication(
            session, [r1.id, r2.id], concurrency=1
        )

        assert job.progress.completed == 2
        assert job.status == "COMPLETED"

    @patch("replication.orchestrator._replicate_single_resource")
    @patch("replication.orchestrator.replicate_lambda_layers")
    def test_parallel_failure_blocks_dependents(self, mock_layers, mock_replicate):
        mock_layers.return_value = {}

        ks = _make_resource("ks1", ResourceType.KINESIS_STREAM)
        fn = _make_resource("fn1", ResourceType.LAMBDA, deps=[ks.id])

        def side_effect(resource, *args, **kwargs):
            if resource.resource_type == ResourceType.KINESIS_STREAM:
                raise RuntimeError("Kinesis error")
            return "arn:aws:test:us-west-2:123456789012:replicated"

        mock_replicate.side_effect = side_effect
        session = _make_session([ks, fn])

        job = run_replication(
            session, [ks.id, fn.id], concurrency=3
        )

        assert job.progress.failed == 1
        assert job.progress.blocked == 1
        assert session.inventory[ks.id].status == ReplicationStatus.FAILED
        assert session.inventory[fn.id].status == ReplicationStatus.BLOCKED
