from __future__ import annotations

import json

from langgraph.graph import END, START, StateGraph

from backend.agents.intent_recognition import intent_recognition_agent
from backend.agents.master import master_agent
from backend.agents.query_rewriting import query_rewriting_agent
from backend.core.state import TravelAgentState
from backend.intent.router import intent_router
from backend.memory.session import agent_session_store
from backend.services.chat_service import chat_service
from backend.services.llm_services import conversation_title_service
from backend.services.runtime_events import emit_event


def _trace(state, agent, output): return [*state.get("trace", []), {"agent": agent, "output": output}]


def fast_intent_node(state: TravelAgentState) -> TravelAgentState:
    question = state.get("original_question") or state.get("request", "")
    result = intent_router.route(question)
    if result is None: return {**state, "original_question": question, "intent_json": {}, "trace": _trace(state, "IntentRecognitionRouter", "L1/L2 未命中，准备改写")}
    data = result.to_dict()
    _schedule_title(state, question, data)
    return {**state, "original_question": question, "intent_json": data, "intent": data.get("primary_intent", ""), "trace": _trace(state, "IntentRecognitionRouter", f"{data.get('source')} 命中")}


def route_after_fast(state):
    if state.get("active_resume_agent") == "IntentRecognitionAgent":
        return "full_intent"
    if state.get("active_resume_agent") == "QueryRewritingAgent":
        return "rewrite"
    if state.get("continuation"):
        return "master_dispatch"
    return "master_dispatch" if state.get("intent_json") else "rewrite"


def rewrite_node(state):
    if state.get("session_id"):
        agent_session_store.set_active_agent(state["session_id"], "QueryRewritingAgent")
    question = state.get("original_question", state.get("request", ""))
    history = ""
    session_id = state.get("session_id")
    if session_id:
        try:
            rows = chat_service.find_recent_messages(session_id, limit=11)
            # Java persists the current user message before building rewrite
            # input (limit + 1), but passes it only once to the agent.  Keep
            # the ten preceding records here because ``question`` is passed
            # separately below.
            if rows and rows[-1].get("content") == question:
                rows = rows[:-1]
            history = "\n".join(
                f"{item.get('role', 'assistant')}: {item.get('content', '')}"
                for item in rows if item.get("content")
            )
        except Exception:  # noqa: BLE001 - rewrite can degrade to current text
            history = ""
    rewritten_raw = query_rewriting_agent.invoke(question, history=history)
    rewritten = _extract_rewritten_question(rewritten_raw) or question
    thinking = _format_query_rewriting_thinking(rewritten)
    if thinking:
        emit_event("thinking", {"agentName": "QueryRewritingAgent", "text": thinking})
    return {**state, "rewritten_question": rewritten, "trace": _trace(state, "QueryRewritingAgent", "已完成上下文补全与指代消解")}


def full_intent_node(state):
    if state.get("session_id"):
        agent_session_store.set_active_agent(state["session_id"], "IntentRecognitionAgent")
    question = state.get("rewritten_question") or state.get("original_question") or state.get("request", "")
    data = intent_recognition_agent.invoke(question)
    _schedule_title(state, question, data)
    thinking = _format_intent_thinking(data)
    if thinking:
        emit_event("thinking", {"agentName": "IntentRecognitionAgent", "text": thinking})
    return {**state, "intent_json": data, "intent": data.get("primary_intent", "GENERAL"), "trace": _trace(state, "IntentRecognitionAgent", "L3 意图识别完成")}


def _schedule_title(state: TravelAgentState, question: str, intent_json: dict) -> None:
    session_id, user_id = state.get("session_id"), state.get("user_id")
    if session_id and user_id and question:
        conversation_title_service.schedule_update(session_id, user_id, question, intent_json)


def _format_query_rewriting_thinking(value) -> str:
    if not isinstance(value, dict):
        try:
            value = json.loads(str(value))
        except (TypeError, ValueError):
            return ""
    parts = []
    if value.get("related") is False:
        parts.append("《全新问题，与历史对话无关》")
    if value.get("reason"):
        parts.append(f"原因：{str(value['reason']).strip()}")
    if value.get("rewritten_question"):
        parts.append(f"改写结果：{str(value['rewritten_question']).strip()}")
    return "\n".join(parts).strip()


