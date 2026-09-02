from types import SimpleNamespace

from backend.agents.master import master_agent
from backend.memory.providers import BailianLongTermMemoryProvider
from backend.rag.providers import BailianKnowledgeProvider, _normalize_documents
from backend.tools.interaction import UserInteractionRequired, ask_user


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_bailian_memory_matches_agentscope_paths_and_payloads():
    calls = []

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        if url.endswith("/add"):
            return _Response({"requestId": "req-add", "memoryNodes": [{"memoryNodeId": "node-1"}]})
        return _Response({"memoryNodes": [{"content": "只坐高铁一等座"}]})

    provider = BailianLongTermMemoryProvider(
        api_key="dash-key",
        memory_library_id="lib-1",
        project_id="project-1",
        http_post=post,
    )
    assert provider.record("user-1", "只坐高铁一等座", "preference") == "node-1"
    assert provider.retrieve("user-1", limit=5, query="交通偏好") == ["只坐高铁一等座"]

    add_url, add_headers, add_payload, _ = calls[0]
    assert add_url == "https://dashscope.aliyuncs.com/api/v2/apps/memory/add"
    assert add_headers == {
        "Authorization": "Bearer dash-key",
        "Content-Type": "application/json",
    }
    assert add_payload["userId"] == "user-1"
    assert add_payload["memoryLibraryId"] == "lib-1"
    assert add_payload["messages"] == [{"role": "user", "content": "只坐高铁一等座"}]

    search_url, _, search_payload, _ = calls[1]
    assert search_url == "https://dashscope.aliyuncs.com/api/v2/apps/memory/memory_nodes/search"
    assert search_payload["userId"] == "user-1"
    assert search_payload["messages"] == [{"role": "user", "content": "交通偏好"}]
    assert search_payload["projectIds"] == ["project-1"]
    assert search_payload["topK"] == 5


def test_bailian_rag_normalizes_java_data_nodes():
    result = _normalize_documents({
        "requestId": "req-1",
        "data": {"nodes": [{"text": "杭州西湖", "score": 0.91,
                              "metadata": {"file": "attractions"}}]},
    })
    assert result == [{
        "content": "杭州西湖",
        "source": "bailian",
        "provider": "bailian",
        "score": 0.91,
        "metadata": {"file": "attractions"},
    }]


def test_bailian_rag_calls_java_retrieve_shape_with_signed_client():
    calls = []

    class Client:
        def retrieve_with_options(self, workspace_id, request, headers, runtime):
            calls.append((workspace_id, request, headers, runtime))
            return SimpleNamespace(body={"data": {"nodes": [{"text": "西湖", "score": 0.8}]}})

    provider = BailianKnowledgeProvider(
        access_key_id="ak",
        access_key_secret="sk",
        workspace_id="ws",
        index_id="idx",
        sdk_client=Client(),
    )
    assert provider.retrieve("杭州景点", limit=3)[0]["content"] == "西湖"
    workspace_id, request, headers, _ = calls[0]
    assert workspace_id == "ws"
    assert request.index_id == "idx"
    assert request.query == "杭州景点"
    assert request.dense_similarity_top_k == 3
    assert request.sparse_similarity_top_k == 3
    assert headers == {}


def test_master_registers_only_java_subagents():
    names = {tool.name for tool in master_agent.tools}
    assert {"itinerary_manage_agent", "itinerary_plan_agent", "info_agent", "booking_agent"} <= names
    assert "itinerary_review_agent" not in names
    assert "reimbursement_agent" not in names


def test_master_does_not_route_to_unimplemented_reimbursement_agent():
    result = master_agent.fallback({
        "request": "帮我报销发票",
        "intent_json": {"primary_intent": "reimbursement", "intents": []},
    })
    assert "尚未实现" in result["final"]
    assert all(item.get("agent") != "ReimbursementAgent" for item in result["trace"])


def test_ask_user_raises_suspend_signal():
    try:
        ask_user.invoke({"question": "请选择座位", "ui_type": "select", "options": ["靠窗"]})
    except UserInteractionRequired as signal:
        assert signal.payload["question"] == "请选择座位"
        assert signal.payload["options"] == ["靠窗"]
    else:  # pragma: no cover - assertion guard
        raise AssertionError("ask_user must suspend the agent")
