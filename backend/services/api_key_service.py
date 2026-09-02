"""User-scoped third-party credentials with DB + Redis encrypted storage."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.config import get_settings
from backend.infrastructure.repositories import api_key_repository
from backend.infrastructure.security import decrypt_api_key, encrypt_api_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    aliases: tuple[str, ...]
    guide_url: str
    key_prefixes: tuple[str, ...] = ()


PROVIDERS: dict[str, ProviderConfig] = {
    "tuniu-cli": ProviderConfig(
        "tuniu-cli", ("tuniu",), "https://open.tuniu.com/mcp/login", ("sk-",),
    ),
    "flight-manager": ProviderConfig(
        "flight-manager", ("flight",), "https://h5.133.cn/webapp/pages/mcpApiKey", ("sk_", "sk-"),
    ),
    "flyai": ProviderConfig("flyai", (), "https://open.fly.ai/", ("sk_", "sk-")),
}


class ApiKeyService:
    def __init__(self) -> None:
        self._redis: Any | None = None

    def _redis_client(self) -> Any | None:
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
            logger.warning("API Key Redis 初始化失败，降级到数据库: %s", exc)
            return None

    @staticmethod
    def _config(provider: str) -> ProviderConfig:
        for config in PROVIDERS.values():
            if provider == config.name or provider in config.aliases:
                return config
        return ProviderConfig(provider, (), "")

    @staticmethod
    def _cache_key(provider: str, user_id: str) -> str:
        return f"apikey:{provider}:{user_id}"

    def get(self, user_id: str, provider: str) -> str | None:
        config = self._config(provider)
        client = self._redis_client()
        encrypted = None
        if client is not None:
            try:
                encrypted = client.get(self._cache_key(config.name, user_id))
            except Exception as exc:
                logger.warning("API Key Redis 读取失败，降级到数据库: %s", exc)
        row = None
        if encrypted is None:
            row = api_key_repository.get(user_id, config.name)
            if row is None:
                for alias in config.aliases:
                    row = api_key_repository.get(user_id, alias)
                    if row is not None:
                        break
            encrypted = row.api_key_enc if row else None
            if encrypted and client is not None:
                try:
                    client.setex(
                        self._cache_key(config.name, user_id),
                        get_settings().api_key_cache_ttl_seconds,
                        encrypted,
                    )
                except Exception as exc:
                    logger.warning("API Key Redis 回填失败: %s", exc)
        if not encrypted:
            return None
        try:
            plaintext = decrypt_api_key(encrypted)
        except Exception:
            logger.warning("用户凭证无法解密，按未配置处理: userId=%s provider=%s", user_id, config.name)
            return None
        # Transparently migrate legacy provider names and XOR ciphertext.
        if row is not None and (row.provider != config.name or not encrypted.startswith("gcm:v1:")):
            self.save(user_id, config.name, plaintext, validate=False)
        return plaintext

    def save(self, user_id: str, provider: str, api_key: str, *, validate: bool = True) -> None:
        config = self._config(provider)
        value = str(api_key or "").strip()
        if not value:
            raise ValueError("API Key 不能为空")
        if validate and config.key_prefixes and not value.startswith(config.key_prefixes):
            raise ValueError(f"API Key 格式不正确，应以 {'/'.join(config.key_prefixes)} 开头")
        encrypted = encrypt_api_key(value)
        api_key_repository.save(user_id, config.name, encrypted)
        for alias in config.aliases:
            if alias != config.name:
                api_key_repository.delete(user_id, alias)
        client = self._redis_client()
        if client is not None:
            try:
                client.setex(
                    self._cache_key(config.name, user_id),
                    get_settings().api_key_cache_ttl_seconds,
                    encrypted,
                )
            except Exception as exc:
                logger.warning("API Key Redis 更新失败: %s", exc)

    def has(self, user_id: str, provider: str) -> bool:
        return self.get(user_id, provider) is not None

    def delete(self, user_id: str, provider: str) -> bool:
        config = self._config(provider)
        deleted = api_key_repository.delete(user_id, config.name)
        for alias in config.aliases:
            deleted = api_key_repository.delete(user_id, alias) or deleted
        client = self._redis_client()
        if client is not None:
            try:
                client.delete(self._cache_key(config.name, user_id))
            except Exception as exc:
                logger.warning("API Key Redis 删除失败: %s", exc)
        return deleted


api_key_service = ApiKeyService()
