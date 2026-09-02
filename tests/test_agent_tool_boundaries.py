import pytest

from backend.runtime.tool_policy import tool_policy_registry


def test_plan_and_info_agents_cannot_receive_booking_writes():
    forbidden = ["book_flight", "book_hotel", "book_train", "execute_booking", "cancel_booking"]
    assert tool_policy_registry.validate_agent_boundary("ItineraryPlanAgent", forbidden) == sorted(forbidden)
    assert tool_policy_registry.validate_agent_boundary("InfoAgent", forbidden) == sorted(forbidden)


def test_manage_and_booking_agents_are_not_artificially_blocked():
    assert tool_policy_registry.validate_agent_boundary("ItineraryManageAgent", ["cancel_booking"]) == []
    assert tool_policy_registry.validate_agent_boundary("BookingAgent", ["cancel_booking"]) == []


def test_legacy_review_is_not_a_runtime_subagent():
    from backend.runtime.agent_executor import LocalSubAgentExecutor

    assert LocalSubAgentExecutor._resolve("ItineraryReviewAgent") is None
    with pytest.raises(ValueError, match="未知的子 Agent"):
        LocalSubAgentExecutor().execute("ItineraryReviewAgent", {})
