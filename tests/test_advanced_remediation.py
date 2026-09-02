import json

import pytest

from backend.core.request_context import bind_context
from backend.domain.planning import RemediationAction, RemediationPlan
from backend.infrastructure.stores import search_candidate_store
from backend.services.candidate_search import TuniuCliCandidateSearchProvider
from backend.services.remediation_service import remediation_service
from backend.tools import review as review_module
from backend.tools.planner import plan_itinerary
from backend.workflow import itinerary_plan_graph as plan_graph


def _candidates():
    return {
        "transport_options": [
            {
                "id": "OUT_BAD",
                "direction": "outbound",
                "type": "flight",
                "price": 300,
                "departure_time": "2099-07-15T23:00",
                "arrival_time": "2099-07-16T01:00",
            },
            {
                "id": "OUT_GOOD",
                "direction": "outbound",
                "type": "train",
                "price": 400,
                "departure_time": "2099-07-15T08:00",
                "arrival_time": "2099-07-15T10:00",
            },
            {
                "id": "BACK",
                "direction": "return",
                "type": "train",
                "price": 400,
                "departure_time": "2099-07-16T18:00",
                "arrival_time": "2099-07-16T20:00",
            },
        ],
        "hotel_options": [
            {"id": "H_EXPENSIVE", "name": "高价酒店", "price": 900, "nights": 1},
            {"id": "H_GOOD", "name": "合规酒店", "price": 400, "nights": 1},
        ],
    }


def test_remediation_compiles_java_style_item_to_candidate_exclusion():
    itinerary = {
        "proposals": [
            {
                "proposal_id": "P1",
                "candidate_ids": {"outbound": "OUT_BAD", "hotel": "H_EXPENSIVE", "return": "BACK"},
                "outbound": {"id": "OUT_BAD"},
                "hotel": {"id": "H_EXPENSIVE"},
                "return": {"id": "BACK"},
            }
        ]
    }
    review = {
        "verdict": "fail",
        "continue_remediation": True,
        "arbitration": {
            "remediation_priority": [
                {
                    "priority": 1,
                    "issue": "酒店超预算",
                    "action": "剔除高价酒店",
                    "hard_constraint": True,
                    "fixable_by_replanning": True,
                    "affected_proposals": ["P1"],
                }
            ]
        },
    }
    plan = remediation_service.compile(review, itinerary, 1)
    applied = remediation_service.apply(
        plan,
        excluded_transport_ids=[],
        excluded_hotel_ids=[],
        score_overrides={},
    )
    assert applied["excluded_hotel_ids"] == ["H_EXPENSIVE"]
    assert plan.actions[0].action_type == "exclude"


def test_planner_explicit_exclusion_removes_candidate_before_combination():
    user_id = "repair-planner-user"
    search_candidate_store.save(user_id, json.dumps(_candidates(), ensure_ascii=False))
    result = plan_itinerary.invoke(
        {
            "user_id": user_id,
            "origin": "北京",
            "destination": "杭州",
            "departure_date": "2099-07-15",
            "return_date": "2099-07-16",
            "excluded_transport_ids": json.dumps(["OUT_BAD"]),
            "excluded_hotel_ids": json.dumps(["H_EXPENSIVE"]),
            "score_overrides": "{}",
            "repair_round": 1,
        }
    )
    assert result["ok"] is True
    assert result["candidate_count"] == 1
    assert result["proposals"][0]["candidate_ids"] == {
        "outbound": "OUT_GOOD",
        "hotel": "H_GOOD",
        "return": "BACK",
    }
    assert result["meta"]["repair_round"] == 1


def test_apply_remediation_detects_idempotent_second_application():
    plan = RemediationPlan(
        actions=[
            RemediationAction(
                action_type="exclude",
                target_type="hotel",
                candidate_ids=["H1"],
                reason="酒店超预算",
                priority=1,
            )
        ]
    )
    first = remediation_service.apply(
        plan,
        excluded_transport_ids=[],
        excluded_hotel_ids=[],
        score_overrides={},
    )
    second = remediation_service.apply(
        plan,
        excluded_transport_ids=[],
        excluded_hotel_ids=first["excluded_hotel_ids"],
        score_overrides={},
    )
    assert first == second


class _Agent:
    @staticmethod
    def render_execution_summary(_state, fallback: str) -> str:
        return fallback


