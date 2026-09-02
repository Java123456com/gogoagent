"""Travel-order conflict checks with Java-compatible severity semantics."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from backend.infrastructure.repositories import travel_order_repository
from backend.services.city_transit_time import estimate_minutes, normalize_city

from ._common import tool

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = {"DRAFT", "SUBMITTED", "APPROVED"}
_SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_SAME_DAY_TIGHT_MINUTES = 8 * 60
_SAME_DAY_IMPOSSIBLE_MINUTES = 24 * 60
_ADJACENT_DAY_MINUTES = 8 * 60


def _parse_date(value: str | None) -> date | None:
    if value is None or not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _same_city(first: str | None, second: str | None) -> bool:
    return bool(normalize_city(first) and normalize_city(first) == normalize_city(second))


def _same_route(existing: Any, departure_city: str, destination: str) -> bool:
    return _same_city(existing.departure_city, departure_city) and _same_city(existing.destination, destination)


def _summary(order: Any) -> dict[str, Any]:
    if hasattr(order, "as_dict"):
        return order.as_dict()
    return {
        "order_id": getattr(order, "order_id", None),
        "departure_city": getattr(order, "departure_city", None),
        "destination": getattr(order, "destination", None),
        "departure_date": getattr(order, "departure_date", None),
        "return_date": getattr(order, "return_date", None),
        "status": getattr(order, "status", None),
    }


def _item(order: Any, conflict_type: str, severity: str, description: str, suggestion: str) -> dict[str, Any]:
    return {
        "type": conflict_type,
        "severity": severity,
        "order_id": getattr(order, "order_id", None),
        "order_summary": _summary(order),
        "description": description,
        "suggestion": suggestion,
    }


def _overlap_conflict(order: Any, candidate_departure: str, candidate_return: str,
                      departure_city: str, destination: str) -> dict[str, Any]:
    if _same_route(order, departure_city, destination):
        return _item(
            order,
            "TIME_OVERLAP_SAME_CITY",
            "LOW",
            f"已有差旅单的时间段（{order.departure_date} ~ {order.return_date}，"
            f"{order.departure_city} ↔ {order.destination}）与本次完全重叠，属于同城市重复提交。",
            "可继续提交，但建议先取消或调整其中一张差旅单，避免重复审批。",
        )
    reversed_route = _same_city(order.departure_city, destination) and _same_city(order.destination, departure_city)
    description = (
        f"已有差旅单（{order.departure_date} ~ {order.return_date}，{order.departure_city} ↔ {order.destination}）"
        f"与本次（{candidate_departure} ~ {candidate_return}，{departure_city} → {destination}）在时间段上重叠，"
        "但目的地不同，物理上不可能同时身处两地。"
    )
    if reversed_route:
        description += "注意：两张差旅单方向恰好相反，请确认是否重复提交。"
    return _item(
        order,
        "TIME_OVERLAP_DIFF_CITY",
        "HIGH",
        description,
        "请调整本次差旅日期，使其与已有差旅单不重叠；如确有需要请先取消或修改已有差旅单。",
    )


def _same_day_conflict(order: Any, end_city: str | None, start_city: str | None) -> dict[str, Any] | None:
    minutes = estimate_minutes(end_city, start_city)
    if minutes <= 0:
        return None
    hours = minutes // 60
    if minutes > _SAME_DAY_IMPOSSIBLE_MINUTES:
        return _item(
            order, "TRANSIT_TOO_TIGHT", "HIGH",
            f"同日需要从 {end_city} 前往 {start_city}，最短衔接时间约 {hours} 小时，超出当日可行范围，物理上无法完成。",
            "请将出发日期至少延后 1 天，或调整目的地。",
        )
    if minutes > _SAME_DAY_TIGHT_MINUTES:
        return _item(
            order, "TRANSIT_TOO_TIGHT", "MEDIUM",
            f"同日需要从 {end_city} 前往 {start_city}，估算最短衔接时间约 {hours} 小时，时间非常紧张。",
            "建议改为次日出发，或选择更早的航班/高铁以预留缓冲时间。",
        )
    return _item(
        order, "TRANSIT_TOO_TIGHT", "MEDIUM",
        f"同日需要从 {end_city} 前往 {start_city}，估算衔接时间约 {hours} 小时。",
        "衔接可行但偏紧，建议选择早班交通，并预留 1~2 小时缓冲。",
    )


def _adjacent_day_conflict(order: Any, end_city: str | None, start_city: str | None,
                           relation: str) -> dict[str, Any] | None:
    if _same_city(end_city, start_city):
        return None
    minutes = estimate_minutes(end_city, start_city)
    if minutes <= _ADJACENT_DAY_MINUTES:
        return None
    return _item(
        order, "DISCONNECTED_ROUTE", "MEDIUM",
        f"{relation}，但跨城交通至少需要 {minutes // 60} 小时，仅 1 天时间衔接偏紧，存在误机/赶不上高铁的风险。",
        "建议在两段行程之间留出 1~2 天缓冲，或将其中一段改为同城行程。",
    )


def _detect(order: Any, departure_city: str, destination: str, candidate_departure: date,
            candidate_return: date, departure_text: str, return_text: str) -> dict[str, Any] | None:
    existing_departure = _parse_date(getattr(order, "departure_date", None))
    existing_return = _parse_date(getattr(order, "return_date", None))
    if existing_departure is None or existing_return is None or existing_departure > existing_return:
        logger.warning("Skipping malformed travel order during conflict detection: order_id=%s", getattr(order, "order_id", None))
        return None

    # Shared date endpoints are handoffs unless both trips are single-day trips.
    if existing_return == candidate_departure and existing_departure != candidate_return:
        return _same_day_conflict(order, getattr(order, "destination", None), departure_city)
    if existing_departure == candidate_return and existing_return != candidate_departure:
        return _same_day_conflict(order, destination, getattr(order, "departure_city", None))
    if existing_return >= candidate_departure and existing_departure <= candidate_return:
        return _overlap_conflict(order, departure_text, return_text, departure_city, destination)
    if existing_return + timedelta(days=1) == candidate_departure:
        relation = f"已有差旅在 {existing_return} 结束于 {order.destination}，候选差旅在次日（{candidate_departure}）从 {departure_city} 出发"
        return _adjacent_day_conflict(order, getattr(order, "destination", None), departure_city, relation)
    if existing_departure - timedelta(days=1) == candidate_return:
        relation = f"候选差旅在 {candidate_return} 结束于 {destination}，已有差旅在次日（{existing_departure}）从 {order.departure_city} 出发"
        return _adjacent_day_conflict(order, destination, getattr(order, "departure_city", None), relation)
    return None


def build_conflict_report(user_id: str, departure_city: str, destination: str,
                          departure_date: str, return_date: str,
                          exclude_order_id: str | None = None) -> dict[str, Any]:
    """Return a conflict report for both the read tool and write-side gate.

    Keeping this undecorated makes the mandatory write gate use exactly the same
    rules as the LLM-facing inspection tool.
    """
    errors: list[str] = []
    if not str(user_id or "").strip():
        errors.append("user_id 不能为空")
    if not str(departure_city or "").strip():
        errors.append("departure_city（出发城市）不能为空")
    if not str(destination or "").strip():
        errors.append("destination（目的地）不能为空")
    candidate_departure, candidate_return = _parse_date(departure_date), _parse_date(return_date)
    if candidate_departure is None:
        errors.append("departure_date（出发日期）格式错误或为空，需 YYYY-MM-DD")
    if candidate_return is None:
        errors.append("return_date（返回日期）格式错误或为空，需 YYYY-MM-DD")
    if errors:
        return {"check_status": "INVALID", "has_conflict": False, "total_conflicts": 0,
                "conflicts": [], "summary": "入参校验失败：" + "；".join(errors)}
    assert candidate_departure is not None and candidate_return is not None
    if candidate_departure > candidate_return:
        return {"check_status": "INVALID", "has_conflict": False, "total_conflicts": 0,
                "conflicts": [],
                "summary": f"出发日期（{departure_date}）晚于返回日期（{return_date}），请先修正日期。"}

    window_start, window_end = candidate_departure - timedelta(days=1), candidate_return + timedelta(days=1)
    conflicts: list[dict[str, Any]] = []
    try:
        candidates = travel_order_repository.list_by_user(user_id)
    except Exception:
        logger.exception("Travel-order conflict lookup failed for user_id=%s", user_id)
        return {"check_status": "FAILED", "has_conflict": False, "total_conflicts": 0,
                "conflicts": [], "summary": "冲突检查暂时不可用，请稍后重试。"}

    for order in candidates:
        if getattr(order, "order_id", None) == exclude_order_id or getattr(order, "status", None) not in _ACTIVE_STATUSES:
            continue
        existing_departure = _parse_date(getattr(order, "departure_date", None))
        existing_return = _parse_date(getattr(order, "return_date", None))
        if existing_departure is None or existing_return is None or existing_departure > existing_return:
            logger.warning("Skipping malformed travel order during conflict candidate filtering: order_id=%s", getattr(order, "order_id", None))
            continue
        if existing_return < window_start or existing_departure > window_end:
            continue
        item = _detect(order, departure_city, destination, candidate_departure, candidate_return,
                       departure_date, return_date)
        if item is not None:
            conflicts.append(item)

    conflicts.sort(key=lambda item: (_SEVERITY_RANK[item["severity"]], item.get("order_id") or ""))
    counts = {severity: sum(item["severity"] == severity for item in conflicts) for severity in _SEVERITY_RANK}
    summary = (
        f"命中 {len(conflicts)} 条冲突（HIGH={counts['HIGH']}, MEDIUM={counts['MEDIUM']}, LOW={counts['LOW']}），请逐条阅读 description 与 suggestion。"
        if conflicts else f"已检查用户已有差旅单，未发现与 {departure_city} → {destination}（{departure_date} ~ {return_date}）存在冲突。"
    )
    return {"check_status": "SUCCESS", "has_conflict": bool(conflicts),
            "total_conflicts": len(conflicts), "conflicts": conflicts, "summary": summary}


@tool
def check_travel_order_conflicts(user_id: str, departure_city: str, destination: str,
                                 departure_date: str, return_date: str,
                                 exclude_order_id: str | None = None) -> dict[str, Any]:
    """检查候选差旅单与生效中差旅单的重叠和城市衔接冲突。"""
    return build_conflict_report(
        user_id, departure_city, destination, departure_date, return_date, exclude_order_id,
    )


def tools():
    return [check_travel_order_conflicts]
