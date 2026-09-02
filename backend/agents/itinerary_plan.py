from __future__ import annotations

import json
import re
from typing import Any

from backend.agents.base import BaseSubAgent
from backend.infrastructure.llm import invoke_text, strong_model_with_thinking
from backend.infrastructure.repositories import travel_order_repository
from backend.infrastructure.stores import search_candidate_store
from backend.runtime.resilient_tool import ResilientToolHook
from backend.services.plan_notebook import plan_notebook
from backend.services.policy_service import travel_policy_service
from backend.services.preference_service import preference_service
from backend.tools.apikey import tools as api_key_tools
from backend.tools.booking import read_tools as booking_read_tools
from backend.tools.live import query_weather
from backend.tools.live import tools as live_tools
from backend.tools.memory import tools as memory_tools
from backend.tools.order import read_tools as order_read_tools
from backend.tools.plan_html import tools as plan_html_tools
from backend.tools.plan_notebook import tools as plan_notebook_tools
from backend.tools.planner import plan_itinerary
from backend.tools.planner import tools as planner_tools
from backend.tools.policy import tools as policy_tools
from backend.tools.review import review_itinerary
from backend.tools.review import tools as review_tools
from backend.tools.skills import tools as skill_tools
from backend.tools.user_info import tools as user_info_tools
from backend.workflow.itinerary_plan_graph import build_itinerary_plan_graph

_CITY_PATTERN = (
    r"(北京|上海|广州|深圳|成都|杭州|重庆|武汉|西安|苏州|天津|南京|长沙|郑州|东莞|"
    r"青岛|沈阳|宁波|昆明|厦门|合肥|佛山|无锡|哈尔滨|济南|福州|大连|贵阳|太原|"
    r"南昌|南宁|石家庄|长春|呼和浩特|兰州|乌鲁木齐|海口|银川|西宁|拉萨)"
)
_TRIP_PATTERN = re.compile(
    rf"(?P<origin>{_CITY_PATTERN})\s*(?:到|去|前往|飞往|赴|至|→|—|~|-)\s*(?P<destination>{_CITY_PATTERN})"
)
_DATE_PATTERN = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")


class ItineraryPlanAgent(BaseSubAgent):
    prompt_file = "itinerary-plan-agent-system.md"
    model_factory = staticmethod(strong_model_with_thinking)
    max_iterations = 30

    def __init__(self):
        super().__init__(
            plan_notebook_tools()
            + planner_tools()
            + policy_tools()
            + review_tools()
            + live_tools()
            + api_key_tools()
            + booking_read_tools()
            + order_read_tools()
            + user_info_tools()
            + plan_html_tools()
            + memory_tools()
            + skill_tools()
        )
        self.execution_graph = build_itinerary_plan_graph(self)

    def render_execution_summary(self, state: dict[str, Any], fallback: str) -> str:
        """Use the thinking profile only to explain verified planning output.

        Tool calls and review decisions have already happened in the explicit
        graph, so this call cannot bypass the Plan/Booking permission boundary.
        """
        evidence = {
            "request": state.get("original_question") or state.get("request"),
            "itinerary": state.get("itinerary"),
            "review": state.get("review"),
            "initial_itinerary": state.get("initial_itinerary"),
            "repair_history": state.get("repair_history") or [],
            "remediation_stop_reason": state.get("remediation_stop_reason"),
        }
        user = (
            "请仅基于以下已验证的规划结果，生成简洁中文方案说明；不得虚构航班、价格、"
            "政策或预订动作。\n" + json.dumps(evidence, ensure_ascii=False)
        )
        return invoke_text(self.model, self.prompt, user, fallback).strip() or fallback

    def fallback(self, state):
        request = str(state.get("original_question") or state.get("request") or "")
        user_id = state.get("user_id", "u_001")
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
            state.get("departure_date"),
            departure_date,
            getattr(latest_order, "departure_date", None),
        )
        return_date = _first_text(
            state.get("return_date"), return_date, getattr(latest_order, "return_date", None)
        )

        if not all((origin, destination, departure_date, return_date)):
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
            return {
                "final": f"请补充{'、'.join(missing)}后我再继续规划。",
                "pending_interaction": {"ui_type": "form", "fields": missing},
                "trace": [{"agent": "ItineraryPlanAgent", "output": "等待行程要素补全"}],
            }

        session_id = str(state.get("session_id") or "default")
        plan_notebook.create(
            session_id,
            "行程规划",
            [
                "准备用户偏好与差旅约束",
                "组合并筛选交通住宿方案",
                "审核行程方案",
                "整理最终方案",
            ],
        )

        candidates = state.get("candidates")
        if isinstance(candidates, dict) and candidates:
            search_candidate_store.save(
                user_id,
                json.dumps(candidates, ensure_ascii=False),
                origin,
                destination,
                departure_date,
                return_date,
            )

        preference_payload = state.get("preferences")
        if not isinstance(preference_payload, dict) or not preference_payload:
            preference_payload = preference_service.get(user_id).get("preferences", {})
        else:
            preference_payload = dict(preference_payload)
        plan_notebook.update_task(session_id, 0, "done")
        long_term_preferences = state.get("long_term_preferences") or []
        if long_term_preferences:
            preference_payload["long_term_memory"] = long_term_preferences

        policy_payload = state.get("policy") if isinstance(state.get("policy"), dict) else None
        if policy_payload is None:
            try:
                policy_payload = travel_policy_service.get_policy(user_id, destination)
            except Exception:
                policy_payload = None

        weather_summary = state.get("weather_summary")
        if not weather_summary:
            try:
                weather = _invoke_tool(query_weather, {"city": destination, "date": departure_date})
                if isinstance(weather, dict):
                    weather_summary = (
                        weather.get("summary")
                        or weather.get("message")
                        or json.dumps(weather, ensure_ascii=False)
                    )
                elif weather:
                    weather_summary = str(weather)
            except Exception:
                weather_summary = None

        result = _invoke_tool(
            plan_itinerary,
            {
                "user_id": user_id,
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "return_date": return_date,
                "preferences": json.dumps(preference_payload or {}, ensure_ascii=False),
                "scores": _stringify_json(state.get("scores")),
                "policy": json.dumps(policy_payload, ensure_ascii=False)
                if policy_payload
                else None,
                "weather_summary": weather_summary,
            },
        )
        plan_notebook.update_task(session_id, 1, "done")
        review = None
        if result.get("ok"):
            review = _invoke_tool(
                review_itinerary,
                {
                    "origin": origin,
                    "destination": destination,
                    "departure_date": departure_date,
                    "policy": json.dumps(policy_payload, ensure_ascii=False)
                    if policy_payload
                    else None,
                    "weather_summary": weather_summary,
                    "news_summary": _stringify_json(state.get("news_summary")),
                    "user_preferences": json.dumps(preference_payload or {}, ensure_ascii=False),
                },
            )
            plan_notebook.update_task(session_id, 2, "done")
        else:
            plan_notebook.update_task(session_id, 2, "abandoned")
        plan_notebook.update_task(session_id, 3, "done")
        plan_notebook.finish(session_id)
        return {
            "final": _format_plan_result(result, review),
            "itinerary": result,
            "review": review,
            "trace": [{"agent": "ItineraryPlanAgent", "output": "确定性行程规划"}],
        }


