"""Pre/post reasoning hooks implemented with Python context managers.

LangGraph does not expose AgentScope's exact event classes, so these hooks are
small state transformations.  They are deliberately usable outside a graph,
which also makes their token and content changes testable.
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime
from itertools import count
from typing import Any, ClassVar

from backend.core.request_context import bind_tool_call_id
from backend.memory.context import _message_content, _replace_message
from backend.observability import record_event
from backend.services.runtime_events import emit_tool_call, emit_tool_done
from backend.services.tool_result_side_effects import tool_result_side_effects

_tool_call_sequence = count(1)


class ToolResultCompressHook:
    """Compress JSON tool results before they enter the next model turn."""

    DROP_KEYS: ClassVar[set[str]] = {"airComImageUrl", "queryId", "craftType", "planModel"}

    def compress(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.compress_text(value)
        cleaned = self._clean(value)
        return cleaned

    def compress_text(self, text: str) -> str:
        if not text or not text.strip():
            return text
        prefix, payload, suffix = self._unwrap_stdout(text)
        try:
            value = json.loads(payload)
        except (TypeError, ValueError):
            return text
        cleaned = self._clean(value)
        compact = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        if len(compact) >= len(payload.strip()):
            return text
        return f"{prefix}{compact}{suffix}"

    def _clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            # MCP responses sometimes carry identical content and
            # structuredContent fields; retain content for downstream tools.
            result = {key: self._clean(item) for key, item in value.items()
                      if key not in self.DROP_KEYS}
            if result.get("content") and isinstance(result.get("content"), (list, dict)):
                result.pop("structuredContent", None)
            return {key: item for key, item in result.items() if not self._empty(item)}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in "[{":
                try:
                    nested = json.loads(stripped)
                    compact = json.dumps(self._clean(nested), ensure_ascii=False, separators=(",", ":"))
                    return compact if len(compact) < len(value) else value
                except (TypeError, ValueError):
                    pass
        return value

    @staticmethod
    def _empty(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip()) or value in ({}, [])

    @staticmethod
    def _unwrap_stdout(text: str) -> tuple[str, str, str]:
        open_tag, close_tag = "<stdout>", "</stdout>"
        start = text.find(open_tag)
        end = text.rfind(close_tag)
        if start >= 0 and end > start:
            body_start = start + len(open_tag)
            return text[:body_start], text[body_start:end], text[end:]
        return "", text.strip(), ""

    def wrap_tools(self, tools: list[Any]) -> list[Any]:
        """Return StructuredTool copies that normalize their output.

        Non-StructuredTool objects are left untouched; this keeps support for
        MCP and custom LangChain tools while covering the project's @tool
        functions.
        """
        try:
            from langchain_core.tools import StructuredTool
        except ImportError:  # pragma: no cover
            return tools
        wrapped: list[Any] = []
        for original in tools:
            if not isinstance(original, StructuredTool) or (original.func is None and original.coroutine is None):
                wrapped.append(original)
                continue
            func = original.func
            coroutine = original.coroutine
            if func is not None:
                def sync_wrapper(*args, _func=func, **kwargs):
                    return self.compress(_func(*args, **kwargs))
            else:
                sync_wrapper = None
            if coroutine is not None:
                async def async_wrapper(*args, _coroutine=coroutine, **kwargs):
                    return self.compress(await _coroutine(*args, **kwargs))
            else:
                async_wrapper = None
            wrapped.append(StructuredTool.from_function(
                func=sync_wrapper,
                coroutine=async_wrapper,
                name=original.name,
                description=original.description,
                args_schema=original.args_schema,
                return_direct=original.return_direct,
            ))
        return wrapped


class ProgressEventHook:
    """Emit AgentScope-compatible tool lifecycle events around LangChain tools."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    AGENT_TOOLS: ClassVar[set[str]] = {
        "itinerary_manage_agent", "info_agent", "itinerary_plan_agent", "booking_agent",
    }
    PLAN_TOOLS: ClassVar[set[str]] = {
        "create_plan", "update_plan_info", "revise_current_plan", "update_subtask_state",
        "finish_subtask", "view_subtasks", "get_subtask_count", "finish_plan",
        "view_historical_plans", "recover_historical_plan",
    }
    HITL_TOOLS: ClassVar[set[str]] = {"ask_user"}

    def _reportable(self, tool_name: str) -> bool:
        return tool_name not in self.AGENT_TOOLS | self.PLAN_TOOLS | self.HITL_TOOLS

    def wrap_tools(self, tools: list[Any]) -> list[Any]:
        try:
            from langchain_core.tools import StructuredTool
        except ImportError:  # pragma: no cover
            return tools
        wrapped: list[Any] = []
        for original in tools:
            if not isinstance(original, StructuredTool) or (original.func is None and original.coroutine is None):
                wrapped.append(original)
                continue
            func, coroutine = original.func, original.coroutine
            if func is not None:
                def sync_wrapper(*args, _func=func, _name=original.name, **kwargs):
                    call_id = str(next(_tool_call_sequence))
                    started = time.perf_counter()
                    if self._reportable(_name):
                        emit_tool_call(self.agent_name, _name, call_id, kwargs)
                    try:
                        with bind_tool_call_id(call_id):
                            result = _func(*args, **kwargs)
                    except Exception as exc:
                        record_event(
                            "tool.completed", agent=self.agent_name, tool=_name, tool_call_id=call_id,
                            duration_ms=round((time.perf_counter() - started) * 1000, 3),
                            result_class="error", error_type=type(exc).__name__,
                        )
                        raise
                    if self._reportable(_name):
                        emit_tool_done(self.agent_name, _name, call_id, result)
                    tool_result_side_effects.process(_name, kwargs, result)
                    record_event(
                        "tool.completed", agent=self.agent_name, tool=_name, tool_call_id=call_id,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        result_class="success",
                    )
                    return result
            else:
                sync_wrapper = None
            if coroutine is not None:
                async def async_wrapper(*args, _coroutine=coroutine, _name=original.name, **kwargs):
                    call_id = str(next(_tool_call_sequence))
                    started = time.perf_counter()
                    if self._reportable(_name):
                        emit_tool_call(self.agent_name, _name, call_id, kwargs)
                    try:
                        with bind_tool_call_id(call_id):
                            result = await _coroutine(*args, **kwargs)
                    except Exception as exc:
                        record_event(
                            "tool.completed", agent=self.agent_name, tool=_name, tool_call_id=call_id,
                            duration_ms=round((time.perf_counter() - started) * 1000, 3),
                            result_class="error", error_type=type(exc).__name__,
                        )
                        raise
                    if self._reportable(_name):
                        emit_tool_done(self.agent_name, _name, call_id, result)
                    tool_result_side_effects.process(_name, kwargs, result)
                    record_event(
                        "tool.completed", agent=self.agent_name, tool=_name, tool_call_id=call_id,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        result_class="success",
                    )
                    return result
            else:
                async_wrapper = None
            wrapped.append(StructuredTool.from_function(
                func=sync_wrapper, coroutine=async_wrapper, name=original.name,
                description=original.description, args_schema=original.args_schema,
                return_direct=original.return_direct,
            ))
        return wrapped


