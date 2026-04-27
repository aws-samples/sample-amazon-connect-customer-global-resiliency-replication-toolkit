"""In-memory session store for local development."""

from __future__ import annotations

from models.session import Session

from .base import SessionStore, SessionSummary


class InMemorySessionStore(SessionStore):
    """Dict-backed session store for local development and testing."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def save_session(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    async def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def list_recent_sessions(self, limit: int = 4) -> list[SessionSummary]:
        sessions = sorted(
            self._sessions.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )[:limit]
        return [
            SessionSummary(
                session_id=s.session_id,
                instance_arn=s.instance_arn,
                source_region=s.source_region,
                target_region=s.target_region,
                created_at=s.created_at.isoformat(),
                updated_at=s.updated_at.isoformat(),
                resource_count=len(s.inventory),
            )
            for s in sessions
        ]
