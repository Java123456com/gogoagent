"""AgentScope PlanNotebook-compatible tool surface."""
from __future__ import annotations

from typing import Any

from backend.core.request_context import current_context
from backend.services.plan_notebook import plan_notebook

from ._common import tool


def _session_id() -> str:
    context = current_context()
    return context.session_id if context and context.session_id else "default"


@tool
def create_plan(name: str, subtasks: list[str]) -> dict[str, Any]:
    """创建一个计划及其有序子任务列表。"""
    return plan_notebook.create(_session_id(), name, subtasks)


@tool
def update_plan_info(name: str) -> dict[str, Any]:
    """修改当前计划名称。"""
    return plan_notebook.update_info(_session_id(), name) or {"error": "当前没有计划"}


@tool
def revise_current_plan(subtasks: list[str]) -> dict[str, Any]:
    """替换当前计划的子任务列表，同时保留同名任务状态。"""
    return plan_notebook.revise(_session_id(), subtasks) or {"error": "当前没有计划"}


@tool
def update_subtask_state(index: int, state: str) -> dict[str, Any]:
    """更新指定下标子任务状态：todo/in_progress/done/abandoned。"""
    return plan_notebook.update_task(_session_id(), index, state) or {"error": "子任务不存在"}


@tool
def finish_subtask(index: int) -> dict[str, Any]:
    """将指定下标子任务标记为完成。"""
    return plan_notebook.update_task(_session_id(), index, "done") or {"error": "子任务不存在"}


@tool
def view_subtasks() -> dict[str, Any]:
    """查看当前计划与全部子任务。"""
    return plan_notebook.view(_session_id()) or {"name": "", "tasks": []}


@tool
def get_subtask_count() -> dict[str, int]:
    """返回当前计划的子任务总数。"""
    plan = plan_notebook.view(_session_id())
    return {"count": len(plan.get("tasks", [])) if plan else 0}


@tool
def finish_plan() -> dict[str, Any]:
    """完成当前计划并移入历史计划。"""
    return plan_notebook.finish(_session_id()) or {"error": "当前没有计划"}


@tool
def view_historical_plans() -> list[dict[str, Any]]:
    """查看当前会话的历史计划。"""
    return plan_notebook.history(_session_id())


@tool
def recover_historical_plan(plan_id: str) -> dict[str, Any]:
    """按计划 ID 恢复一个历史计划。"""
    return plan_notebook.recover(_session_id(), plan_id) or {"error": "历史计划不存在"}


def tools():
    return [
        create_plan,
        update_plan_info,
        revise_current_plan,
        update_subtask_state,
        finish_subtask,
        view_subtasks,
        get_subtask_count,
        finish_plan,
        view_historical_plans,
        recover_historical_plan,
    ]
