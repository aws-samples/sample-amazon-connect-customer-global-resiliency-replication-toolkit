"""Dependency graph construction and topological sort for replication ordering.

Builds a directed acyclic graph (DAG) from resource dependencies and performs
topological sort (Kahn's algorithm) to determine the correct replication order.

Dependency order:
  IAM roles → Lambda functions → Lex bots
  DynamoDB tables → Lambda (via ESM)
  Kinesis streams → Lambda (via ESM)
  KVS streams are independent
"""

from __future__ import annotations

from collections import deque

from models.resources import ResourceBase


def build_dependency_graph(resources: list[ResourceBase]) -> dict[str, list[str]]:
    """Build an adjacency list DAG from resource dependencies.

    Each key is a resource ID, and its value is a list of resource IDs that
    depend on it (i.e., the key must be replicated before its dependents).

    Args:
        resources: List of resources with their ``dependencies`` field populated.

    Returns:
        Adjacency list where ``graph[a]`` contains ``b`` means ``a`` must be
        replicated before ``b`` (``b`` depends on ``a``).
    """
    resource_ids = {r.id for r in resources}
    graph: dict[str, list[str]] = {r.id: [] for r in resources}

    for resource in resources:
        for dep_id in resource.dependencies:
            if dep_id in resource_ids:
                graph.setdefault(dep_id, []).append(resource.id)

    return graph


def topological_sort(graph: dict[str, list[str]]) -> list[str]:
    """Topological sort using Kahn's algorithm.

    Returns resource IDs in replication order — dependencies come before
    the resources that depend on them.

    Args:
        graph: Adjacency list where ``graph[a]`` contains ``b`` means ``a``
               must come before ``b``.

    Returns:
        List of resource IDs in valid replication order.

    Raises:
        ValueError: If the graph contains a cycle.
    """
    in_degree: dict[str, int] = {node: 0 for node in graph}
    for node in graph:
        for neighbor in graph[node]:
            in_degree.setdefault(neighbor, 0)
            in_degree[neighbor] += 1

    queue: deque[str] = deque()
    for node, degree in in_degree.items():
        if degree == 0:
            queue.append(node)

    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for neighbor in graph.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(in_degree):
        raise ValueError(
            "Dependency graph contains a cycle — cannot determine replication order"
        )

    return result


def get_dependents(graph: dict[str, list[str]], resource_id: str) -> set[str]:
    """Get all transitive dependents of a resource.

    Performs a BFS from ``resource_id`` through the adjacency list to find
    every resource that directly or transitively depends on it. Useful for
    cascade-blocking when a dependency fails replication.

    Args:
        graph: Adjacency list (same format as ``build_dependency_graph`` output).
        resource_id: The resource whose dependents to find.

    Returns:
        Set of resource IDs that transitively depend on ``resource_id``.
        Does not include ``resource_id`` itself.
    """
    visited: set[str] = set()
    queue: deque[str] = deque()

    for neighbor in graph.get(resource_id, []):
        if neighbor not in visited:
            visited.add(neighbor)
            queue.append(neighbor)

    while queue:
        current = queue.popleft()
        for neighbor in graph.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return visited
