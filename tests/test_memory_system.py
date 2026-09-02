from uuid import uuid4

from langchain_core.messages import HumanMessage

from backend.infrastructure.bootstrap import bootstrap
from backend.memory.context import (
    ContextCompressionConfig,
    ContextCompressionHook,
    LayeredContextManager,
)
from backend.memory.long_term import LongTermMemory
from backend.memory.store import PersistentSessionStore
from backend.agents.base import BaseSubAgent


def test_context_hook_keeps_recent_messages_and_offloads_large_payload():
    hook = ContextCompressionHook(ContextCompressionConfig(message_threshold=4, keep_last=2,
                                                           large_payload_chars=10, preview_chars=5))
    result = hook.compress([("user", "x" * 20), ("assistant", "old"), ("user", "older"),
                            ("user", "recent"), ("assistant", "done")])
    assert len(result.original_messages) == 5
    assert len(result.working_messages) == 3
    assert result.offload_context
    assert any(event["event_type"] == "large_message_offload" for event in result.events)


def test_persistent_session_store_round_trip():
    bootstrap()
    store = PersistentSessionStore()
    session_id = "memory-test-session"
    store.save(session_id, {"user_id": "u_001", "messages": [HumanMessage(content="hello")]})
    state = store.get(session_id)
    assert state["user_id"] == "u_001"
    assert state["messages"][0].content == "hello"
    store.delete(session_id)


def test_long_term_memory_is_user_isolated_and_cached():
    bootstrap()
    memory = LongTermMemory(durable=True)
    user_id = f"memory-test-user-{uuid4().hex}"
    memory.record(user_id, "只选择高铁一等座")
    assert memory.retrieve(user_id) == ["只选择高铁一等座"]
    assert memory.retrieve("other-user") == []


def test_layered_manager_injects_long_term_preferences():
    memory = LongTermMemory(durable=False)
    memory.record("u1", "酒店需要早餐")
    manager = LayeredContextManager(PersistentSessionStore(), memory,
                                    auto_retrieve_long_term=True)
    state = manager.prepare({"user_id": "u1", "session_id": "s1", "messages": []})
    assert state["long_term_preferences"] == ["酒店需要早餐"]
    assert "memory_original_messages" in state


def test_agent_memory_is_namespaced_by_session_and_agent():
    class DemoAgent(BaseSubAgent):
        def fallback(self, state):
            return {"final": "ok", "trace": []}

    agent = DemoAgent()
    session_id = f"agent-memory-{uuid4().hex}"
    agent.invoke({"user_id": "u1", "session_id": session_id, "request": "first",
                  "messages": [("user", "first")]})
    saved = PersistentSessionStore().get(f"{session_id}:DemoAgent")
    assert saved["user_id"] == "u1"
    assert saved["memory_original_messages"]
