"""ResourceInventory container for managing discovered AWS resources."""

from __future__ import annotations

from .enums import ReplicationStatus, ResourceType
from .resources import ResourceBase


class ResourceInventory:
    """Container for discovered AWS resources with filtering and search capabilities.

    Stores resources in a dict keyed by resource ID. Supports add/remove,
    lookup, filtering by type and status, search by name/ARN, and grouping
    by resource type.
    """

    def __init__(self) -> None:
        self._resources: dict[str, ResourceBase] = {}

    def add_resource(self, resource: ResourceBase) -> None:
        """Add a resource to the inventory."""
        self._resources[resource.id] = resource

    def remove_resource(self, resource_id: str) -> None:
        """Remove a resource by ID. Raises KeyError if not found."""
        del self._resources[resource_id]

    def get_resource(self, resource_id: str) -> ResourceBase | None:
        """Get a resource by ID, or None if not found."""
        return self._resources.get(resource_id)

    def get_all(self) -> list[ResourceBase]:
        """Return all resources in the inventory."""
        return list(self._resources.values())

    def filter_by(
        self,
        resource_type: ResourceType | None = None,
        status: ReplicationStatus | None = None,
    ) -> list[ResourceBase]:
        """Return resources matching ALL active filters.

        If a filter parameter is None, that filter is not applied.
        """
        results = self._resources.values()
        if resource_type is not None:
            results = [r for r in results if r.resource_type == resource_type]
        if status is not None:
            results = [r for r in results if r.status == status]
        return list(results)

    def search(self, query: str) -> list[ResourceBase]:
        """Return resources whose name or ARN contains the query (case-insensitive)."""
        q = query.lower()
        return [
            r
            for r in self._resources.values()
            if q in r.name.lower() or q in r.arn.lower()
        ]

    def get_by_type(self) -> dict[ResourceType, list[ResourceBase]]:
        """Return resources grouped by ResourceType."""
        grouped: dict[ResourceType, list[ResourceBase]] = {}
        for resource in self._resources.values():
            grouped.setdefault(resource.resource_type, []).append(resource)
        return grouped

    def __len__(self) -> int:
        return len(self._resources)

    def __contains__(self, resource_id: str) -> bool:
        return resource_id in self._resources
