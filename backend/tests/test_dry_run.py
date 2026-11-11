"""Tests for the dry run simulation module."""

import pytest
from datetime import datetime, timezone

from replication.dry_run import simulate_replication, _simulate_target_arn
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


class TestSimulateTargetArn:
    """Tests for _simulate_target_arn."""

    def test_lambda_arn(self):
        r = _make_resource("my-fn", ResourceType.LAMBDA)
        arn = _simulate_target_arn(r, "us-west-2")
        assert "lambda" in arn
        assert "us-west-2" in arn
        assert "my-fn" in arn

    def test_iam_role_arn_no_region(self):
        r = _make_resource("my-role", ResourceType.IAM_ROLE)
        arn = _simulate_target_arn(r, "us-west-2", "")
        assert "iam" in arn
        assert "role/my-role" in arn

    def test_kinesis_stream_arn(self):
        r = _make_resource("my-stream", ResourceType.KINESIS_STREAM)
        arn = _simulate_target_arn(r, "us-west-2")
        assert "kinesis" in arn
        assert "us-west-2" in arn
        assert "my-stream" in arn

    def test_s3_bucket_arn(self):
        r = _make_resource("my-bucket", ResourceType.S3_BUCKET)
        arn = _simulate_target_arn(r, "us-west-2")
        assert "s3:::" in arn
        assert "my-bucket-dr" in arn


class TestSimulateReplication:
    """Tests for simulate_replication."""

    def test_empty_resource_ids(self):
        session = _make_session([])
        result = simulate_replication(session, [], "job-1")
        assert result == {}

    def test_single_resource_simulation(self):
        r = _make_resource("my-fn", ResourceType.LAMBDA)
        session = _make_session([r])
        result = simulate_replication(session, [r.id], "job-1")

        assert len(result) == 1
        resource = result[r.id]
        assert resource.status == ReplicationStatus.REPLICATED
        assert resource.replicated_arn.startswith("[DRY RUN]")
        assert resource.error is None

    def test_multiple_resources_all_simulated(self):
        r1 = _make_resource("fn1", ResourceType.LAMBDA)
        r2 = _make_resource("stream1", ResourceType.KINESIS_STREAM)
        session = _make_session([r1, r2])
        result = simulate_replication(session, [r1.id, r2.id], "job-1")

        assert len(result) == 2
        for rid, resource in result.items():
            assert resource.status == ReplicationStatus.REPLICATED
            assert "[DRY RUN]" in resource.replicated_arn

    def test_tags_dont_affect_simulated_arns(self):
        r = _make_resource("my-fn", ResourceType.LAMBDA)
        session = _make_session([r])
        result = simulate_replication(session, [r.id], "job-1", {"env": "dr"})

        assert "my-fn" in result[r.id].replicated_arn

    def test_dependency_order_respected(self):
        role = _make_resource("role1", ResourceType.IAM_ROLE)
        fn = _make_resource("fn1", ResourceType.LAMBDA, deps=[role.id])
        session = _make_session([role, fn])
        result = simulate_replication(session, [role.id, fn.id], "job-1")

        # Both should be simulated successfully
        assert result[role.id].status == ReplicationStatus.REPLICATED
        assert result[fn.id].status == ReplicationStatus.REPLICATED
