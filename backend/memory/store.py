from copy import deepcopy
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from backend.infrastructure.repositories import agent_memory_repository


class InMemorySessionStore:
    """Development checkpointer; replace with Postgres/Redis in production."""

    def __init__(self):
        self._sessions: dict[str, dict[str, Any]] = {}

    def get(self, session_id: str) -> dict[str, Any]:
        return deepcopy(self._sessions.get(session_id, {}))

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        self._sessions[session_id] = deepcopy(state)

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class PersistentSessionStore:
    """Database-backed checkpointer with the same API as the development store."""

    def __init__(self):
        self._fallback = InMemorySessionStore()

    def get(self, session_id: str) -> dict[str, Any]:
        try:
            state = agent_memory_repository.load_checkpoint(session_id)
        except SQLAlchemyError:
            state = self._fallback.get(session_id)
        return deepcopy(_restore_messages(state or {}))

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        safe_state = _json_safe(state)
        try:
            agent_memory_repository.save_checkpoint(session_id, safe_state)
        except SQLAlchemyError:
            self._fallback.save(session_id, safe_state)

    def delete(self, session_id: str) -> None:
        try:
            agent_memory_repository.delete_checkpoint(session_id)
        except SQLAlchemyError:
            self._fallback.delete(session_id)


def _json_safe(value: Any) -> Any:
    """Convert LangChain messages and arbitrary state values to JSON-safe data."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "type") and hasattr(value, "content"):
        return {"__message__": True, "type": value.type, "content": value.content,
                "additional_kwargs": _json_safe(getattr(value, "additional_kwargs", {}))}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _restore_messages(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("__message__"):
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
            message_type = value.get("type")
            cls = {"human": HumanMessage, "ai": AIMessage, "system": SystemMessage}.get(
                message_type, HumanMessage
            )
            return cls(content=value.get("content", ""),
                       additional_kwargs=value.get("additional_kwargs") or {})
        return {key: _restore_messages(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_messages(item) for item in value]
    return value
