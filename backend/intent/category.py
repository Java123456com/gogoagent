"""意图分类枚举（对应 Java IntentCategory）。

枚举值与 ``intent-recognition-agent-system.md`` 中定义的意图类别一一对应，
每种意图标注默认目标子智能体（供 MasterAgent 路由）。
"""
from __future__ import annotations

from enum import Enum


class IntentCategory(Enum):
    TRAVEL_APPLICATION = ("travel_application", "ItineraryManageAgent", "用户要提交新的差旅申请/出差审批")
    TRAVEL_CANCEL = ("travel_cancel", "ItineraryManageAgent", "用户要取消出差申请或审批单")
    TRAVEL_MODIFY = ("travel_modify", "ItineraryManageAgent", "用户要修改差旅申请信息")
    APPROVAL_QUERY = ("approval_query", "ItineraryManageAgent", "用户查询审批进度/状态/结果")
    TRAVEL_ORDER_QUERY = ("travel_order_query", "ItineraryManageAgent", "用户查询已有差旅单详情/状态")
    ITINERARY_PLANNING = ("itinerary_planning", "ItineraryPlanAgent", "用户要求规划行程、做方案")
    FLIGHT_SEARCH = ("flight_search", "ItineraryManageAgent", "用户要查航班")
    TRAIN_SEARCH = ("train_search", "ItineraryManageAgent", "用户要查火车")
    HOTEL_SEARCH = ("hotel_search", "ItineraryManageAgent", "用户要查酒店")
    BOOKING = ("booking", "BookingAgent", "用户要预订/改签/取消已选方案")
    REIMBURSEMENT = ("reimbursement", "ReimbursementAgent", "用户要报销、识别发票、生成报销单")
    POLICY_QUERY = ("policy_query", "InfoAgent", "用户查询差旅政策/餐标/酒店标准/签证入境政策")
    ATTRACTIONS_QUERY = ("attractions_query", "InfoAgent", "用户查询目的地景点、旅游信息")
    GENERAL_INFO = ("general_info", "InfoAgent", "天气/地图/交通/目的地新闻等通用信息查询")
    GREETING = ("greeting", "MasterAgent", "用户打招呼、寒暄")
    UNKNOWN = ("unknown", "MasterAgent", "无法明确分类或信息严重不足")

    def __init__(self, code: str, default_target_agent: str, description: str):
        self.code = code
        self.default_target_agent = default_target_agent
        self.description = description

    @classmethod
    def from_code(cls, code: str | None) -> IntentCategory:
        if not code:
            return cls.UNKNOWN
        for c in cls:
            if c.code == code:
                return c
        return cls.UNKNOWN
