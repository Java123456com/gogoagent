"""SkillBox-compatible tools for the Plan and Booking agents.

The Java implementation exposes ``load_skill_through_path`` and a restricted
``execute_shell_command`` tool.  These synchronous LangChain tools preserve the
same names and user-scoped credential injection while keeping command output
bounded before it re-enters the model context.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from backend.observability import record_event
from backend.services.api_key_service import api_key_service
from backend.services.rgh_isolation import rgh_isolation

from ._common import current_user_id, tool

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "resources" / "skills"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_NODE_BIN = _PROJECT_ROOT / "node_modules" / ".bin"
_RUNTIME_ROOT = _PROJECT_ROOT / ".runtime" / "skill-users"
_SKILL_LOCAL_BIN = _SKILL_ROOT / "rolling-go-hotel" / "bin"
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USER_CREDENTIAL_ENV = {"TUNIU_API_KEY", "FLIGHT_API_KEY", "FLYAI_API_KEY"}
_ALLOWED_COMMANDS = {
    "bash", "cat", "curl", "date", "echo", "env", "grep", "ls", "mkdir", "node",
    "flyai", "npm", "npx", "nohup", "python", "python3", "rgh", "sleep", "source", "tuniu",
    "which",
}
_COMMAND_RE = re.compile(r"(?:^|[;&|()\s])([A-Za-z][A-Za-z0-9_.-]*)(?:\s|$)")


def _safe_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError("技能路径越界")
    return target


@tool
def load_skill_through_path(skill_id: str, path: str = "SKILL.md") -> dict[str, Any]:
    """按需加载 Java SkillBox 风格的技能正文或 references 文档。"""
    if not skill_id or not path:
        return {"ok": False, "error": "skill_id 和 path 不能为空"}
    try:
        skill_dir = _safe_path(_SKILL_ROOT, skill_id)
        target = _safe_path(skill_dir, path)
        if not target.is_file():
            return {"ok": False, "skill_id": skill_id, "path": path, "error": "技能文件不存在"}
        return {
            "ok": True,
            "skill_id": skill_id,
            "path": path,
            "working_directory": str(skill_dir),
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "skill_id": skill_id, "path": path, "error": str(exc)}


@tool
def list_available_skills() -> list[dict[str, Any]]:
    """发现当前已注册的 Skill，返回名称、说明、运行时和依赖命令。"""
    rows: list[dict[str, Any]] = []
    for skill_file in sorted(_SKILL_ROOT.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        metadata = _frontmatter(text)
        command = {
            "tuniu-cli": "tuniu",
            "flight-manager": "curl",
            "flyai": "flyai",
            "rolling-go-hotel": "rgh",
        }.get(skill_file.parent.name)
        rows.append({
            "skill_id": skill_file.parent.name,
            "name": metadata.get("name") or skill_file.parent.name,
            "display_name": metadata.get("display_name") or metadata.get("name") or skill_file.parent.name,
            "description": metadata.get("description") or "",
            "command": command,
            "executable": skill_command_available(command) if command else True,
        })
    return rows


def skill_command_available(command: str) -> bool:
    """Check PATH plus project-local and Skill-local executable directories."""
    if shutil.which(command):
        return True
    for directory in (_PROJECT_NODE_BIN, _SKILL_LOCAL_BIN):
        for name in (command, f"{command}.cmd", f"{command}.exe"):
            if (directory / name).is_file():
                return True
    return False


def _command_names(command: str) -> set[str]:
    return {match.group(1).lower() for match in _COMMAND_RE.finditer(command or "")}


def _user_key(user_id: str, provider: str) -> str | None:
    return api_key_service.get(user_id, provider)


def _observed_command_result(command: str, response: dict[str, Any]) -> dict[str, Any]:
    """Report command metadata without leaking command text or environment values."""
    record_event(
        "external.command",
        tool="execute_shell_command",
        command_names=sorted(_command_names(command)),
        result_class="success" if response.get("ok") else "error",
        exit_code=response.get("exit_code"),
    )
    return response


def _prepare_environment(user_id: str, command: str) -> tuple[dict[str, str], str, str | None]:
    environment = dict(os.environ)
    for variable in _USER_CREDENTIAL_ENV:
        environment.pop(variable, None)
    local_bins = [directory for directory in (_PROJECT_NODE_BIN, _SKILL_LOCAL_BIN)
                  if directory.is_dir()]
    if local_bins:
        environment["PATH"] = os.pathsep.join(
            [*(str(directory) for directory in local_bins), environment.get("PATH", "")]
        )
    baseline = None
    if rgh_isolation.handles(command):
        environment, baseline = rgh_isolation.prepare_environment(user_id, environment)
    else:
        if not _SAFE_USER_ID.fullmatch(user_id):
            raise ValueError(f"非法 userId，拒绝用于文件路径: {user_id}")
        user_dir = (_RUNTIME_ROOT / user_id).resolve()
        user_dir.mkdir(parents=True, exist_ok=True)
        environment["HOME"] = str(user_dir)
        environment["USERPROFILE"] = str(user_dir)
    if "tuniu" in command.lower() and (key := _user_key(user_id, "tuniu-cli")):
        environment["TUNIU_API_KEY"] = key
    if "${FLIGHT_API_KEY}" in command and (key := _user_key(user_id, "flight-manager")):
        environment["FLIGHT_API_KEY"] = key
        if os.name == "nt":
            command = command.replace("${FLIGHT_API_KEY}", "%FLIGHT_API_KEY%")
    if command.lstrip().startswith("flyai ") and (key := _user_key(user_id, "flyai")):
        environment["FLYAI_API_KEY"] = key
    return environment, command, baseline


@tool
def execute_shell_command(command: str, timeout_seconds: int = 180,
                          user_id: str | None = None) -> dict[str, Any]:
    """执行 SkillBox 白名单内的 CLI 命令，并返回有界 stdout/stderr。"""
    if not command or not command.strip():
        return _observed_command_result(command, {"ok": False, "exit_code": 2, "error": "command 不能为空"})
    names = _command_names(command)
    unknown = sorted(name for name in names if name not in _ALLOWED_COMMANDS)
    if unknown:
        return _observed_command_result(command, {
            "ok": False, "exit_code": 126, "error": f"命令不在 Skill 白名单中: {', '.join(unknown)}",
        })
    resolved_user = current_user_id(user_id)
    environment, prepared_command, rgh_baseline = _prepare_environment(resolved_user, command)
    timeout = max(1, min(int(timeout_seconds or 180), 600))
    try:
        completed = subprocess.run(
            prepared_command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(_SKILL_ROOT),
            env=environment,
            check=False,
        )
        response = {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-12000:],
        }
        return _observed_command_result(command, response)
    except subprocess.TimeoutExpired as exc:
        return _observed_command_result(command, {
            "ok": False,
            "exit_code": 124,
            "stdout": str(exc.stdout or "")[-12000:],
            "stderr": str(exc.stderr or "")[-12000:],
            "error": f"命令执行超过 {timeout} 秒，已终止等待",
        })
    except OSError as exc:
        return _observed_command_result(command, {"ok": False, "exit_code": 127, "error": str(exc)})
    finally:
        if rgh_isolation.handles(command):
            rgh_isolation.after_command(resolved_user, command, rgh_baseline)


def tools():
    return [list_available_skills, load_skill_through_path, execute_shell_command]


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    _, _, remainder = text.partition("\n")
    header, separator, _ = remainder.partition("\n---")
    if not separator:
        return {}
    values: dict[str, str] = {}
    for line in header.splitlines():
        key, marker, value = line.partition(":")
        if marker and not line.startswith((" ", "\t")):
            values[key.strip()] = value.strip().strip("\"'")
    return values
