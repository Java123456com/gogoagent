from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from backend.config import get_settings
from backend.core.request_context import (
    apply_context_to_state,
    bind_context_value,
    context_from_state,
    current_context,
)
from backend.hooks.context_hooks import (
    DynamicTimeInjectionHook,
    ProgressEventHook,
    ToolResultCompressHook,
    prepare_agent_state,
)
from backend.infrastructure.llm import invoke_text, stable_model
from backend.memory.context import ContextCompressionHook
from backend.memory.session import agent_session_store
from backend.memory.store import PersistentSessionStore
from backend.prompts import load_static
from backend.runtime.resilient_tool import ResilientToolHook
from backend.runtime.tool_policy import tool_policy_registry
from backend.services.runtime_events import emit_agent_done, emit_agent_start, emit_event
from backend.tools.interaction import UserInteractionRequired

_agent_memory_store = PersistentSessionStore()


def make_react_agent(model, tools, prompt: str, name: str, max_iterations: int):
    """Build a LangGraph ReAct subgraph when an LLM is configured.

    The fallback callable deliberately remains deterministic so CI can exercise business
    services without network credentials.
    """
    if model is None:
        return None
    from langgraph.prebuilt import create_react_agent
    if name in {"ItineraryPlanAgent", "BookingAgent", "InfoAgent"}:
        tools = ToolResultCompressHook().wrap_tools(tools)
        # AgentScope's DynamicTimeInjectionHook adds a second system message
        # on every model turn while keeping the static prompt prefix stable.
        static_prompt = prompt

        def dynamic_prompt(_state):
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            return [
                _prompt_message(static_prompt),
                _prompt_message(f"{DynamicTimeInjectionHook.MARKER} {timestamp}"),
            ]
        prompt = dynamic_prompt
    elif _cache_control_enabled():
        static_prompt = prompt

        def cached_prompt(_state):
            return [_prompt_message(static_prompt)]

        prompt = cached_prompt
    # All structured tools share the same timeout/retry/backoff/circuit policy.
    # Keep this outside the Agent-specific prompt branches so every sub-agent,
    # including MasterAgent, receives the same safety boundary.
    tools = ResilientToolHook(name).wrap_tools(tools)
    tools = ProgressEventHook(name).wrap_tools(tools)
    from langgraph.prebuilt import ToolNode

    def handle_tool_error(error: Exception) -> str:
        if isinstance(error, UserInteractionRequired):
            raise error
        return str(error)

    tool_node = ToolNode(tools, handle_tool_errors=handle_tool_error)
    try:
        return create_react_agent(model, tools=tool_node, prompt=prompt, name=name)
    except TypeError:
        # A misconfigured/incomplete provider object should not make the
        # deterministic fallback unusable (also helps provider health checks).
        return None


