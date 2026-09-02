"""Catalog derived from the executable agent registry."""
from backend.agents.registry import AGENT_REGISTRY

AGENT_CATALOG = {
    item.name: item.kind.value
    for item in AGENT_REGISTRY
}

HOOK_CATALOG = [
    "progress_notifier", "session_persistence", "active_agent_persistence", "execution_registry",
    "execution_logger", "tool_circuit_breaker", "api_key_injection", "booking_persistence",
    "cli_result_compression", "skill_content_collapse", "dynamic_time_injection", "user_isolation",
]
