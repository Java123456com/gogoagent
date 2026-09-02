"""差旅政策服务（对应 Java TravelPolicyService + CabinRankUtil）。

政策规则：4 职级区间 × 3 城市等级 = 12 条；`query_travel_policy` 与 `check_travel_policy`
两个工具共用本服务。
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.config import get_settings
from backend.infrastructure.repositories import policy_repository, user_repository

# 舱位/席别等级映射（数值越大等级越高）
_RANK_MAP = {
    "经济舱": 1, "economy": 1, "经济": 1,
    "商务舱": 2, "business": 2, "商务": 2,
    "头等舱": 3, "first": 3, "头等": 3,
    "二等座": 1, "second": 1,
    "一等座": 2,
    "商务座": 3,
}


def rank_of(cabin: str | None) -> int:
    if not cabin:
        return -1
    return _RANK_MAP.get(cabin.strip().lower(), -1)


def is_cabin_compliant(actual: str | None, allowed_spec: str | None) -> bool:
    if not actual or not allowed_spec:
        return True  # 缺失数据不拦截
    actual_rank = rank_of(actual)
    for allowed in _split_spec(allowed_spec):
        allowed_rank = rank_of(allowed)
        if actual_rank >= 0 and allowed_rank >= 0:
            if actual_rank <= allowed_rank:
                return True
        else:
            a, b = actual.lower(), allowed.lower()
            if a == b or b in a or a in b:
                return True
    return False


def _split_spec(spec: str) -> list[str]:
    return [p for p in re.split(r"[,，/]", spec) if p.strip()]


class TravelPolicyService:
    def get_policy(self, user_id: str, destination_city: str) -> dict[str, Any]:
        level = self._resolve_user_level(user_id)
        city_tier = self._resolve_city_tier(destination_city)
        rule = policy_repository.find_by_level_and_tier(_parse_level_num(level), self._policy_tier(city_tier))
        if rule is None:
            raise RuntimeError(f"未找到匹配的差旅政策规则：level={level}, cityTier={city_tier}")
        return {
            "userLevel": level,
            "destinationCity": destination_city,
            "cityTier": city_tier,
            "flightClass": rule.flight_class,
            "trainSeatClass": rule.train_seat_class,
            "hotelLimit": rule.hotel_limit,
            "hotelStarLimit": rule.hotel_star_limit,
            "dailyMealLimit": rule.daily_meal_limit,
            "dailyTransportLimit": rule.daily_transport_limit,
            "approvalThreshold": rule.approval_threshold,
            "advanceBookingDays": rule.advance_booking_days,
        }

    def check_compliance(self, order_summary: str | dict, policy: dict[str, Any]) -> dict[str, Any]:
        result = {"compliant": True, "violations": [], "suggestions": []}
        order = json.loads(order_summary) if isinstance(order_summary, str) else order_summary
        if not order:
            return result

        biz_type = str(order.get("type", "")).upper()
        amount = float(order.get("amount") or 0)

        if biz_type == "FLIGHT":
            flight_class = order.get("flightClass")
            if flight_class and not is_cabin_compliant(flight_class, policy.get("flightClass")):
                result["compliant"] = False
                result["violations"].append(f"机票舱位超出政策标准：允许 {policy.get('flightClass')}")
        elif biz_type == "HOTEL":
            if amount <= 0:
                result["compliant"] = False
                result["violations"].append(f"酒店订单金额缺失或无效（amount={amount}），无法校验合规性")
                result["suggestions"].append("请在 order_summary 中提供正确的酒店金额后重新校验")
            elif amount > policy.get("hotelLimit", 0):
                result["compliant"] = False
                result["violations"].append(f"酒店金额超出政策上限：{policy.get('hotelLimit')}")
                result["suggestions"].append(f"建议选择 {policy.get('hotelLimit')} 元以下酒店")
        elif biz_type == "TRAIN":
            seat_class = order.get("seatClass")
            if seat_class and not is_cabin_compliant(seat_class, policy.get("trainSeatClass")):
                result["compliant"] = False
                result["violations"].append(f"火车座位超出政策标准：允许 {policy.get('trainSeatClass')}")
        return result

    # ---------------- 私有 ----------------

    def _resolve_user_level(self, user_id: str) -> str:
        profile = user_repository.get_profile(user_id)
        if profile is None:
            raise ValueError(f"用户档案不存在，无法获取差旅政策：userId={user_id}")
        if not profile.level:
            raise RuntimeError(f"用户未配置职级，无法获取差旅政策：userId={user_id}")
        return profile.level

    def _resolve_city_tier(self, city: str | None) -> str:
        if not city:
            return "其他"
        settings = get_settings()
        c = city.strip()
        if c in settings.tier1_cities:
            return "一线"
        if c in settings.new_tier1_cities:
            return "新一线"
        if c in settings.tier2_cities:
            return "二线"
        return "其他"

    @staticmethod
    def _policy_tier(city_tier: str) -> str:
        # 政策规则表城市等级仅区分：一线 / 新一线 / 其他
        return city_tier if city_tier in ("一线", "新一线") else "其他"


def _parse_level_num(level: str) -> int:
    digits = re.sub(r"[^0-9]", "", level)
    if not digits:
        raise ValueError(f"职级格式无效，无法解析数字：{level}")
    return int(digits)


travel_policy_service = TravelPolicyService()
