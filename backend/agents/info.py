from backend.agents.base import BaseSubAgent
from backend.infrastructure.llm import stable_model
from backend.rag.knowledge import travel_knowledge
from backend.services.policy_service import travel_policy_service
from backend.tools.live import tools as live_tools
from backend.tools.policy import tools as policy_tools
from backend.tools.knowledge import tools as knowledge_tools


class InfoAgent(BaseSubAgent):
    prompt_file = "info-agent-system.md"
    model_factory = staticmethod(stable_model)
    max_iterations = 5
    def __init__(self): super().__init__(policy_tools() + live_tools() + knowledge_tools())
    def fallback(self, state):
        question = state.get("original_question") or state.get("request", "")
        user_id = state.get("user_id", "u_001")
        if any(word in question for word in ("政策", "差标", "餐标", "住宿标准", "舱位标准")):
            city = travel_knowledge.extract_city(question) or "北京"
            try:
                policy = travel_policy_service.get_policy(user_id, city)
                final = (
                    f"{city}差旅政策（{policy['userLevel']}）："
                    f"机票{policy['flightClass']}，火车{policy['trainSeatClass']}，"
                    f"酒店每晚不超过{policy['hotelLimit']}元，"
                    f"餐补{policy['dailyMealLimit']}元/天，"
                    f"交通补贴{policy['dailyTransportLimit']}元/天，"
                    f"提前{policy['advanceBookingDays']}天预订。"
                )
                return {"final": final, "policy": policy,
                        "trace": [{"agent": "InfoAgent", "output": "确定性政策查询"}]}
            except (ValueError, RuntimeError) as error:
                return {"final": str(error),
                        "trace": [{"agent": "InfoAgent", "output": "政策查询失败"}]}

        evidence = travel_knowledge.retrieve(question)
        final = "\n".join(
            f"[{item['source']}] {item['content']}"
            for item in evidence if item.get("score", 0) > 0
        )
        return {
            "final": final or "当前未检索到相关知识库内容；可以补充城市、日期或具体问题。",
            "evidence": evidence,
            "trace": [{"agent": "InfoAgent", "output": f"RAG 检索 {len(evidence)} 条"}],
        }


info_agent = InfoAgent()
