from __future__ import annotations

from backend.core.request_context import bind_context
from backend.observability import record_event


def test_observability_redacts_sensitive_nested_fields_and_uses_stable_user_hash(caplog):
    with bind_context("user-1", "session-1", "BookingAgent", request_id="request-1"):
        first = record_event(
            "tool.completed",
            tool="execute_shell_command",
            command_environment={"TUNIU_API_KEY": "sk-secret-value"},
            contact_phone="13800138000",
            payment_url="https://pay.example/?token=top-secret",
        )
    with bind_context("user-1", "session-2", "BookingAgent", request_id="request-2"):
        second = record_event("tool.completed", tool="execute_shell_command")

    assert first["user_correlation"] == second["user_correlation"]
    assert first["command_environment"]["TUNIU_API_KEY"] == "***"
    assert first["contact_phone"] == "***"
    log_text = "\n".join(record.message for record in caplog.records)
    assert "sk-secret-value" not in log_text
    assert "13800138000" not in log_text
    assert "top-secret" not in log_text
