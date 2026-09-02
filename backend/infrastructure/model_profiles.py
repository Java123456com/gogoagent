"""Central model profiles used by the multi-agent runtime.

The runtime assigns a profile per role instead of using another LLM
call to select a model.  Keeping that policy as data makes the mapping auditable
and prevents a low-risk role from accidentally inheriting a thinking model.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.config import get_settings


class ModelProfile(StrEnum):
    FAST = "fast"
    STABLE = "stable"
    STRONG = "strong"
    STRONG_THINKING = "strong_thinking"


@dataclass(frozen=True)
class ModelProfileConfig:
    model_name: str
    temperature: float
    enable_thinking: bool
    thinking_budget: int | None = None

    def extra_body(self) -> dict[str, object]:
        """DashScope OpenAI-compatible parameters for this model profile."""
        body: dict[str, object] = {"enable_thinking": self.enable_thinking}
        if self.enable_thinking and self.thinking_budget:
            body["thinking_budget"] = self.thinking_budget
        return body


def resolve_model_profile(profile: ModelProfile) -> ModelProfileConfig:
    settings = get_settings()
    configs = {
        ModelProfile.FAST: ModelProfileConfig(
            settings.fast_model, 0.0, False,
        ),
        ModelProfile.STABLE: ModelProfileConfig(
            settings.stable_model, 0.0, False,
        ),
        ModelProfile.STRONG: ModelProfileConfig(
            settings.strong_model, 0.2, False,
        ),
        ModelProfile.STRONG_THINKING: ModelProfileConfig(
            settings.strong_model_with_thinking,
            0.2,
            True,
            settings.strong_model_thinking_budget,
        ),
    }
    return configs[profile]
