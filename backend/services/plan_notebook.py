"""Session-scoped equivalent of AgentScope's ``PlanNotebook``."""
from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any
from uuid import uuid4

from backend.services.runtime_events import emit_event

VALID_STATES = frozenset({"todo", "in_progress", "done", "abandoned"})


class PlanNotebookService:
    def __init__(self) -> None:
        self._current: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._lock = RLock()

    def create(self, session_id: str, name: str, subtasks: list[Any]) -> dict[str, Any]:
        tasks = []
        for item in subtasks:
            task_name = item.get("name") if isinstance(item, dict) else item
            if task_name is not None and str(task_name).strip():
                tasks.append({"name": str(task_name).strip(), "state": "todo"})
        plan = {"planId": f"plan_{uuid4().hex}", "name": name or "行程规划", "tasks": tasks}
        with self._lock:
            self._current[session_id] = plan
        self._emit(plan)
        return deepcopy(plan)

    def update_info(self, session_id: str, name: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            plan = self._current.get(session_id)
            if plan is None:
                return None
            if name is not None and name.strip():
                plan["name"] = name.strip()
            snapshot = deepcopy(plan)
        self._emit(snapshot)
        return snapshot

    def revise(self, session_id: str, subtasks: list[Any]) -> dict[str, Any] | None:
        with self._lock:
            current = self._current.get(session_id)
            if current is None:
                return None
            old_states = {task["name"]: task["state"] for task in current["tasks"]}
            current["tasks"] = [
                {
                    "name": str(item.get("name") if isinstance(item, dict) else item).strip(),
                    "state": (
                        str(item.get("state"))
                        if isinstance(item, dict) and str(item.get("state")) in VALID_STATES
                        else old_states.get(
                            str(item.get("name") if isinstance(item, dict) else item).strip(),
                            "todo",
                        )
                    ),
                }
                for item in subtasks
                if str(item.get("name") if isinstance(item, dict) else item).strip()
            ]
            snapshot = deepcopy(current)
        self._emit(snapshot)
        return snapshot

    def update_task(self, session_id: str, index: int, state: str) -> dict[str, Any] | None:
        normalized = state.lower().strip()
        if normalized not in VALID_STATES:
            raise ValueError(f"未知子任务状态: {state}")
        with self._lock:
            plan = self._current.get(session_id)
            if plan is None or index < 0 or index >= len(plan["tasks"]):
                return None
            plan["tasks"][index]["state"] = normalized
            snapshot = deepcopy(plan)
        self._emit(snapshot)
        return snapshot

    def view(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            plan = self._current.get(session_id)
            return deepcopy(plan) if plan else None

    def finish(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            plan = self._current.pop(session_id, None)
            if plan is None:
                return None
            for task in plan["tasks"]:
                if task["state"] not in {"done", "abandoned"}:
                    task["state"] = "done"
            self._history.setdefault(session_id, []).append(deepcopy(plan))
            snapshot = deepcopy(plan)
        self._emit(None)
        return snapshot

    def history(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._history.get(session_id, []))

    def recover(self, session_id: str, plan_id: str) -> dict[str, Any] | None:
        with self._lock:
            plan = next(
                (item for item in self._history.get(session_id, []) if item["planId"] == plan_id),
                None,
            )
            if plan is None:
                return None
            self._current[session_id] = deepcopy(plan)
            snapshot = deepcopy(plan)
        self._emit(snapshot)
        return snapshot

    @staticmethod
    def _emit(plan: dict[str, Any] | None) -> None:
        emit_event("plan_update", {
            "type": "plan_update",
            "agentName": "ItineraryPlanAgent",
            "planName": plan.get("name", "") if plan else "",
            "tasks": [
                {"idx": index, "name": task.get("name", ""), "state": task.get("state", "todo")}
                for index, task in enumerate(plan.get("tasks", []))
            ] if plan else [],
        })


plan_notebook = PlanNotebookService()
