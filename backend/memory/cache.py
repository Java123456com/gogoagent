"""Bounded local caches corresponding to the Java Caffeine session cache."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, TypeVar

T = TypeVar("T")


class ExpiringLRUCache(Generic[T]):
    """Maximum-size, expire-after-access cache for suspended Agent sessions."""

    def __init__(self, max_size: int = 1024, expire_after_seconds: float = 1800) -> None:
        self.max_size = max_size
        self.expire_after_seconds = expire_after_seconds
        self._values: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, key: str, value: T) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._values[key] = (time.monotonic() + self.expire_after_seconds, value)
            self._evict_expired()
            while len(self._values) > self.max_size:
                self._values.popitem(last=False)

    def get(self, key: str) -> T | None:
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                self._values.pop(key, None)
                return None
            self._values.move_to_end(key)
            self._values[key] = (time.monotonic() + self.expire_after_seconds, value)
            return value

    def remove(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for key, (expires_at, _) in list(self._values.items()):
            if expires_at <= now:
                self._values.pop(key, None)


class AgentSessionManager(Generic[T]):
    """Caffeine-compatible local cache used for Human-in-the-Loop resume."""

    def __init__(self, max_size: int = 1024, expire_after_seconds: float = 1800) -> None:
        self._cache = ExpiringLRUCache[T](max_size, expire_after_seconds)

    def register(self, session_id: str, session: T) -> None:
        self._cache.put(session_id, session)

    def get_by_session_id(self, session_id: str) -> T | None:
        return self._cache.get(session_id)

    def remove(self, session_id: str) -> None:
        self._cache.remove(session_id)


agent_session_manager = AgentSessionManager()
