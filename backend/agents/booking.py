from backend.agents.base import BaseSubAgent
from backend.infrastructure.llm import strong_model_with_thinking
from backend.services.booking_service import booking_service
from backend.tools.apikey import tools as api_key_tools
from backend.tools.booking import tools as booking_tools
from backend.tools.memory import tools as memory_tools
from backend.tools.order import read_tools as order_read_tools
from backend.tools.skills import tools as skill_tools
from backend.tools.user_info import tools as user_info_tools


class BookingAgent(BaseSubAgent):
    prompt_file = "booking-agent-system.md"
    model_factory = staticmethod(strong_model_with_thinking)
    max_iterations = 10
    def __init__(self): super().__init__(booking_tools() + order_read_tools() + user_info_tools() + api_key_tools() + memory_tools() + skill_tools())
    def fallback(self, state):
        question = state.get("original_question") or state.get("request", "")
        user_id = state.get("user_id", "u_001")
        records = booking_service.list_by_user(user_id)
        if any(word in question for word in ("取消", "退订", "退票")):
            return {
                "final": "已找到你的预订记录。请提供 booking_id，并确认取消对象后继续。",
                "booking_options": records,
                "pending_interaction": {"ui_type": "confirm", "question": "请输入 booking_id 并确认取消"},
                "trace": [{"agent": "BookingAgent", "output": "等待预订取消确认"}],
            }
        if records:
            text = "\n".join(
                f"{item['booking_id']}: {item.get('title') or item['biz_type']}，状态{item['status']}"
                for item in records
            )
            return {"final": text, "booking_options": records,
                    "trace": [{"agent": "BookingAgent", "output": "查询预订记录"}]}
        return {"final": "当前没有预订记录。请先提供已审批的差旅单和要预订的交通或酒店方案。",
                "trace": [{"agent": "BookingAgent", "output": "缺少可预订方案"}]}


booking_agent = BookingAgent()
