"""Tests for the resource diff module."""

import pytest
from unittest.mock import patch, MagicMock

from diff.resource_diff import (
    compute_resource_diffs,
    _extract_source_config,
    _compute_differences,
    DiffEntry,
)
from models.enums import ResourceType
from models.resources import ResourceBase, LambdaResource


def _make_resource(name, rtype):
    return ResourceBase(
        id=f"id-{name}",
        name=name,
        arn=f"arn:aws:test:us-east-1:123456789012:{name}",
        resource_type=rtype,
    )


class TestComputeDifferences:
    """Tests for _compute_differences."""

    def test_identical_configs_no_diffs(self):
        source = {"Runtime": "python3.12", "MemorySize": 128}
        target = {"Runtime": "python3.12", "MemorySize": 128}
        diffs = _compute_differences(source, target)
        assert diffs == []

    def test_different_values_detected(self):
        source = {"Runtime": "python3.12", "MemorySize": 128}
        target = {"Runtime": "python3.12", "MemorySize": 256}
        diffs = _compute_differences(source, target)
        assert len(diffs) == 1
        assert "MemorySize" in diffs[0]

    def test_missing_key_in_target(self):
        source = {"Runtime": "python3.12", "Timeout": 30}
        target = {"Runtime": "python3.12"}
        diffs = _compute_differences(source, target)
        assert len(diffs) == 1
        assert "Timeout" in diffs[0]

    def test_extra_key_in_target(self):
        source = {"Runtime": "python3.12"}
        target = {"Runtime": "python3.12", "CodeSize": 1024}
        diffs = _compute_differences(source, target)
        assert len(diffs) == 1
        assert "CodeSize" in diffs[0]


class TestExtractSourceConfig:
    """Tests for _extract_source_config."""

    def test_lambda_config_extraction(self):
        r = LambdaResource(
            id="id-fn",
            name="my-fn",
            arn="arn:aws:lambda:us-east-1:123456789012:function:my-fn",
            runtime="python3.12",
            handler="index.handler",
            memory_size=256,
            timeout=30,
            execution_role_arn="arn:aws:iam::123456789012:role/test",
        )
        config = _extract_source_config(r)
        assert config["Runtime"] == "python3.12"
        assert config["MemorySize"] == 256
        assert config["Timeout"] == 30

    def test_base_resource_uses_config_summary(self):
        r = _make_resource("my-fn", ResourceType.LAMBDA)
        r.config_summary = {"runtime": "python3.12", "memory_size": "256"}
        config = _extract_source_config(r)
        assert config["Runtime"] == "python3.12"


class TestComputeResourceDiffs:
    """Tests for compute_resource_diffs."""

    @patch("diff.resource_diff._get_lambda_config")
    def test_resource_exists_in_target(self, mock_get_config):
        mock_get_config.return_value = {
            "Runtime": "python3.12",
            "Handler": "index.handler",
            "MemorySize": 128,
            "Timeout": 3,
            "CodeSize": 1024,
            "Layers": [],
        }

        r = _make_resource("my-fn", ResourceType.LAMBDA)
        inventory = {r.id: r}
        entries = compute_resource_diffs(inventory, "us-west-2", "")

        assert len(entries) == 1
        assert entries[0].existsInTarget is True

    @patch("diff.resource_diff._get_lambda_config")
    def test_resource_not_in_target(self, mock_get_config):
        mock_get_config.return_value = None

        r = _make_resource("my-fn", ResourceType.LAMBDA)
        inventory = {r.id: r}
        entries = compute_resource_diffs(inventory, "us-west-2", "")

        assert len(entries) == 1
        assert entries[0].existsInTarget is False
        assert entries[0].differences == []

    def test_resource_type_without_fetcher(self):
        """IAM roles are global — always exist in target (source role IS target role)."""
        r = _make_resource("my-role", ResourceType.IAM_ROLE)
        inventory = {r.id: r}
        entries = compute_resource_diffs(inventory, "us-west-2", "")

        assert len(entries) == 1
        assert entries[0].existsInTarget is True
