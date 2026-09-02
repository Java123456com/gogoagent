"""Agent lifecycle hooks mirroring the Java AgentScope hook boundary."""

from backend.hooks.context_hooks import (
    DynamicTimeInjectionHook,
    SkillContentCollapseHook,
    ToolResultCompressHook,
    prepare_agent_state,
)

__all__ = [
    "DynamicTimeInjectionHook",
    "SkillContentCollapseHook",
    "ToolResultCompressHook",
    "prepare_agent_state",
]