def test_graph_stops_when_supplement_search_makes_no_progress(monkeypatch):
    monkeypatch.setattr(plan_graph.travel_order_repository, "list_by_user", lambda _: [])
    monkeypatch.setattr(plan_graph.preference_service, "get", lambda _: {"preferences": {}})
    monkeypatch.setattr(plan_graph.travel_policy_service, "get_policy", lambda *_: {})
    monkeypatch.setattr(
        plan_graph,
        "_tool",
        lambda **kwargs: (
            {"summary": "晴"}
            if getattr(kwargs["tool_obj"], "name", "") == "query_weather"
            else {
                "ok": True,
                "origin": "北京",
                "destination": "杭州",
                "proposals": [
                    {
                        "proposal_id": "P1",
                        "candidate_ids": {"outbound": "T1", "hotel": "H1", "return": "T2"},
                        "tags": ["综合最佳"],
                        "metrics": {"total_price": 1000},
                        "scores": {"overall": 90},
                    }
                ],
            }
            if getattr(kwargs["tool_obj"], "name", "") == "plan_itinerary"
            else {
                "verdict": "fail",
                "continue_remediation": True,
                "arbitration": {
                    "remediation_priority": [
                        {
                            "priority": 1,
                            "issue": "需要新候选",
                            "action": "补搜早班航班",
                            "action_type": "supplement_search",
                            "target_type": "flight",
                            "constraints": {
                                "direction": "outbound",
                                "departure_time": "07:00-09:00",
                            },
                            "fixable_by_replanning": True,
                            "affected_proposals": ["P1"],
                        }
                    ]
                },
            }
        ),
    )
    monkeypatch.setattr(
        plan_graph.candidate_search_provider,
        "search",
        lambda **_: {
            "ok": True,
            "calls": [{"kind": "flight", "ok": True}],
            "candidates": _candidates(),
        },
    )
    graph = plan_graph.build_itinerary_plan_graph(_Agent())
    result = graph.invoke(
        {
            "request": "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程",
            "original_question": "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程",
            "user_id": "no-progress",
            "session_id": "no-progress-session",
            "trace": [],
            "candidates": _candidates(),
        }
    )
    assert result["repair_count"] == 1
    assert "停止无效循环" in result["remediation_stop_reason"]
    assert len(result["repair_history"]) == 1


def test_graph_hard_limits_remediation_to_two_rounds(monkeypatch):
    calls = {"plan": 0, "review": 0}
    monkeypatch.setattr(plan_graph.travel_order_repository, "list_by_user", lambda _: [])
    monkeypatch.setattr(plan_graph.preference_service, "get", lambda _: {"preferences": {}})
    monkeypatch.setattr(plan_graph.travel_policy_service, "get_policy", lambda *_: {})

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
                "proposals": [
                    {
                        "proposal_id": "P1",
                        "candidate_ids": {
                            "outbound": "OUT_BAD",
                            "hotel": "H_EXPENSIVE",
                            "return": "BACK",
                        },
                        "tags": ["综合最佳"],
                        "metrics": {"total_price": 1200},
                        "scores": {"overall": 70},
                    }
                ],
            }
        if name == "review_itinerary":
            calls["review"] += 1
            candidate_id, target = (
                ("OUT_BAD", "transport") if calls["review"] == 1 else ("H_EXPENSIVE", "hotel")
            )
            return {
                "verdict": "fail",
                "continue_remediation": True,
                "arbitration": {
                    "remediation_priority": [
                        {
                            "priority": 1,
                            "issue": "仍有硬约束问题",
                            "action": "剔除违规候选",
                            "action_type": "exclude",
                            "target_type": target,
                            "candidate_ids": [candidate_id],
                            "affected_proposals": ["P1"],
                            "hard_constraint": True,
                            "fixable_by_replanning": True,
                        }
                    ]
                },
            }
        raise AssertionError(name)

    monkeypatch.setattr(plan_graph, "_tool", fake_tool)
    graph = plan_graph.build_itinerary_plan_graph(_Agent())
    result = graph.invoke(
        {
            "request": "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程",
            "original_question": "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程",
            "user_id": "max-two",
            "session_id": "max-two-session",
            "trace": [],
            "candidates": _candidates(),
        }
    )
    assert calls == {"plan": 3, "review": 3}
    assert result["repair_count"] == 2
    assert len(result["repair_history"]) == 2


def test_graph_restores_saved_plan_from_next_incomplete_stage(monkeypatch):
    calls = {"plan": 0, "review": 0}
    session_id = "resume-plan-session"
    request = "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程"
    saved = {
        "request": request,
        "original_question": request,
        "user_id": "resume-user",
        "session_id": session_id,
        "origin": "北京",
        "destination": "杭州",
        "departure_date": "2099-07-15",
        "return_date": "2099-07-16",
        "candidates": _candidates(),
        "preferences": {},
        "policy": {},
        "repair_count": 0,
        "repair_history": [],
        "review_history": [],
        "excluded_transport_ids": [],
        "excluded_hotel_ids": [],
        "score_overrides": {},
        "trace": [{"agent": "ItineraryPlanAgent", "output": "中断前已准备候选"}],
        "plan_in_progress": True,
        "next_plan_stage": "generate_proposals",
        "plan_run_id": "plan_resume_test",
    }
    plan_graph._PLAN_RUN_STORE.save(plan_graph._plan_store_key(session_id), saved)

    def fake_tool(*, tool_obj, **_kwargs):
        name = getattr(tool_obj, "name", "")
        if name == "plan_itinerary":
            calls["plan"] += 1
            return {"ok": True, "origin": "北京", "destination": "杭州", "proposals": []}
        if name == "review_itinerary":
            calls["review"] += 1
            return {"verdict": "pass", "continue_remediation": False}
        raise AssertionError(name)

    monkeypatch.setattr(plan_graph, "_tool", fake_tool)
    result = plan_graph.build_itinerary_plan_graph(_Agent()).invoke(
        {
            "request": request,
            "original_question": request,
            "user_id": "resume-user",
            "session_id": session_id,
        }
    )
    assert calls == {"plan": 1, "review": 1}
    assert any("恢复未完成" in item["output"] for item in result["trace"])
    assert plan_graph._PLAN_RUN_STORE.get(plan_graph._plan_store_key(session_id)) == {}


