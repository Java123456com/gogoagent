from typing import Any, TypedDict

from langchain_core.messages import AnyMessage


class TravelAgentState(TypedDict, total=False):
    """Explicit request state shared by the LangGraph pipeline nodes."""

    session_id: str
    request_id: str
    execution_generation: int
    deadline_at: float
    user_id: str
    request: str
    original_question: str
    rewritten_question: str
    messages: list[AnyMessage]
    intent: str
    intent_json: dict[str, Any]
    active_agent: str
    active_agent_name: str
    plan: list[str]
    preferences: dict[str, Any]
    evidence: list[dict[str, Any]]
    policy: dict[str, Any]
    itinerary: dict[str, Any]
    booking_options: list[dict[str, Any]]
    reimbursement: dict[str, Any]
    draft: str
    critique: str
    final: str
    trace: list[dict[str, Any]]
    revision_count: int
    pending_interaction: dict[str, Any]
    pending_tool: dict[str, Any]
    resume_messages: list[AnyMessage]
    latest_user_response: str
    interrupted: bool
    continuation: bool
    active_resume_agent: str
    candidates: dict[str, Any]
    long_term_preferences: list[str]
    memory_original_messages: list[AnyMessage]
    memory_working_messages: list[AnyMessage]
    memory_offload_context: dict[str, Any]
    memory_compression_events: list[dict[str, Any]]
    memory_token_before: int
    memory_token_after: int
