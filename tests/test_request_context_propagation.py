import asyncio
import pickle
import queue

import pytest
from pydantic import ValidationError

from backend.agents import master as master_module
from backend.core.request_context import (
    AgentRequestContext,
    apply_context_to_state,
    bind_context,
    bind_context_payload,
    bind_tool_call_id,
    context_from_state,
    current_context,
)
from backend.runtime import process_executor
from backend.runtime.agent_executor import LocalSubAgentExecutor
from backend.runtime.retry import call_sync


def _subagent_tools():
    return [
        item for item in master_module.master_agent.tools
        if item.name in {
            "itinerary_manage_agent", "itinerary_plan_agent", "info_agent", "booking_agent",
        }
    ]


def test_subagent_tool_schema_hides_identity_and_rejects_extra_fields():
    tools = _subagent_tools()
    assert len(tools) == 4
    for item in tools:
        schema = item.args_schema.model_json_schema()
        assert set(schema["properties"]) == {"request"}
        with pytest.raises(ValidationError):
            item.args_schema(request="查询政策", user_id="forged-user")
        with pytest.raises(ValidationError):
            item.invoke({"request": "查询政策", "user_id": "forged-user"})


def test_subagent_tool_inherits_trusted_context(monkeypatch):
    captured = {}

    class SpyExecutor:
        def execute(self, agent_name, state, agent=None, execution_context=None):
            captured.update(agent_name=agent_name, state=state, context=execution_context)
            return {"final": "ok"}

    monkeypatch.setattr(master_module, "get_subagent_executor", lambda: SpyExecutor())
    tool = next(item for item in _subagent_tools() if item.name == "info_agent")
    with bind_context("authenticated-user", "parent-session", "MasterAgent",
                      "order-1", request_id="request-1", deadline_at=42.0):
        assert tool.invoke({"request": "查询北京政策"}) == "ok"

    assert captured["agent_name"] == "InfoAgent"
    assert captured["state"]["user_id"] == "authenticated-user"
    assert captured["state"]["session_id"] == "parent-session"
    assert captured["state"]["request_id"] == "request-1"
    assert captured["state"]["travel_order_id"] == "order-1"
    assert captured["context"].agent_name == "InfoAgent"
    assert captured["context"].deadline_at is None


def test_trusted_parent_identity_wins_over_nested_state():
    with bind_context("authenticated-user", "parent-session", "MasterAgent",
                      request_id="request-1"):
        context = context_from_state({
            "user_id": "forged-user", "session_id": "forged-session",
            "request_id": "forged-request", "travel_order_id": "order-from-state",
        }, agent_name="InfoAgent")

    assert context.user_id == "authenticated-user"
    assert context.session_id == "parent-session"
    assert context.request_id == "request-1"
    assert context.travel_order_id == "order-from-state"
    assert context.agent_name == "InfoAgent"


def test_local_executor_rebinds_context_and_restores_caller():
    seen = {}

    class SpyAgent:
        def invoke(self, state):
            seen["context"] = current_context()
            seen["state"] = state
            return {"final": "ok"}

    parent = AgentRequestContext("authenticated-user", "parent-session", "MasterAgent")
    with bind_context("outer-user", "outer-session", "OuterAgent"):
        result = LocalSubAgentExecutor().execute(
            "InfoAgent",
            {"user_id": "forged-user", "session_id": "forged-session", "deadline_at": 1.0},
            agent=SpyAgent(), execution_context=parent,
        )
        assert current_context().user_id == "outer-user"

    assert result == {"final": "ok"}
    assert seen["context"].user_id == "authenticated-user"
    assert seen["context"].session_id == "parent-session"
    assert seen["context"].agent_name == "InfoAgent"
    assert seen["state"]["user_id"] == "authenticated-user"
    assert seen["state"]["session_id"] == "parent-session"
    assert "deadline_at" not in seen["state"]


def test_contextvar_isolates_async_tasks_and_tool_threads():
    async def read_context(user_id: str, session_id: str):
        with bind_context(user_id, session_id, "InfoAgent"):
            await asyncio.sleep(0)
            thread_value = call_sync(lambda: current_context().user_id, timeout=1)
            return current_context().user_id, current_context().session_id, thread_value

    async def run_both():
        return await asyncio.gather(
            read_context("user-a", "session-a"),
            read_context("user-b", "session-b"),
        )

    results = asyncio.run(run_both())
    assert set(results) == {
        ("user-a", "session-a", "user-a"),
        ("user-b", "session-b", "user-b"),
    }
    assert current_context() is None


def test_tool_call_binding_nests_and_restores_context():
    with bind_context("user", "session", "InfoAgent", tool_call_id="parent"):
        with bind_tool_call_id("child"):
            assert current_context().tool_call_id == "child"
        assert current_context().tool_call_id == "parent"
    assert current_context() is None


def test_process_payload_roundtrip_restores_and_cleans_context():
    context = AgentRequestContext(
        "user", "session", "InfoAgent", "order", "request", 123.0, "tool", "idem",
    )
    payload = pickle.loads(pickle.dumps(context.to_payload()))
    with bind_context_payload(payload):
        restored = current_context()
        assert restored is not None
        assert restored.to_payload() == context.to_payload()
        assert apply_context_to_state({"user_id": "forged"}, restored)["user_id"] == "user"
    assert current_context() is None


def test_worker_restores_serialized_context_envelope(monkeypatch):
    seen = {}

    class SpyAgent:
        def invoke(self, state):
            seen["context"] = current_context()
            seen["state"] = state
            return {"final": "ok"}

    monkeypatch.setattr(process_executor, "_resolve_agent", lambda _name: SpyAgent())
    requests, responses, events = queue.Queue(), queue.Queue(), queue.Queue()
    context = AgentRequestContext("worker-user", "worker-session", "InfoAgent", request_id="req")
    requests.put({
        "request_id": "req",
        "state": apply_context_to_state({"request": "查询"}, context),
        "context": pickle.loads(pickle.dumps(context.to_payload())),
    })
    requests.put(None)

    process_executor._worker_main("InfoAgent", requests, responses, events)

    assert responses.get_nowait()["request_id"] == "__ready__"
    assert responses.get_nowait() == {"request_id": "req", "ok": True, "result": {"final": "ok"}}
    assert seen["context"].to_payload() == context.to_payload()
    assert seen["state"]["user_id"] == "worker-user"
    assert current_context() is None
