"""Domain worker processes and their supervisor.

Workers are deliberately request/response based and exchange only picklable
state dictionaries.  Durable session, plan, order and memory data remains in
the database/Redis, so a worker can be killed and recreated safely.
"""
from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from backend.config import get_settings
from backend.core.request_context import (
    AgentRequestContext,
    apply_context_to_state,
    bind_context_payload,
    context_from_state,
    current_context,
)
from backend.memory.execution import execution_registry
from backend.services.runtime_events import emit_event


class AgentWorkerTimeout(TimeoutError):
    pass


class AgentWorkerCrashed(RuntimeError):
    pass


class AgentWorkerCancelled(RuntimeError):
    pass


_WORKER_AGENT_NAMES = {
    "ItineraryManageAgent", "ItineraryPlanAgent", "InfoAgent", "BookingAgent",
}


def _resolve_agent(agent_name: str):
    # Imports happen inside the spawned process; this avoids constructing
    # model/MCP clients in the API process when process mode is selected.
    if agent_name == "ItineraryManageAgent":
        from backend.agents.itinerary_manage import itinerary_manage_agent
        return itinerary_manage_agent
    if agent_name == "ItineraryPlanAgent":
        from backend.agents.itinerary_plan import itinerary_plan_agent
        return itinerary_plan_agent
    if agent_name == "InfoAgent":
        from backend.agents.info import info_agent
        return info_agent
    if agent_name == "BookingAgent":
        from backend.agents.booking import booking_agent
        return booking_agent
    raise ValueError(f"不支持的 Worker Agent: {agent_name}")


def _worker_main(agent_name: str, requests, responses, events) -> None:
    try:
        from backend.services.runtime_events import bind_event_sink
        agent = _resolve_agent(agent_name)
        responses.put({"request_id": "__ready__", "ok": True})
        while True:
            envelope = requests.get()
            if envelope is None:
                return
            request_id = envelope.get("request_id")
            state = envelope.get("state") or {}
            try:
                def sink(event: str, data: Any, _request_id=request_id) -> None:
                    events.put({"request_id": _request_id, "event": event, "data": data})

                # ContextVar values never cross a spawn boundary. Restore only
                # the serializable request envelope; SSE sinks remain local and
                # are relayed through the dedicated event queue below.
                with bind_event_sink(sink), bind_context_payload(envelope.get("context")):
                    result = agent.invoke(state)
                responses.put({"request_id": request_id, "ok": True, "result": result})
            except BaseException as exc:  # noqa: BLE001 - worker must isolate Agent failures
                responses.put({
                    "request_id": request_id, "ok": False,
                    "error_type": type(exc).__name__, "error": str(exc),
                })
    except BaseException as exc:  # noqa: BLE001 - startup failure must be reported to parent
        # Startup failures have no request id; the parent observes process exit.
        try:
            responses.put({"request_id": None, "ok": False,
                           "error_type": type(exc).__name__, "error": str(exc)})
        except Exception:  # noqa: BLE001,S110 - reporting is best effort
            pass


@dataclass
class _Worker:
    agent_name: str
    process: Any
    requests: Any
    responses: Any
    events: Any
    restart_times: list[float]


