"""Agent package with lazy exports.

Avoid eagerly constructing every Agent when a domain Worker imports one
submodule; this keeps model/tool/MCP resources isolated per process.
"""
from __future__ import annotations

from importlib import import_module

__all__ = [
    "BookingAgent",
    "InfoAgent",
    "IntentRecognitionAgent",
    "ItineraryManageAgent",
    "ItineraryPlanAgent",
    "ItineraryReviewAgent",
    "MasterAgent",
    "QueryRewritingAgent",
    "ReimbursementAgent",
    "booking_agent",
    "info_agent",
    "intent_recognition_agent",
    "itinerary_manage_agent",
    "itinerary_plan_agent",
    "itinerary_review_agent",
    "master_agent",
    "query_rewriting_agent",
    "reimbursement_agent",
]

_EXPORTS = {
    "MasterAgent": ("master", "MasterAgent"), "master_agent": ("master", "master_agent"),
    "ItineraryManageAgent": ("itinerary_manage", "ItineraryManageAgent"),
    "itinerary_manage_agent": ("itinerary_manage", "itinerary_manage_agent"),
    "ItineraryPlanAgent": ("itinerary_plan", "ItineraryPlanAgent"),
    "itinerary_plan_agent": ("itinerary_plan", "itinerary_plan_agent"),
    "ItineraryReviewAgent": ("itinerary_review", "ItineraryReviewAgent"),
    "itinerary_review_agent": ("itinerary_review", "itinerary_review_agent"),
    "BookingAgent": ("booking", "BookingAgent"), "booking_agent": ("booking", "booking_agent"),
    "InfoAgent": ("info", "InfoAgent"), "info_agent": ("info", "info_agent"),
    "ReimbursementAgent": ("reimbursement", "ReimbursementAgent"),
    "reimbursement_agent": ("reimbursement", "reimbursement_agent"),
    "QueryRewritingAgent": ("query_rewriting", "QueryRewritingAgent"),
    "query_rewriting_agent": ("query_rewriting", "query_rewriting_agent"),
    "IntentRecognitionAgent": ("intent_recognition", "IntentRecognitionAgent"),
    "intent_recognition_agent": ("intent_recognition", "intent_recognition_agent"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module = import_module(f"{__name__}.{target[0]}")
    value = getattr(module, target[1])
    globals()[name] = value
    return value
