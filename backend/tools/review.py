"""Java-equivalent itinerary review orchestration.

The six objective dimensions are deterministic. Experience, resilience and
preference are temporary LLM assessors; their results are fanned out and then
arbitrated where Java ``ItineraryReviewTools`` does it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from backend.core.request_context import current_context
from backend.infrastructure.llm import stable_model
from backend.infrastructure.stores import itinerary_plan_store, review_result_store
from backend.prompts import load_static
from backend.services.policy_service import is_cabin_compliant

from ._common import current_user_id, tool

SOURCE_OBJECTIVE = "objective"
SOURCE_EXPERIENCE = "experience"
SOURCE_RESILIENCE = "resilience"
SOURCE_PREFERENCE = "preference"
SUBJECTIVE_SOURCES = (SOURCE_EXPERIENCE, SOURCE_RESILIENCE, SOURCE_PREFERENCE)
DIMENSION_LABELS = {
    SOURCE_OBJECTIVE: "客观审核（六维确定性校验）",
    SOURCE_EXPERIENCE: "差旅体验审核",
    SOURCE_RESILIENCE: "行程韧性审核",
    SOURCE_PREFERENCE: "偏好匹配审核",
}
HARD_CONSTRAINT_PREFIX = "[硬约束]"


@dataclass
class ReviewResult:
    source: str
    verdict: str
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    details: dict[str, Any] | None = None


class ReviewCollector:
    def __init__(self) -> None:
        self.checks: list[dict[str, str]] = []
        self.issues: list[str] = []
        self.suggestions: list[str] = []

    def check(self, dimension: str, status: str, detail: str) -> None:
        self.checks.append({"dimension": dimension, "status": status, "detail": detail})


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _hm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    elif " " in text:
        text = text.rsplit(" ", 1)[-1]
    return text[:5] if re.fullmatch(r"\d{1,2}:\d{2}.*", text) else text


def _minutes_between(left: Any, right: Any) -> int | None:
    try:
        left_hour, left_minute = (int(item) for item in str(left).split(":", 1))
        right_hour, right_minute = (int(item) for item in str(right).split(":", 1))
        return right_hour * 60 + right_minute - left_hour * 60 - left_minute
    except (TypeError, ValueError):
        return None


def _city_matches(actual: Any, approved: Any) -> bool:
    if actual is None or approved is None:
        return True
    left, right = str(actual), str(approved)
    return left in right or right in left


def _review_completeness(itinerary: dict, policy: dict, out: ReviewCollector) -> None:
    problems = []
    for key, label in (
        ("origin", "出发地（origin）未填写"),
        ("destination", "目的地（destination）未填写"),
        ("departure_date", "去程日期（departure_date）未填写"),
        ("return_date", "返程日期（return_date）未填写"),
    ):
        if _blank(itinerary.get(key)):
            problems.append(label)
    days = itinerary.get("days")
    if not days:
        problems.append("行程中无每日安排（days 为空）")
    else:
        if not any("outbound_transport" in day for day in days):
            problems.append("去程交通未安排")
        if not any("return_transport" in day for day in days):
            problems.append("返程交通未安排")
        missing_hotels = sum(1 for day in days[:-1] if "hotel" not in day)
        no_activities = sum(1 for day in days if not day.get("activities"))
        if missing_hotels:
            problems.append(f"{missing_hotels} 个夜晚未安排住宿")
        if no_activities:
            out.suggestions.append(f"有 {no_activities} 天无具体活动安排，建议补充")
    if problems:
        out.issues.extend(problems)
        out.check("行程完整性", "fail", "存在缺失项：" + "；".join(problems))
    else:
        out.check("行程完整性", "pass", "去程/返程交通、住宿、活动安排均完整")


def _activity_time(activity: Any) -> str | None:
    match = re.match(r"\s*(\d{1,2}:\d{2})", str(activity))
    return match.group(1) if match else None


def _review_timing(itinerary: dict, policy: dict, out: ReviewCollector) -> None:
    days = itinerary.get("days")
    if days is None:
        out.check("时间安排合理性", "warning", "无每日行程，无法校验时间")
        return
    problems: list[tuple[str, str]] = []
    dep_text, ret_text = itinerary.get("departure_date"), itinerary.get("return_date")
    dep, ret = _parse_date(dep_text), _parse_date(ret_text)
    if dep and ret:
        if ret <= dep:
            problems.append(("fail", f"返程日期 {ret_text} 不晚于去程日期 {dep_text}"))
        today = datetime.now().astimezone().date()
        if dep < today:
            problems.append(("warning", f"去程日期 {dep_text} 早于今天 {today.isoformat()}"))
        expected_nights = (ret - dep).days
        actual_nights = sum(_integer((day.get("hotel") or {}).get("nights")) or 0 for day in days)
        if actual_nights > 0 and actual_nights != expected_nights:
            problems.append(
                ("warning", f"住宿总晚数 {actual_nights} 与日期区间 {expected_nights} 晚不匹配")
            )
        advance = _integer(policy.get("advanceBookingDays"))
        booking_days = (dep - today).days
        if advance and advance > 0 and booking_days < advance:
            problems.append(
                ("warning", f"距出发仅 {booking_days} 天，政策要求提前 {advance} 天预订")
            )
    for index, day in enumerate(days):
        day_text = day.get("date")
        outbound = day.get("outbound_transport")
        returning = day.get("return_transport")
        activities = day.get("activities") or []
        if index == 0 and outbound and activities:
            arrival, activity = outbound.get("arrival"), _activity_time(activities[0])
            if arrival and activity:
                buffer = _minutes_between(arrival, activity)
                if buffer is None:
                    problems.append(
                        (
                            "warning",
                            f"{day_text} 时间格式无法解析（到达 {arrival} / 活动 {activity}），未能校验到达缓冲",
                        )
                    )
                elif 0 <= buffer < 60:
                    problems.append(
                        (
                            "warning",
                            f"{day_text} 到达时间 {arrival} 与首个活动 {activity} 之间缓冲不足 60 分钟（实际约 {buffer} 分钟）",
                        )
                    )
        if returning and activities:
            departure, activity = returning.get("departure"), _activity_time(activities[-1])
            if departure and activity:
                buffer = _minutes_between(activity, departure)
                if buffer is None:
                    problems.append(
                        (
                            "warning",
                            f"{day_text} 时间格式无法解析（活动 {activity} / 返程 {departure}），未能校验返程缓冲",
                        )
                    )
                elif buffer < 0:
                    problems.append(
                        (
                            "fail",
                            f"{day_text} 返程出发时间 {departure} 早于最后活动时间 {activity}，存在时间冲突",
                        )
                    )
                elif buffer < 60:
                    problems.append(
                        (
                            "warning",
                            f"{day_text} 返程前缓冲时间仅 {buffer} 分钟，建议预留至少 60 分钟",
                        )
                    )
        transfer = (outbound or {}).get("transfer") or {}
        transfer_minutes = _integer(transfer.get("transfer_minutes")) or 0
        if transfer_minutes:
            minimum = 90 if transfer.get("international") is True else 60
            if transfer_minutes < minimum:
                kind = "国际" if minimum == 90 else "国内"
                problems.append(
                    (
                        "warning",
                        f"{day_text} 中转时间仅 {transfer_minutes} 分钟，{kind}中转建议 ≥ {minimum} 分钟",
                    )
                )
            elif transfer_minutes > 240:
                problems.append(
                    (
                        "warning",
                        f"{day_text} 中转时间长达 {transfer_minutes} 分钟（{transfer_minutes // 60} 小时 {transfer_minutes % 60} 分钟），等待时间过长，建议缩短",
                    )
                )
        for leg, label in ((outbound, "去程"), (returning, "返程")):
            if leg and str(leg.get("type")).lower() == "flight":
                departure, arrival = leg.get("departure"), leg.get("arrival")
                try:
                    dep_hour = int(str(departure).split(":", 1)[0])
                    arr_hour = int(str(arrival).split(":", 1)[0])
                    if dep_hour >= 22 and arr_hour <= 6:
                        problems.append(
                            (
                                "warning",
                                f"{day_text} {label}航班 {departure}→{arrival} 为红眼航班，建议调整",
                            )
                        )
                except (AttributeError, TypeError, ValueError):
                    pass
    if problems:
        messages = [message for _, message in problems]
        out.issues.extend(messages)
        status = "fail" if any(level == "fail" for level, _ in problems) else "warning"
        out.check("时间安排合理性", status, "存在时间问题：" + "；".join(messages))
    else:
        out.check("时间安排合理性", "pass", "日期逻辑与时间衔接正常，无明显冲突")


def _review_origin_destination(itinerary: dict, policy: dict, out: ReviewCollector) -> None:
    origin, destination = itinerary.get("origin"), itinerary.get("destination")
    problems = []
    if not _city_matches(origin, itinerary.get("approved_origin")):
        problems.append(f"出发地「{origin}」与审批单中「{itinerary.get('approved_origin')}」不一致")
    if not _city_matches(destination, itinerary.get("approved_destination")):
        problems.append(
            f"目的地「{destination}」与审批单中「{itinerary.get('approved_destination')}」不一致"
        )
    if problems:
        out.issues.extend(problems)
        out.check("出发地与目的地核实", "fail", "；".join(problems))
    else:
        out.check(
            "出发地与目的地核实", "pass", f"出发地「{origin}」→ 目的地「{destination}」与审批单一致"
        )


def _review_budget(itinerary: dict, policy: dict, out: ReviewCollector) -> None:
    if not policy:
        out.check("预算合规性", "warning", "未提供差旅政策，跳过预算校验")
        out.suggestions.append("建议调用差旅政策查询后再做预算校验")
        return
    over_budget, computed = [], 0.0
    for day in itinerary.get("days") or []:
        day_text, hotel = day.get("date"), day.get("hotel")
        if hotel:
            price = _number(hotel.get("price_per_night"))
            computed += price
            limit_value = policy.get("hotelLimit")
            if limit_value is not None and price > _number(limit_value):
                over_budget.append(f"{day_text} 住宿价格 ¥{price} 超出政策上限 ¥{limit_value}")
            star, limit = _integer(hotel.get("star_rating")), _integer(policy.get("hotelStarLimit"))
            if star is not None and limit is not None and star > limit:
                over_budget.append(f"{day_text} 酒店星级 {star} 超出政策上限 {limit} 星")
        computed += _number((day.get("outbound_transport") or {}).get("price"))
        computed += _number((day.get("return_transport") or {}).get("price"))
    total = (
        _number(itinerary.get("total_budget"), computed)
        if itinerary.get("total_budget") is not None
        else computed
    )
    threshold = policy.get("approvalThreshold")
    if threshold is not None and total > _number(threshold):
        over_budget.append(f"总价 ¥{total:.0f} 超出审批阈值 ¥{_number(threshold):.0f}，需额外审批")
    if over_budget:
        out.issues.extend(over_budget)
        out.check("预算合规性", "warning", "存在超预算项：" + "；".join(over_budget))
        out.suggestions.append("超预算项目需在方案说明中注明原因，或调整为更经济的选项")
    else:
        out.check("预算合规性", "pass", f"预算合规，预计总费用 ¥{total:.0f}")


def _review_transport(itinerary: dict, policy: dict, out: ReviewCollector) -> None:
    if not policy:
        out.check("交通舱位合规性", "warning", "未提供差旅政策，跳过舱位校验")
        return
    flight_class, train_class = policy.get("flightClass"), policy.get("trainSeatClass")
    if flight_class is None and train_class is None:
        out.check("交通舱位合规性", "pass", "差旅政策未限定舱位/席别")
        return
    violations = []
    for day in itinerary.get("days") or []:
        for key, label in (("outbound_transport", "去程"), ("return_transport", "返程")):
            leg = day.get(key)
            if not leg or _blank(leg.get("seat_class")):
                continue
            allowed = train_class if str(leg.get("type")).lower() == "train" else flight_class
            if allowed and not is_cabin_compliant(str(leg["seat_class"]), str(allowed)):
                violations.append(
                    f"{day.get('date')} {label}舱位/席别「{leg['seat_class']}」超出政策允许最高等级「{allowed}」"
                )
    if violations:
        out.issues.extend(violations)
        out.check("交通舱位合规性", "warning", "存在舱位/席别超标：" + "；".join(violations))
        out.suggestions.append("超标舱位/席别需降级至政策允许范围，或在方案说明中注明原因")
    else:
        out.check("交通舱位合规性", "pass", "交通舱位/席别符合差旅政策")


def _review_route(itinerary: dict, policy: dict, out: ReviewCollector) -> None:
    days = itinerary.get("days")
    if not days:
        out.check("路径规划合理性", "pass", "无每日行程，无需路径规划校验")
        return
    warnings = []
    origin, destination = itinerary.get("origin"), itinerary.get("destination")
    for day in days:
        day_text = day.get("date")
        for key, expected_from, expected_to, label in (
            ("outbound_transport", origin, destination, "去程"),
            ("return_transport", destination, origin, "返程"),
        ):
            leg = day.get(key)
            if not leg:
                continue
            if leg.get("origin") is not None and not _city_matches(
                leg.get("origin"), expected_from
            ):
                warnings.append(
                    f"{day_text} {label}出发地「{leg.get('origin')}」与行程出发地「{expected_from}」不一致"
                )
            if leg.get("destination") is not None and not _city_matches(
                leg.get("destination"), expected_to
            ):
                warnings.append(
                    f"{day_text} {label}目的地「{leg.get('destination')}」与行程目的地「{expected_to}」不一致"
                )
        distance = (day.get("hotel") or {}).get("distance_to_main_venue_km")
        if distance is not None and _number(distance) > 15:
            warnings.append(
                f"{day_text} 酒店距主要活动场所约 {_number(distance):.1f} km，建议选择更近的住宿"
            )
    if warnings:
        out.suggestions.extend(warnings)
        out.check("路径规划合理性", "warning", "；".join(warnings))
    else:
        out.check("路径规划合理性", "pass", "交通路线与住宿位置均合理")


OBJECTIVE_REVIEWERS: tuple[Callable[[dict, dict, ReviewCollector], None], ...] = (
    _review_completeness,
    _review_timing,
    _review_origin_destination,
    _review_budget,
    _review_transport,
    _review_route,
)


def objective_review(itinerary: dict, policy: dict | None = None) -> dict:
    collector = ReviewCollector()
    for reviewer in OBJECTIVE_REVIEWERS:
        reviewer(itinerary, policy or {}, collector)
    statuses = {item["status"] for item in collector.checks}
    overall = "fail" if "fail" in statuses else "warning" if "warning" in statuses else "pass"
    return {
        "overall_status": overall,
        "checks": collector.checks,
        "issues": collector.issues,
        "suggestions": collector.suggestions,
    }


def _map_transport(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    result = {
        "type": str(raw.get("type") or raw.get("biz_type") or "").lower(),
        "carrier": raw.get("carrier"),
        "code": raw.get("code"),
        "origin": raw.get("origin") or raw.get("from"),
        "destination": raw.get("destination") or raw.get("to"),
        "departure": _hm(raw.get("departure_time") or raw.get("depart_time")),
        "arrival": _hm(raw.get("arrival_time") or raw.get("arrive_time")),
        "price": _number(raw.get("price")),
        "seat_class": raw.get("cabin_class") or raw.get("cabin") or raw.get("seatClass"),
        "is_direct": raw.get("is_direct"),
    }
    if raw.get("transfer"):
        result["transfer"] = raw["transfer"]
    return result


def _map_hotel(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    return {
        "name": raw.get("name"),
        "room_type": raw.get("room_type"),
        "nights": _integer(raw.get("nights")),
        "price_per_night": _number(
            raw.get("price_per_night") or raw.get("price") or raw.get("nightly_price")
        ),
        "star_rating": _integer(raw.get("star_rating")),
        "brand": raw.get("brand"),
        "distance_to_main_venue_km": raw.get("distance_to_dest_km"),
    }


def _convert_proposal(raw: dict, request: dict, index: int) -> dict:
    metrics = raw.get("metrics") or {}
    view = {
        "origin": request.get("origin"),
        "destination": request.get("destination"),
        "departure_date": request.get("departure_date"),
        "return_date": request.get("return_date"),
        "approved_origin": request.get("origin"),
        "approved_destination": request.get("destination"),
        "total_budget": metrics.get("total_price", raw.get("total_price")),
        "total_transit_hhmm": metrics.get("total_transit_hhmm"),
        "total_transit_min": metrics.get("total_transit_min", raw.get("total_transit_min")),
        "days": [
            {
                "date": request.get("departure_date"),
                "outbound_transport": _map_transport(raw.get("outbound")),
                "hotel": _map_hotel(raw.get("hotel")),
            },
            {
                "date": request.get("return_date"),
                "return_transport": _map_transport(raw.get("return") or raw.get("inbound")),
            },
        ],
    }
    for day in view["days"]:
        for key in tuple(day):
            if day[key] is None:
                day.pop(key)
    tags = raw.get("tags") or []
    return {
        "proposal_id": str(raw.get("proposal_id") or f"P{index + 1}"),
        "label": "/".join(map(str, tags)) or f"方案{index + 1}",
        "scores": raw.get("scores") or {"overall": raw.get("score", 0)},
        "itinerary_view": view,
    }


def _objective_batch(plan: dict, policy: dict) -> ReviewResult:
    request = plan.get("user_request") or {
        "origin": plan.get("origin"),
        "destination": plan.get("destination"),
        "departure_date": plan.get("departure_date"),
        "return_date": plan.get("return_date"),
    }
    reviews, groups = [], {"pass": [], "warning": [], "fail": []}
    best_id, best_score = None, -1.0
    for index, raw in enumerate(plan.get("proposals") or []):
        proposal = _convert_proposal(raw, request, index)
        review = objective_review(proposal["itinerary_view"], policy)
        review.update(
            {
                "proposal_id": proposal["proposal_id"],
                "proposal_label": proposal["label"],
                "planner_scores": proposal["scores"],
            }
        )
        reviews.append(review)
        status = review["overall_status"] if review["overall_status"] in groups else "fail"
        groups[status].append(proposal["proposal_id"])
        weight = {"pass": 1.0, "warning": 0.7, "fail": 0.0}[status]
        effective = round(_number(proposal["scores"].get("overall")) * weight)
        if effective > best_score:
            best_score, best_id = effective, proposal["proposal_id"]
    if not reviews:
        return ReviewResult(SOURCE_OBJECTIVE, "fail", ["规划结果中未找到 proposals 方案数组"])
    overall = "fail" if groups["fail"] else "warning" if groups["warning"] else "pass"

    def prefixed(field_name: str) -> list[str]:
        return [f"[{item['proposal_id']}] {text}" for item in reviews for text in item[field_name]]

    details = {
        "reviews": reviews,
        "best_proposal_id": best_id,
        "summary": {
            "total": len(reviews),
            "pass_count": len(groups["pass"]),
            "warning_count": len(groups["warning"]),
            "fail_count": len(groups["fail"]),
            "pass_ids": groups["pass"],
            "warning_ids": groups["warning"],
            "fail_ids": groups["fail"],
        },
    }
    return ReviewResult(
        SOURCE_OBJECTIVE, overall, prefixed("issues"), prefixed("suggestions"), details
    )


def _fallback_result(source: str, error: Exception | None = None) -> ReviewResult:
    suffix = f"：{error}" if error else ""
    return ReviewResult(
        source, "warning", [f"{source} 评估器执行异常{suffix}"], [f"建议人工复核 {source} 相关维度"]
    )


def _json_object(text: str) -> dict:
    stripped = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.IGNORECASE)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("响应中没有 JSON 对象")
    return json.loads(stripped[start : end + 1])


def _assess(
    source: str, prompt_name: str, plan: dict, additions: list[tuple[str, str | None]]
) -> ReviewResult:
    prompt = load_static(prompt_name)
    for title, content in additions:
        if content and content.strip():
            prompt += f"\n\n## {title}\n{content}"
    if source == SOURCE_PREFERENCE and not additions[0][1]:
        prompt += (
            "\n\n## 当前用户偏好画像\n暂无用户偏好数据，请仅基于方案标签与通用差旅常识进行评估。"
        )
    model = stable_model()
    if model is None:
        return _fallback_result(source)
    try:
        message = (
            "## 行程方案数据\n```json\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n```"
        )
        response = model.invoke([("system", prompt), ("human", message)])
        payload = _json_object(
            response.content if isinstance(response.content, str) else str(response.content)
        )
        return ReviewResult(
            source,
            payload.get("verdict", "warning"),
            payload.get("issues") or [],
            payload.get("suggestions") or [],
            payload.get("details"),
        )
    except Exception as exc:  # noqa: BLE001 - provider/network exceptions are not stable APIs
        return _fallback_result(source, exc)


def _subjective_fanout(
    plan: dict, weather: str | None, news: str | None, preferences: str | None
) -> list[ReviewResult]:
    jobs = (
        (SOURCE_EXPERIENCE, "review/trip-experience-assessor.md", []),
        (
            SOURCE_RESILIENCE,
            "review/resilience-assessor.md",
            [("当前天气摘要", weather), ("当前资讯摘要", news)],
        ),
        (SOURCE_PREFERENCE, "review/preference-assessor.md", [("当前用户偏好画像", preferences)]),
    )
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="review-assessor") as pool:
        futures = [
            pool.submit(_assess, source, prompt, plan, additions)
            for source, prompt, additions in jobs
        ]
        return [future.result() for future in futures]


def _fallback_arbitrate(results: list[ReviewResult]) -> ReviewResult:
    hard_fail = any(item.source == SOURCE_OBJECTIVE and item.verdict == "fail" for item in results)
    verdict = (
        "fail"
        if hard_fail
        else "warning"
        if any(item.verdict == "warning" for item in results)
        else "pass"
    )
    return ReviewResult(
        "arbitrator",
        verdict,
        [issue for item in results for issue in item.issues],
        [suggestion for item in results for suggestion in item.suggestions],
    )


def _arbitrate(results: list[ReviewResult], previous: dict | str | None) -> ReviewResult:
    model = stable_model()
    if model is None:
        return _fallback_arbitrate(results)
    lines = ["# 各维度审核结果汇总", ""]
    for item in results:
        lines.extend(
            [f"## {DIMENSION_LABELS.get(item.source, item.source)}", f"- 判定: {item.verdict}"]
        )
        if item.issues:
            lines.extend(["- 问题:", *[f"  - {value}" for value in item.issues]])
        if item.suggestions:
            lines.extend(["- 建议:", *[f"  - {value}" for value in item.suggestions]])
        if item.details is not None:
            lines.append("- 详情: " + json.dumps(item.details, ensure_ascii=False))
        lines.append("")
    if previous:
        previous_text = (
            previous if isinstance(previous, str) else json.dumps(previous, ensure_ascii=False)
        )
        lines.extend(["## 上一轮审核结果（修复前）", previous_text, ""])
    try:
        response = model.invoke(
            [("system", load_static("review/review-arbitrator.md")), ("human", "\n".join(lines))]
        )
        payload = _json_object(
            response.content if isinstance(response.content, str) else str(response.content)
        )
        return ReviewResult(
            "arbitrator",
            payload.get("verdict", "warning"),
            payload.get("issues") or [],
            payload.get("suggestions") or [],
            payload.get("details"),
        )
    except Exception:  # noqa: BLE001 - preserve Java's model-failure fallback
        return _fallback_arbitrate(results)


def _continue_remediation(arbitration: ReviewResult) -> bool:
    if arbitration.verdict == "fail":
        return True
    details = arbitration.details or {}
    remediation = details.get("remediation_priority") or []
    recommended = details.get("recommended_proposal_id")
    fixable_count, recommended_fixable = 0, False
    for item in remediation:
        if item.get("hard_constraint") is True:
            return True
        priority = _integer(item.get("priority"))
        if item.get("fixable_by_replanning", True) and priority is not None and priority <= 3:
            fixable_count += 1
            recommended_fixable = recommended_fixable or recommended in (
                item.get("affected_proposals") or []
            )
    return recommended_fixable or fixable_count >= 2


@tool
def review_itinerary(
    origin: str,
    destination: str,
    departure_date: str,
    policy: str | None = None,
    weather_summary: str | None = None,
    news_summary: str | None = None,
    user_preferences: str | None = None,
    previous_review: str | None = None,
) -> dict:
    """读取已存规划，执行六维客观审核、三维主观评估与最终仲裁。"""
    started = time.perf_counter()
    context = current_context()
    user_id, session_id = current_user_id(), context.session_id if context else None
    raw = itinerary_plan_store.load(user_id, origin, destination, departure_date)
    if raw is None:
        return {
            "verdict": "fail",
            "hard_vetoed": False,
            "error": "未找到规划结果。请确认行程规划已成功执行。",
            "dimensions": [],
        }
    try:
        plan = json.loads(raw) if isinstance(raw, str) else raw
        policy_data = json.loads(policy) if policy else {}
    except (TypeError, json.JSONDecodeError) as exc:
        return {
            "verdict": "fail",
            "hard_vetoed": False,
            "error": f"规划结果或政策格式异常：{exc}",
            "dimensions": [],
        }
    objective = _objective_batch(plan, policy_data)
    subjective = _subjective_fanout(plan, weather_summary, news_summary, user_preferences)
    if previous_review:
        try:
            previous = json.loads(previous_review)
        except (TypeError, json.JSONDecodeError):
            previous = previous_review
    else:
        previous = review_result_store.load(session_id) if session_id else None
    arbitration = _arbitrate([objective, *subjective], previous)
    hard_vetoed = objective.verdict == "fail"
    if hard_vetoed:
        arbitration.verdict = "fail"
        for issue in objective.issues:
            tagged = HARD_CONSTRAINT_PREFIX + issue
            if tagged not in arbitration.issues and issue not in arbitration.issues:
                arbitration.issues.append(tagged)
    report = {
        "verdict": arbitration.verdict,
        "hard_vetoed": hard_vetoed,
        "continue_remediation": _continue_remediation(arbitration),
        "dimensions": [
            {
                "source": objective.source,
                "label": DIMENSION_LABELS[objective.source],
                "verdict": "hard_fail" if hard_vetoed else objective.verdict,
                "issues": objective.issues,
                "suggestions": objective.suggestions,
            },
            *[
                {
                    "source": item.source,
                    "label": DIMENSION_LABELS[item.source],
                    "verdict": item.verdict,
                    "issues": item.issues,
                    "suggestions": item.suggestions,
                }
                for item in subjective
            ],
        ],
        "best_proposal_id": (objective.details or {}).get("best_proposal_id"),
        "arbitration": arbitration.details,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    if session_id:
        review_result_store.save(session_id, report)
    return report


def tools():
    return [review_itinerary]
