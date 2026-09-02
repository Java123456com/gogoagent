"""Agent tools for user-scoped external-provider credentials."""
from __future__ import annotations

from backend.infrastructure.security import mask_sensitive
from backend.services.api_key_service import PROVIDERS, api_key_service

from ._common import current_user_id, tool


def _check(provider: str, user_id: str | None) -> dict:
    resolved = current_user_id(user_id)
    value = api_key_service.get(resolved, provider)
    config = PROVIDERS[provider]
    return {
        "hasKey": value is not None,
        "configured": value is not None,
        "masked": _mask_key(value) if value else None,
        "guideUrl": config.guide_url,
    }


def _save(provider: str, api_key: str, user_id: str | None) -> dict:
    try:
        api_key_service.save(current_user_id(user_id), provider, api_key)
    except ValueError as exc:
        return {"success": False, "saved": False, "message": str(exc)}
    return {"success": True, "saved": True, "provider": provider}


def _mask_key(value: str) -> str:
    masked = mask_sensitive(value)
    if masked != value:
        return masked
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


@tool
def check_flight_api_key(user_id: str | None = None) -> dict:
    """检查当前用户是否配置 flight-manager API Key。"""
    return _check("flight-manager", user_id)


@tool
def save_flight_api_key(api_key: str, user_id: str | None = None) -> dict:
    """校验并加密保存当前用户的 flight-manager API Key。"""
    return _save("flight-manager", api_key, user_id)


@tool
def check_tuniu_api_key(user_id: str | None = None) -> dict:
    """检查当前用户是否配置 tuniu-cli API Key。"""
    return _check("tuniu-cli", user_id)


@tool
def save_tuniu_api_key(api_key: str, user_id: str | None = None) -> dict:
    """校验并加密保存当前用户的 tuniu-cli API Key。"""
    return _save("tuniu-cli", api_key, user_id)


@tool
def check_flyai_api_key(user_id: str | None = None) -> dict:
    """检查当前用户是否配置可选的 FlyAI/飞猪增强 API Key。"""
    return _check("flyai", user_id)


@tool
def save_flyai_api_key(api_key: str, user_id: str | None = None) -> dict:
    """校验并加密保存当前用户的 FlyAI/飞猪增强 API Key。"""
    return _save("flyai", api_key, user_id)


def tools():
    return [
        check_flight_api_key,
        save_flight_api_key,
        check_tuniu_api_key,
        save_tuniu_api_key,
        check_flyai_api_key,
        save_flyai_api_key,
    ]
