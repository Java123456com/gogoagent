from backend.agents.base import BaseSubAgent
from backend.core.request_context import apply_context_to_state, require_context
from backend.infrastructure.llm import strong_model
from backend.memory.session import agent_session_store
from backend.runtime.agent_executor import get_subagent_executor
from backend.tools._common import tool
from backend.tools.interaction import UserInteractionRequired
from backend.tools.interaction import tools as interaction_tools
from backend.tools.memory import tools as memory_tools
from pydantic import BaseModel, ConfigDict, Field


class SubAgentRequest(BaseModel):
    """LLM-visible schema for every Master → sub-Agent tool call."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, description="需要子智能体处理的任务描述")


def _subagent_tool(name, description, agent_name):
    @tool(name, args_schema=SubAgentRequest)
    def call_subagent(request: str) -> str:
        """调用 GoGo 子智能体处理差旅子任务。

        用户和会话身份由可信请求上下文注入，不能由模型作为工具参数
        提供或覆盖。
        """
        context = require_context()
        child_context = context.derive(agent_name=agent_name, deadline_at=None)
        session_id = child_context.session_id
        suspended = False
        if session_id:
            agent_session_store.set_active_agent(session_id, agent_name)
        try:
            context_state = apply_context_to_state({"request": request}, child_context)
            result = get_subagent_executor().execute(
                agent_name, context_state, execution_context=child_context,
            )
            pending = result.get("pending_interaction") or {}
            if pending:
                suspended = True
                pending.setdefault("agent_name", agent_name)
                raise UserInteractionRequired(pending)
            return result.get("final", str(result))
        finally:
            # Java's ActiveAgentPersistenceHook records the last non-master
            # ReAct agent and leaves it available for exact continuation
            # signals on the next chat turn.  The next ordinary request starts
            # a fresh pipeline and explicitly resets the router to MasterAgent.
            if session_id and suspended:
                agent_session_store.set_active_agent(session_id, agent_name)
    call_subagent.description = description
    return call_subagent


class MasterAgent(BaseSubAgent):
    prompt_file = "master-agent-system.md"
    model_factory = staticmethod(strong_model)
    max_iterations = 15
    def __init__(self):
        # Java MasterAgent 的 Toolkit 只注册这四个真实子 Agent。
        # ItineraryReviewAgent 是已废弃的独立 Bean，ReimbursementAgent 的
        # build() 返回 null，因此二者不能被伪装成可调度的 Master 子 Agent。
        tools = interaction_tools() + memory_tools() + [
            _subagent_tool("itinerary_manage_agent", "处理差旅单申请、审批、修改和取消", "ItineraryManageAgent"),
            _subagent_tool("itinerary_plan_agent", "搜索、规划和审核交通酒店方案", "ItineraryPlanAgent"),
            _subagent_tool("info_agent", "查询差旅政策、景点、签证、天气信息", "InfoAgent"),
            _subagent_tool("booking_agent", "执行机票、酒店、火车票预订或取消", "BookingAgent"),
        ]
        super().__init__(tools)
    def fallback(self, state):
        executor = get_subagent_executor()
        request = state.get("request", "")
        intent = (state.get("intent_json") or {}).get("primary_intent")
        intents = {
            item.get("intent")
            for item in (state.get("intent_json") or {}).get("intents", [])
            if isinstance(item, dict)
        }
        if len(intents) > 1:
            results = []
            agents = []
            for current in intents:
                if current in {"policy_query", "attractions_query", "general_info"}:
                    agent_name = "InfoAgent"
                elif current == "reimbursement":
                    results.append("报销 Agent 在 Java 原版中尚未实现，当前不能执行发票识别或报销提交。")
                    continue
                elif current == "booking":
                    agent_name = "BookingAgent"
                else:
                    agent_name = "ItineraryPlanAgent"
                result = executor.execute(agent_name, state)
                agents.append(result)
                results.append(result.get("final", ""))
            return {
                "final": "\n\n".join(item for item in results if item),
                "trace": [
                    {"agent": "MasterAgent", "output": f"已拆分处理 {len(agents)} 个子任务"},
                    *(item for result in agents for item in result.get("trace", [])),
                ],
            }
        if intent in {"policy_query", "attractions_query", "general_info"}:
            result = executor.execute("InfoAgent", state)
        elif intent == "booking" or any(x in request for x in ("预订", "订票", "订酒店")):
            result = executor.execute("BookingAgent", state)
        elif intent == "reimbursement" or any(x in request for x in ("报销", "发票", "报销单")):
            return {
                **state,
                "final": "报销 Agent 在 Java 原版中尚未实现，当前不能执行发票识别或报销提交。",
                "trace": [{"agent": "MasterAgent", "output": "ReimbursementAgent 为未实现占位"}],
            }
        elif intent in {
            "travel_application", "approval_query", "travel_order_query",
            "travel_cancel", "travel_modify",
        } or any(x in request for x in ("申请", "审批", "取消差旅")):
            result = executor.execute("ItineraryManageAgent", state)
        else: result = executor.execute("ItineraryPlanAgent", state)
        return {**result, "trace": [{"agent": "MasterAgent", "output": "已路由子智能体"}, *result.get("trace", [])]}


master_agent = MasterAgent()
