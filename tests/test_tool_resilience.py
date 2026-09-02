from __future__ import annotations

import asyncio
import threading
import time
from uuid import uuid4

import pytest

from backend.config import get_settings
from backend.core.request_context import bind_context
from backend.infrastructure.bootstrap import bootstrap
from backend.runtime.agent_executor import close_subagent_executor
from backend.runtime.process_executor import ProcessSubAgentExecutor
from backend.runtime.resilient_tool import ResilientToolHook, ToolDeadlineExceeded
from backend.runtime.tool_policy import ToolEffect, ToolPolicy, ToolPolicyRegistry
from backend.services.circuit_breaker import ToolCircuitBreaker
from backend.services.runtime_events import bind_event_sink, emit_tool_call


def _registry(effect: ToolEffect, attempts: int = 1, timeout: float = 1.0) -> ToolPolicyRegistry:
    registry = ToolPolicyRegistry()
    registry.register("test_tool", ToolPolicy(
        effect=effect, timeout_seconds=timeout, max_attempts=attempts,
        initial_backoff_seconds=0, max_backoff_seconds=0, jitter_ratio=0,
    ))
    return registry


def test_read_only_tool_retries_transient_connection_error(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(get_settings(), "tool_policy_enabled", True)

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("temporary")
        return {"ok": True}

    result = ResilientToolHook("InfoAgent", _registry(ToolEffect.READ_ONLY, 3)).execute(
        "test_tool", flaky, (), {},
    )
    assert result == {"ok": True}
    assert calls["count"] == 3


def test_structured_temporary_failure_is_retried_and_preserved(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(get_settings(), "tool_policy_enabled", True)

    def flaky_result():
        calls["count"] += 1
        if calls["count"] == 1:
            return {"available": False, "error": "upstream unavailable"}
        return {"available": True, "value": 1}

    result = ResilientToolHook("InfoAgent", _registry(ToolEffect.READ_ONLY, 2)).execute(
        "test_tool", flaky_result, (), {},
    )
    assert result == {"available": True, "value": 1}
    assert calls["count"] == 2


def test_non_idempotent_tool_does_not_retry(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(get_settings(), "tool_policy_enabled", True)

    def failing():
        calls["count"] += 1
        raise ConnectionError("unknown outcome")

    with pytest.raises(ConnectionError):
        ResilientToolHook("BookingAgent", _registry(ToolEffect.NON_IDEMPOTENT_WRITE, 3)).execute(
            "test_tool", failing, (), {},
        )
    assert calls["count"] == 1


def test_sync_timeout_is_reported_without_blocking_caller(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_policy_enabled", True)

    started = time.monotonic()
    with pytest.raises(ToolDeadlineExceeded):
        ResilientToolHook("InfoAgent", _registry(ToolEffect.READ_ONLY, 1, 0.02)).execute(
            "test_tool", lambda: time.sleep(0.2), (), {},
        )
    assert time.monotonic() - started < 0.15


def test_idempotent_write_reuses_completed_result(monkeypatch):
    bootstrap()
    calls = {"count": 0}
    monkeypatch.setattr(get_settings(), "tool_policy_enabled", True)
    registry = _registry(ToolEffect.IDEMPOTENT_WRITE, 1)
    request_id = f"idem-{uuid4().hex}"

    def write_once():
        calls["count"] += 1
        return {"saved": True, "sequence": calls["count"]}

    with bind_context("u_001", f"session-{uuid4().hex}", "InfoAgent", request_id=request_id):
        first = ResilientToolHook("InfoAgent", registry).execute("test_tool", write_once, (), {})
        second = ResilientToolHook("InfoAgent", registry).execute("test_tool", write_once, (), {})
    assert first == second == {"saved": True, "sequence": 1}
    assert calls["count"] == 1


def test_process_executor_runs_info_agent_in_worker(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "subagent_worker_start_timeout_seconds", 20.0)
    monkeypatch.setattr(settings, "subagent_deadline_info_seconds", 20.0)
    executor = ProcessSubAgentExecutor()
    try:
        result = executor.execute("InfoAgent", {
            "request": "介绍上海景点",
            "original_question": "介绍上海景点",
            "user_id": "u_001",
            "session_id": f"process-{uuid4().hex}",
            "request_id": uuid4().hex,
        })
        assert result.get("final")
        assert executor.health()["InfoAgent"]["alive"] is True
    finally:
        executor.close()
        close_subagent_executor()


def test_process_executor_restarts_a_crashed_domain_worker(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "subagent_worker_start_timeout_seconds", 20.0)
    monkeypatch.setattr(settings, "subagent_deadline_info_seconds", 20.0)
    executor = ProcessSubAgentExecutor()
    state = {
        "request": "介绍上海景点", "original_question": "介绍上海景点",
        "user_id": "u_001", "session_id": f"restart-{uuid4().hex}",
        "request_id": uuid4().hex,
    }
    try:
        assert executor.execute("InfoAgent", state).get("final")
        old_pid = executor.health()["InfoAgent"]["pid"]
        worker = executor._workers["InfoAgent"]
        worker.process.terminate()
        worker.process.join(2.0)
        assert executor.execute("InfoAgent", {**state, "request_id": uuid4().hex}).get("final")
        assert executor.health()["InfoAgent"]["pid"] != old_pid
    finally:
        executor.close()


def test_async_retry_path(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_policy_enabled", True)
    calls = {"count": 0}

    async def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionError("temporary")
        return "ok"

    async def run():
        return await ResilientToolHook("InfoAgent", _registry(ToolEffect.READ_ONLY, 2)).aexecute(
            "test_tool", flaky, (), {},
        )

    assert asyncio.run(run()) == "ok"
    assert calls["count"] == 2


def test_progress_events_redact_credentials():
    events = []
    with bind_event_sink(lambda event, data: events.append((event, data))):
        emit_tool_call("InfoAgent", "save_flight_api_key", "1", {
            "api_key": "do-not-leak", "nested": {"accessKeySecret": "also-secret"},
        })
    payload = events[0][1]
    assert "do-not-leak" not in payload["arguments"]
    assert "also-secret" not in payload["arguments"]
    assert "***" in payload["arguments"]


def test_circuit_breaker_allows_only_one_half_open_probe():
    breaker = ToolCircuitBreaker(threshold=1, recovery_seconds=0.01)
    with pytest.raises(RuntimeError):
        breaker.call("half-open", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    time.sleep(0.02)
    entered = threading.Event()
    release = threading.Event()

    def probe():
        entered.set()
        release.wait(1)
        return "recovered"

    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("value", breaker.call("half-open", probe)))
    thread.start()
    assert entered.wait(1)
    with pytest.raises(RuntimeError, match="半开探测进行中"):
        breaker.call("half-open", lambda: "second")
    release.set()
    thread.join(1)
    assert result["value"] == "recovered"
