"""Abstract base class for session store."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.session import Session


class SessionSummary:
    """Lightweight session summary for listing (avoids deserializing full inventory)."""

    def __init__(
        self,
        session_id: str,
        instance_arn: str,
        source_region: str,
        target_region: str,
        created_at: str,
        updated_at: str,
        resource_count: int = 0,
    ) -> None:
        self.session_id = session_id
        self.instance_arn = instance_arn
        self.source_region = source_region
        self.target_region = target_region
        self.created_at = created_at
        self.updated_at = updated_at
        self.resource_count = resource_count


class SessionStore(ABC):
    """Interface for persisting and retrieving session state."""

    @abstractmethod
    async def get_session(self, session_id: str) -> Session | None:
        """Retrieve a session by ID, or None if not found."""
        ...

    @abstractmethod
    async def save_session(self, session: Session) -> None:
        """Persist a session (create or update)."""
        ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """Delete a session by ID."""
        ...

    @abstractmethod
    async def list_recent_sessions(self, limit: int = 4) -> list[SessionSummary]:
        """Return the most recent sessions, sorted by created_at descending."""
        ...
