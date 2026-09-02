from backend.tools.skills import execute_shell_command, load_skill_through_path


def test_load_skill_through_path_reads_java_skill_resource():
    result = load_skill_through_path.invoke({"skill_id": "tuniu-cli", "path": "SKILL.md"})
    assert result["ok"] is True
    assert "tuniu CLI" in result["content"]


def test_load_skill_rejects_path_traversal():
    result = load_skill_through_path.invoke({"skill_id": "tuniu-cli", "path": "../booking-agent-system.md"})
    assert result["ok"] is False


def test_execute_shell_command_enforces_skill_whitelist():
    result = execute_shell_command.invoke({"command": "whoami"})
    assert result["ok"] is False
    assert result["exit_code"] == 126


def test_execute_shell_command_runs_allowed_echo():
    result = execute_shell_command.invoke({"command": "echo skill-ok"})
    assert result["ok"] is True
    assert "skill-ok" in result["stdout"]
