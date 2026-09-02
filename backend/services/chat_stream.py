"""Bridge synchronous Agent execution and the Java-compatible HTTP SSE stream."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from queue import Queue
from threading import Event, Thread
from typing import Any

from backend.infrastructure.security import mask_sensitive
from backend.services.chat_service import chat_service
from backend.services.llm_services import question_recommendation_service
from backend.services.runtime_events import bind_event_sink, session_event_registry
from backend.services.sse import (
    event,
    event_done,
    event_error,
    event_interrupted,
    event_message,
    event_message_id,
    event_suggestions,
    event_user_interaction,
)

AgentResult = dict[str, Any]
ExecuteAgent = Callable[[], AgentResult]

_END = object()


def stream_agent_execution(
    execute: ExecuteAgent,
    *,
    session_id: str,
    latest_user_text: str,
    user_id: str,
    persist_user_reply: bool = False,
) -> Iterator[str]:
    """Run an Agent in a worker and yield Hook events as soon as they occur."""
    queue: Queue[tuple[str, Any] | object] = Queue()
    interrupted_notified = Event()

    def enqueue(event_name: str, data: Any) -> None:
        if event_name == "interrupted":
            interrupted_notified.set()
        queue.put((event_name, data))

    def worker() -> None:
        try:
            with session_event_registry.bind(session_id, enqueue), bind_event_sink(enqueue):
                result = execute()
            if persist_user_reply:
                chat_service.save_user_message(session_id, user_id, latest_user_text)
            _enqueue_result(queue, result, session_id, latest_user_text, interrupted_notified)
        except Exception as exc:  # noqa: BLE001 - encode worker failures after response start
            enqueue("error", mask_sensitive(str(exc)) or "Agent 执行失败")
        finally:
            queue.put(_END)

    Thread(target=worker, name=f"chat-sse-{session_id}", daemon=True).start()

    while True:
        item = queue.get()
        if item is _END:
            break
        event_name, data = item
        yield event(event_name, data)


def _enqueue_result(
    queue: Queue[tuple[str, Any] | object],
    result: AgentResult,
    session_id: str,
    latest_user_text: str,
    interrupted_notified: Event | None = None,
) -> None:
    def enqueue(event_name: str, data: Any) -> None:
        queue.put((event_name, data))

    if result.get("interrupted"):
        if interrupted_notified is None or not interrupted_notified.is_set():
            enqueue("interrupted", "已停止生成")
        enqueue("done", {})
        return

    pending = result.get("pending_interaction") or {}
    if pending:
        enqueue("user_interaction", pending)
        enqueue("done", {})
        return

    final = mask_sensitive(str(result.get("final") or ""))
    active_agent = str(result.get("active_agent") or "MasterAgent")
    saved = chat_service.save_agent_message(
        session_id,
        final,
        active_agent,
        {"trace": result.get("trace", [])},
    )
    enqueue("message", final)
    if saved.message_id:
        enqueue("message_id", saved.message_id)

    suggestions = question_recommendation_service.generate(latest_user_text, final)
    if suggestions:
        enqueue("suggestions", suggestions)
    enqueue("done", {})


__all__ = [
    "event_done",
    "event_error",
    "event_interrupted",
    "event_message",
    "event_message_id",
    "event_suggestions",
    "event_user_interaction",
    "stream_agent_execution",
]
