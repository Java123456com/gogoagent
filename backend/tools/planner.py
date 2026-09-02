"""Deterministic itinerary planner equivalent to Java ``CandidateRanker``.

Search hooks provide normalized candidate lists.  The planner obtains
candidate-level preference scores from the LLM-backed scoring service (with a
deterministic fallback), prunes oversized candidate spaces, performs objective
math and policy penalties, then selects representative proposals.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from itertools import product
from math import floor, prod
from typing import Any

from backend.config import get_settings
from backend.infrastructure.stores import itinerary_plan_store, search_candidate_store
from backend.services.policy_service import is_cabin_compliant
from backend.services.preference_scoring import score_candidate_preferences
from backend.services.tool_result_side_effects import build_search_candidates

from ._common import current_user_id, tool

DEFAULT_WEIGHTS = {"time": 0.20, "price": 0.10, "preference": 0.40, "experience": 0.30}
TAG_ORDER = ("综合最佳", "时间最短", "价格最低", "最符合偏好")
BAD_WEATHER = ("暴雨", "大雾", "大风", "雷暴", "暴雪", "雾霾", "台风", "冰雹")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _minutes(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text and "T" not in text:
        try:
            hours, minutes = text.split(":", 1)
            return int(hours) * 60 + int(minutes)
        except ValueError:
            return None
    return int(_number(text)) if text.replace(".", "", 1).isdigit() else None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "")
    if not text:
        return None
    for candidate in (text, text.replace("/", "-")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _duration(option: dict) -> int | None:
    explicit = _minutes(option.get("duration") or option.get("duration_min"))
    if explicit is not None:
        return explicit
    departure = _parse_dt(option.get("departure_time") or option.get("depart_time"))
    arrival = _parse_dt(option.get("arrival_time") or option.get("arrive_time"))
    if departure and arrival:
        return max(0, int((arrival - departure).total_seconds() / 60))
    return None


def _city_matches(left: Any, right: Any) -> bool:
    left, right = str(left or "").strip(), str(right or "").strip()
    return bool(left and right and (left.startswith(right) or right.startswith(left[:2])))


def _direction(
    option: dict, origin: str, destination: str, departure_date: str, return_date: str
) -> str:
    explicit = str(option.get("direction") or "").strip().lower()
    if explicit in {"outbound", "去程", "departure", "depart", "go"}:
        return "outbound"
    if explicit in {"inbound", "return", "返程", "back"}:
        return "return"
    source = option.get("from") or option.get("origin")
    target = option.get("to") or option.get("destination")
    if _city_matches(source, origin) and _city_matches(target, destination):
        return "outbound"
    if _city_matches(source, destination) and _city_matches(target, origin):
        return "return"
    departure = _parse_dt(
        option.get("departure_time") or option.get("depart_time") or option.get("date")
    )
    if departure:
        if return_date and departure.date().isoformat() >= return_date.replace("/", "-"):
            return "return"
        if departure_date and departure.date().isoformat() <= departure_date.replace("/", "-"):
            return "outbound"
    return "outbound"


def _normalise_options(candidates: dict) -> tuple[list[dict], list[dict]]:
    transports, hotels = [], []
    for index, raw in enumerate(candidates.get("transport_options") or [], start=1):
        item = dict(raw or {})
        if not item.get("id"):
            item["id"] = _stable_id("T", item, index)
        item.setdefault("type", str(item.get("biz_type") or "flight").lower())
        transports.append(item)
    for index, raw in enumerate(candidates.get("hotel_options") or [], start=1):
        item = dict(raw or {})
        if not item.get("id"):
            item["id"] = _stable_id("H", item, index)
        item.setdefault("nights", 1)
        hotels.append(item)
    return transports, hotels


def _stable_id(prefix: str, item: dict, index: int) -> str:
    """Generate a stable candidate id so remediation survives merges/reordering."""
    identity = {
        key: item.get(key)
        for key in (
            "flightNumber",
            "trainNum",
            "code",
            "departure_time",
            "depart_time",
            "arrival_time",
            "arrive_time",
            "hotelId",
            "name",
            "hotelName",
            "room_type",
        )
        if item.get(key) not in (None, "")
    }
    if not identity:
        identity = {"index": index, "value": item}
    digest = hashlib.sha1(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _parse_preferences(value: str | None) -> Any:
    """Accept both documented free text and the Python structured form."""
    if not value:
        return {}
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip()


def _entry_score(score_map: dict[str, Any], candidate_id: Any) -> float:
    value = score_map.get(str(candidate_id))
    if isinstance(value, (int, float)):
        return float(value)
    return _number(value.get("score")) if isinstance(value, dict) else 0.0


def _transport_policy_fit(item: dict, policy: dict) -> int:
    cabin = item.get("cabin_class") or item.get("cabin") or item.get("seatClass")
    if not cabin or not policy:
        return 1
    allowed = (
        policy.get("trainSeatClass")
        if str(item.get("type") or "").lower() == "train"
        else policy.get("flightClass")
    )
    return int(not allowed or is_cabin_compliant(str(cabin), str(allowed)))


def _hotel_policy_fit(item: dict, policy: dict) -> int:
    if not policy:
        return 1
    nightly = _number(item.get("price_per_night") or item.get("price") or item.get("nightly_price"))
    limit = policy.get("hotelLimit")
    star, star_limit = item.get("star_rating"), policy.get("hotelStarLimit")
    price_ok = limit is None or nightly <= _number(limit)
    star_ok = star is None or star_limit is None or _number(star) <= _number(star_limit)
    return int(price_ok and star_ok)


def _transport_risk(item: dict) -> tuple[int, int]:
    departure = _parse_dt(item.get("departure_time") or item.get("depart_time"))
    arrival = _parse_dt(item.get("arrival_time") or item.get("arrive_time"))
    red_eye = int(bool((departure and departure.hour >= 22) or (arrival and arrival.hour < 6)))
    return red_eye, _duration(item) or 10**9


def _select_diverse(
    options: list[dict], limit: int, rankings: list[tuple[Any, bool]]
) -> list[dict]:
    """Round-robin several objectives so pruning keeps useful diversity."""
    if limit >= len(options):
        return list(options)
    ordered = [
        sorted(options, key=lambda item: (key(item), str(item.get("id") or "")), reverse=reverse)
        for key, reverse in rankings
    ]
    positions = [0] * len(ordered)
    selected: list[dict] = []
    selected_ids: set[str] = set()
    while len(selected) < limit:
        progressed = False
        for index, rows in enumerate(ordered):
            while positions[index] < len(rows):
                candidate = rows[positions[index]]
                positions[index] += 1
                candidate_id = str(candidate.get("id"))
                if candidate_id in selected_ids:
                    continue
                selected.append(candidate)
                selected_ids.add(candidate_id)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _dimension_caps(lengths: tuple[int, int, int], target: int) -> tuple[int, int, int]:
    caps = list(lengths)
    while prod(caps) > target:
        shrinkable = [index for index, value in enumerate(caps) if value > 1]
        if not shrinkable:
            break
        index = max(shrinkable, key=lambda idx: caps[idx])
        caps[index] -= 1
    return caps[0], caps[1], caps[2]


def _prune_candidate_space(
    outbound: list[dict], hotels: list[dict], inbound: list[dict], score_data: dict, policy: dict
) -> tuple[list[dict], list[dict], list[dict], dict[str, Any]]:
    settings = get_settings()
    lengths = (len(outbound), len(hotels), len(inbound))
    raw_combo_count = prod(lengths)
    trigger = max(1, int(settings.planner_pruning_trigger))
    ratio = max(0.001, min(1.0, float(settings.planner_target_keep_ratio)))
    max_combinations = max(1, int(settings.planner_max_combinations))
    if raw_combo_count <= trigger:
        caps = lengths
    else:
        target = max(1, min(max_combinations, floor(raw_combo_count * ratio)))
        caps = _dimension_caps(lengths, target)

    transport_scores = score_data.get("transport_scores") or {}
    hotel_scores = score_data.get("hotel_scores") or {}
    transport_rankings = [
        (lambda item: _number(item.get("price"), float("inf")), False),
        (lambda item: _duration(item) if _duration(item) is not None else 10**9, False),
        (lambda item: _entry_score(transport_scores, item.get("id")), True),
        (lambda item: _transport_policy_fit(item, policy), True),
        (_transport_risk, False),
    ]
    hotel_rankings = [
        (
            lambda item: _number(
                item.get("price_per_night") or item.get("price") or item.get("nightly_price"),
                float("inf"),
            ),
            False,
        ),
        (lambda item: _entry_score(hotel_scores, item.get("id")), True),
        (lambda item: _hotel_policy_fit(item, policy), True),
        (lambda item: _number(item.get("distance_to_dest_km"), float("inf")), False),
    ]
    short_outbound = _select_diverse(outbound, caps[0], transport_rankings)
    short_hotels = _select_diverse(hotels, caps[1], hotel_rankings)
    short_inbound = _select_diverse(inbound, caps[2], transport_rankings)
    enumerated = len(short_outbound) * len(short_hotels) * len(short_inbound)
    pruning_filtered = raw_combo_count - enumerated
    return (
        short_outbound,
        short_hotels,
        short_inbound,
        {
            "raw_candidate_counts": {
                "outbound": lengths[0],
                "hotel": lengths[1],
                "return": lengths[2],
            },
            "shortlisted_candidate_counts": {
                "outbound": len(short_outbound),
                "hotel": len(short_hotels),
                "return": len(short_inbound),
            },
            "raw_combo_count": raw_combo_count,
            "enumerated_combo_count": enumerated,
            "pruning_filtered_count": pruning_filtered,
            "pruning_rate": round(pruning_filtered * 100 / raw_combo_count, 2)
            if raw_combo_count
            else 0.0,
            "pruning_applied": enumerated < raw_combo_count,
        },
    )


def _policy_check(
    outbound: dict, hotel: dict, inbound: dict, total_price: float, policy: dict
) -> tuple[list[str], list[str], float]:
    if not policy:
        return [], [], 100.0
    violations, warnings = [], []
    hotel_limit = policy.get("hotelLimit")
    hotel_price = _number(
        hotel.get("price_per_night") or hotel.get("price") or hotel.get("nightly_price")
    )
    if hotel_limit is not None and hotel_price > _number(hotel_limit):
        violations.append(f"酒店单晚 {hotel_price:g} 超过限额 {_number(hotel_limit):g}")
    star, star_limit = hotel.get("star_rating"), policy.get("hotelStarLimit")
    if star is not None and star_limit is not None and _number(star) > _number(star_limit):
        warnings.append(f"酒店星级 {_number(star):g} 超过政策上限 {_number(star_limit):g} 星")
    threshold = policy.get("approvalThreshold")
    if threshold is not None and total_price > _number(threshold):
        violations.append(f"总价 {total_price:.0f} 超过审批阈值 {_number(threshold):g}")
    for leg, label in ((outbound, "去程"), (inbound, "返程")):
        cabin = leg.get("cabin_class") or leg.get("cabin") or leg.get("seatClass")
        if not cabin:
            continue
        allowed = (
            policy.get("trainSeatClass")
            if str(leg.get("type", "")).lower() == "train"
            else policy.get("flightClass")
        )
        if allowed and not is_cabin_compliant(str(cabin), str(allowed)):
            warnings.append(f"{label}舱位 '{cabin}' 超出政策允许最高等级 {allowed}")
    score = max(0.0, 100.0 - 40.0 * len(violations) - 15.0 * len(warnings))
    return violations, warnings, round(score, 2)


def _experience(outbound: dict, inbound: dict, weather: str | None) -> tuple[float, list[str]]:
    score = 100.0
    flags = []
    for leg, label in ((outbound, "去程"), (inbound, "返程")):
        kind = str(leg.get("type") or "").lower()
        departure = _parse_dt(leg.get("departure_time") or leg.get("depart_time"))
        arrival = _parse_dt(leg.get("arrival_time") or leg.get("arrive_time"))
        if kind == "flight" and (
            (departure and departure.hour >= 22) or (arrival and arrival.hour < 6)
        ):
            score -= 30
            flags.append(f"{label}为红眼航班")
        if label == "去程" and arrival and arrival.hour >= 21:
            score -= 15
            flags.append("去程到达时间过晚（21:00 后）")
        if label == "返程" and departure and departure.hour >= 21 and kind == "flight":
            score -= 10
            flags.append("返程接近末班交通")
        duration = _duration(leg)
        if duration is not None and duration >= 360:
            score -= 25
            flags.append(f"{label}单程通勤过长")
        elif duration is not None and duration >= 240:
            score -= 15
            flags.append(f"{label}单程通勤较长")
    if (
        weather
        and any(word in weather for word in BAD_WEATHER)
        and (
            str(outbound.get("type") or "").lower() == "flight"
            or str(inbound.get("type") or "").lower() == "flight"
        )
    ):
        score -= 25
        flags.append("恶劣天气下选择飞机出行")
    return round(max(0.0, score), 2), list(dict.fromkeys(flags))


def _minmax(value: float | None, low: float, high: float, higher: bool = False) -> float:
    if value is None:
        return 0.0
    if high == low:
        return 100.0
    ratio = (value - low) / (high - low)
    if not higher:
        ratio = 1.0 - ratio
    return round(max(0.0, min(1.0, ratio)) * 100, 2)


def _score_entry(score_map: dict, key: str, neutral: bool) -> tuple[float, list[str]]:
    value = score_map.get(key)
    if value is None:
        return (50.0, ["无偏好设置，使用中性分"]) if neutral else (0.0, [])
    if isinstance(value, (int, float)):
        return float(value), []
    return _number(value.get("score")), list(value.get("basis") or [])


def _format_duration(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _view_transport(option: dict | None) -> dict | None:
    if option is None:
        return None
    result = {
        key: option.get(key)
        for key in (
            "id",
            "type",
            "carrier",
            "code",
            "departure_time",
            "arrival_time",
            "origin",
            "destination",
            "price",
            "cabin_class",
            "is_direct",
            "refund_policy",
        )
    }
    result.update(
        {"transit_min": _duration(option), "transit_hhmm": _format_duration(_duration(option))}
    )
    return result


def _view_hotel(option: dict | None) -> dict | None:
    if option is None:
        return None
    return {
        key: option.get(key)
        for key in (
            "id",
            "name",
            "brand",
            "star_rating",
            "room_type",
            "price_per_night",
            "nights",
            "distance_to_dest_km",
            "breakfast_included",
            "cancel_policy",
        )
    }


def _pick(combos: list[dict], key: str, higher: bool, tie: str) -> dict:
    if higher:
        return max(combos, key=lambda item: (item.get(key, 0), item.get(tie, 0)))
    return min(combos, key=lambda item: (item.get(key, 0), item.get(tie, 0)))


def _pick_experience_aware(combos: list[dict], key: str, tie: str) -> dict:
    best = _pick(combos, key, False, tie)
    if not best.get("experience_flags"):
        return best
    threshold = best.get(key, 0) * 1.15
    clean = [
        item
        for item in combos
        if not item.get("experience_flags") and item.get(key, 0) <= threshold
    ]
    return _pick(clean, key, False, tie) if clean else best


@tool
def plan_itinerary(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str,
    preferences: str | None = None,
    scores: str | None = None,
    policy: str | None = None,
    weather_summary: str | None = None,
    excluded_transport_ids: str | None = None,
    excluded_hotel_ids: str | None = None,
    score_overrides: str | None = None,
    repair_round: int = 0,
    user_id: str | None = None,
) -> dict:
    """组合交通与酒店候选，按 Java 原版六维评分选择代表方案。"""
    user_id = current_user_id(user_id)
    raw = search_candidate_store.load(user_id, origin, destination, departure_date, return_date)
    candidates = json.loads(raw) if isinstance(raw, str) else (raw or {})
    if not isinstance(candidates, dict) or not {
        "transport_options",
        "hotel_options",
    }.intersection(candidates):
        candidates = (
            build_search_candidates(
                user_id,
                origin,
                destination,
                departure_date,
                return_date,
            )
            or {}
        )
    transports, hotels = _normalise_options(candidates)
    try:
        excluded_transports = set(json.loads(excluded_transport_ids or "[]"))
        excluded_hotels = set(json.loads(excluded_hotel_ids or "[]"))
        override_data = json.loads(score_overrides or "{}")
        if not isinstance(override_data, dict):
            raise TypeError("score_overrides 必须是 JSON object")
    except (TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"整改参数不是合法 JSON：{exc}", "proposals": []}
    transports = [item for item in transports if str(item.get("id")) not in excluded_transports]
    hotels = [item for item in hotels if str(item.get("id")) not in excluded_hotels]
    if not transports or not hotels:
        return {
            "ok": False,
            "error": "输入缺少 transport_options 或 hotel_options，无法规划。",
            "proposals": [],
        }
    outbound = [
        item
        for item in transports
        if _direction(item, origin, destination, departure_date, return_date) == "outbound"
    ]
    inbound = [
        item
        for item in transports
        if _direction(item, origin, destination, departure_date, return_date) == "return"
    ]
    if not outbound or not inbound:
        return {
            "ok": False,
            "error": f"去程或返程为空（去程 {len(outbound)} 条 / 返程 {len(inbound)} 条）。",
            "proposals": [],
        }
    explicit_scores = bool(scores and str(scores).lower() != "auto")
    try:
        policy_data = json.loads(policy) if policy else {}
        preference_data = _parse_preferences(preferences)
        score_data = json.loads(scores) if explicit_scores else {}
    except (TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"scores / policy 不是合法 JSON：{exc}", "proposals": []}
    if not isinstance(score_data, dict):
        return {"ok": False, "error": "scores 必须是 JSON object", "proposals": []}

    if explicit_scores:
        preference_scoring = {
            "mode": "provided",
            "basis": "调用方提供候选偏好分",
        }
    else:
        preference_scoring = score_candidate_preferences(
            preference_data,
            {"transport_options": transports, "hotel_options": hotels},
        )
        score_data = preference_scoring["scores"]
    transport_scores = score_data.setdefault("transport_scores", {})
    hotel_scores = score_data.setdefault("hotel_scores", {})
    for candidate_id, score in override_data.items():
        target = (
            hotel_scores
            if str(candidate_id).startswith("H_")
            or any(str(item.get("id")) == str(candidate_id) for item in hotels)
            else transport_scores
        )
        target[str(candidate_id)] = {
            "score": max(0.0, min(100.0, _number(score))),
            "basis": [f"第 {int(repair_round)} 轮整改评分调整"],
        }

    outbound, hotels, inbound, filtering = _prune_candidate_space(
        outbound,
        hotels,
        inbound,
        score_data,
        policy_data,
    )
    neutral_scores = False
    combos = []
    temporal_filtered_count = 0
    for out, hotel, back in product(outbound, hotels, inbound):
        arrival = _parse_dt(out.get("arrival_time") or out.get("arrive_time"))
        departure = _parse_dt(back.get("departure_time") or back.get("depart_time"))
        if arrival and departure and departure <= arrival:
            temporal_filtered_count += 1
            continue
        out_min, back_min = _duration(out), _duration(back)
        total_transit = (out_min or 0) + (back_min or 0)
        nights = int(_number(hotel.get("nights"), 1)) or 1
        nightly = _number(
            hotel.get("price_per_night") or hotel.get("price") or hotel.get("nightly_price")
        )
        total_price = _number(out.get("price")) + _number(back.get("price")) + nightly * nights
        violations, warnings, policy_score = _policy_check(
            out, hotel, back, total_price, policy_data
        )
        experience, flags = _experience(out, back, weather_summary)
        transport_scores = score_data.get("transport_scores") or {}
        hotel_scores = score_data.get("hotel_scores") or {}
        out_score, out_basis = _score_entry(transport_scores, str(out.get("id")), neutral_scores)
        hotel_score, hotel_basis = _score_entry(hotel_scores, str(hotel.get("id")), neutral_scores)
        back_score, back_basis = _score_entry(transport_scores, str(back.get("id")), neutral_scores)
        preference = round((out_score + hotel_score + back_score) / 3, 2)
        stay_hours = (
            round((departure - arrival).total_seconds() / 3600, 1)
            if arrival and departure
            else None
        )
        combos.append(
            {
                "combo_id": f"{out.get('id')}|{hotel.get('id')}|{back.get('id')}",
                "outbound": out,
                "hotel": hotel,
                "return": back,
                "total_price": round(total_price, 2),
                "total_transit_min": total_transit,
                "total_transit_hhmm": _format_duration(total_transit),
                "stay_hours": stay_hours,
                "policy_score": policy_score,
                "policy_violations": violations,
                "warnings": warnings,
                "experience_score_raw": experience,
                "experience_flags": flags,
                "preference_score": preference,
                "preference_basis": (
                    [f"去程：{text}" for text in out_basis]
                    + [f"住宿：{text}" for text in hotel_basis]
                    + [f"返程：{text}" for text in back_basis]
                ),
            }
        )
    if not combos:
        return {
            "ok": False,
            "error": "所有组合都被过滤，可能是返程时间都早于去程到达。",
            "proposals": [],
        }
    filtering["temporal_filtered_count"] = temporal_filtered_count
    filtering["feasible_combo_count"] = len(combos)
    filtering["total_filtered_count"] = filtering["raw_combo_count"] - len(combos)
    filtering["filter_rate"] = (
        round(
            filtering["total_filtered_count"] * 100 / filtering["raw_combo_count"],
            2,
        )
        if filtering["raw_combo_count"]
        else 0.0
    )
    prices = [item["total_price"] for item in combos]
    times = [item["total_transit_min"] for item in combos]
    experiences = [item["experience_score_raw"] for item in combos]
    policy_penalty_weight = max(0.0, float(get_settings().planner_policy_penalty_weight))
    for combo in combos:
        combo["time_score"] = _minmax(combo["total_transit_min"], min(times), max(times))
        combo["price_score"] = _minmax(combo["total_price"], min(prices), max(prices))
        combo["experience_score"] = _minmax(
            combo["experience_score_raw"], min(experiences), max(experiences), True
        )
        base_score = (
            combo["time_score"] * DEFAULT_WEIGHTS["time"]
            + combo["price_score"] * DEFAULT_WEIGHTS["price"]
            + combo["preference_score"] * DEFAULT_WEIGHTS["preference"]
            + combo["experience_score"] * DEFAULT_WEIGHTS["experience"]
        )
        combo["policy_penalty"] = round(
            (100.0 - combo["policy_score"]) * policy_penalty_weight,
            2,
        )
        combo["overall_score"] = round(
            max(0.0, base_score - combo["policy_penalty"]),
            2,
        )
    picks = {
        "综合最佳": _pick(combos, "overall_score", True, "preference_score"),
        "时间最短": _pick_experience_aware(combos, "total_transit_min", "total_price"),
        "价格最低": _pick_experience_aware(combos, "total_price", "total_transit_min"),
        "最符合偏好": _pick(combos, "preference_score", True, "overall_score"),
    }
    merged: dict[str, dict] = {}
    for tag in TAG_ORDER:
        combo = picks[tag]
        merged.setdefault(combo["combo_id"], combo).setdefault("tags", []).append(tag)
    proposals = []
    for combo in merged.values():
        proposal_id = "P_" + hashlib.sha1(combo["combo_id"].encode("utf-8")).hexdigest()[:10]
        proposals.append(
            {
                "proposal_id": proposal_id,
                "combo_id": combo["combo_id"],
                "candidate_ids": {
                    "outbound": str(combo["outbound"].get("id")),
                    "hotel": str(combo["hotel"].get("id")),
                    "return": str(combo["return"].get("id")),
                },
                "tags": combo["tags"],
                "outbound": _view_transport(combo["outbound"]),
                "hotel": _view_hotel(combo["hotel"]),
                "return": _view_transport(combo["return"]),
                "metrics": {
                    "total_price": combo["total_price"],
                    "total_transit_min": combo["total_transit_min"],
                    "total_transit_hhmm": combo["total_transit_hhmm"],
                    "stay_hours": combo["stay_hours"] if combo["stay_hours"] is not None else "",
                },
                "scores": {
                    "time": combo["time_score"],
                    "price": combo["price_score"],
                    "preference": combo["preference_score"],
                    "policy": combo["policy_score"],
                    "policy_penalty": combo["policy_penalty"],
                    "experience": combo["experience_score"],
                    "overall": combo["overall_score"],
                },
                "preference_basis": combo["preference_basis"],
                "policy_violations": combo["policy_violations"],
                "warnings": combo["warnings"],
                "experience_flags": combo["experience_flags"],
            }
        )
    proposals.sort(key=lambda item: item["scores"]["overall"], reverse=True)
    result = {
        "ok": True,
        "meta": {
            "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "dimension_weights": DEFAULT_WEIGHTS,
            "combo_count": len(combos),
            "policy_penalty_weight": policy_penalty_weight,
            "preference_scoring": {
                "mode": preference_scoring["mode"],
                "basis": preference_scoring["basis"],
            },
            "filtering": filtering,
            "repair_round": int(repair_round),
            "excluded_transport_ids": sorted(map(str, excluded_transports)),
            "excluded_hotel_ids": sorted(map(str, excluded_hotels)),
        },
        "user_request": {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "return_date": return_date,
        },
        "origin": origin,
        "destination": destination,
        "preferences": preference_data,
        "combo_count": len(combos),
        "filter_rate": filtering["filter_rate"],
        "preference_scoring": {
            "mode": preference_scoring["mode"],
            "basis": preference_scoring["basis"],
        },
        "proposals": proposals,
    }
    itinerary_plan_store.save(
        user_id, origin, destination, departure_date, json.dumps(result, ensure_ascii=False)
    )
    return {**result, "candidate_count": len(combos)}


@tool
def get_candidates(user_id: str | None = None) -> dict:
    """读取当前用户最近一次搜索候选。"""
    raw = search_candidate_store.load(current_user_id(user_id))
    return (
        json.loads(raw)
        if isinstance(raw, str)
        else (raw or {"transport_options": [], "hotel_options": []})
    )


@tool
def get_proposals(user_id: str | None = None) -> dict:
    """读取当前用户最近一次规划代表方案。"""
    raw = itinerary_plan_store.latest(current_user_id(user_id))
    return json.loads(raw) if isinstance(raw, str) else (raw or {"proposals": []})


def tools():
    return [plan_itinerary, get_candidates, get_proposals]
