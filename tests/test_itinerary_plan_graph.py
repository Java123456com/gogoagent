from backend.workflow import itinerary_plan_graph as plan_graph


class _Agent:
    @staticmethod
    def render_execution_summary(_state, fallback: str) -> str:
        return fallback


def test_plan_graph_runs_explicit_stages_and_bounded_repair(monkeypatch):
    calls = {"plan": 0, "review": 0}

    monkeypatch.setattr(plan_graph.travel_order_repository, "list_by_user", lambda _: [])
    monkeypatch.setattr(
        plan_graph.preference_service, "get", lambda _: {"preferences": {"hotel": "近会场"}}
    )
    monkeypatch.setattr(
        plan_graph.travel_policy_service, "get_policy", lambda *_: {"hotelLimit": 600}
    )

    def fake_tool(*, tool_obj, **_kwargs):
        name = getattr(tool_obj, "name", "")
        if name == "query_weather":
            return {"summary": "晴"}
        if name == "plan_itinerary":
            calls["plan"] += 1
            return {
                "ok": True,
                "origin": "北京",
                "destination": "杭州",
                "weather": "晴",
                "proposals": [
                    {
                        "proposal_id": "P_TEST",
                        "tags": ["综合最佳"],
                        "candidate_ids": {"outbound": "T1", "hotel": "H1", "return": "T2"},
                        "total_price": 1200,
                        "score": 90,
                        "policy_violations": [],
                    }
                ],
            }
        if name == "review_itinerary":
            calls["review"] += 1
            return (
                {
                    "verdict": "fail",
                    "continue_remediation": True,
                    "arbitration": {
                        "remediation_priority": [
                            {
                                "priority": 1,
                                "issue": "去程时间不佳",
                                "action": "降低该候选评分",
                                "action_type": "rescore",
                                "target_type": "transport",
                                "candidate_ids": ["T1"],
                                "score_adjustments": {"T1": 0},
                                "affected_proposals": ["P_TEST"],
                                "hard_constraint": False,
                                "fixable_by_replanning": True,
                            }
                        ]
                    },
                }
                if calls["review"] == 1
                else {"verdict": "pass", "continue_remediation": False}
            )
        raise AssertionError(name)

    monkeypatch.setattr(plan_graph, "_tool", fake_tool)
    graph = plan_graph.build_itinerary_plan_graph(_Agent())
    result = graph.invoke(
        {
            "request": "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程",
            "original_question": "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程",
            "user_id": "u_001",
            "session_id": "plan-graph-test",
            "trace": [],
            "candidates": {
                "transport_options": [
                    {"id": "T1", "direction": "outbound"},
                    {"id": "T2", "direction": "return"},
                ],
                "hotel_options": [{"id": "H1"}],
            },
        }
    )

    assert calls == {"plan": 2, "review": 2}
    assert result["repair_count"] == 1
    assert "已完成 北京 → 杭州" in result["final"]
    assert [item["output"] for item in result["trace"]][-1] == "已整理最终方案"


def test_plan_graph_pauses_when_required_input_is_missing():
    graph = plan_graph.build_itinerary_plan_graph(_Agent())
    result = graph.invoke(
        {"request": "帮我规划出差", "user_id": "u_001", "session_id": "plan-missing"}
    )

    assert result["pending_interaction"]["ui_type"] == "form"
    assert "出发地" in result["pending_interaction"]["fields"]