class BaseSubAgent:
    prompt_file: str = ""
    model_factory: Callable = staticmethod(stable_model)
    max_iterations: int = 8
    execution_graph: Any = None

    def __init__(self, tools: list[Any] | None = None):
        self.tools = tools or []
        unknown = tool_policy_registry.validate([
            str(getattr(item, "name", None) or getattr(item, "__name__", ""))
            for item in self.tools
        ])
        tool_names = [
            str(getattr(item, "name", None) or getattr(item, "__name__", ""))
            for item in self.tools
        ]
        forbidden = tool_policy_registry.validate_agent_boundary(self.__class__.__name__, tool_names)
        if forbidden:
            raise ValueError(
                f"{self.__class__.__name__} 不允许注册有副作用工具: {', '.join(forbidden)}"
            )
        if unknown and get_settings().tool_policy_strict_registration:
            raise ValueError(f"工具未登记执行策略: {', '.join(unknown)}")
        self.model = self.model_factory()
        self.prompt = load_static(self.prompt_file) if self.prompt_file else ""
        self.graph = make_react_agent(self.model, self.tools, self.prompt, self.__class__.__name__, self.max_iterations)

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        agent_name = self.__class__.__name__
        emit_agent_start(agent_name)
        parent_context = current_context()
        execution_context = context_from_state(state, agent_name=agent_name)
        # State remains the compatibility contract for existing business tools,
        # but nested calls must never accept identity values from model-owned
        # state over an already authenticated request context.
        state = apply_context_to_state(state, execution_context)
        session_id = state.get("session_id")
        if session_id and agent_name != "MasterAgent":
            # Java ActiveAgentPersistenceHook records the last non-master
            # Agent at PreReasoning time, including stateful sub-agents.
            agent_session_store.set_active_agent(session_id, agent_name)
        state = self._restore_session(state)
        resume_messages = state.get("resume_messages")
        if resume_messages:
            # Java ChatAgentExecutor.resume supplies a ToolResultBlock to the
            # same ReActAgent.  LangGraph has no mutable AgentScope instance,
            # so restore the equivalent message pair before invoking the graph.
            existing = list(state.get("messages") or [])
            existing_ids = {
                getattr(message, "tool_call_id", None)
                for message in existing
                if getattr(message, "tool_call_id", None)
            }
            additions = [message for message in resume_messages if
                         getattr(message, "tool_call_id", None) not in existing_ids]
            state = {**state, "messages": [*existing, *additions]}
        # AgentScope's AutoContextHook runs for every ReAct agent turn.  Apply
        # the same tuned compression before either the real graph or fallback.
        state = ContextCompressionHook().before_model(state)
        # Skill folding is a PreReasoning-only transform: it must run after
        # AutoContext captured the durable original/working memory copies.
        state = prepare_agent_state(state, self.__class__.__name__)
        deadline = state.get("deadline_at")
        # A Master's orchestration budget is not a child Agent's execution
        # budget. Each nested Agent receives its own configured deadline.
        if parent_context is not None and parent_context.agent_name != agent_name:
            deadline = None
        if deadline is None or deadline <= monotonic():
            deadline = monotonic() + self._deadline_seconds()
        execution_context = execution_context.derive(agent_name=agent_name, deadline_at=deadline)
        state = apply_context_to_state({**state, "deadline_at": deadline}, execution_context)
        try:
            with bind_context_value(execution_context):
                if self.execution_graph is not None:
                    graph_result = self.execution_graph.invoke(state)
                    result = {**state, **graph_result}
                elif self.graph is not None:
                    messages = list(state.get("messages", []))
                    request = state.get("request", "")
                    last_content = getattr(messages[-1], "content", None) if messages else None
                    if messages and isinstance(messages[-1], (tuple, list)):
                        last_content = messages[-1][1] if len(messages[-1]) > 1 else None
                    if messages and isinstance(messages[-1], dict):
                        last_content = messages[-1].get("content")
                    if request and last_content != request:
                        messages.append(("user", request))
                    messages = _cache_last_message(messages)
                    try:
                        graph_result = self.graph.invoke(
                            {"messages": messages},
                            config={"recursion_limit": self.max_iterations * 2 + 1},
                        )
                        result = {**state, **graph_result}
                        _emit_reasoning_event(agent_name, graph_result)
                    except UserInteractionRequired as signal:
                        payload = dict(signal.payload)
                        payload.setdefault("agent_name", agent_name)
                        payload.setdefault("tool_name", "ask_user")
                        payload.setdefault("toolUseId", f"tool_{uuid4().hex}")
                        payload.setdefault("tool_arguments", {
                            key: payload.get(key)
                            for key in ("question", "ui_type", "options", "fields",
                                        "default_value", "allow_other")
                            if key in payload
                        })
                        result = {
                            **state,
                            "final": payload["question"],
                            "pending_interaction": payload,
                            "pending_tool": payload,
                            "trace": [{"agent": agent_name, "output": "等待用户输入"}],
                        }
                else:
                    result = {**state, **self.fallback(state)}
                # ``resume_messages`` is an invocation control value, not a
                # durable state field.  The synthetic ToolResult itself stays
                # in ``messages`` so the next turn has the same transcript.
                result.pop("resume_messages", None)
                result = apply_context_to_state(result, execution_context)
                self._save_session(result)
                return result
        finally:
            emit_agent_done(agent_name)

    def _session_key(self, state: dict[str, Any]) -> str | None:
        session_id = state.get("session_id")
        return f"{session_id}:{self.__class__.__name__}" if session_id else None

    def _restore_session(self, state: dict[str, Any]) -> dict[str, Any]:
        """Restore the Java ``sessionId:agentName`` memory namespace."""
        session_key = self._session_key(state)
        if not session_key:
            return dict(state)
        previous = _agent_memory_store.get(session_key)
        if not previous:
            return dict(state)
        messages = list(previous.get("memory_original_messages") or previous.get("messages") or [])
        request = str(state.get("request") or "")
        if request and (not messages or _message_text(messages[-1]) != request):
            messages.append(("user", request))
        return {**previous, **state, "messages": messages}

    def _save_session(self, state: dict[str, Any]) -> None:
        session_key = self._session_key(state)
        if session_key:
            _agent_memory_store.save(session_key, state)

    def fallback(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"final": "该智能体暂未配置模型，已完成确定性校验。", "trace": [{"agent": self.__class__.__name__, "output": "fallback"}]}

    def _deadline_seconds(self) -> float:
        settings = get_settings()
        values = {
            "ItineraryManageAgent": settings.subagent_deadline_manage_seconds,
            "ItineraryPlanAgent": settings.subagent_deadline_plan_seconds,
            "InfoAgent": settings.subagent_deadline_info_seconds,
            "BookingAgent": settings.subagent_deadline_booking_seconds,
        }
        return max(1.0, float(values.get(self.__class__.__name__, settings.tool_default_timeout_seconds)))


