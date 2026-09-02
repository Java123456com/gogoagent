"""Sub-agent execution boundary.

The Master and continuation paths depend on this small interface instead of
calling a concrete Agent singleton.  ``local`` keeps the existing development
behaviour; ``process`` delegates to the supervised domain workers.
"""
from __future__ import annotations

from typing import Any, Protocol

from backend.config import get_settings
from backend.core.request_context import (
    AgentRequestContext,
    apply_context_to_state,
    bind_context_value,
    context_from_state,
    current_context,
)


class SubAgentExecutor(Protocol):
    def execute(self, agent_name: str, state: dict[str, Any], agent: Any | None = None,
                execution_context: AgentRequestContext | None = None) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class LocalSubAgentExecutor:
    def execute(self, agent_name: str, state: dict[str, Any], agent: Any | None = None,
                execution_context: AgentRequestContext | None = None) -> dict[str, Any]:
        if agent is None:
            agent = self._resolve(agent_name)
        if agent is None:
            raise ValueError(f"未知的子 Agent: {agent_name}")
        source_context = execution_context if execution_context is not None else current_context()
        context = context_from_state(state, agent_name=agent_name, parent=source_context)
        if source_context is not None and source_context.agent_name != agent_name:
            context = context.derive(deadline_at=None)
        trusted_state = apply_context_to_state(state, context)
        with bind_context_value(context):
            return agent.invoke(trusted_state)

    @staticmethod
    def _resolve(agent_name: str):
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

    def close(self) -> None:
        return None


_executor: SubAgentExecutor | None = None
_executor_mode: str | None = None


def get_subagent_executor() -> SubAgentExecutor:
    """Lazily create the configured executor so imports stay test-friendly."""
    global _executor, _executor_mode
    mode = str(get_settings().subagent_execution_mode or "local").strip().lower()
    if mode not in {"local", "process"}:
        raise ValueError(f"不支持的 subagent_execution_mode: {mode}")
    if _executor is not None and _executor_mode == mode:
        return _executor
    if _executor is not None:
        _executor.close()
    if mode == "process":
        from backend.runtime.process_executor import ProcessSubAgentExecutor
        _executor = ProcessSubAgentExecutor()
    else:
        _executor = LocalSubAgentExecutor()
    _executor_mode = mode
    return _executor


def close_subagent_executor() -> None:
    global _executor, _executor_mode
    if _executor is not None:
        _executor.close()
    _executor = None
    _executor_mode = None
