import json

from backend.core.state import TravelAgentState
from backend.infrastructure.bootstrap import bootstrap
from backend.infrastructure.stores import search_candidate_store
from backend.intent.router import intent_router
from backend.memory.store import InMemorySessionStore
from backend.rag.knowledge import travel_knowledge
from backend.services.order_service import _is_international, _order_url
from backend.services.policy_service import travel_policy_service
from backend.services.travel_service import TravelAgentService
from backend.tools.planner import plan_itinerary
from backend.workflow.graph import build_pipeline_graph
from backend.workflow.pipeline import route_after_fast


def test_policy_and_binary_knowledge_fallback():
    bootstrap()
    policy = travel_policy_service.get_policy("u_001", "上海")
    assert policy["cityTier"] == "一线"
    assert policy["flightClass"] == "经济舱/商务舱"

    evidence = travel_knowledge.retrieve("差旅酒店标准")
    assert evidence
    assert any(item["source"].endswith(".docx") for item in evidence)


def test_planner_builds_representative_proposals():
    search_candidate_store.save(
        "u_001",
        json.dumps(
            {
                "transport_options": [
                    {
                        "id": "T1",
                        "direction": "OUTBOUND",
                        "from": "北京",
                        "to": "上海",
                        "price": 600,
                        "duration": 120,
                        "arrive_time": "2026-09-01 10:00",
                    },
                    {
                        "id": "T2",
                        "direction": "INBOUND",
                        "from": "上海",
                        "to": "北京",
                        "price": 500,
                        "duration": 130,
                        "depart_time": "2026-09-03 15:00",
                    },
                ],
                "hotel_options": [{"id": "H1", "price": 300, "nights": 2}],
            },
            ensure_ascii=False,
        ),
    )
    result = plan_itinerary.invoke(
        {
            "user_id": "u_001",
            "origin": "北京",
            "destination": "上海",
            "departure_date": "2026-09-01",
            "return_date": "2026-09-03",
        }
    )
    assert result["candidate_count"] == 1
    # Java CandidateRanker merges identical representative picks and keeps
    # their four labels on the single physical proposal.
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["tags"] == ["综合最佳", "时间最短", "价格最低", "最符合偏好"]


def test_graph_compiles_and_routes_rule_hit():
    result = intent_router.route("查一下上海差旅政策")
    assert result is not None
    assert result.primary_intent == "policy_query"

    graph = build_pipeline_graph()
    state = graph.invoke(
        {
            "request": "查一下上海差旅政策",
            "original_question": "查一下上海差旅政策",
            "user_id": "u_001",
            "session_id": "test-session",
            "messages": [],
        }
    )
    assert state["active_agent"] == "MasterAgent"
    assert state["final"]


def test_active_agent_continuation_routes_like_java_pipeline():
    assert route_after_fast({"active_resume_agent": "QueryRewritingAgent"}) == "rewrite"
    assert route_after_fast({"active_resume_agent": "IntentRecognitionAgent"}) == "full_intent"
    assert route_after_fast({"continuation": True}) == "master_dispatch"


def test_my_travel_view_helpers_match_java_shapes():
    assert _is_international("日本东京") is True
    assert _is_international("上海") is False
    assert _order_url("tuniu", "TN-1") == "https://www.tuniu.com/order/detail/TN-1"
    assert _order_url("mock", "M-1") is None


def test_generic_ask_user_resume_rebuilds_tool_call_and_result():
    class FakeAgent:
        def __init__(self):
            self.received = None

        def invoke(self, state):
            self.received = state
            return {**state, "final": "已收到上海", "pending_interaction": {},
                    "active_agent": "InfoAgent", "trace": []}

    service = TravelAgentService(store=InMemorySessionStore())
    previous: TravelAgentState = {
        "session_id": "generic-resume",
        "user_id": "u_001",
        "request": "查询天气",
        "original_question": "查询天气",
        "messages": [],
        "pending_interaction": {
            "requires_user_input": True,
            "question": "请选择城市",
            "ui_type": "select",
            "options": ["北京", "上海"],
            "agent_name": "InfoAgent",
            "tool_name": "ask_user",
            "toolUseId": "tool-generic-1",
        },
    }
    service.store.save("generic-resume", previous)
    agent = FakeAgent()
    service._active_agent = lambda _name: agent

    result = service.resume("generic-resume", "上海", "u_001", "tool-generic-1")

    assert result["final"] == "已收到上海"
    assert agent.received is not None
    messages = agent.received["messages"]
    assert messages[-2].tool_calls[0]["id"] == "tool-generic-1"
    assert messages[-2].tool_calls[0]["name"] == "ask_user"
    assert messages[-1].tool_call_id == "tool-generic-1"
    assert messages[-1].content == "上海"
