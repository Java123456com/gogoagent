from __future__ import annotations

from types import SimpleNamespace

from backend.tools import order as order_tools


def _order(order_id: str, *, destination: str = "上海", departure_city: str = "北京",
           departure_date: str = "2026-09-10", return_date: str = "2026-09-12",
           purpose: str = "客户拜访", status: str = "SUBMITTED"):
    row = SimpleNamespace(
        order_id=order_id, user_id="u-1", destination=destination,
        departure_city=departure_city, departure_date=departure_date,
        return_date=return_date, purpose=purpose, status=status, approval_id="ap-old",
    )
    row.as_dict = lambda: {
        "order_id": row.order_id, "destination": row.destination,
        "departure_city": row.departure_city, "departure_date": row.departure_date,
        "return_date": row.return_date, "purpose": row.purpose, "status": row.status,
    }
    return row


class _WriteService:
    def __init__(self):
        self.created = []
        self.modified = []

    def create_and_submit(self, user_id, values):
        self.created.append((user_id, values))
        return SimpleNamespace(as_dict=lambda: {"order_id": "to-new", "status": "PENDING"})

    def modify_and_resubmit(self, existing, values):
        self.modified.append((existing, values))
        return SimpleNamespace(
            old_approval_id="ap-old", old_approval_cancelled=True,
            new_approval=SimpleNamespace(as_dict=lambda: {"process_instance_id": "ap-new"}),
        )


def _bind(monkeypatch, rows):
    service = _WriteService()
    monkeypatch.setattr(order_tools.travel_order_repository, "list_by_user", lambda _user: rows)
    monkeypatch.setattr(order_tools.travel_order_repository, "get", lambda order_id: next(
        (row for row in rows if row.order_id == order_id), None,
    ))
    monkeypatch.setattr(order_tools, "travel_order_service", service)
    return service


def _submit(**extra):
    return order_tools.submit_travel_approval.invoke({
        "user_id": "u-1", "departure_city": "北京", "destination": "上海",
        "departure_date": "2026-09-10", "return_date": "2026-09-12", "purpose": "客户拜访",
        **extra,
    })


def test_submit_allows_low_conflict_and_returns_report(monkeypatch):
    service = _bind(monkeypatch, [_order("to-low", purpose="旧申请")])
    result = _submit()
    assert result["success"] is True
    assert result["conflictReport"]["check_status"] == "SUCCESS"
    assert result["conflictReport"]["conflicts"][0]["severity"] == "LOW"
    assert len(service.created) == 1


def test_submit_blocks_high_until_user_explicitly_confirms(monkeypatch):
    service = _bind(monkeypatch, [_order("to-high", destination="广州")])
    blocked = _submit()
    assert blocked["success"] is False
    assert blocked["needConfirm"] is True
    assert blocked["errorCode"] == "TRAVEL_CONFLICT"
    assert blocked["highestSeverity"] == "HIGH"
    assert service.created == []

    allowed = _submit(ignore_conflicts=True)
    assert allowed["success"] is True
    assert len(service.created) == 1


def test_submit_is_idempotent_and_modify_excludes_current_order(monkeypatch):
    existing = _order("to-same")
    service = _bind(monkeypatch, [existing])
    duplicate = _submit()
    assert duplicate["success"] is True
    assert duplicate["idempotent"] is True
    assert service.created == []

    changed = order_tools.modify_travel_order.invoke({
        "order_id": "to-same", "user_id": "u-1", "purpose": "项目评审",
    })
    assert changed["success"] is True
    assert changed["conflictReport"]["conflicts"] == []
    assert service.modified == [(existing, {"purpose": "项目评审"})]


def test_modify_without_changes_short_circuits(monkeypatch):
    service = _bind(monkeypatch, [_order("to-1")])
    result = order_tools.modify_travel_order.invoke({"order_id": "to-1", "user_id": "u-1"})
    assert result["success"] is True
    assert result["noChanges"] is True
    assert service.modified == []
