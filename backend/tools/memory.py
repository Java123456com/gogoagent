"""Agent-controlled long-term memory tools.

These are the LangChain equivalent of AgentScope's automatically registered
``record_to_memory`` and ``retrieve_from_memory`` tools.
"""
from __future__ import annotations

from backend.core.request_context import current_context
from backend.memory.long_term import long_term_memory
from backend.tools._common import tool


def _user_id(explicit: str | None) -> str:
    context = current_context()
    return explicit or (context.user_id if context else "demo-user")


@tool
def record_to_memory(content: str, memory_type: str = "preference", user_id: str | None = None) -> dict:
    """Record a durable user preference or useful travel fact."""
    resolved = _user_id(user_id)
    memory_id = long_term_memory.record(resolved, content, memory_type)
    return {"saved": bool(memory_id or content.strip()), "memory_id": memory_id,
            "provider": long_term_memory.provider_name}


@tool
def retrieve_from_memory(query: str | None = None, limit: int = 10,
                         user_id: str | None = None) -> dict:
    """Retrieve preferences relevant to the current travel request."""
    resolved = _user_id(user_id)
    return {"user_id": resolved, "memories": long_term_memory.retrieve(resolved, limit, query),
            "provider": long_term_memory.provider_name}


def tools():
    return [record_to_memory, retrieve_from_memory]
