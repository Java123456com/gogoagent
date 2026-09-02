"""Structured contracts for itinerary remediation and resumable plan runs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RemediationActionType = Literal["exclude", "rescore", "supplement_search", "manual"]
RemediationTargetType = Literal["transport", "flight", "train", "hotel", "proposal"]


class RemediationAction(BaseModel):
    """One validated, machine-executable action derived from an audit item."""

    action_type: RemediationActionType
    target_type: RemediationTargetType = "proposal"
    candidate_ids: list[str] = Field(default_factory=list)
    affected_proposal_ids: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    score_adjustments: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    priority: int = 99
    hard_constraint: bool = False
    fixable_by_replanning: bool = True


class RemediationPlan(BaseModel):
    """Validated repair plan consumed by the deterministic LangGraph nodes."""

    actions: list[RemediationAction] = Field(default_factory=list)
    source_verdict: str = "warning"
    repair_round: int = 0

    @property
    def executable_actions(self) -> list[RemediationAction]:
        return [
            action
            for action in self.actions
            if action.fixable_by_replanning and action.action_type != "manual"
        ]


class RepairAttempt(BaseModel):
    """Durable audit record for one bounded remediation round."""

    repair_round: int
    actions: list[RemediationAction] = Field(default_factory=list)
    before_fingerprint: str = ""
    after_fingerprint: str = ""
    changed: bool = False
    stop_reason: str | None = None
