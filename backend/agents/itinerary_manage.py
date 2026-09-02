import re

from backend.agents.base import BaseSubAgent
from backend.infrastructure.repositories import approval_repository, travel_order_repository
from backend.tools.booking import read_tools as booking_read_tools
from backend.tools.conflict import tools as conflict_tools
from backend.tools.order import tools as order_tools
from backend.tools.user_info import tools as user_info_tools

_CITY_PATTERN = r"(北京|上海|广州|深圳|成都|杭州|重庆|武汉|西安|苏州|天津|南京|长沙|郑州|东莞|青岛|沈阳|宁波|昆明|厦门|合肥|佛山|无锡|哈尔滨|济南|福州|大连|贵阳|太原|南昌|南宁|石家庄|长春|呼和浩特|兰州|乌鲁木齐|海口|银川|西宁|拉萨)"
_DATE_PATTERN = r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"


class ItineraryManageAgent(BaseSubAgent):
    prompt_file = "itinerary-manage-agent-system.md"
    model_factory = staticmethod(__import__("backend.infrastructure.llm", fromlist=["strong_model"]).strong_model)
    max_iterations = 10

    def __init__(self): super().__init__(order_tools() + booking_read_tools() + conflict_tools() + user_info_tools())

    def fallback(self, state):
        question = "\n".join(filter(None, [state.get("original_question", ""), state.get("request", "")]))
        user_id = state.get("user_id", "u_001")
        orders = travel_order_repository.list_by_user(user_id)
        if any(word in question for word in ("审批", "差旅单", "出差记录", "我的出差")):
            rows = []
            for order in orders:
                approval = approval_repository.find_latest_by_order_id(order.order_id)
                rows.append(
                    f"{order.order_id}: {order.departure_city or '?'}→{order.destination or '?'} "
                    f"{order.departure_date or '?'}~{order.return_date or '?'}，"
                    f"差旅单{order.status}，审批{approval.status if approval else '无'}"
                )
            return {
                "final": "\n".join(rows) or "当前没有差旅单。",
                "trace": [{"agent": "ItineraryManageAgent", "output": "查询差旅单与审批"}],
            }
        if any(word in question for word in ("取消", "撤回", "撤销")):
            order_id = next(
                (value for value in re.findall(r"(?:to|TO)_[A-Za-z0-9_]+", question)),
                None,
            )
            target = travel_order_repository.get(order_id) if order_id else (orders[0] if len(orders) == 1 else None)
            if target is None:
                return {
                    "final": "请提供要取消的差旅单号。",
                    "pending_interaction": {"ui_type": "text", "question": "请输入要取消的差旅单号"},
                    "trace": [{"agent": "ItineraryManageAgent", "output": "等待取消目标"}],
                }
            return {
                "final": f"确认取消差旅单 {target.order_id}（{target.destination}，{target.departure_date}~{target.return_date}）吗？",
                "pending_interaction": {
                    "ui_type": "confirm", "order_id": target.order_id,
                    "question": f"是否取消差旅单 {target.order_id}？",
                },
                "trace": [{"agent": "ItineraryManageAgent", "output": "取消前确认"}],
            }
        if any(word in question for word in ("申请", "我要出差", "发起", "报备")):
            cities = re.findall(_CITY_PATTERN, question)
            dates = [item.replace("/", "-") for item in re.findall(_DATE_PATTERN, question)]
            if len(cities) >= 2 and len(dates) >= 2:
                purpose = _extract_purpose(question) or "商务出差"
                fields = {
                    "destination": cities[1], "departure_city": cities[0],
                    "departure_date": dates[0], "return_date": dates[1], "purpose": purpose,
                }
                return {
                    "final": (f"请确认提交差旅审批：{fields['departure_city']} → {fields['destination']}，"
                              f"{fields['departure_date']} 至 {fields['return_date']}，事由：{purpose}。"),
                    "pending_interaction": {
                        "ui_type": "confirm", "action": "submit_travel_approval", "fields": fields,
                        "question": "是否确认提交差旅审批？",
                    },
                    "trace": [{"agent": "ItineraryManageAgent", "output": "差旅申请信息待确认"}],
                }
            return {
                "final": "可以帮你发起差旅申请，请补充出发城市、目的地、出发日期、返程日期和出差事由。",
                "pending_interaction": {
                    "ui_type": "form",
                    "fields": ["departure_city", "destination", "departure_date", "return_date", "purpose"],
                },
                "trace": [{"agent": "ItineraryManageAgent", "output": "收集差旅申请字段"}],
            }
        return {"final": "请说明要查询、申请、修改还是取消哪一张差旅单。",
                "trace": [{"agent": "ItineraryManageAgent", "output": "等待差旅单操作"}]}


def _extract_purpose(question: str) -> str | None:
    match = re.search(
        rf"(?:{_DATE_PATTERN})\s*(?:到|至|至|—|-)\s*(?:{_DATE_PATTERN})\s*[，,、;；:]?\s*(?:事由[:：]?\s*)?([^\n，,。；;]+)",
        question,
    )
    if match:
        value = match.group(1).strip()
        if value and value not in {"用户补充", "请提交"}:
            return value
    for keyword in ("客户会议", "商务会议", "客户拜访", "项目培训", "现场支持", "商务出差"):
        if keyword in question:
            return keyword
    return None


itinerary_manage_agent = ItineraryManageAgent()