class SkillContentCollapseHook:
    """Fold old ``load_skill_through_path`` results in model input only."""

    LOAD_TOOL = "load_skill_through_path"
    KEEP_RECENT_MSGS = 10

    def collapse(self, messages: list[Any]) -> list[Any]:
        if len(messages) <= self.KEEP_RECENT_MSGS:
            return list(messages)
        result = deepcopy(messages)
        for index, message in enumerate(result[:-self.KEEP_RECENT_MSGS]):
            if not self._is_skill_result(message):
                continue
            content = _message_content(message)
            placeholder = (
                "[技能文档正文已省略以节省上下文] 该技能说明此前已加载。"
                "如仍需查阅，请重新调用 load_skill_through_path。"
            )
            if len(content) > len(placeholder):
                result[index] = _replace_message(message, placeholder)
        return result

    def _is_skill_result(self, message: Any) -> bool:
        if isinstance(message, dict):
            return message.get("name") == self.LOAD_TOOL or message.get("tool_name") == self.LOAD_TOOL
        return getattr(message, "name", None) == self.LOAD_TOOL


class DynamicTimeInjectionHook:
    """Keep the static system prompt at position zero and inject time after it."""

    MARKER = "[当前时间上下文]"

    def inject(self, messages: list[Any], now: datetime | None = None) -> list[Any]:
        if not messages or self.MARKER in _message_content(messages[0]):
            return list(messages)
        first = messages[0]
        role = first.get("role") if isinstance(first, dict) else getattr(first, "type", None)
        if role not in {"system", "SYSTEM"}:
            return list(messages)
        timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
        dynamic = _replace_message(first, f"{self.MARKER} {timestamp}")
        return [first, dynamic, *list(messages[1:])]


def prepare_agent_state(state: dict[str, Any], agent_name: str | None = None) -> dict[str, Any]:
    """Apply the model-input-only hooks without mutating persisted messages."""
    messages = list(state.get("messages") or [])
    if not messages:
        return dict(state)
    if agent_name in {"ItineraryPlanAgent", "BookingAgent"}:
        messages = SkillContentCollapseHook().collapse(messages)
    if agent_name in {"ItineraryManageAgent", "ItineraryPlanAgent", "BookingAgent", "InfoAgent"}:
        messages = DynamicTimeInjectionHook().inject(messages)
    prepared = dict(state)
    prepared["messages"] = messages
    prepared["active_agent_name"] = agent_name or state.get("active_agent_name")
    return prepared
