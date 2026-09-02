from __future__ import annotations

from types import SimpleNamespace

from backend.config import get_settings
from backend.tools import conflict


def _order(order_id: str, departure_city: str, destination: str, departure_date: str,
           return_date: str, status: str = "APPROVED"):
    return SimpleNamespace(
        order_id=order_id,
        departure_city=departure_city,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        status=status,
    )


def _report(monkeypatch, orders, **candidate):
    monkeypatch.setattr(conflict.travel_order_repository, "list_by_user", lambda _user_id: orders)
    values = {
        "user_id": "user-1",
        "departure_city": "北京",
        "destination": "上海",
        "departure_date": "2026-09-10",
        "return_date": "2026-09-12",
        **candidate,
    }
    return conflict.check_travel_order_conflicts.invoke(values)


def test_conflict_validation_and_same_route_overlap(monkeypatch):
    invalid = _report(monkeypatch, [], departure_date="2026-09-12", return_date="2026-09-10")
    assert invalid["check_status"] == "INVALID"
    assert invalid["has_conflict"] is False
    assert "晚于" in invalid["summary"]

    report = _report(monkeypatch, [_order("low", "北京市", "上海市", "2026-09-10", "2026-09-12")])
    assert report["check_status"] == "SUCCESS"
    assert report["conflicts"][0]["type"] == "TIME_OVERLAP_SAME_CITY"
    assert report["conflicts"][0]["severity"] == "LOW"


def test_conflict_handoff_and_adjacency_thresholds(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "city_pair_minutes", {"上海-广州": 270, "上海-乌鲁木齐": 1500, "上海-拉萨": 540})

    same_day = _report(monkeypatch, [_order("medium", "北京", "上海", "2026-09-08", "2026-09-10")])
    assert same_day["conflicts"][0]["type"] == "TRANSIT_TOO_TIGHT"
    assert same_day["conflicts"][0]["severity"] == "MEDIUM"

    impossible = _report(
        monkeypatch,
        [_order("high", "北京", "上海", "2026-09-08", "2026-09-10")],
        departure_city="乌鲁木齐",
    )
    assert impossible["conflicts"][0]["severity"] == "HIGH"

    enough_next_day = _report(
        monkeypatch,
        [_order("enough", "北京", "上海", "2026-09-08", "2026-09-09")],
        departure_city="广州",
    )
    assert enough_next_day["conflicts"] == []

    tight_next_day = _report(
        monkeypatch,
        [_order("adjacent", "北京", "上海", "2026-09-08", "2026-09-09")],
        departure_city="拉萨",
    )
    assert tight_next_day["conflicts"][0]["type"] == "DISCONNECTED_ROUTE"
    assert tight_next_day["conflicts"][0]["severity"] == "MEDIUM"


def test_conflicts_filter_and_sort_by_severity(monkeypatch):
    orders = [
        _order("low", "北京", "上海", "2026-09-10", "2026-09-12"),
        _order("high", "广州", "深圳", "2026-09-10", "2026-09-12"),
        _order("inactive", "成都", "重庆", "2026-09-10", "2026-09-12", "CANCELLED"),
        _order("bad", "成都", "重庆", "bad-date", "2026-09-12"),
    ]
    report = _report(monkeypatch, orders)
    assert [item["order_id"] for item in report["conflicts"]] == ["high", "low"]
    assert [item["severity"] for item in report["conflicts"]] == ["HIGH", "LOW"]

    excluded = _report(monkeypatch, orders, exclude_order_id="high")
    assert [item["order_id"] for item in excluded["conflicts"]] == ["low"]


def test_conflict_lookup_failure_is_not_reported_as_no_conflict(monkeypatch):
    monkeypatch.setattr(
        conflict.travel_order_repository, "list_by_user",
        lambda _user_id: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    report = conflict.check_travel_order_conflicts.invoke({
        "user_id": "user-1", "departure_city": "北京", "destination": "上海",
        "departure_date": "2026-09-10", "return_date": "2026-09-12",
    })
    assert report["check_status"] == "FAILED"
    assert report["has_conflict"] is False
