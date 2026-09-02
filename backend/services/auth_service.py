"""基于不透明 Token 与 Redis 兼容会话存储的鉴权服务。

演示环境口令按 schema.sql 明文存储；生产应替换为 BCrypt。
"""
from __future__ import annotations

import time

from backend.config import get_settings
from backend.infrastructure.repositories import user_repository
from backend.infrastructure.security import new_token
from backend.infrastructure.stores import session_store


class AuthService:
    def login(self, username: str, password: str) -> dict:
        user = user_repository.find_by_username(username)
        # 兼容明文种子账号与未来 BCrypt 哈希
        if user is None or (user.password != password):
            raise ValueError("用户名或密码错误")
        token = new_token()
        expires_at = time.time() + get_settings().token_timeout_seconds
        session_store.set(f"auth:session:{token}", {"user_id": user.user_id, "expires_at": expires_at})
        return {"token": token, "tokenName": "Authorization"}

    def current_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        token = token.removeprefix("Bearer ").strip()
        session = session_store.get(f"auth:session:{token}")
        if not session or session.get("expires_at", 0) < time.time():
            return None
        account = user_repository.find_by_id(session["user_id"])
        if account is None:
            return None
        return {"user_id": account.user_id, "username": account.username,
                "admin": account.role == "ADMIN"}

    def logout(self, token: str) -> None:
        session_store.delete(f"auth:session:{token.removeprefix('Bearer ').strip()}")


auth_service = AuthService()