def test_cli_search_provider_rejects_untrusted_constraint_text():
    provider = TuniuCliCandidateSearchProvider()
    with pytest.raises(ValueError, match="非法字符"):
        provider._commands(
            origin="北京",
            destination="杭州",
            departure_date="2099-07-15",
            return_date="2099-07-16",
            target_type="hotel",
            constraints={"keyword": "亚朵'; whoami"},
        )


def test_real_graph_excludes_failed_hotel_and_reaudits(monkeypatch):
    user_id, session_id = "real-repair-user", "real-repair-session"
    candidates = {
        "transport_options": [
            {
                "id": "OUT",
                "direction": "outbound",
                "type": "train",
                "origin": "北京",
                "destination": "杭州",
                "price": 300,
                "departure_time": "2099-07-15T08:00",
                "arrival_time": "2099-07-15T10:00",
                "cabin_class": "二等座",
            },
            {
                "id": "BACK",
                "direction": "return",
                "type": "train",
                "origin": "杭州",
                "destination": "北京",
                "price": 300,
                "departure_time": "2099-07-16T18:00",
                "arrival_time": "2099-07-16T20:00",
                "cabin_class": "二等座",
            },
        ],
        "hotel_options": [
            {
                "id": "H_BAD",
                "name": "超标酒店",
                "price": 900,
                "nights": 1,
                "star_rating": 5,
                "distance_to_dest_km": 1,
            },
            {
                "id": "H_GOOD",
                "name": "合规酒店",
                "price": 400,
                "nights": 1,
                "star_rating": 4,
                "distance_to_dest_km": 2,
            },
        ],
    }
    scores = {
        "transport_scores": {"OUT": 50, "BACK": 50},
        "hotel_scores": {"H_BAD": 100, "H_GOOD": 0},
    }
    request = "请规划北京到杭州 2099-07-15 至 2099-07-16 的行程"
    arbitration_calls = {"count": 0}

    def fake_arbitrate(_results, _previous):
        arbitration_calls["count"] += 1
        if arbitration_calls["count"] == 1:
            return review_module.ReviewResult(
                "arbitrator",
                "fail",
                ["酒店超预算"],
                ["剔除超标酒店"],
                {
                    "recommended_proposal_id": "P1",
                    "remediation_priority": [
                        {
                            "priority": 1,
                            "issue": "酒店超预算",
                            "action": "剔除超标酒店",
                            "action_type": "exclude",
                            "target_type": "hotel",
                            "candidate_ids": ["H_BAD"],
                            "affected_proposals": [],
                            "hard_constraint": True,
                            "fixable_by_replanning": True,
                        }
                    ],
                },
            )
        return review_module.ReviewResult(
            "arbitrator",
            "pass",
            [],
            [],
            {"recommended_proposal_id": "P1", "remediation_priority": []},
        )

    monkeypatch.setattr(review_module, "_subjective_fanout", lambda *_: [])
    monkeypatch.setattr(review_module, "_arbitrate", fake_arbitrate)
    with bind_context(user_id, session_id, "ItineraryPlanAgent"):
        result = plan_graph.build_itinerary_plan_graph(_Agent()).invoke(
            {
                "request": request,
                "original_question": request,
                "user_id": user_id,
                "session_id": session_id,
                "candidates": candidates,
                "scores": scores,
                "preferences": {"hotel": "优先舒适度"},
                "policy": {
                    "hotelLimit": 500,
                    "hotelStarLimit": 4,
                    "trainSeatClass": "二等座",
                    "advanceBookingDays": 3,
                },
                "weather_summary": "晴",
                "trace": [],
            }
        )
    assert result["repair_count"] == 1
    assert "H_BAD" in result["excluded_hotel_ids"]
    assert all(
        proposal["candidate_ids"]["hotel"] == "H_GOOD"
        for proposal in result["itinerary"]["proposals"]
    )
    assert len(result["review_history"]) == 2
