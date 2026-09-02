from __future__ import annotations

import base64
import os
from uuid import uuid4

import pytest

from backend.config import get_settings
from backend.core.request_context import bind_context
from backend.infrastructure.bootstrap import bootstrap
from backend.infrastructure.mcp import call_mcp_tool
from backend.infrastructure.security import (
    decrypt_api_key,
    encrypt_api_key,
    mask_sensitive,
    sanitize_sensitive,
)
from backend.services.api_key_service import api_key_service
from backend.services.candidate_search import MultiSourceCandidateSearchProvider
from backend.services.rgh_isolation import RghIsolationManager
from backend.services.runtime_events import bind_event_sink, emit_event
from backend.tools import skills
from backend.tools._common import current_user_id
from backend.tools.skills import (
    _prepare_environment,
    list_available_skills,
    skill_command_available,
)


def test_aes_gcm_is_random_authenticated_and_reads_legacy_ciphertext():
    first = encrypt_api_key("sk-secret-value")
    second = encrypt_api_key("sk-secret-value")
    assert first.startswith("gcm:v1:")
    assert first != second
    assert decrypt_api_key(first) == "sk-secret-value"

    damaged = first[:-2] + ("AA" if first[-2:] != "AA" else "BB")
    with pytest.raises(Exception):  # noqa: B017 - authentication failure type is backend-specific
        decrypt_api_key(damaged)

    secret = get_settings().api_key_encrypt_secret.encode()
    plaintext = b"legacy-key"
    legacy = base64.urlsafe_b64encode(
        bytes(value ^ secret[index % len(secret)] for index, value in enumerate(plaintext))
    ).decode()
    assert decrypt_api_key(legacy) == "legacy-key"


def test_all_event_boundaries_redact_nested_credentials_and_pii():
    payload = {
        "authorization": "Bearer top-secret-token",
        "nested": {
            "phone": "13800138000",
            "chineseName": "张三",
            "message": "api_key=do-not-leak",
        },
    }
    cleaned = sanitize_sensitive(payload)
    assert cleaned["authorization"] == "***"
    assert cleaned["nested"]["phone"] == "***"
    assert cleaned["nested"]["chineseName"] == "***"
    assert "do-not-leak" not in cleaned["nested"]["message"]
    assert "abc123" not in mask_sensitive("Authorization: Bearer abc123.def456")
    combined = mask_sensitive(
        "乘客 张三丰，证件号 11010119900307761X，卡号 6225880112345678，"
        "namePinyin=ZHANG SAN"
    )
    for secret in ("张三丰", "11010119900307761X", "6225880112345678", "ZHANG SAN"):
        assert secret not in combined

    events = []
    with bind_event_sink(lambda name, data: events.append((name, data))):
        emit_event("custom", payload)
    assert events[0][1] == cleaned


def test_skill_registry_discovers_four_external_travel_skills():
    rows = list_available_skills.invoke({})
    skills = {item["skill_id"] for item in rows}
    assert {"tuniu-cli", "flight-manager", "flyai", "rolling-go-hotel"} <= skills
    assert set(MultiSourceCandidateSearchProvider().providers) == {
        "tuniu-cli", "flight-manager", "flyai", "rolling-go-hotel",
    }


def test_api_keys_are_user_scoped_and_injected_through_environment():
    bootstrap()
    user_a, user_b = f"key-{uuid4().hex[:8]}", f"key-{uuid4().hex[:8]}"
    try:
        api_key_service.save(user_a, "flight-manager", "sk_user_a_secret")
        api_key_service.save(user_b, "flight-manager", "sk_user_b_secret")
        env_a, command_a, _ = _prepare_environment(
            user_a, 'curl -H "Authorization: Bearer ${FLIGHT_API_KEY}" https://example.test',
        )
        env_b, _, _ = _prepare_environment(
            user_b, 'curl -H "Authorization: Bearer ${FLIGHT_API_KEY}" https://example.test',
        )
        assert env_a["FLIGHT_API_KEY"] == "sk_user_a_secret"
        assert env_b["FLIGHT_API_KEY"] == "sk_user_b_secret"
        assert "sk_user_a_secret" not in command_a
    finally:
        api_key_service.delete(user_a, "flight-manager")
        api_key_service.delete(user_b, "flight-manager")


def test_authenticated_tenant_cannot_be_overridden_by_tool_argument(monkeypatch):
    monkeypatch.setenv("TUNIU_API_KEY", "server-global-key-must-not-leak")
    with bind_context("tenant-a", "session-a"):
        assert current_user_id("tenant-b") == "tenant-a"
        environment, _, _ = _prepare_environment("tenant-a", "tuniu list")
    assert "TUNIU_API_KEY" not in environment


def test_rgh_workspace_projects_and_writes_back_user_token(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "rgh_workspace", str(tmp_path / "rgh-users"))
    monkeypatch.setattr(get_settings(), "redis_url", "redis://configured-for-authority")
    manager = RghIsolationManager()

    class Tokens:
        value = '{"access_token":"old"}'
        saved = None

        def get(self, _user_id):
            return self.value

        def save(self, _user_id, token):
            self.saved = token

        def delete(self, _user_id):
            self.value = None

    tokens = Tokens()
    manager.tokens = tokens
    environment, baseline = manager.prepare_environment("user_001", {})
    assert environment["HOME"].endswith("user_001")
    assert manager.workspace.read_token("user_001") == tokens.value
    manager.workspace.write_token("user_001", '{"access_token":"refreshed"}')
    manager.after_command("user_001", "rgh whoami", baseline)
    assert tokens.saved == '{"access_token":"refreshed"}'
    with pytest.raises(ValueError):
        manager.workspace.user_dir("../escape")


def test_rollinggo_local_bin_is_available_without_global_install(monkeypatch, tmp_path):
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    (local_bin / "rgh").write_text("binary", encoding="utf-8")
    monkeypatch.setattr(skills, "_SKILL_LOCAL_BIN", local_bin)
    monkeypatch.setattr(skills.shutil, "which", lambda _command: None)
    assert skill_command_available("rgh")
    environment, _, _ = _prepare_environment("user_001", "rgh --help")
    assert str(local_bin) in environment["PATH"].split(os.pathsep)


def test_project_local_node_bin_is_available_without_global_install(monkeypatch, tmp_path):
    project_bin = tmp_path / "node_modules" / ".bin"
    project_bin.mkdir(parents=True)
    (project_bin / "flyai").write_text("binary", encoding="utf-8")
    monkeypatch.setattr(skills, "_PROJECT_NODE_BIN", project_bin)
    monkeypatch.setattr(skills.shutil, "which", lambda _command: None)
    assert skills.skill_command_available("flyai")
    environment, _, _ = _prepare_environment("user_001", "flyai --help")
    assert str(project_bin) in environment["PATH"].split(os.pathsep)


def test_mcp_tool_whitelist_rejects_before_transport_creation():
    with pytest.raises(PermissionError, match="未在白名单"):
        call_mcp_tool(
            endpoint="https://example.invalid/mcp",
            tool_name="dangerous_tool",
            allowed_tools=["safe_tool"],
        )
