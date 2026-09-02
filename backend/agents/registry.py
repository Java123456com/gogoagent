"""The executable multi-agent topology.

This is intentionally the single source of truth for the operating architecture:
four ReAct roles, one planning graph, two one-shot agents, and two lightweight
LLM services.  Legacy classes stay visible only as non-routable compatibility
entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.infrastructure.model_profiles import ModelProfile


class AgentKind(StrEnum):
    REACT = "react"
    PLAN_AND_EXECUTE = "plan_and_execute"
    ONE_SHOT = "one_shot"
    LIGHTWEIGHT_SERVICE = "lightweight_service"
    LEGACY = "legacy"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class AgentRegistration:
    name: str
    kind: AgentKind
    profile: ModelProfile | None
    max_iterations: int | None = None
    master_dispatchable: bool = False
    process_isolated: bool = False
    tool_effects: frozenset[str] = frozenset()


AGENT_REGISTRY: tuple[AgentRegistration, ...] = (
    AgentRegistration("MasterAgent", AgentKind.REACT, ModelProfile.STRONG, 15),
    AgentRegistration(
        "ItineraryManageAgent", AgentKind.REACT, ModelProfile.STRONG, 10,
        master_dispatchable=True, process_isolated=True,
        tool_effects=frozenset({"read", "reversible_write"}),
    ),
    AgentRegistration(
        "ItineraryPlanAgent", AgentKind.PLAN_AND_EXECUTE, ModelProfile.STRONG_THINKING, 30,
        master_dispatchable=True, process_isolated=True,
        tool_effects=frozenset({"read"}),
    ),
    AgentRegistration(
        "InfoAgent", AgentKind.REACT, ModelProfile.STABLE, 5,
        master_dispatchable=True, process_isolated=True,
        tool_effects=frozenset({"read"}),
    ),
    AgentRegistration(
        "BookingAgent", AgentKind.REACT, ModelProfile.STRONG_THINKING, 10,
        master_dispatchable=True, process_isolated=True,
        tool_effects=frozenset({"read", "irreversible_write"}),
    ),
    AgentRegistration("QueryRewritingAgent", AgentKind.ONE_SHOT, ModelProfile.STABLE),
    AgentRegistration("IntentRecognitionAgent", AgentKind.ONE_SHOT, ModelProfile.STABLE),
    AgentRegistration("ConversationTitleService", AgentKind.LIGHTWEIGHT_SERVICE, ModelProfile.FAST),
    AgentRegistration("QuestionRecommendationService", AgentKind.LIGHTWEIGHT_SERVICE, ModelProfile.FAST),
    AgentRegistration("ItineraryReviewAgent", AgentKind.LEGACY, ModelProfile.STABLE, 8),
    AgentRegistration("ReimbursementAgent", AgentKind.NOT_IMPLEMENTED, None),
)


def registrations(kind: AgentKind | None = None) -> tuple[AgentRegistration, ...]:
    return tuple(item for item in AGENT_REGISTRY if kind is None or item.kind == kind)


def registration_for(name: str) -> AgentRegistration:
    for item in AGENT_REGISTRY:
        if item.name == name:
            return item
    raise KeyError(f"Unknown agent registration: {name}")


def master_dispatchable_agents() -> tuple[AgentRegistration, ...]:
    return tuple(item for item in AGENT_REGISTRY if item.master_dispatchable)
