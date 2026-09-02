"""RollingGo OAuth workspace and token isolation, mirroring the Java hooks."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from backend.config import get_settings
from backend.infrastructure.security import decrypt_api_key, encrypt_api_key

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RghWorkspace:
    TOKEN_DIR = ".hotel-cli"
    TOKEN_FILE = "token.json"
    LOGIN_OUTPUT_FILE = "rgh_login_output.txt"

    @property
    def base_dir(self) -> Path:
        configured = Path(get_settings().rgh_workspace)
        if not configured.is_absolute():
            configured = _PROJECT_ROOT / configured
        return configured.resolve()

    def user_dir(self, user_id: str) -> Path:
        if not _SAFE_USER_ID.fullmatch(str(user_id or "")):
            raise ValueError(f"非法 userId，拒绝用于文件路径: {user_id}")
        return self.base_dir / user_id

    def prepare(self, user_id: str) -> Path:
        directory = self.user_dir(user_id)
        token_dir = directory / self.TOKEN_DIR
        token_dir.mkdir(parents=True, exist_ok=True)
        self._chmod(directory, 0o700)
        self._chmod(token_dir, 0o700)
        return directory

    def token_file(self, user_id: str) -> Path:
        return self.user_dir(user_id) / self.TOKEN_DIR / self.TOKEN_FILE

    def login_output(self, user_id: str) -> Path:
        return self.user_dir(user_id) / self.LOGIN_OUTPUT_FILE

    def read_token(self, user_id: str) -> str | None:
        path = self.token_file(user_id)
        try:
            value = path.read_text(encoding="utf-8") if path.exists() else ""
            return value if value.strip() else None
        except OSError as exc:
            logger.warning("RollingGo 本地 Token 读取失败: userId=%s error=%s", user_id, exc)
            return None

    def write_token(self, user_id: str, token: str) -> None:
        path = self.token_file(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        self._chmod(path, 0o600)

    def delete_token(self, user_id: str) -> None:
        self._delete(self.token_file(user_id))

    def delete_login_output(self, user_id: str) -> None:
        self._delete(self.login_output(user_id))

    def cleanup_stale(self) -> None:
        base = self.base_dir
        if not base.is_dir():
            return
        now = time.time()
        settings = get_settings()
        for directory in base.iterdir():
            if not directory.is_dir():
                continue
            output = directory / self.LOGIN_OUTPUT_FILE
            try:
                if output.exists() and now - output.stat().st_mtime > settings.rgh_login_output_retention_seconds:
                    output.unlink(missing_ok=True)
                latest = max((item.stat().st_mtime for item in directory.rglob("*")), default=directory.stat().st_mtime)
                if now - latest > settings.rgh_workspace_retention_seconds:
                    shutil.rmtree(directory)
            except OSError as exc:
                logger.warning("RollingGo 工作区清理失败: path=%s error=%s", directory, exc)

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    @staticmethod
    def _delete(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class RghTokenStore:
    PREFIX = "rgh:token:"

    def __init__(self) -> None:
        self._redis: Any | None = None

    def _client(self) -> Any | None:
        if self._redis is not None:
            return self._redis
        url = get_settings().redis_url
        if not url:
            return None
        try:
            import redis

            self._redis = redis.from_url(url, decode_responses=True)
            return self._redis
        except Exception as exc:
            logger.warning("RollingGo Token Redis 初始化失败: %s", exc)
            return None

    def get(self, user_id: str) -> str | None:
        client = self._client()
        if client is None:
            return None
        try:
            stored = client.get(self.PREFIX + user_id)
            if not stored:
                return None
            try:
                return decrypt_api_key(stored)
            except Exception:
                if str(stored).lstrip().startswith("{"):
                    self.save(user_id, stored)
                    return stored
                return None
        except Exception as exc:
            logger.warning("RollingGo Token Redis 读取失败: %s", exc)
            return None

    def save(self, user_id: str, token: str) -> None:
        client = self._client()
        if client is None:
            return
        try:
            client.setex(
                self.PREFIX + user_id,
                get_settings().rgh_token_ttl_seconds,
                encrypt_api_key(token),
            )
        except Exception as exc:
            logger.warning("RollingGo Token Redis 保存失败: %s", exc)

    def delete(self, user_id: str) -> None:
        client = self._client()
        if client is not None:
            try:
                client.delete(self.PREFIX + user_id)
            except Exception as exc:
                logger.warning("RollingGo Token Redis 删除失败: %s", exc)


class RghIsolationManager:
    def __init__(self) -> None:
        self.workspace = RghWorkspace()
        self.tokens = RghTokenStore()
        self._watching: set[str] = set()
        self._lock = threading.RLock()
        self._last_cleanup = 0.0

    @staticmethod
    def handles(command: str) -> bool:
        return bool(re.search(r"(?:^|\s)rgh(?:\s|$)", command or ""))

    def prepare_environment(self, user_id: str, environment: dict[str, str]) -> tuple[dict[str, str], str | None]:
        directory = self.workspace.prepare(user_id)
        if time.monotonic() - self._last_cleanup > 3600:
            self.workspace.cleanup_stale()
            self._last_cleanup = time.monotonic()
        remote_token = self.tokens.get(user_id)
        if remote_token is not None:
            self.workspace.write_token(user_id, remote_token)
        elif get_settings().redis_url:
            # Redis is the authority in clustered mode; stale node-local state
            # must not silently resurrect a logged-out session.
            self.workspace.delete_token(user_id)
        else:
            # Standalone mode keeps its authority in the per-user local file.
            remote_token = self.workspace.read_token(user_id)
        updated = dict(environment)
        updated["HOME"] = str(directory)
        updated["USERPROFILE"] = str(directory)  # Windows-aware CLI runtimes
        return updated, remote_token

    def after_command(self, user_id: str, command: str, baseline: str | None) -> None:
        if re.search(r"(?:^|\s)rgh\s+logout(?:\s|$)", command):
            self.tokens.delete(user_id)
            self.workspace.delete_token(user_id)
            self.workspace.delete_login_output(user_id)
            with self._lock:
                self._watching.discard(user_id)
            return
        local = self.workspace.read_token(user_id)
        if local and local != baseline:
            self.tokens.save(user_id, local)
        if re.search(r"(?:^|\s)rgh\s+login(?:\s|$)", command):
            self._start_watcher(user_id, baseline)

    def _start_watcher(self, user_id: str, baseline: str | None) -> None:
        with self._lock:
            if user_id in self._watching:
                return
            self._watching.add(user_id)

        def watch() -> None:
            deadline = time.monotonic() + get_settings().rgh_login_watch_timeout_seconds
            try:
                while time.monotonic() < deadline:
                    token = self.workspace.read_token(user_id)
                    if token and token != baseline:
                        self.tokens.save(user_id, token)
                        self.workspace.delete_login_output(user_id)
                        return
                    time.sleep(2)
            finally:
                with self._lock:
                    self._watching.discard(user_id)

        threading.Thread(target=watch, name=f"rgh-login-{user_id}", daemon=True).start()


rgh_isolation = RghIsolationManager()
