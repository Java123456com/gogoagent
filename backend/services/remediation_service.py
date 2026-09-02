"""Compile audit findings into safe, deterministic itinerary repair actions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from backend.domain.planning import RemediationAction, RemediationPlan

_VALID_TARGETS = {"transport", "flight", "train", "hotel", "proposal"}
_VALID_ACTIONS = {"exclude", "rescore", "supplement_search", "manual"}


def planning_fingerprint(*values: Any) -> str:
    """Stable digest used to stop repair loops that make no material progress."""
    payload = json.dumps(
        values, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RemediationService:
    def compile(
        self, review: dict[str, Any], itinerary: dict[str, Any], repair_round: int
    ) -> RemediationPlan:
        details = review.get("arbitration") if isinstance(review, dict) else None
        raw_items = (details or {}).get("remediation_priority") or []
        proposal_index = self._proposal_index(itinerary)
        actions: list[RemediationAction] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            action = self._compile_item(raw, proposal_index)
            if action is not None:
                actions.append(action)

        # Model-free/hard-fail fallback: objective findings remain actionable even
        # when the arbitrator could not provide a structured remediation list.
        if not actions and review.get("continue_remediation"):
            actions.extend(self._fallback_actions(review, proposal_index))

        actions.sort(key=lambda item: item.priority)
        return RemediationPlan(
            actions=actions,
            source_verdict=str(review.get("verdict") or "warning"),
            repair_round=repair_round,
        )

    def apply(
        self,
        plan: RemediationPlan,
        *,
        excluded_transport_ids: list[str],
        excluded_hotel_ids: list[str],
        score_overrides: dict[str, float],
    ) -> dict[str, Any]:
        transports = set(excluded_transport_ids)
        hotels = set(excluded_hotel_ids)
        overrides = {str(key): float(value) for key, value in score_overrides.items()}
        searches: list[dict[str, Any]] = []

        for action in plan.executable_actions:
            if action.action_type == "exclude":
                target = hotels if action.target_type == "hotel" else transports
                target.update(action.candidate_ids)
            elif action.action_type == "rescore":
                for candidate_id in action.candidate_ids:
                    overrides[candidate_id] = float(action.score_adjustments.get(candidate_id, 0.0))
                overrides.update(
                    {str(key): float(value) for key, value in action.score_adjustments.items()}
                )
            elif action.action_type == "supplement_search":
                searches.append(
                    {
                        "target_type": action.target_type,
                        "constraints": action.constraints,
                        "reason": action.reason,
                        "priority": action.priority,
                    }
                )

        return {
            "excluded_transport_ids": sorted(transports),
            "excluded_hotel_ids": sorted(hotels),
            "score_overrides": overrides,
            "supplement_searches": searches,
        }

    def _compile_item(
        self, raw: dict[str, Any], proposal_index: dict[str, dict[str, Any]]
    ) -> RemediationAction | None:
        issue = str(raw.get("issue") or "")
        action_text = str(raw.get("action") or "")
        text = f"{issue} {action_text}".strip()
        fixable = bool(raw.get("fixable_by_replanning", True))
        action_type = str(raw.get("action_type") or "").lower()
        if action_type not in _VALID_ACTIONS:
            action_type = self._infer_action_type(text, fixable)
        target_type = str(raw.get("target_type") or "").lower()
        if target_type not in _VALID_TARGETS:
            target_type = self._infer_target_type(text)
        affected = [str(value) for value in (raw.get("affected_proposals") or [])]
        candidate_ids = [str(value) for value in (raw.get("candidate_ids") or [])]
        if not candidate_ids and action_type in {"exclude", "rescore"}:
            candidate_ids = self._candidate_ids(affected, proposal_index, target_type, text)
        constraints = raw.get("constraints") if isinstance(raw.get("constraints"), dict) else {}
        if action_type == "supplement_search" and not constraints:
            constraints = self._constraints_from_text(text)
        adjustments = raw.get("score_adjustments")
        if not isinstance(adjustments, dict):
            adjustments = {}
        if action_type == "rescore" and not adjustments:
            adjustments = {candidate_id: 0.0 for candidate_id in candidate_ids}
        try:
            priority = max(1, int(raw.get("priority") or 99))
        except (TypeError, ValueError):
            priority = 99
        if action_type in {"exclude", "rescore"} and not candidate_ids:
            action_type = "manual"
            fixable = False
        return RemediationAction(
            action_type=action_type,
            target_type=target_type,
            candidate_ids=list(dict.fromkeys(candidate_ids)),
            affected_proposal_ids=affected,
            constraints=constraints,
            score_adjustments={
                str(key): float(value) for key, value in adjustments.items() if _is_number(value)
            },
            reason=action_text or issue,
            priority=priority,
            hard_constraint=bool(raw.get("hard_constraint", False)),
            fixable_by_replanning=fixable,
        )

    @staticmethod
    def _infer_action_type(text: str, fixable: bool) -> str:
        if not fixable:
            return "manual"
        if re.search(r"补搜|重新搜索|增加候选|候选池.*(?:没有|不存在)|新候选", text):
            return "supplement_search"
        if re.search(r"调整.*分|重新打分|降低.*分|提高.*分", text):
            return "rescore"
        return "exclude"

    @staticmethod
    def _infer_target_type(text: str) -> str:
        if re.search(r"酒店|住宿|房间|早餐", text):
            return "hotel"
        if re.search(r"高铁|火车|动车|车次", text):
            return "train"
        if re.search(r"航班|机票|飞机|红眼", text):
            return "flight"
        return "transport"

    @staticmethod
    def _proposal_index(itinerary: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result = {}
        for index, proposal in enumerate(itinerary.get("proposals") or [], start=1):
            if not isinstance(proposal, dict):
                continue
            proposal_id = str(proposal.get("proposal_id") or f"P{index}")
            result[proposal_id] = proposal
        return result

    @staticmethod
    def _candidate_ids(
        affected: list[str], proposal_index: dict[str, dict[str, Any]], target_type: str, text: str
    ) -> list[str]:
        values: list[str] = []
        for proposal_id in affected:
            proposal = proposal_index.get(proposal_id) or {}
            if target_type == "hotel":
                candidate = (proposal.get("candidate_ids") or {}).get("hotel") or (
                    proposal.get("hotel") or {}
                ).get("id")
                if candidate:
                    values.append(str(candidate))
                continue
            ids = proposal.get("candidate_ids") or {}
            legs = []
            if "返程" in text:
                legs = [ids.get("return") or (proposal.get("return") or {}).get("id")]
            elif "去程" in text:
                legs = [ids.get("outbound") or (proposal.get("outbound") or {}).get("id")]
            else:
                legs = [
                    ids.get("outbound") or (proposal.get("outbound") or {}).get("id"),
                    ids.get("return") or (proposal.get("return") or {}).get("id"),
                ]
            values.extend(str(value) for value in legs if value)
        return list(dict.fromkeys(values))

    @staticmethod
    def _constraints_from_text(text: str) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        match = re.search(r"(\d{1,2}:\d{2})\s*(?:-|至|到|以后|之前)\s*(\d{1,2}:\d{2})?", text)
        if match:
            constraints["departure_time"] = (
                f"{match.group(1)}-{match.group(2)}" if match.group(2) else match.group(1)
            )
        if "含早" in text or "早餐" in text:
            constraints["breakfast_included"] = True
        price = re.search(r"(?:低于|不超过|限额|预算)[^\d]{0,6}(\d+(?:\.\d+)?)", text)
        if price:
            constraints["max_price"] = float(price.group(1))
        keyword = re.search(
            r"(?:换|选择|搜索)([\u4e00-\u9fffA-Za-z0-9]{2,12})(?:酒店|航班|高铁)", text
        )
        if keyword:
            constraints["keyword"] = keyword.group(1)
        return constraints

    def _fallback_actions(
        self, review: dict[str, Any], proposal_index: dict[str, dict[str, Any]]
    ) -> list[RemediationAction]:
        actions: list[RemediationAction] = []
        for dimension in review.get("dimensions") or []:
            if not isinstance(dimension, dict) or dimension.get("verdict") not in {
                "fail",
                "hard_fail",
            }:
                continue
            for issue in dimension.get("issues") or []:
                text = str(issue)
                proposal_ids = re.findall(r"\[(P(?:_[A-Za-z0-9]+|\d+))\]", text)
                target = self._infer_target_type(text)
                candidate_ids = self._candidate_ids(proposal_ids, proposal_index, target, text)
                if candidate_ids:
                    actions.append(
                        RemediationAction(
                            action_type="exclude",
                            target_type=target,
                            candidate_ids=candidate_ids,
                            affected_proposal_ids=proposal_ids,
                            reason=text,
                            priority=1,
                            hard_constraint=True,
                        )
                    )
        if not actions:
            actions.append(
                RemediationAction(
                    action_type="manual",
                    target_type="proposal",
                    reason="审核要求整改，但没有可安全执行的结构化整改项",
                    priority=1,
                    hard_constraint=bool(review.get("hard_vetoed")),
                    fixable_by_replanning=False,
                )
            )
        return actions


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


remediation_service = RemediationService()