def text_agent(prompt_file: str, question: str, fallback: str) -> str:
    return invoke_text(stable_model(), load_static(prompt_file), question, fallback)


def _cache_control_enabled() -> bool:
    settings = get_settings()
    return bool(settings.dashscope_api_key and settings.dashscope_cache_control)


def _prompt_message(text: str):
    if not _cache_control_enabled():
        return ("system", text)
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=[{
        "type": "text", "text": text,
        "cache_control": {"type": "ephemeral"},
    }])


def _cache_last_message(messages: list[Any]) -> list[Any]:
    if not messages or not _cache_control_enabled():
        return messages
    from langchain_core.messages import HumanMessage
    last = messages[-1]
    if isinstance(last, (tuple, list)) and len(last) > 1:
        return [*messages[:-1], HumanMessage(content=[{
            "type": "text", "text": str(last[1]),
            "cache_control": {"type": "ephemeral"},
        }])]
    if isinstance(last, dict) and isinstance(last.get("content"), str):
        return [*messages[:-1], {**last, "content": [{
            "type": "text", "text": last["content"],
            "cache_control": {"type": "ephemeral"},
        }]}]
    if isinstance(last, HumanMessage) and isinstance(last.content, str):
        last_copy = HumanMessage(content=[{
            "type": "text", "text": last.content,
            "cache_control": {"type": "ephemeral"},
        }], additional_kwargs=last.additional_kwargs)
        return [*messages[:-1], last_copy]
    return messages


def _message_text(message: Any) -> str:
    if isinstance(message, (tuple, list)):
        return str(message[1]) if len(message) > 1 else ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _emit_reasoning_event(agent_name: str, graph_result: dict[str, Any]) -> None:
    """Expose provider reasoning blocks using Java's ``thinking`` SSE shape.

    DashScope's OpenAI-compatible response has appeared as both
    ``reasoning_content`` and ``reasoningContent`` across SDK versions.  The
    normal answer is intentionally excluded so the UI does not render it twice.
    """
    messages = graph_result.get("messages") or []
    for message in reversed(messages):
        additional = getattr(message, "additional_kwargs", None)
        if not isinstance(additional, dict):
            additional = message.get("additional_kwargs") if isinstance(message, dict) else {}
        if not isinstance(additional, dict):
            additional = {}
        reasoning = next(
            (additional.get(key) for key in ("reasoning_content", "reasoningContent", "thinking")
             if additional.get(key)),
            None,
        )
        if reasoning:
            text = reasoning if isinstance(reasoning, str) else str(reasoning)
            emit_event("thinking", {"agentName": agent_name, "text": text})
            return
        content = getattr(message, "content", None)
        if isinstance(content, list):
            parts = [
                str(item.get("text") or item.get("content"))
                for item in content
                if isinstance(item, dict)
                and item.get("type") in {"reasoning", "thinking"}
                and (item.get("text") or item.get("content"))
            ]
            if parts:
                emit_event("thinking", {"agentName": agent_name, "text": "".join(parts)})
                return