def _first_text(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _extract_cities(text: str) -> tuple[str | None, str | None]:
    match = _TRIP_PATTERN.search(text or "")
    if match:
        return match.group("origin"), match.group("destination")
    cities = [item.group(1) for item in re.finditer(_CITY_PATTERN, text or "")]
    if len(cities) >= 2:
        return cities[0], cities[1]
    return None, None


def _extract_dates(text: str) -> tuple[str | None, str | None]:
    dates = [item.replace("/", "-") for item in _DATE_PATTERN.findall(text or "")]
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], None
    return None, None


def _stringify_json(value) -> str | None:
    if value in (None, "", {}):
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _invoke_tool(tool_obj: Any, payload: dict[str, Any]) -> Any:
    return ResilientToolHook("ItineraryPlanAgent").invoke(tool_obj, payload)


def _format_plan_result(result: dict, review: dict | None = None) -> str:
    if not result.get("ok"):
        return str(result.get("error") or "行程规划失败。")
    proposals = result.get("proposals", [])
    origin = result.get("origin") or "未知出发地"
    destination = result.get("destination") or "未知目的地"
    lines = [f"已完成 {origin} → {destination} 的行程规划，共生成 {len(proposals)} 套代表方案。"]
    for index, proposal in enumerate(proposals[:3], start=1):
        tags = "、".join(proposal.get("tags") or [])
        metrics, scores = proposal.get("metrics") or {}, proposal.get("scores") or {}
        lines.append(
            f"{index}. {tags or '方案'}：总价 {float(metrics.get('total_price', proposal.get('total_price', 0)) or 0):.0f}，"
            f"评分 {scores.get('overall', proposal.get('score', 0))}，政策违规 {len(proposal.get('policy_violations') or [])} 项"
        )
    if result.get("weather"):
        lines.append(f"天气参考：{result['weather']}")
    if review:
        verdict = {"pass": "通过", "warning": "有提醒", "fail": "未通过"}.get(
            review.get("verdict"),
            str(review.get("verdict") or "未知"),
        )
        lines.append(f"方案审核：{verdict}。")
        issues = (review.get("arbitration") or {}).get("issues") or []
        if issues:
            lines.append("注意事项：" + "；".join(str(item) for item in issues[:3]))
    repair_history = result.get("repair_history") or []
    if repair_history:
        lines.append(f"自动整改：已执行 {len(repair_history)} 轮。")
    return "\n".join(lines)


itinerary_plan_agent = ItineraryPlanAgent()
