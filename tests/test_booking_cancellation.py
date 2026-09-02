from __future__ import annotations

import json

from backend.services import booking_cancellation as cancellation_module
from backend.services.booking_cancellation import BookingCancellationService


class _Bookings:
    def __init__(self, record):
        self.record = record
        self.cancel_calls = []

    def get_by_id(self, booking_id):
        return self.record.copy() if self.record and self.record["booking_id"] == booking_id else None

    def cancel(self, booking_id, reason=None):
        self.cancel_calls.append((booking_id, reason))
        self.record["status"] = "CANCELLED"
        self.record["remark"] = reason
        return self.record.copy()


class _Executor:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, arguments):
        self.calls.append(arguments)
        return self.response


def _record(**overrides):
    return {
        "booking_id": "bk-1",
        "user_id": "user-1",
        "biz_type": "FLIGHT",
        "external_order_no": "TN-1",
        "status": "CONFIRMED",
        **overrides,
    }


def _service(monkeypatch, record, response=None, has_key=True):
    bookings = _Bookings(record)
    executor = _Executor(response or {"ok": True, "exit_code": 0, "stdout": '{"successCode": true}'})
    monkeypatch.setattr(cancellation_module, "booking_service", bookings)
    monkeypatch.setattr(cancellation_module.api_key_service, "get", lambda *_: "sk-secret" if has_key else None)
    monkeypatch.setattr(cancellation_module, "execute_shell_command", executor)
    return BookingCancellationService(), bookings, executor


def test_flight_cancel_updates_only_after_wrapped_platform_success(monkeypatch):
    wrapped = {
        "ok": True,
        "exit_code": 0,
        "stdout": json.dumps({
            "success": True,
            "result": {"structuredContent": {"result": json.dumps({"success": True})}},
        }),
    }
    service, bookings, executor = _service(monkeypatch, _record(), wrapped)

    result = service.cancel("user-1", "bk-1", "行程变更")

    assert result["success"] is True
    assert result["platform_cancelled"] is True
    assert bookings.record["status"] == "CANCELLED"
    assert bookings.record["remark"] == "行程变更"
    assert executor.calls[0]["command"].startswith("tuniu call flight cancelOrder")
    assert "sk-secret" not in executor.calls[0]["command"]


def test_platform_failures_and_missing_key_preserve_local_status(monkeypatch):
    service, bookings, _ = _service(
        monkeypatch, _record(), {"ok": True, "exit_code": 0, "stdout": '{"success": false, "msg": "拒绝"}'},
    )
    result = service.cancel("user-1", "bk-1")
    assert result["error_code"] == "PLATFORM_CANCEL_FAILED"
    assert bookings.record["status"] == "CONFIRMED"
    assert bookings.cancel_calls == []

    service, bookings, executor = _service(monkeypatch, _record(), has_key=False)
    result = service.cancel("user-1", "bk-1")
    assert result["error_code"] == "PLATFORM_CANCEL_FAILED"
    assert bookings.record["status"] == "CONFIRMED"
    assert executor.calls == []


def test_hotel_is_local_only_and_cancel_is_idempotent(monkeypatch):
    service, bookings, executor = _service(monkeypatch, _record(biz_type="HOTEL"))
    result = service.cancel("user-1", "bk-1")
    assert result["success"] is True
    assert result["platform_cancelled"] is False
    assert bookings.record["status"] == "CANCELLED"
    assert executor.calls == []

    repeat = service.cancel("user-1", "bk-1")
    assert repeat["success"] is True
    assert repeat["platform_cancelled"] is False
    assert len(bookings.cancel_calls) == 1


def test_cancellation_validates_ownership_and_legacy_success(monkeypatch):
    service, _bookings, executor = _service(monkeypatch, _record(biz_type="TRAIN"))
    denied = service.cancel("another-user", "bk-1")
    assert denied["error_code"] == "PERMISSION_DENIED"
    assert executor.calls == []

    success = service.cancel("user-1", "bk-1")
    assert success["success"] is True
    assert success["biz_type"] == "TRAIN"
    assert "train cancelOrder" in executor.calls[0]["command"]
