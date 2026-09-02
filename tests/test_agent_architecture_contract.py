from backend.agents.registry import AgentKind, master_dispatchable_agents, registrations
from backend.infrastructure.model_profiles import ModelProfile, resolve_model_profile


def test_operating_topology_has_4_plus_1_plus_2_plus_2_roles():
    assert len(registrations(AgentKind.REACT)) == 4
    assert len(registrations(AgentKind.PLAN_AND_EXECUTE)) == 1
    assert len(registrations(AgentKind.ONE_SHOT)) == 2
    assert len(registrations(AgentKind.LIGHTWEIGHT_SERVICE)) == 2
    assert [item.name for item in master_dispatchable_agents()] == [
        "ItineraryManageAgent", "ItineraryPlanAgent", "InfoAgent", "BookingAgent",
    ]


def test_model_profiles_preserve_role_assignment():
    assert resolve_model_profile(ModelProfile.FAST).enable_thinking is False
    assert resolve_model_profile(ModelProfile.STABLE).enable_thinking is False
    assert resolve_model_profile(ModelProfile.STRONG).enable_thinking is False
    thinking = resolve_model_profile(ModelProfile.STRONG_THINKING)
    assert thinking.enable_thinking is True
    assert thinking.extra_body()["thinking_budget"] == 2048
