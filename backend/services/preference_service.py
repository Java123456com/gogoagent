"""用户偏好配置与长期记忆同步服务。

选项结构与前端偏好页保持一致；本地存储使用 user_preference 表，并可同步远程记忆。
"""
from __future__ import annotations

import json
from typing import Any

from backend.infrastructure.llm import fast_model, invoke_text
from backend.infrastructure.repositories import preference_repository
from backend.memory.long_term import long_term_memory

OPTIONS = [
    {"category": "flight", "label": "机票偏好", "icon": "✈️", "items": [
        {"key": "flight_cabin", "label": "舱位偏好", "type": "single", "options": ["经济舱", "超级经济舱", "公务舱", "头等舱"]},
        {"key": "flight_airline", "label": "偏好航司", "type": "multi", "options": ["国航(CA)", "东航(MU)", "南航(CZ)", "海航(HU)", "厦航(MF)", "深航(ZH)", "川航(3U)", "春秋(9C)", "吉祥(HO)", "山航(SC)"]},
        {"key": "flight_seat", "label": "座位位置", "type": "single", "options": ["靠窗", "靠过道", "前排", "紧急出口排", "无偏好"]},
        {"key": "flight_time", "label": "航班时间", "type": "single", "options": ["早班(6:00-9:00)", "上午(9:00-12:00)", "下午(12:00-18:00)", "晚班(18:00-21:00)", "红眼航班也可以", "无偏好"]},
        {"key": "flight_direct", "label": "中转偏好", "type": "single", "options": ["只选直飞", "可接受一次中转", "价格优先不限中转", "无偏好"]},
    ]},
    {"category": "hotel", "label": "酒店偏好", "icon": "🏨", "items": [
        {"key": "hotel_star", "label": "星级偏好", "type": "single", "options": ["经济型", "舒适型(三星)", "高档型(四星)", "豪华型(五星)", "无偏好"]},
        {"key": "hotel_brand", "label": "偏好品牌", "type": "multi", "options": ["全季", "亚朵", "如家商旅", "汉庭", "维也纳", "桔子", "希尔顿", "万豪", "洲际", "凯悦", "香格里拉", "华住"]},
        {"key": "hotel_room", "label": "房型偏好", "type": "single", "options": ["大床房", "双床房", "无偏好"]},
        {"key": "hotel_floor", "label": "楼层偏好", "type": "single", "options": ["高楼层", "低楼层(方便出行)", "无偏好"]},
        {"key": "hotel_location", "label": "位置偏好", "type": "multi", "options": ["靠近办公/会议地点", "靠近地铁/交通枢纽", "靠近市中心", "安静环境", "有停车场"]},
        {"key": "hotel_facilities", "label": "设施需求", "type": "multi", "options": ["健身房", "早餐", "免费Wi-Fi", "商务中心", "洗衣服务", "接机服务"]},
    ]},
    {"category": "train", "label": "高铁/火车偏好", "icon": "🚄", "items": [
        {"key": "train_seat", "label": "座位等级", "type": "single", "options": ["二等座", "一等座", "商务座", "无偏好"]},
        {"key": "train_time", "label": "出发时段", "type": "single", "options": ["早班(6:00-9:00)", "上午(9:00-12:00)", "下午(12:00-18:00)", "晚班(18:00-21:00)", "无偏好"]},
        {"key": "train_position", "label": "座位位置", "type": "single", "options": ["靠窗", "靠过道", "无偏好"]},
    ]},
    {"category": "general", "label": "出行习惯", "icon": "🧳", "items": [
        {"key": "transport", "label": "市内交通", "type": "single", "options": ["地铁/公交优先", "打车优先", "自驾/租车", "无偏好"]},
        {"key": "reimburse_priority", "label": "费用敏感度", "type": "single", "options": ["价格优先(尽量省钱)", "性价比优先(合理范围选舒适)", "体验优先(预算内最舒适)", "无偏好"]},
        {"key": "schedule_priority", "label": "时间安排", "type": "single", "options": ["尽量当天往返", "提前一晚到达", "会议/办事结束当天返回", "灵活安排", "无偏好"]},
        {"key": "meal", "label": "餐饮偏好", "type": "multi", "options": ["无特殊要求", "清淡饮食", "素食", "清真", "无辣", "不含海鲜", "无麸质"]},
    ]},
]


class PreferenceService:
    def options(self) -> list[dict[str, Any]]:
        return OPTIONS

    def get(self, user_id: str) -> dict[str, Any]:
        preferences = preference_repository.get_all(user_id)
        decoded = {}
        for key, value in preferences.items():
            try:
                decoded[key] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                decoded[key] = value

        # Bailian can provide the authoritative profile. Keep the local
        # form mirror as a fallback when the provider is disabled/unavailable.
        memories = long_term_memory.retrieve(
            user_id,
            limit=10,
            query="我的差旅偏好，包括机票、酒店、火车、餐饮、交通",
        ) if long_term_memory.provider else []
        summary = "；".join(memories) if memories else "；".join(
            f"{key}: {value}" for key, value in decoded.items()
        )
        parsed = self._parse_memory(summary) if memories else decoded
        return {"userId": user_id, "preferences": parsed or decoded,
                "memorySummary": summary}

    def save(self, user_id: str, preferences: dict[str, Any]) -> None:
        preference_repository.save(user_id, preferences)
        summary = "；".join(f"{key}: {value}" for key, value in preferences.items() if value not in (None, ""))
        if summary:
            long_term_memory.record(user_id, summary, memory_type="preference_form")

    @staticmethod
    def _parse_memory(summary: str) -> dict[str, Any]:
        if not summary:
            return {}
        prompt = """你是 JSON 提取器。根据差旅偏好描述提取结构化 JSON。
只输出 JSON；字段只能使用前端定义的 preference key；未提及的字段不要输出。
single 输出字符串，multi 输出字符串数组。无法匹配的值忽略。
可用字段：flight_cabin, flight_airline, flight_seat, flight_time, flight_direct,
hotel_star, hotel_brand, hotel_room, hotel_floor, hotel_location, hotel_facilities,
train_seat, train_time, train_position, transport, reimburse_priority,
schedule_priority, meal。
"""
        text = invoke_text(fast_model(), prompt, summary, "{}")
        text = text.strip()
        if text.startswith("```"):
            text = text.removeprefix("```").removeprefix("json").removesuffix("```").strip()
        try:
            result = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return {}
        return result if isinstance(result, dict) else {}


preference_service = PreferenceService()
