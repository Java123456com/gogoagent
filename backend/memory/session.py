"""Agent session fields persisted in Redis-compatible records."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from backend.infrastructure.repositories import agent_memory_repository
from backend.memory.store import PersistentSessionStore


class AgentSessionStore:
    """Durable active-agent/pending-tool state with a local cache fast path."""

    ACTIVE_AGENT = "active_agent"
    PENDING_TOOL = "pending_tool"

    def __init__(self, state_store: PersistentSessionStore | None = None) -> None:
        self.state_store = state_store or PersistentSessionStore()

    def set_active_agent(self, session_id: str, agent_name: str) -> None:
        self._save(self.ACTIVE_AGENT, session_id, agent_name)

    def get_active_agent(self, session_id: str) -> str | None:
        value = self._load(self.ACTIVE_AGENT, session_id)
        return value if isinstance(value, str) else None

    def clear_active_agent(self, session_id: str) -> None:
        self._delete(self.ACTIVE_AGENT, session_id)

    def set_pending_tool(self, session_id: str, value: dict[str, Any]) -> None:
        self._save(self.PENDING_TOOL, session_id, value)

    def get_pending_tool(self, session_id: str) -> dict[str, Any] | None:
        value = self._load(self.PENDING_TOOL, session_id)
        return value if isinstance(value, dict) else None

    def clear_pending_tool(self, session_id: str) -> None:
        self._delete(self.PENDING_TOOL, session_id)

    @staticmethod
    def _save(key: str, session_id: str, value: Any) -> None:
        try:
            agent_memory_repository.save_session_field(session_id, key, value)
        except SQLAlchemyError:
            # In-memory state remains available through the normal checkpoint;
            # a database outage must not make a conversation unusable.
            pass

    @staticmethod
    def _load(key: str, session_id: str) -> Any:
        try:
            return agent_memory_repository.load_session_field(session_id, key)
        except SQLAlchemyError:
            return None

    @staticmethod
    def _delete(key: str, session_id: str) -> None:
        try:
            agent_memory_repository.delete_session_field(session_id, key)
        except SQLAlchemyError:
            pass


agent_session_store = AgentSessionStore()
