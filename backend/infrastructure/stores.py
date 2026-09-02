"""用于规划结果、搜索候选、会话和熔断状态的 KV 存储。

生产可替换为真实 Redis；这里用线程安全的进程内 dict 保证零依赖可运行，
接口与 Redis 语义对齐（key 带用户隔离）。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from backend.config import get_settings


class _MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._redis = None
        redis_url = get_settings().redis_url
        if redis_url:
            try:
                import redis
                self._redis = redis.from_url(redis_url, decode_responses=True)
            except (ImportError, ValueError):
                self._redis = None

    def set(self, key: str, value: Any) -> None:
        if self._redis:
            try:
                self._redis.set(key, json.dumps(value, ensure_ascii=False, default=str))
                return
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            self._data[key] = value

    def get(self, key: str) -> Any:
        if self._redis:
            try:
                value = self._redis.get(key)
                return json.loads(value) if value is not None else None
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            return self._data.get(key)

    def delete(self, key: str) -> None:
        if self._redis:
            try:
                self._redis.delete(key)
                return
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            self._data.pop(key, None)

    def hash_set(self, key: str, field: str, value: Any) -> None:
        if self._redis:
            try:
                self._redis.hset(key, field, json.dumps(value, ensure_ascii=False, default=str))
                return
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            bucket = self._data.setdefault(key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                self._data[key] = bucket
            bucket[field] = value

    def hash_get(self, key: str, field: str) -> Any:
        if self._redis:
            try:
                value = self._redis.hget(key, field)
                return json.loads(value) if value is not None else None
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            bucket = self._data.get(key, {})
            return bucket.get(field) if isinstance(bucket, dict) else None

    def hash_all(self, key: str) -> dict[str, Any]:
        if self._redis:
            try:
                return {str(field): json.loads(value) for field, value in self._redis.hgetall(key).items()}
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            bucket = self._data.get(key, {})
            return dict(bucket) if isinstance(bucket, dict) else {}

    def expire(self, key: str, seconds: int) -> None:
        if self._redis:
            try:
                self._redis.expire(key, seconds)
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass


class ItineraryPlanStore(_MemoryStore):
    """行程规划结果（对应 ItineraryPlanStore，按 userId 隔离）。"""

    @staticmethod
    def key_of(user_id: str) -> str:
        safe = str(user_id or "default").strip()
        safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in safe)
        return f"planner:result:{safe or 'default'}"

    @staticmethod
    def trip_key(origin: str, destination: str, departure_date: str) -> str:
        return f"{origin}_{destination}_{departure_date}"

    def save(self, user_id: str, origin: str, destination: str, departure_date: str, result_json: str) -> None:
        key = self.key_of(user_id)
        self.hash_set(key, self.trip_key(origin, destination, departure_date), result_json)
        self.expire(key, 24 * 60 * 60)

    def load(self, user_id: str, origin: str, destination: str, departure_date: str) -> str | None:
        return self.hash_get(self.key_of(user_id), self.trip_key(origin, destination, departure_date))

    def latest(self, user_id: str) -> str | None:
        entries = self.hash_all(self.key_of(user_id))
        if not entries:
            return None
        return list(entries.values())[-1]


class SearchCandidateStore(_MemoryStore):
    """搜索候选（对应 SearchCandidateStore，供 plan_itinerary 读取）。"""

    @staticmethod
    def key_of(user_id: str) -> str:
        safe = str(user_id or "default").strip()
        safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in safe)
        return f"planner:search:{safe or 'default'}"

    @staticmethod
    def flight_field(origin: str, destination: str, departure_date: str) -> str:
        return f"flight_{origin}_{destination}_{departure_date}"

    @staticmethod
    def train_field(origin: str, destination: str, departure_date: str) -> str:
        return f"train_{origin}_{destination}_{departure_date}"

    @staticmethod
    def hotel_field(city: str, check_in: str, check_out: str) -> str:
        return f"hotel_{city}_{check_in}_{check_out}"

    def put(self, user_id: str, field: str, entry: dict[str, Any]) -> None:
        key = self.key_of(user_id)
        self.hash_set(key, field, entry)
        self.expire(key, 24 * 60 * 60)

    def get_entry(self, user_id: str, field: str) -> dict[str, Any] | None:
        value = self.hash_get(self.key_of(user_id), field)
        return value if isinstance(value, dict) else None

    def load_by_trip(self, user_id: str, origin: str, destination: str,
                     departure_date: str, return_date: str) -> dict[str, dict[str, Any]]:
        fields = (
            self.flight_field(origin, destination, departure_date),
            self.train_field(origin, destination, departure_date),
            self.flight_field(destination, origin, return_date),
            self.train_field(destination, origin, return_date),
            self.hotel_field(destination, departure_date, return_date),
        )
        return {
            field: value
            for field in fields
            if isinstance((value := self.hash_get(self.key_of(user_id), field)), dict)
        }

    @staticmethod
    def trip_key(origin: str | None, destination: str | None,
                 departure_date: str | None, return_date: str | None) -> str:
        return "_".join(str(value or "") for value in (origin, destination, departure_date, return_date))

    def save(self, user_id: str, candidates_json: str, origin: str | None = None,
             destination: str | None = None, departure_date: str | None = None,
             return_date: str | None = None) -> None:
        if any(value is not None for value in (origin, destination, departure_date, return_date)):
            key = f"__legacy_trip__:{self.trip_key(origin, destination, departure_date, return_date)}"
            self.hash_set(self.key_of(user_id), key, candidates_json)
            self.expire(self.key_of(user_id), 24 * 60 * 60)
        else:
            self.hash_set(self.key_of(user_id), "__legacy_latest__", candidates_json)
            self.expire(self.key_of(user_id), 24 * 60 * 60)

    def load(self, user_id: str, origin: str | None = None, destination: str | None = None,
             departure_date: str | None = None, return_date: str | None = None) -> str | None:
        if any(value is not None for value in (origin, destination, departure_date, return_date)):
            precise = self.hash_get(
                self.key_of(user_id),
                f"__legacy_trip__:{self.trip_key(origin, destination, departure_date, return_date)}",
            )
            if precise is not None:
                return precise
        direct = self.hash_get(self.key_of(user_id), "__legacy_latest__")
        if direct is not None:
            return direct
        entries = self.hash_all(self.key_of(user_id))
        legacy = [value for field, value in entries.items() if field.startswith("__legacy_")]
        return legacy[-1] if legacy else None


class SessionStore(_MemoryStore):
    """会话态（对应 Redis 的 agentscope_session / activeAgent / pendingTool）。"""


class SessionExecutionFence(_MemoryStore):
    """Monotonic per-session request generation shared by clustered nodes.

    An old model/tool call may return after a newer request has superseded it.
    The generation lets the caller discard that stale result before it writes a
    durable checkpoint. Redis ``INCR`` makes this atomic across application
    nodes; the local dictionary preserves deterministic development tests.
    """

    _prefix = "agent:session-fence:"

    def __init__(self) -> None:
        super().__init__()
        self._generations: dict[str, int] = {}
        self._generation_lock = threading.RLock()

    @classmethod
    def _key(cls, session_id: str) -> str:
        safe = str(session_id or "").strip()
        return cls._prefix + safe

    def begin(self, session_id: str) -> int:
        key = self._key(session_id)
        if self._redis:
            try:
                generation = int(self._redis.incr(key))
                self._redis.expire(key, max(60, int(get_settings().session_fence_ttl_seconds)))
                return generation
            except Exception:  # noqa: S110 - development fallback
                pass
        with self._generation_lock:
            generation = self._generations.get(key, 0) + 1
            self._generations[key] = generation
            return generation

    def current(self, session_id: str) -> int:
        key = self._key(session_id)
        if self._redis:
            try:
                return int(self._redis.get(key) or 0)
            except Exception:  # noqa: S110 - development fallback
                pass
        with self._generation_lock:
            return self._generations.get(key, 0)

    def is_current(self, session_id: str, generation: int) -> bool:
        return generation > 0 and self.current(session_id) == generation


class ReviewResultStore:
    """上一轮审核报告的会话级存储。"""

    _suffix = ":review_result"

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def save(self, session_id: str, report: dict | str) -> None:
        self.store.set(f"{session_id}{self._suffix}", report)

    def load(self, session_id: str) -> dict | str | None:
        return self.store.get(f"{session_id}{self._suffix}")

    def clear(self, session_id: str) -> None:
        self.store.delete(f"{session_id}{self._suffix}")


class CircuitBreakerStore(_MemoryStore):
    """工具级熔断（对应 ToolCircuitBreakerHook 的 Redis 共享态）。"""

    def __init__(self) -> None:
        super().__init__()
        self._failures: dict[str, int] = {}
        self._generation: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._probes: dict[str, tuple[str, float]] = {}
        self._open_groups: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _fail_key(tool_name: str) -> str:
        return f"{get_settings().circuit_breaker_redis_key_prefix}fail:{tool_name}"

    @staticmethod
    def _gen_key(tool_name: str) -> str:
        return f"{get_settings().circuit_breaker_redis_key_prefix}gen:{tool_name}"

    def increment_failure_count(self, tool_name: str) -> int:
        if self._redis:
            try:
                value = int(self._redis.incr(self._fail_key(tool_name)))
                self._redis.expire(
                    self._fail_key(tool_name),
                    get_settings().circuit_breaker_state_ttl_seconds,
                )
                return value
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            self._failures[tool_name] = self._failures.get(tool_name, 0) + 1
            return self._failures[tool_name]

    def get_failure_count(self, tool_name: str) -> int:
        if self._redis:
            try:
                return int(self._redis.get(self._fail_key(tool_name)) or 0)
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            return self._failures.get(tool_name, 0)

    def record_failure(self, tool_name: str) -> None:
        self.increment_failure_count(tool_name)

    def reset_failure_count(self, tool_name: str) -> None:
        if self._redis:
            try:
                self._redis.delete(self._fail_key(tool_name))
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            self._failures.pop(tool_name, None)

    def open_with_next_generation(self, tool_name: str) -> float:
        settings = get_settings()
        now_ms = int(time.time() * 1000)
        generation = None
        if self._redis:
            try:
                pipeline = self._redis.pipeline(transaction=True)
                pipeline.hincrby(self._gen_key(tool_name), "n", 1)
                pipeline.hset(self._gen_key(tool_name), "at", now_ms)
                pipeline.expire(
                    self._gen_key(tool_name), settings.circuit_breaker_state_ttl_seconds,
                )
                generation = int(pipeline.execute()[0])
            except Exception:
                generation = None
        with self._lock:
            if generation is None:
                generation = self._generation.get(tool_name, 0) + 1
            self._generation[tool_name] = generation
            self._opened_at[tool_name] = now_ms
        return self._compute_cooldown(generation)

    def is_open(self, tool_name: str) -> bool:
        if self._redis:
            try:
                return bool(self._redis.hexists(self._gen_key(tool_name), "at"))
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            return tool_name in self._opened_at

    def clear_open(self, tool_name: str) -> None:
        if self._redis:
            try:
                self._redis.delete(self._gen_key(tool_name))
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            self._generation.pop(tool_name, None)
            self._opened_at.pop(tool_name, None)

    def get_generation(self, tool_name: str) -> int:
        if self._redis:
            try:
                return int(self._redis.hget(self._gen_key(tool_name), "n") or 0)
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            return self._generation.get(tool_name, 0)

    def get_cooldown_seconds(self, tool_name: str) -> float:
        return self._compute_cooldown(self.get_generation(tool_name))

    def is_cooldown_expired(self, tool_name: str) -> bool:
        opened_at = None
        if self._redis:
            try:
                value = self._redis.hget(self._gen_key(tool_name), "at")
                opened_at = float(value) if value is not None else None
            except Exception:
                opened_at = None
        if opened_at is None:
            with self._lock:
                opened_at = self._opened_at.get(tool_name)
        if opened_at is None:
            return True
        return time.time() * 1000 >= opened_at + self.get_cooldown_seconds(tool_name) * 1000

    def try_acquire_probe(self, tool_name: str, ttl_seconds: float) -> str | None:
        """Acquire the single half-open probe slot for a tool.

        Redis uses ``SET NX`` so the slot is shared across API/Worker
        processes.  The in-memory path mirrors the same lease semantics for
        local development and tests.
        """
        token = f"{time.time_ns()}"
        ttl = max(1, int(ttl_seconds))
        key = f"{get_settings().circuit_breaker_redis_key_prefix}probe:{tool_name}"
        if self._redis:
            try:
                return token if self._redis.set(key, token, nx=True, ex=ttl) else None
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        now = time.time()
        with self._lock:
            current = self._probes.get(tool_name)
            if current is not None and current[1] > now:
                return None
            self._probes[tool_name] = (token, now + ttl)
            return token

    def release_probe(self, tool_name: str, token: str | None = None) -> None:
        key = f"{get_settings().circuit_breaker_redis_key_prefix}probe:{tool_name}"
        if self._redis:
            try:
                if token is None:
                    self._redis.delete(key)
                elif hasattr(self._redis, "eval"):
                    self._redis.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then "
                        "return redis.call('del', KEYS[1]) else return 0 end",
                        1, key, token,
                    )
                elif self._redis.get(key) == token:
                    self._redis.delete(key)
            except Exception:  # noqa: S110 - Redis outage falls back locally
                pass
        with self._lock:
            current = self._probes.get(tool_name)
            if token is None or current is None or current[0] == token:
                self._probes.pop(tool_name, None)

    @staticmethod
    def _compute_cooldown(generation: int) -> float:
        if generation <= 0:
            return 0
        settings = get_settings()
        raw = settings.circuit_breaker_initial_cooldown_seconds * (
            settings.circuit_breaker_backoff_multiplier ** max(0, generation - 1)
        )
        return min(raw, settings.circuit_breaker_max_cooldown_seconds)

    def reset(self, tool_name: str) -> None:
        self.reset_failure_count(tool_name)
        self.clear_open(tool_name)

    def cooldown_until(self, tool_name: str, seconds: float) -> None:
        """Compatibility shim for older callers; new code uses generation state."""
        with self._lock:
            self._generation[tool_name] = 1
            self._opened_at[tool_name] = (time.time() + seconds - self._compute_cooldown(1)) * 1000

    def mark_group_closed(self, group: str) -> None:
        with self._lock:
            self._open_groups.add(group)

    def is_group_closed(self, group: str) -> bool:
        with self._lock:
            return group in self._open_groups


# 进程级单例
itinerary_plan_store = ItineraryPlanStore()
search_candidate_store = SearchCandidateStore()
session_store = SessionStore()
session_execution_fence = SessionExecutionFence()
review_result_store = ReviewResultStore(session_store)
circuit_breaker_store = CircuitBreakerStore()
