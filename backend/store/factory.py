"""Factory for creating the appropriate session store based on deployment mode."""

from __future__ import annotations

import os

from .base import SessionStore
from .dynamodb_store import DynamoDBSessionStore
from .memory_store import InMemorySessionStore


def create_session_store() -> SessionStore:
    """Return a session store based on the DEPLOYMENT_MODE env var.

    - ``lambda`` → DynamoDB-backed store
    - ``local`` (default) → in-memory dict store
    """
    mode = os.environ.get("DEPLOYMENT_MODE", "local").lower()
    if mode == "lambda":
        table_name = os.environ.get("SESSION_TABLE_NAME", "ReplicatorSessions")
        return DynamoDBSessionStore(table_name=table_name)
    return InMemorySessionStore()
