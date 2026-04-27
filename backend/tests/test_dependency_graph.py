"""Unit tests for dependency graph construction and topological sort."""

import pytest

from models.enums import ReplicationStatus, ResourceType
from models.resources import (
    ResourceBase,
    LambdaResource,
    LexBotResource,
    KinesisStreamResource,
    KinesisVideoResource,
    IAMRoleResource,
)
from replication.dependency_graph import (
    build_dependency_graph,
    topological_sort,
    get_dependents,
)


def _base(id: str, deps: list[str] | None = None) -> ResourceBase:
    return ResourceBase(
        id=id,
        name=id,
        arn=f"arn:aws:test:us-west-2:123456789012:{id}",
        resource_type=ResourceType.LAMBDA,
        dependencies=deps or [],
    )


# ---------------------------------------------------------------------------
# build_dependency_graph
# ---------------------------------------------------------------------------


class TestBuildDependencyGraph:
    def test_empty_resources(self):
        assert build_dependency_graph([]) == {}

    def test_single_resource_no_deps(self):
        graph = build_dependency_graph([_base("a")])
        assert graph == {"a": []}

    def test_simple_dependency(self):
        """b depends on a → graph[a] should contain b."""
        resources = [_base("a"), _base("b", deps=["a"])]
        graph = build_dependency_graph(resources)
        assert "b" in graph["a"]
        assert graph["b"] == []

    def test_ignores_deps_outside_resource_set(self):
        """Dependencies referencing IDs not in the resource list are ignored."""
        resources = [_base("a", deps=["external"])]
        graph = build_dependency_graph(resources)
        assert graph == {"a": []}
        assert "external" not in graph

    def test_multiple_dependents(self):
        """a has two dependents: b and c."""
        resources = [_base("a"), _base("b", deps=["a"]), _base("c", deps=["a"])]
        graph = build_dependency_graph(resources)
        assert set(graph["a"]) == {"b", "c"}

    def test_chain_dependency(self):
        """a → b → c chain."""
        resources = [_base("a"), _base("b", deps=["a"]), _base("c", deps=["b"])]
        graph = build_dependency_graph(resources)
        assert "b" in graph["a"]
        assert "c" in graph["b"]
        assert graph["c"] == []


# ---------------------------------------------------------------------------
# topological_sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_empty_graph(self):
        assert topological_sort({}) == []

    def test_single_node(self):
        assert topological_sort({"a": []}) == ["a"]

    def test_simple_chain(self):
        """a → b → c should produce [a, b, c]."""
        order = topological_sort({"a": ["b"], "b": ["c"], "c": []})
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond_dependency(self):
        """a → b, a → c, b → d, c → d."""
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        order = topological_sort(graph)
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_independent_nodes(self):
        """Disconnected nodes should all appear in the result."""
        order = topological_sort({"a": [], "b": [], "c": []})
        assert set(order) == {"a", "b", "c"}

    def test_cycle_raises_error(self):
        with pytest.raises(ValueError, match="cycle"):
            topological_sort({"a": ["b"], "b": ["a"]})

    def test_self_cycle_raises_error(self):
        with pytest.raises(ValueError, match="cycle"):
            topological_sort({"a": ["a"]})

    def test_realistic_dependency_order(self):
        """IAM role → Lambda → Lex bot, Kinesis → Lambda (via ESM)."""
        iam_role = IAMRoleResource(
            id="iam-1",
            name="my-role",
            arn="arn:aws:iam::123456789012:role/my-role",
            assume_role_policy={"Version": "2012-10-17"},
        )
        kinesis = KinesisStreamResource(
            id="kinesis-1",
            name="my-stream",
            arn="arn:aws:kinesis:us-west-2:123456789012:stream/my-stream",
            shard_count=1,
            retention_period=24,
            stream_mode="ON_DEMAND",
        )
        kinesis2 = KinesisStreamResource(
            id="kinesis-2",
            name="my-stream-2",
            arn="arn:aws:kinesis:us-west-2:123456789012:stream/my-stream-2",
            shard_count=1,
            retention_period=24,
            stream_mode="ON_DEMAND",
        )
        lambda_fn = LambdaResource(
            id="lambda-1",
            name="my-func",
            arn="arn:aws:lambda:us-west-2:123456789012:function:my-func",
            runtime="python3.12",
            handler="index.handler",
            memory_size=128,
            timeout=30,
            execution_role_arn=iam_role.arn,
            dependencies=["iam-1", "kinesis-1", "kinesis-2"],
        )
        lex_bot = LexBotResource(
            id="lex-1",
            name="my-bot",
            arn="arn:aws:lex:us-west-2:123456789012:bot/my-bot",
            bot_id="BOTID",
            dependencies=["lambda-1"],
        )
        kvs = KinesisVideoResource(
            id="kvs-1",
            name="my-kvs",
            arn="arn:aws:kinesisvideo:us-west-2:123456789012:stream/my-kvs",
            data_retention_in_hours=24,
        )

        resources = [iam_role, kinesis, kinesis2, lambda_fn, lex_bot, kvs]
        graph = build_dependency_graph(resources)
        order = topological_sort(graph)

        # IAM role before Lambda
        assert order.index("iam-1") < order.index("lambda-1")
        # Kinesis before Lambda (ESM)
        assert order.index("kinesis-1") < order.index("lambda-1")
        # Kinesis-2 before Lambda (ESM)
        assert order.index("kinesis-2") < order.index("lambda-1")
        # Lambda before Lex bot
        assert order.index("lambda-1") < order.index("lex-1")
        # KVS is independent — just needs to be present
        assert "kvs-1" in order


# ---------------------------------------------------------------------------
# get_dependents
# ---------------------------------------------------------------------------


class TestGetDependents:
    def test_no_dependents(self):
        graph = {"a": [], "b": []}
        assert get_dependents(graph, "a") == set()

    def test_direct_dependents(self):
        graph = {"a": ["b", "c"], "b": [], "c": []}
        assert get_dependents(graph, "a") == {"b", "c"}

    def test_transitive_dependents(self):
        """a → b → c: dependents of a should include both b and c."""
        graph = {"a": ["b"], "b": ["c"], "c": []}
        assert get_dependents(graph, "a") == {"b", "c"}

    def test_does_not_include_self(self):
        graph = {"a": ["b"], "b": []}
        assert "a" not in get_dependents(graph, "a")

    def test_diamond_transitive(self):
        """a → b, a → c, b → d, c → d."""
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        assert get_dependents(graph, "a") == {"b", "c", "d"}

    def test_unknown_resource_id(self):
        graph = {"a": ["b"], "b": []}
        assert get_dependents(graph, "nonexistent") == set()

    def test_cascade_blocking_scenario(self):
        """If IAM role fails, Lambda and Lex bot should be blocked."""
        graph = {
            "iam-1": ["lambda-1"],
            "lambda-1": ["lex-1"],
            "lex-1": [],
            "ddb-1": ["lambda-1"],
            "kvs-1": [],
        }
        blocked = get_dependents(graph, "iam-1")
        assert blocked == {"lambda-1", "lex-1"}

    def test_partial_cascade(self):
        """If DynamoDB fails, only Lambda and its dependents are blocked, not IAM."""
        graph = {
            "iam-1": ["lambda-1"],
            "ddb-1": ["lambda-1"],
            "lambda-1": ["lex-1"],
            "lex-1": [],
        }
        blocked = get_dependents(graph, "ddb-1")
        assert blocked == {"lambda-1", "lex-1"}
        # IAM role is NOT blocked by DynamoDB failure
        assert "iam-1" not in blocked
