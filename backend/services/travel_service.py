import json
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage

from backend.core.state import TravelAgentState
from backend.infrastructure.llm import llm_enabled
from backend.infrastructure.repositories import travel_order_repository
from backend.infrastructure.stores import session_execution_fence
from backend.memory.cache import agent_session_manager
from backend.memory.context import ContextCompressionHook, LayeredContextManager
from backend.memory.execution import execution_registry
from backend.memory.long_term import long_term_memory
from backend.memory.session import agent_session_store
from backend.memory.store import PersistentSessionStore
from backend.runtime.agent_executor import get_subagent_executor
from backend.services.interrupt_broadcast import interrupt_broadcast
from backend.services.order_service import travel_order_service
from backend.tools.order import submit_travel_approval
from backend.workflow.travel_graph import travel_graph


class TravelAgentService:
    def __init__(self, store=None, memory_manager: LayeredContextManager | None = None):
        self.store = store or PersistentSessionStore()
        self.memory_manager = memory_manager or LayeredContextManager(
            self.store, long_term_memory, ContextCompressionHook(),
            # Java uses AGENT_CONTROL for LLM agents.  Deterministic mode has
            # no model tool call, so it may prefetch preferences for parity of
            # the local demo experience.
            auto_retrieve_long_term=not llm_enabled(),
        )

    def run(self, request: str, user_id: str = "demo-user", session_id: str | None = None,
            continuation: bool = False, active_resume_agent: str | None = None) -> TravelAgentState:
        session_id = session_id or str(uuid4())
        generation = session_execution_fence.begin(session_id)
        handle = execution_registry.begin(session_id, generation=generation)
        agent_session_manager.register(session_id, handle)
        agent_session_store.set_active_agent(session_id, "MasterAgent")
        try:
            previous = self.memory_manager.load(session_id)
            if previous.get("user_id") not in (None, user_id):
                raise PermissionError("无权访问该会话")
            context_question = request
            if continuation and previous.get("original_question"):
                # Java continuation calls the same agent with the persisted
                # conversation, so a short answer such as "上海" retains the
                # original travel-application intent.
                context_question = f"{previous['original_question']}\n用户补充：{request}"
            state: TravelAgentState = {
                **previous,
                "request": request,
                "original_question": context_question,
                "user_id": user_id,
                "session_id": session_id,
                "request_id": uuid4().hex,
                "execution_generation": generation,
                "messages": [*(previous.get("memory_original_messages") or previous.get("messages", [])),
                             HumanMessage(content=request)],
                "trace": [],
                "interrupted": False,
                "continuation": continuation,
                "active_resume_agent": active_resume_agent,
                "resume_messages": [],
            }
            state = self.memory_manager.prepare(state)
            self._bind_interrupt_checkpoint(handle, state)
            if handle.stop.is_set():
                result = self._interrupted_result(state)
            else:
                try:
                    result = travel_graph.invoke(state)
                except Exception:
                    if handle.stop.is_set():
                        result = self._interrupted_result(state)
                    else:
                        raise
                if handle.stop.is_set():
                    result = self._interrupted_result(result)
            if not self._is_current(session_id, generation):
                result = self._interrupted_result(result)
            else:
                pending = result.get("pending_interaction") or {}
                if pending:
                    pending = _normalize_pending(pending)
                    result = {**result, "pending_interaction": pending}
                    agent_session_store.set_pending_tool(session_id, pending)
                else:
                    agent_session_store.clear_pending_tool(session_id)
                self.memory_manager.save(session_id, result)
            return result
        finally:
            if handle.stop.is_set():
                agent_session_store.clear_pending_tool(session_id)
            agent_session_manager.remove(session_id)
            execution_registry.end(session_id, handle)

    def run_active(self, request: str, user_id: str, session_id: str) -> TravelAgentState:
        """Continue the Java ``activeAgent`` on an exact continuation signal.

        This path is used by ``POST /chat/{sessionId}`` for short replies such
        as "确认" or "继续".  ``POST /chat/respond`` remains the explicit
        ToolSuspend resume endpoint and is handled by :meth:`resume`.
        """
        previous = self.memory_manager.load(session_id)
        if not previous:
            return self.run(request, user_id, session_id)
        if previous.get("user_id") not in (None, user_id):
            raise PermissionError("无权访问该会话")

        pending = previous.get("pending_interaction") or agent_session_store.get_pending_tool(session_id)
        if pending:
            # A pending ask_user has a concrete ToolResult resume protocol.  A
            # chat continuation is still accepted as a convenience, but uses
            # the same validated path and tool id as /respond.
            return self.resume(session_id, request, user_id, pending.get("toolUseId"))

        agent_name = agent_session_store.get_active_agent(session_id)
        if agent_name in {"QueryRewritingAgent", "IntentRecognitionAgent"}:
            # ChatAgentExecutor.executePipeline mirrors these two Java cases:
            # a rewriting agent restarts the full pipeline; an intent agent
            # resumes at intent recognition and then dispatches MasterAgent.
            return self.run(
                request,
                user_id,
                session_id,
                continuation=True,
                active_resume_agent=agent_name,
            )
        agent = self._active_agent(agent_name)
        if agent is None:
            return self.run(request, user_id, session_id)

        generation = session_execution_fence.begin(session_id)
        handle = execution_registry.begin(session_id, agent_name or "MasterAgent", generation=generation)
        agent_session_manager.register(session_id, handle)
        try:
            original = str(previous.get("original_question") or previous.get("request") or request)
            state: TravelAgentState = {
                **previous,
                "request": request,
                "original_question": f"{original}\n用户补充：{request}",
                "user_id": user_id,
                "session_id": session_id,
                "request_id": uuid4().hex,
                "execution_generation": generation,
                "messages": [*(previous.get("memory_original_messages") or previous.get("messages", [])),
                             HumanMessage(content=request)],
                "trace": [],
                "interrupted": False,
                "continuation": True,
                "active_agent": agent_name,
                "active_agent_name": agent_name,
                "resume_messages": [],
            }
            state = self.memory_manager.prepare(state)
            self._bind_interrupt_checkpoint(handle, state)
            try:
                result = get_subagent_executor().execute(agent_name, state, agent=agent)
            except Exception:
                if handle.stop.is_set():
                    result = self._interrupted_result(state)
                else:
                    raise
            if handle.stop.is_set():
                result = self._interrupted_result(result)
            if not self._is_current(session_id, generation):
                result = self._interrupted_result(result)
            else:
                pending = result.get("pending_interaction") or {}
                if pending:
                    pending = _normalize_pending(pending)
                    result = {**result, "pending_interaction": pending}
                    agent_session_store.set_pending_tool(session_id, pending)
                    agent_session_store.set_active_agent(session_id, agent_name)
                else:
                    agent_session_store.clear_pending_tool(session_id)
                    agent_session_store.set_active_agent(session_id, agent_name)
                self.memory_manager.save(session_id, result)
            return result
        finally:
            agent_session_manager.remove(session_id)
            execution_registry.end(session_id, handle)

    def _bind_interrupt_checkpoint(self, handle, state: TravelAgentState) -> None:
        """Persist the latest pre-invocation checkpoint when local cancellation arrives.

        LangGraph/tool execution can be interrupted between normal save points.
        This mirrors Java's explicit save-on-interrupt behavior. A request
        generation fence prevents an old callback from overwriting a newer
        conversation turn.
        """
        snapshot = dict(state)

        def persist(_handle) -> None:
            generation = int(snapshot.get("execution_generation") or 0)
            if generation and not self._is_current(_handle.session_id, generation):
                return
            interrupted = self._interrupted_result(snapshot)
            interrupted["execution_generation"] = generation
            self.memory_manager.save(_handle.session_id, interrupted)

        execution_registry.set_interrupt_callback(handle, persist)

    @staticmethod
    def _is_current(session_id: str, generation: int) -> bool:
        return session_execution_fence.is_current(session_id, generation)

    @staticmethod
    def _active_agent(agent_name: str | None):
        if not agent_name or agent_name == "MasterAgent":
            return None
        # Imports stay local so module-level Agent singletons do not create a
        # cycle while the API router is being imported.
        from backend.agents.booking import booking_agent
        from backend.agents.info import info_agent
        from backend.agents.itinerary_manage import itinerary_manage_agent
        from backend.agents.itinerary_plan import itinerary_plan_agent
        return {
            "BookingAgent": booking_agent,
            "InfoAgent": info_agent,
            "ItineraryManageAgent": itinerary_manage_agent,
            "ItineraryPlanAgent": itinerary_plan_agent,
        }.get(agent_name)

    @staticmethod
    def _interrupted_result(state: dict) -> TravelAgentState:
        return {
            **state,
            "final": "已停止生成。请告诉我接下来有什么可以帮您的？",
            "pending_interaction": {},
            "interrupted": True,
            "trace": [*state.get("trace", []),
                      {"agent": state.get("active_agent", "MasterAgent"), "output": "用户请求中断"}],
        }

    def interrupt(self, session_id: str, user_id: str) -> bool:
        """Mark a session interrupted and ask an in-flight graph to stop."""
        previous = self.memory_manager.load(session_id)
        if not previous:
            raise ValueError("会话不存在")
        if previous.get("user_id") != user_id:
            raise PermissionError("无权中断该会话")
        generation = session_execution_fence.current(session_id)
        running = interrupt_broadcast.interrupt_and_broadcast(
            session_id, generation=generation or None, reason="user_interrupt",
        )
        state = self._interrupted_result(previous)
        state["execution_generation"] = generation
        if not generation or self._is_current(session_id, generation):
            self.memory_manager.save(session_id, state)
        return running

    @staticmethod
    def interrupt_previous(session_id: str) -> bool:
        """Cooperatively stop stale work on this and other application nodes."""
        generation = session_execution_fence.current(session_id)
        return interrupt_broadcast.interrupt_and_broadcast(
            session_id, generation=generation or None, reason="superseded_request",
        )

    def resume(
        self,
        session_id: str,
        answer,
        user_id: str,
        tool_use_id: str | None = None,
    ) -> TravelAgentState:
        previous = self.memory_manager.load(session_id)
        if not previous:
            raise ValueError("会话不存在")
        if previous.get("user_id") != user_id:
            raise PermissionError("无权恢复该会话")

        pending = previous.get("pending_interaction") or agent_session_store.get_pending_tool(session_id) or {}
        expected_tool_use_id = pending.get("toolUseId")
        if tool_use_id and expected_tool_use_id and tool_use_id != expected_tool_use_id:
            raise ValueError("交互请求已过期，请重新发起操作")
        if pending.get("ui_type") == "confirm" and pending.get("action") == "submit_travel_approval":
            accepted = answer is True or str(answer).strip().lower() in {
                "yes", "y", "是", "确认", "确定",
            }
            if accepted:
                fields = pending.get("fields") or {}
                submission = submit_travel_approval.invoke({
                    **fields,
                    "user_id": user_id,
                    "ignore_conflicts": bool(pending.get("ignore_conflicts")),
                })
                if not submission.get("success") and submission.get("needConfirm"):
                    result = {
                        **previous,
                        "request": str(answer),
                        "final": "检测到行程冲突，请阅读冲突明细后确认是否仍要提交。",
                        "pending_interaction": {
                            "ui_type": "confirm", "action": "submit_travel_approval",
                            "fields": fields, "ignore_conflicts": True,
                            "conflictReport": submission.get("conflictReport"),
                            "question": "是否确认忽略冲突并继续提交？",
                        },
                        "interrupted": False,
                        "active_agent": "ItineraryManageAgent",
                        "trace": [{"agent": "ItineraryManageAgent", "output": "等待冲突确认"}],
                    }
                    self.memory_manager.save(session_id, result)
                    return result
                if not submission.get("success"):
                    result = {
                        **previous,
                        "request": str(answer),
                        "final": submission.get("message") or "差旅申请未提交。",
                        "pending_interaction": {},
                        "interrupted": False,
                        "active_agent": "ItineraryManageAgent",
                        "trace": [{"agent": "ItineraryManageAgent", "output": "差旅申请未提交"}],
                    }
                    self.memory_manager.save(session_id, result)
                    agent_session_store.clear_pending_tool(session_id)
                    return result
                order = submission.get("order") or travel_order_repository.get(submission.get("order_id"))
                order_id = order.get("order_id") if isinstance(order, dict) else order.order_id
                status = order.get("status") if isinstance(order, dict) else order.status
                result = {
                    **previous,
                    "request": str(answer),
                    "final": f"差旅申请已提交，差旅单号：{order_id}，当前状态：{status}。",
                    "pending_interaction": {},
                    "interrupted": False,
                    "active_agent": "ItineraryManageAgent",
                    "trace": [{"agent": "ItineraryManageAgent", "output": "提交差旅审批"}],
                }
                self.memory_manager.save(session_id, result)
                agent_session_store.clear_pending_tool(session_id)
                return result

            if not accepted:
                return self._cancel_pending(session_id, previous, user_id, pending)

        if pending.get("ui_type") == "confirm" and pending.get("order_id"):
            accepted = answer is True or str(answer).strip().lower() in {
                "yes", "y", "是", "确认", "确定",
            }
            if accepted:
                order = travel_order_repository.get(pending["order_id"])
                if order is None or order.user_id != user_id:
                    raise ValueError("差旅单不存在")
                outcome = travel_order_service.cancel_with_approval(order)
                result = {
                    **previous,
                    "request": str(answer),
                    "final": f"差旅单 {order.order_id} 已取消。",
                    "pending_interaction": {},
                    "interrupted": False,
                    "active_agent": "ItineraryManageAgent",
                    "trace": [{"agent": "ItineraryManageAgent", "output": outcome.__dict__}],
                }
                self.memory_manager.save(session_id, result)
                agent_session_store.clear_pending_tool(session_id)
                return result

            if not accepted:
                return self._cancel_pending(session_id, previous, user_id, pending)

        if pending.get("ui_type") == "confirm":
            accepted = answer is True or str(answer).strip().lower() in {
                "yes", "y", "是", "确认", "确定",
            }
            if not accepted:
                return self._cancel_pending(session_id, previous, user_id, pending)

        # Generic AgentScope ToolSuspend/ToolResult resume.  Business-specific
        # confirmations above intentionally stay deterministic; every other
        # ask_user interaction is resumed by feeding the original ReAct agent
        # a synthetic tool call/result pair, matching ChatAgentExecutor.resume.
        if pending.get("tool_name") == "ask_user":
            agent_name = str(pending.get("agent_name") or
                             agent_session_store.get_active_agent(session_id) or "")
            agent = self._active_agent(agent_name)
            if agent is None:
                raise ValueError(f"无法恢复会话：未知的 Agent 类型 {agent_name or 'unknown'}")
            return self._resume_react_agent(session_id, previous, user_id, pending,
                                            answer, agent_name, agent)

        result = self.run(str(answer), user_id, session_id, continuation=True)
        result["continuation"] = True
        self.memory_manager.save(session_id, result)
        return result

    def _resume_react_agent(self, session_id: str, previous: TravelAgentState,
                            user_id: str, pending: dict, answer: Any,
                            agent_name: str, agent) -> TravelAgentState:
        """Resume a suspended LangGraph ReAct agent with a ToolMessage.

        AgentScope keeps the in-memory ReAct instance in Caffeine and sends a
        ``ToolResultBlock`` on resume.  Python reconstructs that boundary from
        the durable session checkpoint so the same path works after a process
        restart or on another worker.
        """
        from langchain_core.messages import AIMessage, ToolMessage

        tool_use_id = str(pending.get("toolUseId") or pending.get("tool_use_id") or
                          f"tool_{uuid4().hex}")
        response_text = answer if isinstance(answer, str) else json.dumps(
            answer, ensure_ascii=False, default=str,
        )
        arguments = pending.get("tool_arguments") or {
            key: pending.get(key)
            for key in ("question", "ui_type", "options", "fields",
                        "default_value", "allow_other")
            if key in pending
        }
        resume_messages = [
            AIMessage(content="", tool_calls=[{
                "name": "ask_user", "args": arguments, "id": tool_use_id,
                "type": "tool_call",
            }]),
            ToolMessage(content=response_text, tool_call_id=tool_use_id, name="ask_user"),
        ]
        generation = session_execution_fence.begin(session_id)
        handle = execution_registry.begin(session_id, agent_name, generation=generation)
        agent_session_manager.register(session_id, handle)
        try:
            state: TravelAgentState = {
                **previous,
                "request": "",
                "latest_user_response": response_text,
                "messages": [*(previous.get("messages") or []), *resume_messages],
                "resume_messages": resume_messages,
                "pending_interaction": {},
                "pending_tool": {},
                "user_id": user_id,
                "session_id": session_id,
                "request_id": uuid4().hex,
                "execution_generation": generation,
                "trace": [],
                "interrupted": False,
                "continuation": True,
                "active_agent": agent_name,
                "active_agent_name": agent_name,
            }
            state = self.memory_manager.prepare(state)
            self._bind_interrupt_checkpoint(handle, state)
            try:
                result = get_subagent_executor().execute(agent_name, state, agent=agent)
            except Exception:
                if handle.stop.is_set():
                    result = self._interrupted_result(state)
                else:
                    raise
            if handle.stop.is_set():
                result = self._interrupted_result(result)
            if not self._is_current(session_id, generation):
                result = self._interrupted_result(result)
            else:
                next_pending = result.get("pending_interaction") or {}
                if next_pending:
                    next_pending = _normalize_pending(next_pending)
                    result = {**result, "pending_interaction": next_pending}
                    agent_session_store.set_pending_tool(session_id, next_pending)
                else:
                    agent_session_store.clear_pending_tool(session_id)
                result["request"] = response_text
                result.pop("resume_messages", None)
                self.memory_manager.save(session_id, result)
            return result
        finally:
            agent_session_manager.remove(session_id)
            execution_registry.end(session_id, handle)

    def _cancel_pending(self, session_id: str, previous: TravelAgentState,
                        user_id: str, pending: dict) -> TravelAgentState:
        """Close a rejected HITL confirmation without re-running the request."""
        agent_name = str(pending.get("agent_name") or agent_session_store.get_active_agent(session_id)
                         or "MasterAgent")
        result: TravelAgentState = {
            **previous,
            "request": "取消",
            "final": "已取消本次操作。",
            "pending_interaction": {},
            "interrupted": False,
            "active_agent": agent_name,
            "active_agent_name": agent_name,
            "trace": [*previous.get("trace", []), {"agent": agent_name, "output": "用户拒绝交互操作"}],
        }
        self.memory_manager.save(session_id, result)
        agent_session_store.clear_pending_tool(session_id)
        return result


def _normalize_pending(value: dict[str, Any]) -> dict[str, Any]:
    pending = dict(value)
    pending.setdefault("tool_name", "ask_user")
    pending.setdefault("toolUseId", f"tool_{uuid4().hex}")
    pending.setdefault("tool_arguments", {
        key: pending.get(key)
        for key in ("question", "ui_type", "options", "fields", "default_value", "allow_other")
        if key in pending
    })
    return pending
