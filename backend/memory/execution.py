"""Local execution registry used by the interrupt endpoint.

The runtime keeps a bounded entry for the live Agent and an execution registry for
cooperative cancellation. Python cannot safely kill a thread running an HTTP
request, so cancellation is cooperative: the request is marked immediately,
and the completed graph result is discarded/annotated if the stop flag was set
while a model or tool call was in flight.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

logger = logging.getLogger(__name__)

@dataclass
class ExecutionHandle:
    session_id: str
    started_at: float
    stop: threading.Event
    agent_name: str = "MasterAgent"
    execution_id: str = ""
    generation: int = 0
    on_interrupt: Callable[[ExecutionHandle], None] | None = None


class AgentExecutionRegistry:
    def __init__(self) -> None:
        # A Master Agent may have nested sub-agent execution under the same
        # session. Keep every local handle as a session -> Set<Agent> mapping.
        # registry rather than overwriting the previous one.
        self._handles: dict[str, dict[str, ExecutionHandle]] = {}
        self._lock = threading.RLock()

    def begin(self, session_id: str, agent_name: str = "MasterAgent", *,
              generation: int = 0,
              on_interrupt: Callable[[ExecutionHandle], None] | None = None) -> ExecutionHandle:
        handle = ExecutionHandle(
            session_id, time.time(), threading.Event(), agent_name,
            uuid4().hex, generation, on_interrupt,
        )
        with self._lock:
            self._handles.setdefault(session_id, {})[handle.execution_id] = handle
        return handle

    def get(self, session_id: str) -> ExecutionHandle | None:
        with self._lock:
            handles = self._handles.get(session_id, {})
            return max(handles.values(), key=lambda item: item.started_at, default=None)

    def get_all(self, session_id: str) -> list[ExecutionHandle]:
        with self._lock:
            return list(self._handles.get(session_id, {}).values())

    def set_interrupt_callback(self, handle: ExecutionHandle,
                               callback: Callable[[ExecutionHandle], None] | None) -> None:
        with self._lock:
            stored = self._handles.get(handle.session_id, {}).get(handle.execution_id)
            if stored is handle:
                stored.on_interrupt = callback

    def interrupt(self, session_id: str, *, generation: int | None = None) -> bool:
        callbacks: list[tuple[Callable[[ExecutionHandle], None], ExecutionHandle]] = []
        with self._lock:
            handles = list(self._handles.get(session_id, {}).values())
            if generation is not None:
                handles = [item for item in handles if item.generation == generation]
            for handle in handles:
                if handle.stop.is_set():
                    continue
                handle.stop.set()
                if handle.on_interrupt is not None:
                    callbacks.append((handle.on_interrupt, handle))
        # Persistence callbacks may touch the database. Never invoke them
        # while holding the registry lock.
        for callback, handle in callbacks:
            try:
                callback(handle)
            except Exception:
                # Cancellation must remain best effort even if checkpointing
                # temporarily fails; the service will still discard stale work.
                logger.warning(
                    "持久化 Agent 中断检查点失败: session_id=%s execution_id=%s",
                    handle.session_id,
                    handle.execution_id,
                    exc_info=True,
                )
        return bool(handles)

    def end(self, session_id: str, handle: ExecutionHandle | None = None) -> None:
        with self._lock:
            if handle is None:
                self._handles.pop(session_id, None)
                return
            handles = self._handles.get(session_id)
            if handles is None:
                return
            if handles.get(handle.execution_id) is handle:
                handles.pop(handle.execution_id, None)
            if not handles:
                self._handles.pop(session_id, None)


execution_registry = AgentExecutionRegistry()
