from backend.services.plan_notebook import PlanNotebookService
from backend.services.runtime_events import bind_event_sink


def test_plan_notebook_emits_java_plan_update_contract_and_recovers_history():
    notebook = PlanNotebookService()
    events = []
    with bind_event_sink(lambda name, data: events.append((name, data))):
        plan = notebook.create("session-plan", "行程规划", ["搜索交通", "搜索酒店"])
        notebook.update_task("session-plan", 0, "in_progress")
        notebook.update_task("session-plan", 0, "done")
        finished = notebook.finish("session-plan")
        recovered = notebook.recover("session-plan", plan["planId"])

    assert finished is not None
    assert recovered is not None
    assert recovered["tasks"][0]["state"] == "done"
    plan_events = [data for name, data in events if name == "plan_update"]
    assert plan_events[0] == {
        "type": "plan_update",
        "agentName": "ItineraryPlanAgent",
        "planName": "行程规划",
        "tasks": [
            {"idx": 0, "name": "搜索交通", "state": "todo"},
            {"idx": 1, "name": "搜索酒店", "state": "todo"},
        ],
    }
    assert plan_events[-2]["planName"] == ""
    assert plan_events[-2]["tasks"] == []
    assert plan_events[-1]["tasks"][0]["state"] == "done"
