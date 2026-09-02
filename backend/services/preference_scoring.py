"""Candidate-level preference scoring for itinerary planning.

The planner remains deterministic once scores are available.  This service
adds the subjective track: it asks the configured LLM to score transport and
hotel candidates against real user preferences, validates the response, and
fills every missing/invalid entry with a deterministic rule-based score.
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.infrastructure.llm import invoke_text, strong_model

_NO_PREFERENCE = ("无偏好", "无特殊要求", "不限", "随意")
_TIME_RANGES = {
    "早班": (6, 9),
    "上午": (9, 12),
    "下午": (12, 18),
    "晚班": (18, 21),
}


def score_candidate_preferences(preferences: Any, candidates: dict[str, Any]) -> dict[str, Any]:
    """Return validated scores for every candidate plus scoring provenance."""
    preference_text = _preference_text(preferences)
    transports = [dict(item or {}) for item in candidates.get("transport_options") or []]
    hotels = [dict(item or {}) for item in candidates.get("hotel_options") or []]
    neutral = not preference_text or preference_text in _NO_PREFERENCE

    fallback_scores = {
        "transport_scores": {
            str(item.get("id")): _neutral_entry() if neutral else _score_transport(item, preference_text)
            for item in transports if item.get("id") is not None
        },
        "hotel_scores": {
            str(item.get("id")): _neutral_entry() if neutral else _score_hotel(item, preference_text)
            for item in hotels if item.get("id") is not None
        },
    }
    if neutral:
        return {
            "scores": fallback_scores,
            "mode": "neutral",
            "basis": "未提供有效个人偏好，所有候选使用中性分",
        }

    model = strong_model()
    if model is None:
        return {
            "scores": fallback_scores,
            "mode": "deterministic_fallback",
            "basis": "模型未启用，使用可复现的偏好规则评分",
        }

    compact = {
        "preferences": preferences,
        "transport_options": [_compact_transport(item) for item in transports],
        "hotel_options": [_compact_hotel(item) for item in hotels],
    }
    fallback_json = json.dumps(fallback_scores, ensure_ascii=False)
    response = invoke_text(
        model,
        (
            "你是企业差旅行程偏好评分器。只依据用户明确表达的偏好，对每个候选打0到100分。"
            "不得把价格、差旅政策、天气和一般常识当作用户偏好。只输出JSON，结构必须为"
            "{transport_scores:{id:{score,basis:[]}},hotel_scores:{id:{score,basis:[]}}}。"
            "每个候选必须出现，basis用1到3条简短中文说明。"
        ),
        json.dumps(compact, ensure_ascii=False),
        fallback_json,
    )
    parsed = _parse_json(response)
    validated = _validate_scores(parsed, fallback_scores)
    used_llm = parsed is not None and response.strip() != fallback_json.strip()
    return {
        "scores": validated,
        "mode": "llm" if used_llm else "deterministic_fallback",
        "basis": "LLM按真实用户偏好逐项评分" if used_llm else "模型调用失败，使用可复现的偏好规则评分",
    }


def _preference_text(preferences: Any) -> str:
    if preferences in (None, "", {}, []):
        return ""
    if isinstance(preferences, str):
        text = preferences.strip()
        if not text:
            return ""
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return text
        return _preference_text(decoded)
    if isinstance(preferences, dict):
        parts = []
        for key, value in preferences.items():
            rendered = _preference_text(value)
            if rendered and not any(marker == rendered for marker in _NO_PREFERENCE):
                parts.append(f"{key}:{rendered}")
        return "；".join(parts)
    if isinstance(preferences, (list, tuple, set)):
        parts = [rendered for rendered in (_preference_text(item) for item in preferences)
                 if rendered and rendered not in _NO_PREFERENCE]
        return "；".join(parts)
    return str(preferences).strip()


def _neutral_entry() -> dict[str, Any]:
    return {"score": 50.0, "basis": ["无偏好设置，使用中性分"]}


def _score_transport(item: dict[str, Any], preferences: str) -> dict[str, Any]:
    score, basis = 50.0, []
    blob = _blob(item)
    kind = str(item.get("type") or item.get("biz_type") or "").lower()

    direct_preference = any(word in preferences for word in ("只选直飞", "直飞", "直达"))
    if direct_preference:
        direct = bool(item.get("is_direct")) or "直飞" in blob or "直达" in blob
        score += 25 if direct else -25
        basis.append("命中直飞/直达偏好" if direct else "未满足直飞/直达偏好")

    refund_preference = any(word in preferences for word in ("可退", "可改", "灵活退改"))
    if refund_preference:
        refundable = any(word in blob for word in ("可退", "免费取消", "可改"))
        score += 15 if refundable else -10
        basis.append("退改条件符合偏好" if refundable else "退改条件未匹配偏好")

    departure = _departure_hour(item)
    for label, (start, end) in _TIME_RANGES.items():
        if label in preferences and departure is not None:
            matched = start <= departure < end
            score += 20 if matched else -10
            basis.append(f"出发时段{'命中' if matched else '未命中'}{label}偏好")
            break

    fields = [item.get("carrier"), item.get("code"), item.get("cabin_class"),
              item.get("cabin"), item.get("seatClass")]
    for value in filter(None, fields):
        token = str(value)
        if token and token.lower() in preferences.lower():
            score += 20
            basis.append(f"命中偏好：{token}")
            break

    if kind == "train" and "高铁" in preferences:
        score += 15
        basis.append("命中高铁出行偏好")
    elif kind == "flight" and any(word in preferences for word in ("飞机", "航班")):
        score += 15
        basis.append("命中飞机出行偏好")

    return _entry(score, basis)


def _score_hotel(item: dict[str, Any], preferences: str) -> dict[str, Any]:
    score, basis = 50.0, []
    for value in filter(None, (item.get("brand"), item.get("name"))):
        token = str(value)
        matched = next((part for part in _meaningful_tokens(preferences)
                        if len(part) >= 2 and (part in token or token in part)), None)
        if matched:
            score += 30
            basis.append(f"命中酒店品牌偏好：{matched}")
            break

    room = str(item.get("room_type") or "")
    if room and room in preferences:
        score += 20
        basis.append(f"命中房型偏好：{room}")

    if "早餐" in preferences:
        included = bool(item.get("breakfast_included")) or "含早" in _blob(item)
        score += 15 if included else -10
        basis.append("包含早餐" if included else "未满足早餐偏好")

    if any(word in preferences for word in ("近会场", "靠近办公", "靠近会议", "距离近")):
        distance = _number_or_none(item.get("distance_to_dest_km"))
        if distance is not None:
            nearby = distance <= 3
            score += 20 if nearby else -10
            basis.append("距离目的地较近" if nearby else "距离目的地偏远")

    star = _number_or_none(item.get("star_rating"))
    star_expectations = (("五星", 5), ("四星", 4), ("三星", 3))
    for label, expected in star_expectations:
        if label in preferences and star is not None:
            matched = int(star) == expected
            score += 15 if matched else -5
            basis.append(f"酒店星级{'命中' if matched else '未命中'}{label}偏好")
            break

    return _entry(score, basis)


def _entry(score: float, basis: list[str]) -> dict[str, Any]:
    return {
        "score": round(max(0.0, min(100.0, score)), 2),
        "basis": list(dict.fromkeys(basis))[:3] or ["未发现明确匹配项，使用中性分"],
    }


def _meaningful_tokens(text: str) -> list[str]:
    tokens = re.split(r"[\s,，;；:/|()（）\[\]{}'\"]+", text)
    ignored = {"hotel_brand", "hotel", "brand", "酒店", "偏好", "品牌"}
    return [token for token in tokens if token and token not in ignored
            and not any(marker in token for marker in _NO_PREFERENCE)]


def _departure_hour(item: dict[str, Any]) -> int | None:
    text = str(item.get("departure_time") or item.get("depart_time") or "")
    match = re.search(r"(?:T|\s|^)(\d{1,2}):(\d{2})", text)
    return int(match.group(1)) if match else None


def _blob(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, default=str).lower()


def _number_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _compact_transport(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "type", "carrier", "code", "departure_time", "arrival_time",
            "duration", "cabin_class", "is_direct", "refund_policy")
    return {key: item.get(key) for key in keys if item.get(key) is not None}


def _compact_hotel(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "brand", "star_rating", "room_type", "distance_to_dest_km",
            "breakfast_included", "cancel_policy")
    return {key: item.get(key) for key in keys if item.get(key) is not None}


def _parse_json(text: str) -> dict[str, Any] | None:
    content = str(text or "").strip()
    if content.startswith("```"):
        content = content.removeprefix("```").removeprefix("json").removesuffix("```").strip()
    try:
        result = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _validate_scores(parsed: dict[str, Any] | None,
                     fallback: dict[str, Any]) -> dict[str, Any]:
    parsed = parsed or {}
    result: dict[str, dict[str, Any]] = {"transport_scores": {}, "hotel_scores": {}}
    for group, output_group in result.items():
        supplied = parsed.get(group) if isinstance(parsed.get(group), dict) else {}
        for candidate_id, fallback_entry in fallback[group].items():
            raw = supplied.get(candidate_id)
            if isinstance(raw, (int, float)):
                output_group[candidate_id] = _entry(float(raw), [])
            elif isinstance(raw, dict) and isinstance(raw.get("score"), (int, float)):
                basis = raw.get("basis") if isinstance(raw.get("basis"), list) else []
                output_group[candidate_id] = _entry(float(raw["score"]), [str(x) for x in basis])
            else:
                output_group[candidate_id] = fallback_entry
    return result
