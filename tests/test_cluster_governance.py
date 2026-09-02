from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.infrastructure.stores import SessionExecutionFence
from backend.memory.execution import AgentExecutionRegistry, execution_registry
from backend.services.interrupt_broadcast import RedisInterruptBroadcast
from backend.services.runtime_events import session_event_registry


def _settings(**overrides):
    values = {
        "redis_url": None,
        "cluster_mode": False,
        "app_instance_id": "node-a",
        "interrupt_broadcast_channel": "agent:interrupt",
        "interrupt_broadcast_poll_seconds": 0.01,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_execution_registry_interrupts_every_nested_handle_and_runs_callbacks():
    registry = AgentExecutionRegistry()
    callbacks = []
    first = registry.begin("s1", "MasterAgent", generation=7,
                           on_interrupt=lambda item: callbacks.append(item.agent_name))
    second = registry.begin("s1", "InfoAgent", generation=7,
                            on_interrupt=lambda item: callbacks.append(item.agent_name))
    other_generation = registry.begin("s1", "BookingAgent", generation=8)

    assert registry.interrupt("s1", generation=7) is True
    assert first.stop.is_set() and second.stop.is_set()
    assert not other_generation.stop.is_set()
    assert set(callbacks) == {"MasterAgent", "InfoAgent"}

    registry.end("s1", first)
    assert registry.get("s1") is other_generation
    registry.end("s1")
    assert registry.get("s1") is None


def test_interrupt_broadcast_handles_foreign_node_and_skips_own_loopback():
    calls = []
    broadcast = RedisInterruptBroadcast(
        settings=_settings(app_instance_id="node-a"),
        local_interrupt=lambda session_id, payload: calls.append((session_id, payload)) or True,
    )
    assert broadcast.handle_payload({"session_id": "session-1", "source_instance_id": "node-b"})
    assert calls[0][0] == "session-1"
    assert not broadcast.handle_payload({"session_id": "session-1", "source_instance_id": "node-a"})
    assert len(calls) == 1


def test_cluster_mode_requires_redis_at_listener_startup():
    broadcast = RedisInterruptBroadcast(settings=_settings(cluster_mode=True))
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        broadcast.start()


def test_real_local_handler_stops_handle_and_notifies_only_local_sse_sink():
    session_id = f"cluster-{uuid4().hex}"
    handle = execution_registry.begin(session_id, generation=3)
    events = []
    broadcast = RedisInterruptBroadcast(settings=_settings(app_instance_id="node-a"))
    try:
        with session_event_registry.bind(session_id, lambda event, data: events.append((event, data))):
            assert broadcast.handle_payload({
                "session_id": session_id,
                "generation": 3,
                "source_instance_id": "node-b",
            })
        assert handle.stop.is_set()
        assert events == [("interrupted", "已停止生成")]
    finally:
        execution_registry.end(session_id, handle)


def test_session_execution_fence_discards_previous_generation():
    fence = SessionExecutionFence()
    first = fence.begin("s1")
    second = fence.begin("s1")
    assert second == first + 1
    assert not fence.is_current("s1", first)
    assert fence.is_current("s1", second)

