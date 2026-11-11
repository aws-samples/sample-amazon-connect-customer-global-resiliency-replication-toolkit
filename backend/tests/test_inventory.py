"""Unit tests for ResourceInventory container."""

import pytest

from models.enums import ReplicationStatus, ResourceType
from models.inventory import ResourceInventory
from models.resources import ResourceBase, LambdaResource


def _make_resource(
    id: str = "r1",
    name: str = "my-resource",
    arn: str = "arn:aws:lambda:us-west-2:123456789012:function:my-func",
    resource_type: ResourceType = ResourceType.LAMBDA,
    status: ReplicationStatus = ReplicationStatus.NOT_REPLICATED,
) -> ResourceBase:
    return ResourceBase(
        id=id, name=name, arn=arn, resource_type=resource_type, status=status
    )


class TestAddRemoveGet:
    def test_add_and_get(self):
        inv = ResourceInventory()
        r = _make_resource()
        inv.add_resource(r)
        assert inv.get_resource("r1") is r
        assert len(inv) == 1

    def test_get_missing_returns_none(self):
        inv = ResourceInventory()
        assert inv.get_resource("nonexistent") is None

    def test_remove_resource(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource())
        inv.remove_resource("r1")
        assert inv.get_resource("r1") is None
        assert len(inv) == 0

    def test_remove_missing_raises(self):
        inv = ResourceInventory()
        with pytest.raises(KeyError):
            inv.remove_resource("nonexistent")

    def test_contains(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a"))
        assert "a" in inv
        assert "b" not in inv

    def test_add_overwrites_same_id(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="r1", name="first"))
        inv.add_resource(_make_resource(id="r1", name="second"))
        assert inv.get_resource("r1").name == "second"
        assert len(inv) == 1


class TestGetAll:
    def test_empty(self):
        assert ResourceInventory().get_all() == []

    def test_returns_all(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a"))
        inv.add_resource(_make_resource(id="b"))
        assert len(inv.get_all()) == 2


class TestFilterBy:
    def test_no_filters_returns_all(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a"))
        inv.add_resource(_make_resource(id="b"))
        assert len(inv.filter_by()) == 2

    def test_filter_by_type(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a", resource_type=ResourceType.LAMBDA))
        inv.add_resource(_make_resource(id="b", resource_type=ResourceType.IAM_ROLE))
        result = inv.filter_by(resource_type=ResourceType.LAMBDA)
        assert len(result) == 1
        assert result[0].id == "a"

    def test_filter_by_status(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a", status=ReplicationStatus.REPLICATED))
        inv.add_resource(_make_resource(id="b", status=ReplicationStatus.FAILED))
        result = inv.filter_by(status=ReplicationStatus.FAILED)
        assert len(result) == 1
        assert result[0].id == "b"

    def test_filter_by_both(self):
        inv = ResourceInventory()
        inv.add_resource(
            _make_resource(
                id="a",
                resource_type=ResourceType.LAMBDA,
                status=ReplicationStatus.REPLICATED,
            )
        )
        inv.add_resource(
            _make_resource(
                id="b",
                resource_type=ResourceType.LAMBDA,
                status=ReplicationStatus.FAILED,
            )
        )
        inv.add_resource(
            _make_resource(
                id="c",
                resource_type=ResourceType.IAM_ROLE,
                status=ReplicationStatus.REPLICATED,
            )
        )
        result = inv.filter_by(
            resource_type=ResourceType.LAMBDA, status=ReplicationStatus.REPLICATED
        )
        assert len(result) == 1
        assert result[0].id == "a"


class TestSearch:
    def test_search_by_name(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a", name="my-lambda-func"))
        inv.add_resource(
            _make_resource(
                id="b", name="other-thing", arn="arn:aws:iam::123456789012:role/myrole"
            )
        )
        result = inv.search("lambda")
        assert len(result) == 1
        assert result[0].id == "a"

    def test_search_by_arn(self):
        inv = ResourceInventory()
        inv.add_resource(
            _make_resource(id="a", arn="arn:aws:dynamodb:us-west-2:123:table/Users")
        )
        inv.add_resource(
            _make_resource(id="b", arn="arn:aws:lambda:us-west-2:123:function:foo")
        )
        result = inv.search("dynamodb")
        assert len(result) == 1
        assert result[0].id == "a"

    def test_search_case_insensitive(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a", name="MyLambdaFunc"))
        result = inv.search("MYLAMBDA")
        assert len(result) == 1

    def test_search_empty_query_returns_all(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a"))
        inv.add_resource(_make_resource(id="b"))
        assert len(inv.search("")) == 2

    def test_search_no_match(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a", name="foo", arn="arn:aws:bar"))
        assert inv.search("zzz") == []


class TestGetByType:
    def test_groups_correctly(self):
        inv = ResourceInventory()
        inv.add_resource(_make_resource(id="a", resource_type=ResourceType.LAMBDA))
        inv.add_resource(_make_resource(id="b", resource_type=ResourceType.LAMBDA))
        inv.add_resource(_make_resource(id="c", resource_type=ResourceType.IAM_ROLE))
        grouped = inv.get_by_type()
        assert len(grouped[ResourceType.LAMBDA]) == 2
        assert len(grouped[ResourceType.IAM_ROLE]) == 1
        assert ResourceType.LEX_BOT not in grouped

    def test_empty_inventory(self):
        assert ResourceInventory().get_by_type() == {}
