"""Explicit Plan-and-Execute graph for itinerary planning.

The Java agent combines ReAct, PlanNotebook, planner and review tools.  This
graph keeps the same tool boundary but makes the durable stages, remediation
limit, and recovery state visible to Python callers.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from backend.infrastructure.repositories import travel_order_repository
from backend.infrastructure.stores import search_candidate_store
from backend.memory.store import PersistentSessionStore
from backend.services.candidate_search import candidate_search_provider
from backend.services.plan_notebook import plan_notebook
from backend.services.policy_service import travel_policy_service
from backend.services.preference_service import preference_service
from backend.services.remediation_service import planning_fingerprint, remediation_service
from backend.services.runtime_events import emit_event
from backend.services.tool_result_side_effects import build_search_candidates
from backend.tools.live import query_weather
from backend.tools.planner import plan_itinerary
from backend.tools.review import review_itinerary

if TYPE_CHECKING:
    from backend.agents.itinerary_plan import ItineraryPlanAgent


_CITY_PATTERN = (
    r"(北京|上海|广州|深圳|成都|杭州|重庆|武汉|西安|苏州|天津|南京|长沙|郑州|东莞|"
    r"青岛|沈阳|宁波|昆明|厦门|合肥|佛山|无锡|哈尔滨|济南|福州|大连|贵阳|太原|"
    r"南昌|南宁|石家庄|长春|呼和浩特|兰州|乌鲁木齐|海口|银川|西宁|拉萨)"
)
_TRIP_PATTERN = re.compile(
    rf"(?P<origin>{_CITY_PATTERN})\s*(?:到|去|前往|飞往|赴|至|→|—|~|-)\s*(?P<destination>{_CITY_PATTERN})"
)
_DATE_PATTERN = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_TASKS = [
    "准备用户偏好与差旅约束",
    "组合并筛选交通住宿方案",
    "审核行程方案",
    "修复审核问题",
    "整理最终方案",
]
_MAX_REPAIRS = 2
_PLAN_RUN_STORE = PersistentSessionStore()


class PlanExecutionState(TypedDict, total=False):
    request: str
    original_question: str
    user_id: str
    session_id: str
    origin: str
    departure_city: str
    destination: str
    departure_date: str
    return_date: str
    candidates: dict[str, Any]
    scores: dict[str, Any]
    news_summary: dict[str, Any] | str
    long_term_preferences: list[str]
    preferences: dict[str, Any]
    policy: dict[str, Any]
    weather_summary: str
    itinerary: dict[str, Any]
    review: dict[str, Any]
    repair_count: int
    plan_run_id: str
    plan_in_progress: bool
    next_plan_stage: str
    review_history: list[dict[str, Any]]
    repair_history: list[dict[str, Any]]
    remediation_plan: dict[str, Any]
    remediation_stop_reason: str
    excluded_transport_ids: list[str]
    excluded_hotel_ids: list[str]
    score_overrides: dict[str, float]
    initial_itinerary: dict[str, Any]
    final: str
    pending_interaction: dict[str, Any]
    trace: list[dict[str, Any]]


def build_itinerary_plan_graph(agent: ItineraryPlanAgent):
    graph = StateGraph(PlanExecutionState)
    graph.add_node("restore_plan", _restore_plan)
    graph.add_node("validate_input", _validate_input)
    graph.add_node("create_plan", _create_plan)
    graph.add_node("load_constraints", _load_constraints)
    graph.add_node("ensure_candidates", _ensure_candidates)
    graph.add_node("generate_proposals", _generate_proposals)
    graph.add_node("review_proposals", _review_proposals)
    graph.add_node("repair_plan", _repair_plan)
    graph.add_node("render_plan", lambda state: _render_plan(agent, state))
    graph.add_edge(START, "restore_plan")
    graph.add_conditional_edges(
        "restore_plan",
        _after_restore,
        {
            "validate_input": "validate_input",
            "create_plan": "create_plan",
            "load_constraints": "load_constraints",
            "ensure_candidates": "ensure_candidates",
            "generate_proposals": "generate_proposals",
            "review_proposals": "review_proposals",
            "repair_plan": "repair_plan",
            "render_plan": "render_plan",
        },
    )
    graph.add_conditional_edges(
        "validate_input",
        _after_validate,
        {"create_plan": "create_plan", "end": END},
    )
    graph.add_edge("create_plan", "load_constraints")
    graph.add_edge("load_constraints", "ensure_candidates")
    graph.add_edge("ensure_candidates", "generate_proposals")
    graph.add_conditional_edges(
        "generate_proposals",
        _after_generate,
        {"review_proposals": "review_proposals", "render_plan": "render_plan"},
    )
    graph.add_conditional_edges(
        "review_proposals",
        _after_review,
        {"repair_plan": "repair_plan", "render_plan": "render_plan"},
    )
    graph.add_conditional_edges(
        "repair_plan",
        _after_repair,
        {"generate_proposals": "generate_proposals", "render_plan": "render_plan"},
    )
    graph.add_edge("render_plan", END)
    return graph.compile()


def _plan_store_key(session_id: str) -> str:
    return f"plan-run:{session_id}:ItineraryPlanAgent"


def _restore_plan(state: PlanExecutionState) -> PlanExecutionState:
    session_id = str(state.get("session_id") or "")
    if not session_id:
        return _fresh_plan_state(state)
    saved = _PLAN_RUN_STORE.get(_plan_store_key(session_id))
    if not saved or not saved.get("plan_in_progress"):
        return _fresh_plan_state(state)
    same_request = (
        not state.get("original_question")
        or state.get("continuation")
        or saved.get("original_question") == state.get("original_question")
    )
    if not same_request:
        _PLAN_RUN_STORE.delete(_plan_store_key(session_id))
        return _fresh_plan_state(state)
    # Invocation metadata may be newer, but persisted graph-control fields must
    # win over a stale completed Agent checkpoint from an earlier turn.
    invocation_keys = {
        "request",
        "original_question",
        "user_id",
        "session_id",
        "messages",
        "continuation",
        "deadline_at",
        "request_id",
    }
    restored = {
        **saved,
        **{
            key: value
            for key, value in state.items()
            if key in invocation_keys and value not in (None, "", [], {})
        },
    }
    restored["trace"] = _trace(restored, "已恢复未完成的行程规划")
    _restore_notebook(restored)
    return restored


def _fresh_plan_state(state: PlanExecutionState) -> PlanExecutionState:
    fresh: PlanExecutionState = {
        "next_plan_stage": "validate_input",
        "plan_in_progress": False,
        "plan_run_id": "",
        "repair_count": 0,
        "review_history": [],
        "repair_history": [],
        "remediation_plan": {},
        "remediation_stop_reason": "",
        "excluded_transport_ids": [],
        "excluded_hotel_ids": [],
        "score_overrides": {},
        "initial_itinerary": {},
        "review": {},
        "itinerary": {},
        "pending_interaction": {},
        "final": "",
    }
    # Direct graph callers may intentionally inject candidates/scores.  Do not
    # carry them from a completed Agent checkpoint into an unrelated new run.
    if not state.get("plan_run_id") and isinstance(state.get("candidates"), dict):
        fresh["candidates"] = state["candidates"]
    elif state.get("next_plan_stage") == "completed":
        fresh["candidates"] = {}
        fresh["scores"] = {}
    return fresh


def _after_restore(state: PlanExecutionState) -> str:
    stage = str(state.get("next_plan_stage") or "validate_input")
    return (
        stage
        if stage
        in {
            "validate_input",
            "create_plan",
            "load_constraints",
            "ensure_candidates",
            "generate_proposals",
            "review_proposals",
            "repair_plan",
            "render_plan",
        }
        else "validate_input"
    )


def _checkpoint(
    state: PlanExecutionState, updates: dict[str, Any], next_stage: str
) -> PlanExecutionState:
    merged = {**state, **updates, "next_plan_stage": next_stage, "plan_in_progress": True}
    session_id = str(merged.get("session_id") or "")
    if session_id:
        _PLAN_RUN_STORE.save(_plan_store_key(session_id), merged)
    return {**updates, "next_plan_stage": next_stage, "plan_in_progress": True}


def _validate_input(state: PlanExecutionState) -> PlanExecutionState:
    request = str(state.get("original_question") or state.get("request") or "")
    user_id = str(state.get("user_id") or "u_001")
    origin, destination = _extract_cities(request)
    departure_date, return_date = _extract_dates(request)
    latest_order = next(iter(travel_order_repository.list_by_user(user_id)), None)
    origin = _first_text(
        state.get("origin"),
        state.get("departure_city"),
        origin,
        getattr(latest_order, "departure_city", None),
    )
    destination = _first_text(
        state.get("destination"), destination, getattr(latest_order, "destination", None)
    )
    departure_date = _first_text(
        state.get("departure_date"), departure_date, getattr(latest_order, "departure_date", None)
    )
    return_date = _first_text(
        state.get("return_date"), return_date, getattr(latest_order, "return_date", None)
    )
    missing = [
        label
        for label, value in (
            ("出发地", origin),
            ("目的地", destination),
            ("出发日期", departure_date),
            ("返程日期", return_date),
        )
        if not value
    ]
    if missing:
        return {
            "pending_interaction": {"ui_type": "form", "fields": missing},
            "final": f"请补充{'、'.join(missing)}后我再继续规划。",
            "trace": _trace(state, "等待行程要素补全"),
        }
    updates = {
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "return_date": return_date,
        "repair_count": int(state.get("repair_count") or 0),
        "plan_run_id": str(state.get("plan_run_id") or f"plan_{uuid4().hex}"),
        "review_history": list(state.get("review_history") or []),
        "repair_history": list(state.get("repair_history") or []),
        "excluded_transport_ids": list(state.get("excluded_transport_ids") or []),
        "excluded_hotel_ids": list(state.get("excluded_hotel_ids") or []),
        "score_overrides": dict(state.get("score_overrides") or {}),
        "trace": _trace(state, "行程要素校验完成"),
    }
    return _checkpoint(state, updates, "create_plan")


def _after_validate(state: PlanExecutionState) -> str:
    return "end" if state.get("pending_interaction") else "create_plan"


def _create_plan(state: PlanExecutionState) -> PlanExecutionState:
    plan_notebook.create(str(state.get("session_id") or "default"), "行程规划", _TASKS)
    # A new run must not inherit an unrelated review from an earlier trip in
    # the same conversation.  Review history for this run lives in graph state.
    from backend.infrastructure.stores import review_result_store

    if state.get("session_id"):
        review_result_store.clear(str(state["session_id"]))
    return _checkpoint(
        state,
        {"trace": _trace(state, "已创建 PlanNotebook")},
        "load_constraints",
    )


def _load_constraints(state: PlanExecutionState) -> PlanExecutionState:
    user_id, destination = str(state.get("user_id") or "u_001"), str(state["destination"])
    preference_payload = state.get("preferences")
    if not isinstance(preference_payload, dict) or not preference_payload:
        preference_payload = preference_service.get(user_id).get("preferences", {})
    else:
        preference_payload = dict(preference_payload)
    if state.get("long_term_preferences"):
        preference_payload["long_term_memory"] = state["long_term_preferences"]
    policy_payload = state.get("policy") if isinstance(state.get("policy"), dict) else None
    if policy_payload is None:
        try:
            policy_payload = travel_policy_service.get_policy(user_id, destination)
        except Exception:  # noqa: BLE001 - optional policy lookup must not stop planning
            policy_payload = None
    weather_summary = state.get("weather_summary")
    if not weather_summary:
        try:
            weather = _tool(
                agent_name="ItineraryPlanAgent",
                tool_obj=query_weather,
                payload={"city": destination, "date": state["departure_date"]},
            )
            weather_summary = (
                (
                    weather.get("summary")
                    or weather.get("message")
                    or json.dumps(weather, ensure_ascii=False)
                )
                if isinstance(weather, dict)
                else str(weather or "")
            )
        except Exception:  # noqa: BLE001 - optional source degrades independently
            weather_summary = ""
    plan_notebook.update_task(str(state.get("session_id") or "default"), 0, "done")
    updates = {
        "preferences": preference_payload,
        "policy": policy_payload or {},
        "weather_summary": weather_summary,
        "trace": _trace(state, "已加载偏好、政策与天气约束"),
    }
    return _checkpoint(state, updates, "ensure_candidates")


def _ensure_candidates(state: PlanExecutionState) -> PlanExecutionState:
    candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else None
    if not _has_plannable_candidates(candidates):
        stored = build_search_candidates(
            str(state.get("user_id") or "u_001"),
            str(state["origin"]),
            str(state["destination"]),
            str(state["departure_date"]),
            str(state["return_date"]),
        )
        candidates = _merge_candidates(candidates, stored)
    search_result = None
    if not _has_plannable_candidates(candidates):
        search_result = candidate_search_provider.search(
            user_id=str(state.get("user_id") or "u_001"),
            origin=str(state["origin"]),
            destination=str(state["destination"]),
            departure_date=str(state["departure_date"]),
            return_date=str(state["return_date"]),
        )
        candidates = _merge_candidates(candidates, search_result.get("candidates"))
    if candidates:
        search_candidate_store.save(
            str(state.get("user_id") or "u_001"),
            json.dumps(candidates, ensure_ascii=False),
            str(state["origin"]),
            str(state["destination"]),
            str(state["departure_date"]),
            str(state["return_date"]),
        )
    summary = "候选数据已准备"
    if search_result is not None:
        ok_count = sum(1 for item in search_result.get("calls") or [] if item.get("ok"))
        summary = f"已执行候选搜索（成功 {ok_count}/{len(search_result.get('calls') or [])}）"
    updates = {"candidates": candidates or {}, "trace": _trace(state, summary)}
    return _checkpoint(state, updates, "generate_proposals")


def _generate_proposals(state: PlanExecutionState) -> PlanExecutionState:
    candidates = state.get("candidates")
    if isinstance(candidates, dict) and candidates:
        search_candidate_store.save(
            str(state.get("user_id") or "u_001"),
            json.dumps(candidates, ensure_ascii=False),
            str(state["origin"]),
            str(state["destination"]),
            str(state["departure_date"]),
            str(state["return_date"]),
        )
    result = _tool(
        agent_name="ItineraryPlanAgent",
        tool_obj=plan_itinerary,
        payload={
            "user_id": str(state.get("user_id") or "u_001"),
            "origin": state["origin"],
            "destination": state["destination"],
            "departure_date": state["departure_date"],
            "return_date": state["return_date"],
            "preferences": json.dumps(state.get("preferences") or {}, ensure_ascii=False),
            "scores": _json_or_none(state.get("scores")),
            "policy": json.dumps(state.get("policy") or {}, ensure_ascii=False),
            "weather_summary": state.get("weather_summary") or None,
            "excluded_transport_ids": json.dumps(
                state.get("excluded_transport_ids") or [],
                ensure_ascii=False,
            ),
            "excluded_hotel_ids": json.dumps(
                state.get("excluded_hotel_ids") or [],
                ensure_ascii=False,
            ),
            "score_overrides": json.dumps(
                state.get("score_overrides") or {},
                ensure_ascii=False,
            ),
            "repair_round": int(state.get("repair_count") or 0),
        },
    )
    session_id = str(state.get("session_id") or "default")
    plan_notebook.update_task(session_id, 1, "done" if result.get("ok") else "abandoned")
    updates: dict[str, Any] = {
        "itinerary": result,
        "trace": _trace(state, "已生成并排序候选方案"),
    }
    if not state.get("initial_itinerary") and result.get("ok"):
        updates["initial_itinerary"] = result
    next_stage = "review_proposals" if result.get("ok") else "render_plan"
    return _checkpoint(state, updates, next_stage)


def _after_generate(state: PlanExecutionState) -> str:
    return "review_proposals" if (state.get("itinerary") or {}).get("ok") else "render_plan"


def _review_proposals(state: PlanExecutionState) -> PlanExecutionState:
    itinerary = state.get("itinerary") or {}
    if not itinerary.get("ok"):
        return {"review": {}, "trace": _trace(state, "规划失败，跳过审核")}
    review = _tool(
        agent_name="ItineraryPlanAgent",
        tool_obj=review_itinerary,
        payload={
            "origin": state["origin"],
            "destination": state["destination"],
            "departure_date": state["departure_date"],
            "policy": json.dumps(state.get("policy") or {}, ensure_ascii=False),
            "weather_summary": state.get("weather_summary") or None,
            "news_summary": _json_or_none(state.get("news_summary")),
            "user_preferences": json.dumps(state.get("preferences") or {}, ensure_ascii=False),
            "previous_review": _json_or_none((state.get("review_history") or [None])[-1]),
        },
    )
    plan_notebook.update_task(str(state.get("session_id") or "default"), 2, "done")
    history = [*state.get("review_history", []), review]
    updates = {
        "review": review,
        "review_history": history,
        "trace": _trace(state, "已完成行程审核"),
    }
    next_stage = "repair_plan" if _should_repair(state, review) else "render_plan"
    return _checkpoint(state, updates, next_stage)


def _after_review(state: PlanExecutionState) -> str:
    return "repair_plan" if _should_repair(state, state.get("review") or {}) else "render_plan"


def _repair_plan(state: PlanExecutionState) -> PlanExecutionState:
    repair_count = int(state.get("repair_count") or 0) + 1
    plan_notebook.update_task(str(state.get("session_id") or "default"), 3, "in_progress")
    remediation = remediation_service.compile(
        state.get("review") or {},
        state.get("itinerary") or {},
        repair_count,
    )
    applied = remediation_service.apply(
        remediation,
        excluded_transport_ids=state.get("excluded_transport_ids") or [],
        excluded_hotel_ids=state.get("excluded_hotel_ids") or [],
        score_overrides=state.get("score_overrides") or {},
    )
    candidates = state.get("candidates") or {}
    before = planning_fingerprint(
        candidates,
        state.get("excluded_transport_ids") or [],
        state.get("excluded_hotel_ids") or [],
        state.get("score_overrides") or {},
    )
    search_results = []
    if applied["supplement_searches"]:
        result = candidate_search_provider.search(
            user_id=str(state.get("user_id") or "u_001"),
            origin=str(state["origin"]),
            destination=str(state["destination"]),
            departure_date=str(state["departure_date"]),
            return_date=str(state["return_date"]),
            requests=applied["supplement_searches"],
        )
        search_results = result.get("calls") or []
        candidates = _merge_candidates(candidates, result.get("candidates"))
    after = planning_fingerprint(
        candidates,
        applied["excluded_transport_ids"],
        applied["excluded_hotel_ids"],
        applied["score_overrides"],
    )
    changed = before != after and bool(remediation.executable_actions)
    stop_reason = None
    if not remediation.executable_actions:
        stop_reason = "审核没有提供可安全执行的整改动作"
    elif not changed:
        stop_reason = "整改未改变候选集合、排除条件或评分，已停止无效循环"
    attempt = {
        "repair_round": repair_count,
        "actions": [action.model_dump() for action in remediation.actions],
        "before_fingerprint": before,
        "after_fingerprint": after,
        "changed": changed,
        "stop_reason": stop_reason,
        "search_results": search_results,
    }
    emit_event(
        "plan_repair",
        {
            "type": "plan_repair",
            "agentName": "ItineraryPlanAgent",
            "round": repair_count,
            "changed": changed,
            "actionCount": len(remediation.executable_actions),
            "stopReason": stop_reason,
        },
    )
    plan_notebook.update_task(str(state.get("session_id") or "default"), 3, "done")
    updates = {
        "repair_count": repair_count,
        "remediation_plan": remediation.model_dump(),
        "repair_history": [*state.get("repair_history", []), attempt],
        "excluded_transport_ids": applied["excluded_transport_ids"],
        "excluded_hotel_ids": applied["excluded_hotel_ids"],
        "score_overrides": applied["score_overrides"],
        "candidates": candidates,
        "remediation_stop_reason": stop_reason or "",
        "trace": _trace(
            state,
            f"第 {repair_count} 轮整改已执行" if changed else str(stop_reason),
        ),
    }
    next_stage = "generate_proposals" if changed else "render_plan"
    return _checkpoint(state, updates, next_stage)


def _after_repair(state: PlanExecutionState) -> str:
    return "render_plan" if state.get("remediation_stop_reason") else "generate_proposals"


def _render_plan(agent: ItineraryPlanAgent, state: PlanExecutionState) -> PlanExecutionState:
    itinerary, review = state.get("itinerary") or {}, state.get("review") or None
    fallback = _format_plan_result(itinerary, review)
    repair_history = state.get("repair_history") or []
    if repair_history:
        fallback += f"\n自动整改：已执行 {len(repair_history)} 轮。"
    if state.get("remediation_stop_reason"):
        fallback += f"\n整改停止原因：{state['remediation_stop_reason']}。"
    final = agent.render_execution_summary(state, fallback)
    session_id = str(state.get("session_id") or "default")
    plan_notebook.update_task(session_id, 4, "done")
    plan_notebook.finish(session_id)
    _PLAN_RUN_STORE.delete(_plan_store_key(session_id))
    return {
        "final": final,
        "plan_in_progress": False,
        "next_plan_stage": "completed",
        "trace": _trace(state, "已整理最终方案"),
    }


def _should_repair(state: PlanExecutionState, review: dict[str, Any]) -> bool:
    return bool(
        review.get("continue_remediation")
        and int(state.get("repair_count") or 0) < _MAX_REPAIRS
        and not state.get("remediation_stop_reason")
    )


def _has_plannable_candidates(candidates: dict[str, Any] | None) -> bool:
    if not isinstance(candidates, dict):
        return False
    transports = candidates.get("transport_options") or []
    hotels = candidates.get("hotel_options") or []
    if not transports or not hotels:
        return False
    directions = {str(item.get("direction") or "").lower() for item in transports}
    if directions.intersection(
        {"outbound", "departure", "depart", "go", "去程"}
    ) and directions.intersection({"inbound", "return", "back", "返程"}):
        return True
    # Provider results often carry route endpoints instead of an explicit direction.
    return len(transports) >= 2


def _merge_candidates(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    result = {
        "transport_options": list((left or {}).get("transport_options") or []),
        "hotel_options": list((left or {}).get("hotel_options") or []),
    }
    for field in ("transport_options", "hotel_options"):
        merged: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(
            [
                *result[field],
                *((right or {}).get(field) or []),
            ]
        ):
            if not isinstance(raw, dict):
                continue
            candidate_id = str(raw.get("id") or f"legacy_{field}_{index}")
            merged[candidate_id] = {**merged.get(candidate_id, {}), **raw, "id": candidate_id}
        result[field] = list(merged.values())
    return result


def _restore_notebook(state: PlanExecutionState) -> None:
    session_id = str(state.get("session_id") or "default")
    plan_notebook.create(session_id, "行程规划", _TASKS)
    stage_order = {
        "load_constraints": 0,
        "ensure_candidates": 0,
        "generate_proposals": 1,
        "review_proposals": 2,
        "repair_plan": 3,
        "render_plan": 4,
    }
    completed_before = stage_order.get(str(state.get("next_plan_stage") or ""), 0)
    for index in range(completed_before):
        plan_notebook.update_task(session_id, index, "done")


def _tool(*, agent_name: str, tool_obj: Any, payload: dict[str, Any]) -> Any:
    from backend.runtime.resilient_tool import ResilientToolHook

    return ResilientToolHook(agent_name).invoke(tool_obj, payload)


def _trace(state: PlanExecutionState, output: str) -> list[dict[str, str]]:
    return [*state.get("trace", []), {"agent": "ItineraryPlanAgent", "output": output}]


def _first_text(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _extract_cities(text: str) -> tuple[str | None, str | None]:
    match = _TRIP_PATTERN.search(text or "")
    if match:
        return match.group("origin"), match.group("destination")
    cities = [item.group(1) for item in re.finditer(_CITY_PATTERN, text or "")]
    return (cities[0], cities[1]) if len(cities) >= 2 else (None, None)


def _extract_dates(text: str) -> tuple[str | None, str | None]:
    dates = [item.replace("/", "-") for item in _DATE_PATTERN.findall(text or "")]
    return (
        (dates[0], dates[1]) if len(dates) >= 2 else ((dates[0], None) if dates else (None, None))
    )


def _json_or_none(value: Any) -> str | None:
    if value in (None, "", {}):
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _format_plan_result(result: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    if not result.get("ok"):
        return str(result.get("error") or "行程规划失败。")
    proposals = result.get("proposals", [])
    lines = [
        f"已完成 {result.get('origin') or '未知出发地'} → {result.get('destination') or '未知目的地'} 的行程规划，共生成 {len(proposals)} 套代表方案。"
    ]
    for index, proposal in enumerate(proposals[:3], start=1):
        metrics, scores = proposal.get("metrics") or {}, proposal.get("scores") or {}
        lines.append(
            f"{index}. {'、'.join(proposal.get('tags') or []) or '方案'}：总价 "
            f"{float(metrics.get('total_price', proposal.get('total_price', 0)) or 0):.0f}，"
            f"评分 {scores.get('overall', proposal.get('score', 0))}，政策违规 {len(proposal.get('policy_violations') or [])} 项"
        )
    if result.get("weather"):
        lines.append(f"天气参考：{result['weather']}")
    if review:
        verdict = {"pass": "通过", "warning": "有提醒", "fail": "未通过"}.get(
            review.get("verdict"), "未知"
        )
        lines.append(f"方案审核：{verdict}。")
    return "\n".join(lines)
