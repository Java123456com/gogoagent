"""Small, privacy-safe structured diagnostics for agent execution boundaries."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from backend.core.request_context import current_context
from backend.infrastructure.security import sanitize_sensitive

logger = logging.getLogger(__name__)


def _user_correlation(user_id: str | None) -> str | None:
    if not user_id:
        return None
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def record_event(event: str, **fields: Any) -> dict[str, Any]:
    """Log bounded metadata only; diagnostics must never affect business flow."""
    try:
        context = current_context()
        payload = {
            "event": event,
            "request_id": context.request_id if context else None,
            "session_id": context.session_id if context else None,
            "user_correlation": _user_correlation(context.user_id if context else None),
            "agent": context.agent_name if context else None,
            "tool_call_id": context.tool_call_id if context else None,
            **fields,
        }
        cleaned = sanitize_sensitive(payload)
        logger.info("agent_observability=%s", json.dumps(cleaned, ensure_ascii=False, default=str))
        return cleaned
    except Exception:  # noqa: BLE001 - telemetry cannot block user execution
        return {"event": event}


def trace_event(state: dict[str, Any], agent: str, output: str) -> dict[str, Any]:
    """Compatibility seam for graph tracing, now with a privacy-safe log record."""
    record_event("agent.trace", agent=agent, result_class="trace")
    return {"agent": agent, "output": output}
