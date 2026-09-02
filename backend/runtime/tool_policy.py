"""Central tool execution policy registry.

The registry is intentionally conservative.  A tool not explicitly classified
is given a timeout but is never retried automatically, which is safer than
guessing whether an unknown tool has side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.config import get_settings


class ToolEffect(str, Enum):
    READ_ONLY = "READ_ONLY"
    LOCAL_IDEMPOTENT = "LOCAL_IDEMPOTENT"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"
    INTERACTIVE = "INTERACTIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ToolPolicy:
    effect: ToolEffect = ToolEffect.UNKNOWN
    timeout_seconds: float = 60.0
    max_attempts: int = 1
    initial_backoff_seconds: float = 3.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 15.0
    jitter_ratio: float = 0.20
    circuit_breaker: bool = False
    retry_exceptions: tuple[type[BaseException], ...] = ()

    @property
    def retryable(self) -> bool:
        return self.effect in {
            ToolEffect.READ_ONLY, ToolEffect.LOCAL_IDEMPOTENT,
            ToolEffect.IDEMPOTENT_WRITE,
        } and self.max_attempts > 1


# Tools with an external or potentially flaky dependency.  Pure local tools
# still receive deadlines, but do not need a circuit shared across workers.
_CIRCUIT_TOOLS = {
    "query_weather", "query_destination_news", "quick_visa_check",
    "check_visa_requirement", "query_attraction_knowledge",
    "query_corporate_travel_policy_knowledge",
    "query_corporate_travel_guidelines_knowledge", "execute_shell_command",
}

_READ_ONLY = {
    "query_booking_record", "query_travel_order", "query_travel_order_by_order_id",
    "query_travel_orders", "query_approval_status", "check_travel_time_validity",
    "check_travel_order_approval", "check_travel_order_conflicts", "query_user_contact_info", "query_user_base_location",
    "check_flight_api_key", "check_tuniu_api_key", "check_flyai_api_key",
    "list_available_skills", "get_candidates", "get_proposals",
    "view_subtasks", "get_subtask_count", "view_historical_plans",
    "query_travel_policy", "check_travel_policy", "query_weather", "query_destination_news",
    "quick_visa_check", "check_visa_requirement", "query_attraction_knowledge",
    "query_corporate_travel_policy_knowledge", "query_corporate_travel_guidelines_knowledge",
    "retrieve_from_memory", "load_skill_through_path", "get_user_preferences",
}

_LOCAL_IDEMPOTENT = {
    "plan_itinerary", "review_itinerary", "objective_review", "generate_expense_report",
}

_IDEMPOTENT_WRITE = {
    "update_user_contact_info", "update_user_base_location", "record_to_memory",
    "save_flight_api_key", "save_tuniu_api_key", "save_flyai_api_key",
    "create_plan", "update_plan_info",
    "revise_current_plan", "update_subtask_state", "finish_subtask", "finish_plan",
    "recover_historical_plan",
}

_NON_IDEMPOTENT_WRITE = {
    "submit_travel_approval", "cancel_travel_order", "modify_travel_order",
    "cancel_booking", "execute_booking", "book_flight", "book_hotel", "book_train",
    "ocr_invoice", "submit_reimbursement", "save_plan_html", "itinerary_manage_agent",
    "itinerary_plan_agent", "info_agent", "booking_agent",
}

_INTERACTIVE = {"ask_user"}
_EXPLICIT_UNKNOWN = {"execute_shell_command"}

# These restrictions are enforced at agent construction time, rather than being
# delegated to prompt wording.  In particular, planning and information roles
# may never receive a booking write capability.
_FORBIDDEN_AGENT_TOOLS = {
    "ItineraryPlanAgent": frozenset({
        "cancel_booking", "execute_booking", "book_flight", "book_hotel", "book_train",
    }),
    "InfoAgent": frozenset({
        "submit_travel_approval", "cancel_travel_order", "modify_travel_order",
        "cancel_booking", "execute_booking", "book_flight", "book_hotel", "book_train",
    }),
}


class ToolPolicyRegistry:
    def __init__(self) -> None:
        self._overrides: dict[str, ToolPolicy] = {}

    def register(self, name: str, policy: ToolPolicy) -> None:
        self._overrides[name] = policy

    def classify(self, name: str) -> ToolEffect:
        if name in _READ_ONLY:
            return ToolEffect.READ_ONLY
        if name in _LOCAL_IDEMPOTENT:
            return ToolEffect.LOCAL_IDEMPOTENT
        if name in _IDEMPOTENT_WRITE:
            return ToolEffect.IDEMPOTENT_WRITE
        if name in _NON_IDEMPOTENT_WRITE:
            return ToolEffect.NON_IDEMPOTENT_WRITE
        if name in _INTERACTIVE:
            return ToolEffect.INTERACTIVE
        return ToolEffect.UNKNOWN

    def policy(self, name: str) -> ToolPolicy:
        settings = get_settings()
        if name in self._overrides:
            return self._overrides[name]
        effect = self.classify(name)
        attempts = settings.tool_default_max_attempts if effect in {
            ToolEffect.READ_ONLY, ToolEffect.LOCAL_IDEMPOTENT,
            ToolEffect.IDEMPOTENT_WRITE,
        } else 1
        # Interactive and unknown tools remain one-shot even if the global
        # default is accidentally configured higher.
        if effect in {ToolEffect.INTERACTIVE, ToolEffect.NON_IDEMPOTENT_WRITE,
                       ToolEffect.UNKNOWN}:
            attempts = 1
        timeout = max(0.1, float(settings.tool_default_timeout_seconds))
        # Skill CLI has its own bounded timeout (1..600s); keep the common
        # Agent default conservative while allowing normal search commands to
        # finish before the subprocess-level deadline.
        if name == "execute_shell_command":
            timeout = max(timeout, 180.0)
        return ToolPolicy(
            effect=effect,
            timeout_seconds=timeout,
            max_attempts=max(1, int(attempts)),
            initial_backoff_seconds=max(0.0, float(settings.tool_retry_initial_backoff_seconds)),
            backoff_multiplier=max(1.0, float(settings.tool_retry_backoff_multiplier)),
            max_backoff_seconds=max(0.0, float(settings.tool_retry_max_backoff_seconds)),
            jitter_ratio=min(1.0, max(0.0, float(settings.tool_retry_jitter_ratio))),
            circuit_breaker=bool(settings.circuit_breaker_enabled and name in _CIRCUIT_TOOLS),
        )

    def validate(self, names: list[str]) -> list[str]:
        """Return unknown names; callers may enforce strict startup checks."""
        return sorted({name for name in names
                       if self.classify(name) is ToolEffect.UNKNOWN
                       and name not in _EXPLICIT_UNKNOWN})

    def validate_agent_boundary(self, agent_name: str, names: list[str]) -> list[str]:
        """Return tool names that violate an architectural permission boundary."""
        forbidden = _FORBIDDEN_AGENT_TOOLS.get(agent_name, frozenset())
        return sorted(set(names).intersection(forbidden))

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "effect": policy.effect.value,
                "timeout_seconds": policy.timeout_seconds,
                "max_attempts": policy.max_attempts,
                "circuit_breaker": policy.circuit_breaker,
            }
            for name, policy in self._overrides.items()
        }


tool_policy_registry = ToolPolicyRegistry()
