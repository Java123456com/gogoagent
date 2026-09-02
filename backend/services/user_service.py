"""用户档案读写服务。"""
from __future__ import annotations

from typing import Any

from backend.infrastructure.repositories import user_repository


class UserService:
    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        profile = user_repository.get_profile(user_id)
        return profile.as_dict() if profile else None

    def update_profile(self, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        return user_repository.upsert_profile(user_id, values).as_dict()


user_service = UserService()
