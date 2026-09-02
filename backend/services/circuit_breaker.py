"""支持本地与 Redis 状态的工具级熔断器。

三态：关闭(closed) / 打开(open) / 半开(half-open)；连续失败达阈值进入冷却期，
冷却期按指数退避递增；也支持显式关闭整个工具组（如 TOOL_CIRCUIT_BREAKER_GROUP）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from backend.config import get_settings
from backend.infrastructure.stores import circuit_breaker_store

logger = logging.getLogger(__name__)

TOOL_CIRCUIT_BREAKER_GROUP = "TOOL_CIRCUIT_BREAKER_GROUP"


class ToolCircuitBreaker:
    def __init__(self, threshold: int | None = None, recovery_seconds: float | None = None) -> None:
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._probe_lock = threading.Lock()
        self._probe_inflight: set[str] = set()

    def call(self, name: str, fn: Callable[..., Any], *args, force: bool = False, **kwargs) -> Any:
        settings = get_settings()
        threshold = self.threshold or settings.circuit_breaker_failure_threshold
        recovery_seconds = self.recovery_seconds or settings.circuit_breaker_initial_cooldown_seconds
        custom = self.threshold is not None or self.recovery_seconds is not None
        if not settings.circuit_breaker_enabled and not custom and not force:
            return fn(*args, **kwargs)

        # Keep an expired custom cooldown in the open state so callers enter
        # the guarded half-open probe path instead of racing as fully closed.
        is_open = name in self._open_until if custom else circuit_breaker_store.is_open(name)
        monitored = force or custom or (
            name in settings.circuit_breaker_monitored_tools
            and name not in settings.circuit_breaker_excluded_tools
        )
        if not monitored:
            return fn(*args, **kwargs)
        cooldown_expired = (
            self._open_until.get(name, 0) <= time.time()
            if custom else circuit_breaker_store.is_cooldown_expired(name)
        )
        probe_token = None
        if monitored and is_open:
            if not cooldown_expired:
                raise RuntimeError(f"circuit open: 工具 {name} 已熔断（冷却中）")
            probe_token = self._acquire_probe(name, custom, recovery_seconds)
            if probe_token is None:
                raise RuntimeError(f"circuit open: 工具 {name} 半开探测进行中")

        try:
            result = fn(*args, **kwargs)
            if custom:
                self._failures.pop(name, None)
                self._open_until.pop(name, None)
            else:
                if is_open:
                    circuit_breaker_store.clear_open(name)
                circuit_breaker_store.reset_failure_count(name)
            return result
        except Exception:
            cooldown = 0
            if custom:
                self._failures[name] = self._failures.get(name, 0) + 1
                failures = self._failures[name]
            else:
                if is_open and cooldown_expired:
                    cooldown = circuit_breaker_store.open_with_next_generation(name)
                    failures = circuit_breaker_store.get_failure_count(name)
                else:
                    failures = circuit_breaker_store.increment_failure_count(name)
                    cooldown = 0
            if custom and failures >= threshold:
                cooldown = recovery_seconds
                self._open_until[name] = time.time() + cooldown
            elif not custom and not (is_open and cooldown_expired) and failures >= threshold:
                cooldown = circuit_breaker_store.open_with_next_generation(name)
            if cooldown:
                if custom:
                    self._open_until[name] = time.time() + cooldown
                logger.warning("[CIRCUIT_BREAKER] 工具 %s 连续失败 %d 次，冷却 %ss", name, failures, cooldown)
            raise
        finally:
            if probe_token is not None:
                self._release_probe(name, custom, probe_token)

    async def acall(self, name: str, fn: Callable[..., Any], *args, force: bool = False, **kwargs) -> Any:
        """Async equivalent used by coroutine tools; state semantics match ``call``."""
        settings = get_settings()
        threshold = self.threshold or settings.circuit_breaker_failure_threshold
        recovery_seconds = self.recovery_seconds or settings.circuit_breaker_initial_cooldown_seconds
        custom = self.threshold is not None or self.recovery_seconds is not None
        if not settings.circuit_breaker_enabled and not custom and not force:
            return await fn(*args, **kwargs)
        is_open = name in self._open_until if custom else circuit_breaker_store.is_open(name)
        monitored = force or custom or (
            name in settings.circuit_breaker_monitored_tools
            and name not in settings.circuit_breaker_excluded_tools
        )
        if not monitored:
            return await fn(*args, **kwargs)
        cooldown_expired = (
            self._open_until.get(name, 0) <= time.time()
            if custom else circuit_breaker_store.is_cooldown_expired(name)
        )
        probe_token = None
        if is_open:
            if not cooldown_expired:
                raise RuntimeError(f"circuit open: 工具 {name} 已熔断（冷却中）")
            probe_token = self._acquire_probe(name, custom, recovery_seconds)
            if probe_token is None:
                raise RuntimeError(f"circuit open: 工具 {name} 半开探测进行中")
        try:
            result = await fn(*args, **kwargs)
            if custom:
                self._failures.pop(name, None)
                self._open_until.pop(name, None)
            else:
                if is_open:
                    circuit_breaker_store.clear_open(name)
                circuit_breaker_store.reset_failure_count(name)
            return result
        except Exception:
            cooldown = 0
            if custom:
                self._failures[name] = self._failures.get(name, 0) + 1
                failures = self._failures[name]
            else:
                if is_open and cooldown_expired:
                    cooldown = circuit_breaker_store.open_with_next_generation(name)
                    failures = circuit_breaker_store.get_failure_count(name)
                else:
                    failures = circuit_breaker_store.increment_failure_count(name)
            if custom and failures >= threshold:
                cooldown = recovery_seconds
                self._open_until[name] = time.time() + cooldown
            elif not custom and not (is_open and cooldown_expired) and failures >= threshold:
                cooldown = circuit_breaker_store.open_with_next_generation(name)
            if cooldown and custom:
                self._open_until[name] = time.time() + cooldown
            raise
        finally:
            if probe_token is not None:
                self._release_probe(name, custom, probe_token)

    def _acquire_probe(self, name: str, custom: bool, ttl_seconds: float) -> str | None:
        if not custom:
            return circuit_breaker_store.try_acquire_probe(name, ttl_seconds)
        with self._probe_lock:
            if name in self._probe_inflight:
                return None
            self._probe_inflight.add(name)
            return name

    def _release_probe(self, name: str, custom: bool, token: str) -> None:
        if not custom:
            circuit_breaker_store.release_probe(name, token)
            return
        with self._probe_lock:
            self._probe_inflight.discard(name)


tool_circuit_breaker = ToolCircuitBreaker()