def _format_intent_thinking(value: dict) -> str:
    labels = {
        "itinerary_planning": "行程规划", "flight_booking": "机票预订",
        "hotel_booking": "酒店预订", "train_booking": "高铁预订",
        "reimbursement": "报销处理", "travel_order_query": "订单查询",
        "approval": "审批申请", "approval_query": "审批查询",
        "info_query": "信息查询", "general_info": "信息查询",
        "cancel_order": "取消订单", "travel_cancel": "取消订单",
        "modify_order": "修改订单", "travel_modify": "修改订单",
        "policy_query": "差旅政策", "attractions_query": "景点查询",
        "booking": "预订", "travel_application": "差旅申请",
    }
    confidence = {"high": "高", "medium": "中", "low": "低"}
    items = value.get("intents") if isinstance(value, dict) else None
    if not isinstance(items, list) or not items:
        return ""
    rendered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent") or "")
        label = labels.get(intent, intent)
        level = item.get("confidence")
        rendered.append(f"{label}（置信度：{confidence.get(str(level).lower(), level)}）"
                        if level else label)
    if not rendered:
        return ""
    text = "识别结果：" + "、".join(rendered)
    if value.get("overall_reason"):
        text += f"\n原因：{str(value['overall_reason']).strip()}"
    return text


def master_dispatch_node(state):
    state = {**state, "active_agent": "MasterAgent", "active_agent_name": "MasterAgent"}
    if state.get("session_id"):
        agent_session_store.set_active_agent(state["session_id"], "MasterAgent")
    question = state.get("original_question") or state.get("request", "")
    # Java AgentPipelineService.buildMasterInput prepends both pipeline
    # artifacts as SYSTEM messages before the original conversation.
    pipeline_messages = []
    if state.get("rewritten_question"):
        pipeline_messages.append(("system", "问题改写结果：\n" + str(state["rewritten_question"])))
    if state.get("intent_json"):
        pipeline_messages.append(("system", "意图识别结果：\n" + json.dumps(
            state["intent_json"], ensure_ascii=False,
        )))
    master_state = {
        **state,
        "request": question,
        "messages": [*pipeline_messages, *(state.get("messages") or [])],
    }
    result = master_agent.invoke(master_state)
    messages = result.get("messages", [])
    last = messages[-1] if messages else {}
    final = result.get("final") or (last.get("content", "") if isinstance(last, dict) else getattr(last, "content", ""))
    return {**state, **result, "final": final, "trace": _trace(state, "MasterAgent", "完成子智能体路由") + result.get("trace", [])}


def _extract_rewritten_question(value) -> str:
    """Parse QueryRewritingAgent JSON output like Java's parseRewrittenQuestion."""
    if isinstance(value, dict):
        return str(value.get("rewritten_question") or value.get("rewrittenQuestion") or "").strip()
    text = str(value or "").strip()
    if text.startswith("```"):
        start, end = text.find("{"), text.rfind("}")
        text = text[start:end + 1] if start >= 0 and end > start else text
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(parsed, dict):
        return str(parsed.get("rewritten_question") or parsed.get("rewrittenQuestion") or text).strip()
    return text


def build_pipeline_graph():
    graph = StateGraph(TravelAgentState)
    for name, node in (("fast_intent", fast_intent_node), ("rewrite", rewrite_node), ("full_intent", full_intent_node), ("master_dispatch", master_dispatch_node)): graph.add_node(name, node)
    graph.add_edge(START, "fast_intent")
    graph.add_conditional_edges(
        "fast_intent",
        route_after_fast,
        {"rewrite": "rewrite", "full_intent": "full_intent", "master_dispatch": "master_dispatch"},
    )
    graph.add_edge("rewrite", "full_intent"); graph.add_edge("full_intent", "master_dispatch"); graph.add_edge("master_dispatch", END)
    return graph.compile()


pipeline_graph = build_pipeline_graph()
