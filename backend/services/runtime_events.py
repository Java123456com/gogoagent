"""Request-local event sink used to bridge agent hooks to HTTP SSE streams."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from backend.infrastructure.security import mask_sensitive, sanitize_sensitive

EventSink = Callable[[str, Any], None]

_sink: ContextVar[EventSink | None] = ContextVar("gogo_runtime_event_sink", default=None)
logger = logging.getLogger(__name__)


class SessionEventRegistry:
    """Local SSE sink registry used by Redis interrupt subscribers.

    Redis tells every application node that a session was cancelled. Only the
    node holding the browser's SSE connection has a registered sink, so only
    that node emits the immediate ``interrupted`` event to the client.
    """

    def __init__(self) -> None:
        self._sinks: dict[str, set[EventSink]] = {}
        self._lock = threading.RLock()

    @contextmanager
    def bind(self, session_id: str, sink: EventSink) -> Iterator[None]:
        with self._lock:
            self._sinks.setdefault(session_id, set()).add(sink)
        try:
            yield
        finally:
            with self._lock:
                values = self._sinks.get(session_id)
                if values is not None:
                    values.discard(sink)
                    if not values:
                        self._sinks.pop(session_id, None)

    def emit(self, session_id: str, event: str, data: Any) -> int:
        with self._lock:
            targets = list(self._sinks.get(session_id, set()))
        for sink in targets:
            try:
                sink(event, sanitize_sensitive(data))
            except Exception:
                # A disconnected browser must not affect cancellation on this
                # or other application nodes.
                logger.debug("投递会话运行时事件失败: session_id=%s", session_id, exc_info=True)
        return len(targets)


session_event_registry = SessionEventRegistry()

AGENT_LABELS = {
    "MasterAgent": "总协调",
    "ItineraryManageAgent": "差旅单管理",
    "ItineraryPlanAgent": "行程规划",
    "BookingAgent": "预订执行",
    "InfoAgent": "信息查询",
    "QueryRewritingAgent": "问题改写",
    "IntentRecognitionAgent": "意图识别",
}

TOOL_LABELS = {
    "plan_itinerary": "规划往返行程",
    "review_itinerary": "多维审核出行方案",
    "save_plan_html": "保存方案",
    "query_travel_policy": "查询差旅政策",
    "query_weather": "查询天气",
    "query_destination_news": "查询目的地资讯",
    "retrieve_from_memory": "召回长期记忆",
    "record_to_memory": "记录长期记忆",
    "load_skill_through_path": "加载技能",
    "execute_shell_command": "执行技能命令",
}


@contextmanager
def bind_event_sink(sink: EventSink) -> Iterator[None]:
    token: Token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def emit_event(event: str, data: Any) -> None:
    sink = _sink.get()
    if sink is not None:
        sink(event, sanitize_sensitive(data))


def emit_agent_start(agent_name: str) -> None:
    label = AGENT_LABELS.get(agent_name, agent_name)
    emit_event("agent-switch", agent_name)
    emit_event("progress", {
        "type": "agent_start", "stepId": f"agent_{agent_name}",
        "agentName": agent_name, "message": f"{label} 处理中",
    })


def emit_agent_done(agent_name: str) -> None:
    label = AGENT_LABELS.get(agent_name, agent_name)
    emit_event("progress", {
        "type": "agent_done", "stepId": f"agent_{agent_name}",
        "agentName": agent_name, "message": f"{label} 已完成",
    })


def emit_tool_call(agent_name: str, tool_name: str, call_id: str, arguments: dict) -> None:
    label = TOOL_LABELS.get(tool_name, tool_name)
    emit_event("progress", {
        "type": "tool_call", "stepId": f"tool_{call_id}", "agentName": agent_name,
        "toolName": tool_name, "message": label,
        "arguments": _bounded(arguments, 500),
    })


def emit_tool_done(agent_name: str, tool_name: str, call_id: str, result: Any) -> None:
    label = TOOL_LABELS.get(tool_name, tool_name)
    emit_event("progress", {
        "type": "tool_done", "stepId": f"tool_{call_id}", "agentName": agent_name,
        "toolName": tool_name, "message": f"{label} 完成",
        "result": _bounded(result, 800),
    })


def emit_tool_retry(agent_name: str, tool_name: str, call_id: str, attempt: int,
                    delay_seconds: float, error_type: str) -> None:
    """Expose retry decisions without leaking the original tool arguments."""
    emit_event("progress", {
        "type": "tool_retry", "stepId": f"tool_{call_id}", "agentName": agent_name,
        "toolName": tool_name, "attempt": attempt, "nextAttempt": attempt + 1,
        "delaySeconds": round(delay_seconds, 3), "errorType": error_type,
        "message": f"{TOOL_LABELS.get(tool_name, tool_name)} 临时失败，准备重试",
    })


def emit_tool_timeout(agent_name: str, tool_name: str, call_id: str, attempt: int,
                      timeout_seconds: float | None) -> None:
    emit_event("progress", {
        "type": "tool_timeout", "stepId": f"tool_{call_id}", "agentName": agent_name,
        "toolName": tool_name, "attempt": attempt,
        "timeoutSeconds": round(timeout_seconds, 3) if timeout_seconds is not None else None,
        "message": f"{TOOL_LABELS.get(tool_name, tool_name)} 超时",
    })


def emit_tool_circuit_open(agent_name: str, tool_name: str, call_id: str) -> None:
    emit_event("progress", {
        "type": "tool_circuit_open", "stepId": f"tool_{call_id}", "agentName": agent_name,
        "toolName": tool_name, "message": f"{TOOL_LABELS.get(tool_name, tool_name)} 暂时不可用",
    })


def _bounded(value: Any, limit: int) -> str:
    import json

    text = mask_sensitive(value) if isinstance(value, str) else json.dumps(_redact(value), ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "\n...已截断"


def _redact(value: Any) -> Any:
    """Remove credential-like fields before progress data reaches SSE/IPC."""
    return sanitize_sensitive(value)
