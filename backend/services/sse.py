"""SSE 流式推送（对应 Java ProgressNotifierHook + ChatSseNotifier 的事件协议）。

支持事件：message / thinking / progress / travel_data / plan_update / plan_html /
user_interaction / suggestions / agent-switch / booking_result / interrupted / done。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from backend.infrastructure.security import mask_sensitive, sanitize_sensitive

RAW_TEXT_EVENTS = frozenset({
    "message",
    "message_id",
    "error",
    "agent-switch",
    "interrupted",
})


def sse(event: str, data: Any, *, raw: bool = False) -> str:
    """Encode one SSE event using the Java frontend contract.

    Spring sends text events as unquoted strings and structured events as JSON.
    Splitting embedded newlines into multiple ``data:`` fields keeps the frame
    valid while allowing the browser parser to reconstruct the original text.
    """
    payload = (
        mask_sensitive(str(data))
        if raw
        else json.dumps(sanitize_sensitive(data), ensure_ascii=False, default=str)
    )
    lines = payload.splitlines() or [""]
    encoded_data = "".join(f"data: {line}\n" for line in lines)
    return f"event: {event}\n{encoded_data}\n"


def event(event_name: str, data: Any) -> str:
    """Encode a runtime event emitted by an Agent or tool Hook."""
    return sse(event_name, data, raw=event_name in RAW_TEXT_EVENTS)


def event_message(content: str) -> str:
    return sse("message", content, raw=True)


def event_message_id(message_id: str) -> str:
    return sse("message_id", message_id, raw=True)


def event_error(message: str) -> str:
    return sse("error", message, raw=True)


def event_thinking(payload: dict[str, Any]) -> str:
    return sse("thinking", payload)


def event_progress(item: dict[str, Any]) -> str:
    return sse("progress", item)


def event_travel_data(data: Any) -> str:
    return sse("travel_data", data)


def event_plan_update(plan: Any) -> str:
    return sse("plan_update", plan)


def event_plan_html(html: str, title: str | None = None) -> str:
    payload = {"type": "plan_html", "html": html}
    if title:
        payload["title"] = title
    return sse("plan_html", payload)


def event_user_interaction(payload: dict[str, Any]) -> str:
    return sse("user_interaction", payload)


def event_suggestions(items: list[str]) -> str:
    return sse("suggestions", items)


def event_agent_switch(agent_name: str) -> str:
    return sse("agent-switch", agent_name, raw=True)


def event_booking_result(data: Any) -> str:
    return sse("booking_result", data)


def event_interrupted(message: str = "已停止生成") -> str:
    return sse("interrupted", message, raw=True)


def event_done() -> str:
    return sse("done", {})


def stream_trace(trace: Iterable[dict[str, Any]], final: str):
    for item in trace:
        yield event_progress(item)
    yield event_message(final)
    yield event_done()