class ProcessSubAgentExecutor:
    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")
        self._workers: dict[str, _Worker] = {}
        self._restart_history: dict[str, list[float]] = {}
        self._agent_locks: dict[str, threading.Lock] = {}
        self._closed = False

    def execute(self, agent_name: str, state: dict[str, Any], agent: Any | None = None,
                execution_context: AgentRequestContext | None = None) -> dict[str, Any]:
        # One request per domain worker at a time.  This prevents concurrent
        # callers from consuming one another's response while allowing
        # different domains to execute in parallel.
        lock = self._agent_locks.setdefault(agent_name, threading.Lock())
        with lock:
            return self._execute(agent_name, state, agent, execution_context)

    def _execute(self, agent_name: str, state: dict[str, Any], agent: Any | None = None,
                 execution_context: AgentRequestContext | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("SubAgentExecutor 已关闭")
        if agent_name not in _WORKER_AGENT_NAMES:
            # ReviewAgent is an independent/deprecated bean; retain a safe local
            # fallback for continuation paths that may still reference it.
            from backend.runtime.agent_executor import LocalSubAgentExecutor
            return LocalSubAgentExecutor().execute(
                agent_name, state, agent, execution_context=execution_context,
            )
        source_context = execution_context if execution_context is not None else current_context()
        context = context_from_state(state, agent_name=agent_name, parent=source_context)
        trusted_state = apply_context_to_state(state, context)
        worker = self._ensure_worker(agent_name)
        request_id = str(trusted_state.get("request_id") or uuid4().hex)
        # A Master deadline is an orchestration deadline, not the domain
        # worker's budget.  Let each sub-agent receive its configured budget;
        # callers that provide an explicit child deadline still take priority.
        deadline = None if (
            trusted_state.get("active_agent_name") == "MasterAgent"
            or (source_context is not None and source_context.agent_name == "MasterAgent")
        ) else trusted_state.get("deadline_at")
        if deadline is None or float(deadline) <= time.monotonic():
            seconds = self._deadline_seconds(agent_name)
            deadline = time.monotonic() + seconds
        context = context.derive(agent_name=agent_name, request_id=request_id, deadline_at=deadline)
        trusted_state = apply_context_to_state(
            {**trusted_state, "request_id": request_id, "deadline_at": deadline}, context,
        )
        worker.requests.put({
            "request_id": request_id,
            "state": trusted_state,
            "context": context.to_payload(),
        })
        while True:
            self._drain_events(worker, request_id)
            handle = execution_registry.get(str(trusted_state.get("session_id") or ""))
            if handle is not None and handle.stop.is_set():
                self._discard_worker(agent_name, terminate=True)
                raise AgentWorkerCancelled(f"{agent_name} Worker 已取消")
            if not worker.process.is_alive():
                self._discard_worker(agent_name, terminate=False)
                raise AgentWorkerCrashed(f"{agent_name} Worker 意外退出")
            remaining = max(0.0, float(deadline) - time.monotonic())
            if remaining <= 0:
                self._discard_worker(agent_name, terminate=True)
                raise AgentWorkerTimeout(f"{agent_name} Worker 超过截止时间")
            try:
                response = worker.responses.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            except (EOFError, OSError) as exc:
                self._discard_worker(agent_name, terminate=False)
                raise AgentWorkerCrashed(f"{agent_name} Worker 响应通道已断开") from exc
            if response.get("request_id") != request_id:
                # One worker handles one request at a time; retain no stale
                # response.  A stale response is reported as a crash boundary.
                continue
            self._drain_events(worker, request_id)
            if response.get("ok"):
                return response.get("result") or {}
            error_type = response.get("error_type", "WorkerError")
            if error_type == "ToolUnknownOutcome":
                from backend.runtime.resilient_tool import ToolUnknownOutcome
                raise ToolUnknownOutcome(response.get("error", "写操作结果未知"))
            raise RuntimeError(f"{agent_name} 执行失败 [{error_type}]: {response.get('error', '')}")

    def close(self) -> None:
        self._closed = True
        for agent_name in list(self._workers):
            self._discard_worker(agent_name, terminate=False)
        self._workers.clear()

    def health(self) -> dict[str, Any]:
        return {
            name: {"alive": worker.process.is_alive(), "pid": worker.process.pid}
            for name, worker in self._workers.items()
        }

    def _ensure_worker(self, agent_name: str) -> _Worker:
        current = self._workers.get(agent_name)
        if current is not None and current.process.is_alive():
            return current
        if current is not None:
            self._discard_worker(agent_name, terminate=False)
        settings = get_settings()
        now = time.monotonic()
        restart_times = list(self._restart_history.get(agent_name, []))
        window = max(1.0, float(settings.subagent_worker_restart_window_seconds))
        restart_times = [value for value in restart_times if now - value <= window]
        if len(restart_times) >= max(1, int(settings.subagent_worker_max_restarts_per_window)):
            raise AgentWorkerCrashed(f"{agent_name} Worker 重启次数超过限制")
        if restart_times:
            time.sleep(min(1.0, 0.05 * (2 ** min(len(restart_times), 4))))
        # Count every launch attempt, including one that fails during Agent or
        # MCP initialization, so a broken Worker cannot be restarted forever.
        restart_times = [*restart_times, now]
        self._restart_history[agent_name] = restart_times
        requests = self._ctx.Queue()
        responses = self._ctx.Queue()
        events = self._ctx.Queue()
        process = self._ctx.Process(target=_worker_main,
                                    args=(agent_name, requests, responses, events),
                                    name=f"gogo-{agent_name}")
        process.daemon = False
        process.start()
        timeout = max(0.1, float(settings.subagent_worker_start_timeout_seconds))
        started = time.monotonic()
        ready = False
        while time.monotonic() - started < timeout:
            if not process.is_alive():
                break
            try:
                response = responses.get(timeout=0.05)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            if response.get("request_id") == "__ready__" and response.get("ok"):
                ready = True
                break
            if response.get("request_id") is None and not response.get("ok"):
                break
        if not ready:
            self._safe_terminate(process)
            for channel in (requests, responses, events):
                try:
                    channel.close()
                    channel.join_thread()
                except Exception:  # noqa: BLE001,S110 - best-effort startup cleanup
                    pass
            raise AgentWorkerCrashed(f"{agent_name} Worker 启动失败或超时")
        worker = _Worker(agent_name, process, requests, responses, events, restart_times)
        self._workers[agent_name] = worker
        return worker

    def _drain_events(self, worker: _Worker, request_id: str) -> None:
        while True:
            try:
                event = worker.events.get_nowait()
            except queue.Empty:
                return
            if event.get("request_id") == request_id:
                emit_event(event.get("event", "progress"), event.get("data"))

    def _discard_worker(self, agent_name: str, terminate: bool) -> None:
        worker = self._workers.pop(agent_name, None)
        if worker is None:
            return
        if terminate and worker.process.is_alive():
            self._safe_terminate(worker.process)
        elif worker.process.is_alive():
            try:
                worker.requests.put(None)
                worker.process.join(max(0.1, float(get_settings().subagent_worker_shutdown_grace_seconds)))
            except Exception:  # noqa: BLE001,S110 - best-effort shutdown
                pass
            if worker.process.is_alive():
                self._safe_terminate(worker.process)
        for channel in (worker.requests, worker.responses, worker.events):
            try:
                channel.close()
                channel.join_thread()
            except Exception:  # noqa: BLE001,S110 - best-effort shutdown
                pass

    @staticmethod
    def _safe_terminate(process) -> None:
        try:
            process.terminate()
            process.join(1.0)
        except Exception:  # noqa: BLE001,S110 - best-effort process cleanup
            pass

    @staticmethod
    def _deadline_seconds(agent_name: str) -> float:
        settings = get_settings()
        values = {
            "ItineraryManageAgent": settings.subagent_deadline_manage_seconds,
            "ItineraryPlanAgent": settings.subagent_deadline_plan_seconds,
            "InfoAgent": settings.subagent_deadline_info_seconds,
            "BookingAgent": settings.subagent_deadline_booking_seconds,
        }
        return max(1.0, float(values.get(agent_name, settings.tool_default_timeout_seconds)))
