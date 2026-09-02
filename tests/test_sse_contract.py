from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api import routers
from backend.api.dependencies import current_user
from backend.api.main import app
from backend.core.continuation import is_continuation_message
from backend.memory.session import agent_session_store
from backend.services import chat_stream
from backend.services.runtime_events import emit_event
from backend.services.sse import event_agent_switch, event_message, event_suggestions


class _TravelStub:
    def __init__(self) -> None:
        self.resume_calls = []
        self.run_active_calls = []

    def interrupt_previous(self, session_id: str) -> bool:
        return False

    def run(self, message: str, user_id: str, session_id: str):
        emit_event(
            "plan_html",
            {"type": "plan_html", "html": "<main>方案</main>", "title": "差旅方案"},
        )
        return {
            "final": "已生成方案",
            "active_agent": "MasterAgent",
            "trace": [],
        }

    def run_active(self, message: str, user_id: str, session_id: str):
        self.run_active_calls.append((message, user_id, session_id))
        return {"final": "已由活跃 Agent 继续", "active_agent": "InfoAgent", "trace": []}

    def resume(
        self,
        session_id: str,
        answer,
        user_id: str,
        tool_use_id: str | None = None,
    ):
        self.resume_calls.append((session_id, answer, user_id, tool_use_id))
        return {"final": "已继续", "active_agent": "MasterAgent", "trace": []}


class _ChatStub:
    def __init__(self) -> None:
        self.user_messages = []

    def save_user_message(self, session_id: str, user_id: str, content: str) -> None:
        self.user_messages.append((session_id, user_id, content))

    def save_agent_message(self, *_args, **_kwargs):
        return SimpleNamespace(message_id="msg_real_001")


class _RecommendationStub:
    @staticmethod
    def generate(_question: str, _answer: str) -> list[str]:
        return ["查询我的差旅", "查看差旅政策"]


def _client(monkeypatch):
    travel = _TravelStub()
    chat = _ChatStub()
    monkeypatch.setattr(routers, "travel", travel)
    monkeypatch.setattr(routers, "chat_service", chat)
    monkeypatch.setattr(chat_stream, "chat_service", chat)
    monkeypatch.setattr(chat_stream, "question_recommendation_service", _RecommendationStub())
    app.dependency_overrides[current_user] = lambda: {"user_id": "u001", "admin": False}
    return TestClient(app), travel, chat


def test_text_and_structured_sse_payloads_match_frontend_contract():
    assert event_message("第一行\n第二行") == (
        "event: message\ndata: 第一行\ndata: 第二行\n\n"
    )
    assert event_agent_switch("MasterAgent") == (
        "event: agent-switch\ndata: MasterAgent\n\n"
    )
    assert event_suggestions(["问题一", "问题二"]) == (
        'event: suggestions\ndata: ["问题一", "问题二"]\n\n'
    )


def test_chat_streams_runtime_events_and_persisted_message_id(monkeypatch):
    client, _travel, chat = _client(monkeypatch)
    try:
        response = client.post("/api/chat/session-1", json={"message": "帮我规划行程"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: plan_html" in response.text
    assert 'data: {"type": "plan_html", "html": "<main>方案</main>"' in response.text
    assert "event: message\ndata: 已生成方案\n\n" in response.text
    assert "event: message_id\ndata: msg_real_001\n\n" in response.text
    assert 'event: suggestions\ndata: ["查询我的差旅", "查看差旅政策"]' in response.text
    assert chat.user_messages == [("session-1", "u001", "帮我规划行程")]


def test_respond_is_sse_and_passes_response_and_tool_use_id(monkeypatch):
    client, travel, chat = _client(monkeypatch)
    try:
        response = client.post(
            "/api/chat/respond",
            json={"sessionId": "session-2", "toolUseId": "tool-7", "response": "上海"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: message\ndata: 已继续\n\n" in response.text
    assert travel.resume_calls == [("session-2", "上海", "u001", "tool-7")]
    assert chat.user_messages == [("session-2", "u001", "上海")]


def test_confirm_is_sse_and_maps_approve_decision_to_true(monkeypatch):
    client, travel, _chat = _client(monkeypatch)
    try:
        response = client.post(
            "/api/chat/session-3/confirm",
            json={"decision": "approve"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert travel.resume_calls == [("session-3", True, "u001", None)]


def test_chat_exact_continuation_signal_reuses_active_agent(monkeypatch):
    client, travel, chat = _client(monkeypatch)
    monkeypatch.setattr(agent_session_store, "get_active_agent", lambda _session: "InfoAgent")
    try:
        response = client.post("/api/chat/session-active", json={"message": "确认"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "event: message\ndata: 已由活跃 Agent 继续\n\n" in response.text
    assert travel.run_active_calls == [("确认", "u001", "session-active")]
    assert chat.user_messages == [("session-active", "u001", "确认")]


def test_continuation_signals_are_exact_matches():
    assert is_continuation_message(" 确认 ") is True
    assert is_continuation_message("确认一下") is False
    assert is_continuation_message("规划上海行程") is False
