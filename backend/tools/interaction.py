from typing import Any

from ._common import tool


class UserInteractionRequired(RuntimeError):
    """Tool-suspend signal corresponding to AgentScope ToolSuspendException."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("question") or "等待用户输入")
        self.payload = payload


@tool
def ask_user(question: str, ui_type: str = "text", options: list[str] | None = None,
             fields: list[dict[str, Any]] | None = None, default_value: Any = None,
             allow_other: bool = False) -> dict:
    """向前端发起 Human-in-the-Loop 结构化问题并暂停 Agent 推理。"""
    payload = {
        "requires_user_input": True, "question": question, "ui_type": ui_type,
        "options": options or [], "fields": fields or [], "default_value": default_value,
        "allow_other": allow_other,
    }
    raise UserInteractionRequired(payload)


def tools(): return [ask_user]
