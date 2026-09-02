"""User-scoped long-term memory with a durable local backend and cache."""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from backend.config import get_settings
from backend.infrastructure.repositories import agent_memory_repository
from backend.memory.providers import BailianLongTermMemoryProvider, LongTermMemoryProvider


class RedisPreferenceCache:
    """Optional Redis cache; construction falls back cleanly when Redis is unavailable."""

    def __init__(self, url: str | None = None, ttl_seconds: int = 1800, client=None) -> None:
        self.ttl_seconds = ttl_seconds
        self.client = client
        if self.client is None and url:
            try:
                import redis
                self.client = redis.from_url(url, decode_responses=True)
            except (ImportError, ValueError):
                self.client = None

    def _key(self, user_id: str) -> str:
        return f"ltm:travel-pref:{user_id}"

    def get(self, user_id: str) -> str | None:
        return self.client.get(self._key(user_id)) if self.client else None

    def put(self, user_id: str, value: str) -> None:
        if self.client and value:
            self.client.set(self._key(user_id), value, ex=self.ttl_seconds)

    def evict(self, user_id: str) -> None:
        if self.client:
            self.client.delete(self._key(user_id))


class LongTermMemory:
    def __init__(self, durable: bool = True, cache_ttl_seconds: int = 1800,
                 cache_backend: RedisPreferenceCache | None = None,
                 provider: LongTermMemoryProvider | None = None,
                 fallback_to_local: bool = True) -> None:
        self.durable = durable
        self.cache_ttl_seconds = cache_ttl_seconds
        self._entries: dict[str, list[dict[str, Any]]] = {}
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self.cache_backend = cache_backend
        self.provider = provider
        self.fallback_to_local = fallback_to_local
        self._lock = threading.RLock()

    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "provider_name", "local") if self.provider else "local"

    def record(self, user_id: str, content: str, memory_type: str = "preference",
               metadata: dict[str, Any] | None = None) -> str | None:
        if not user_id or not content or not content.strip():
            return None
        content = content.strip()
        memory_id = None
        provider_succeeded = False
        if self.provider:
            try:
                memory_id = self.provider.record(user_id, content, memory_type, metadata)
                provider_succeeded = True
            except Exception:
                if not self.fallback_to_local:
                    raise
        if self.durable:
            # A configured remote provider is authoritative.  Store locally
            # only when it is disabled or the provider explicitly falls back.
            if not self.provider or not provider_succeeded:
                try:
                    memory_id = agent_memory_repository.record_memory(user_id, content, memory_type, metadata)
                except SQLAlchemyError:
                    memory_id = None
        with self._lock:
            self._entries.setdefault(user_id, []).append({"content": content, "type": memory_type})
            self.evict(user_id)
        if self.cache_backend:
            self.cache_backend.evict(user_id)
        return memory_id

    def retrieve(self, user_id: str, limit: int = 10, query: str | None = None) -> list[str]:
        if not user_id:
            return []
        now = time.monotonic()
        cache_key = f"{user_id}:{query or ''}:{limit}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return list(cached[1])
        values: list[str] = []
        provider_succeeded = False
        if self.cache_backend and not query:
            cached_text = self.cache_backend.get(user_id)
            if cached_text:
                values = [line for line in cached_text.split("\n") if line]
        if self.provider and not values:
            try:
                values = self.provider.retrieve(user_id, limit, query)
                provider_succeeded = True
            except Exception:
                if not self.fallback_to_local:
                    raise
        if self.durable and not values and (not self.provider or not provider_succeeded):
            try:
                values = [row.content for row in agent_memory_repository.retrieve_memories(user_id, limit, query)]
            except SQLAlchemyError:
                values = []
            if values and self.cache_backend and not query:
                self.cache_backend.put(user_id, "\n".join(values))
        if not values:
            with self._lock:
                entries = self._entries.get(user_id, [])
                values = [entry["content"] for entry in entries[-limit:]]
            if query:
                terms = {term for term in query.split() if term}
                matched = [item for item in values if any(term in item for term in terms)]
                values = matched or values
        with self._lock:
            self._cache[cache_key] = (now + self.cache_ttl_seconds, list(values))
        return values

    def evict(self, user_id: str) -> None:
        for key in [key for key in self._cache if key.startswith(f"{user_id}:")]:
            self._cache.pop(key, None)


_settings = get_settings()
_bailian_provider = None
if (_settings.bailian_memory_enabled
        and (_settings.dashscope_api_key or _settings.openai_api_key)):
    _bailian_provider = BailianLongTermMemoryProvider(
        endpoint=_settings.bailian_memory_endpoint or "https://dashscope.aliyuncs.com",
        api_key=_settings.dashscope_api_key or _settings.openai_api_key,
        access_key_id=_settings.bailian_access_key_id,
        access_key_secret=_settings.bailian_access_key_secret,
        workspace_id=_settings.bailian_workspace_id,
        memory_library_id=_settings.bailian_memory_library_id,
        project_id=_settings.bailian_project_id,
        profile_schema=_settings.bailian_profile_schema,
        timeout=_settings.external_request_timeout_seconds,
    )
long_term_memory = LongTermMemory(
    cache_ttl_seconds=_settings.memory_cache_ttl_seconds,
    cache_backend=RedisPreferenceCache(_settings.redis_url, _settings.memory_cache_ttl_seconds),
    provider=_bailian_provider,
)


class TravelPreferenceMemoryCache:
    """Request/session-level cache for repeated long-term preference retrieval."""

    def __init__(self, memory: LongTermMemory | None = None) -> None:
        self.memory = memory or long_term_memory
        self._cache: dict[str, list[str]] = {}

    def get_or_retrieve(self, user_id: str, query: str | None = None) -> list[str]:
        key = f"{user_id}:{query or ''}"
        if key not in self._cache:
            self._cache[key] = self.memory.retrieve(user_id, query=query)
        return list(self._cache[key])

    def evict(self, user_id: str) -> None:
        for key in [key for key in self._cache if key.startswith(f"{user_id}:")]:
            self._cache.pop(key, None)
        self.memory.evict(user_id)
