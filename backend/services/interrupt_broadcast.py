"""Redis Pub/Sub control plane for cross-node Agent interruption.

The database remains the durable source of session/checkpoint state.  Redis
Pub/Sub is deliberately used only for the low-latency "stop this running
session" signal: every API node subscribes and cancels only handles it owns.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from backend.config import get_settings
from backend.memory.cache import agent_session_manager
from backend.memory.execution import execution_registry
from backend.memory.session import agent_session_store
from backend.services.runtime_events import session_event_registry

logger = logging.getLogger(__name__)


class RedisInterruptBroadcast:
    """Cluster-safe Agent interruption broadcaster.

    ``local_interrupt`` is injectable for focused tests.  In production the
    default handler cancels local execution handles, clears local suspended
    state and informs only SSE clients attached to this application instance.
    """

    def __init__(self, *, redis_client_factory: Callable[[str], Any] | None = None,
                 local_interrupt: Callable[[str, dict[str, Any]], bool] | None = None,
                 settings=None) -> None:
        self._settings = settings
        self._redis_client_factory = redis_client_factory
        self._local_interrupt = local_interrupt or self._interrupt_local
        self._redis: Any | None = None
        self._pubsub: Any | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._started = threading.Event()
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stopped.is_set())

    def start(self) -> bool:
        """Start the listener once per API process.

        Local development with no Redis stays functional.  Cluster mode is
        intentionally strict: advertising cross-node cancellation without a
        shared Redis broker would be misleading, so startup fails.
        """
        with self._lock:
            if self.running:
                return True
            settings = self._get_settings()
            if not settings.redis_url:
                if settings.cluster_mode:
                    raise RuntimeError("CLUSTER_MODE=true 时必须配置 REDIS_URL")
                return False
            self._stopped.clear()
            self._started.clear()
            self._redis = self._create_client(settings.redis_url)
            self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            self._pubsub.subscribe(settings.interrupt_broadcast_channel)
            self._thread = threading.Thread(
                target=self._listen,
                name="gogo-redis-interrupt-listener",
                daemon=True,
            )
            self._thread.start()
        self._started.wait(timeout=max(0.1, float(settings.interrupt_broadcast_poll_seconds) * 4))
        return True

    def stop(self) -> None:
        with self._lock:
            self._stopped.set()
            pubsub, thread = self._pubsub, self._thread
            self._pubsub = None
            self._thread = None
        if pubsub is not None:
            try:
                pubsub.unsubscribe()
                pubsub.close()
            except Exception:
                logger.debug("关闭 Redis 中断订阅失败", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def broadcast(self, session_id: str, *, generation: int | None = None,
                  request_id: str | None = None, reason: str = "user_interrupt") -> bool:
        if not session_id:
            return False
        settings = self._get_settings()
        if not settings.redis_url:
            if settings.cluster_mode:
                raise RuntimeError("集群中断广播需要 REDIS_URL")
            return False
        redis_client = self._redis or self._create_client(settings.redis_url)
        payload = {
            "session_id": session_id,
            "generation": generation,
            "request_id": request_id,
            "reason": reason,
            "source_instance_id": self.instance_id,
            "timestamp": time.time(),
        }
        try:
            redis_client.publish(settings.interrupt_broadcast_channel, json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            if settings.cluster_mode:
                raise
            logger.warning("Redis 中断广播发布失败，已降级为本地中断", exc_info=True)
            return False

    def interrupt_and_broadcast(self, session_id: str, *, generation: int | None = None,
                                request_id: str | None = None,
                                reason: str = "user_interrupt") -> bool:
        """Perform idempotent local cleanup, then notify other application nodes."""
        local = self._local_interrupt(session_id, {
            "session_id": session_id,
            "generation": generation,
            "request_id": request_id,
            "reason": reason,
            "source_instance_id": self.instance_id,
        })
        published = self.broadcast(session_id, generation=generation, request_id=request_id, reason=reason)
        return local or published

    @property
    def instance_id(self) -> str:
        configured = self._get_settings().app_instance_id
        return configured.strip() if configured and configured.strip() else f"{socket.gethostname()}-{os.getpid()}"

    def handle_payload(self, payload: dict[str, Any] | str | bytes) -> bool:
        """Handle one Pub/Sub payload; public to make protocol tests deterministic."""
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                return False
        if not isinstance(payload, dict):
            return False
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return False
        # The publisher already performed local cleanup.  Skip its Pub/Sub
        # loopback so a newly registered SSE stream cannot receive a stale
        # duplicate interruption notification.
        if payload.get("source_instance_id") == self.instance_id:
            return False
        return self._local_interrupt(session_id, payload)

    def _listen(self) -> None:
        self._started.set()
        settings = self._get_settings()
        poll = max(0.05, float(settings.interrupt_broadcast_poll_seconds))
        while not self._stopped.is_set():
            pubsub = self._pubsub
            if pubsub is None:
                return
            try:
                message = pubsub.get_message(timeout=poll)
            except Exception:
                logger.warning("Redis 中断广播监听失败", exc_info=True)
                if not self._stopped.is_set() and settings.cluster_mode:
                    # Do not crash the API process from a listener thread. The
                    # health endpoint exposes listener state; a failed Redis
                    # control plane is still observable and restartable.
                    self._stopped.set()
                return
            if not message or message.get("type") != "message":
                continue
            self.handle_payload(message.get("data"))

    def _interrupt_local(self, session_id: str, payload: dict[str, Any]) -> bool:
        raw_generation = payload.get("generation")
        try:
            generation = int(raw_generation) if raw_generation is not None else None
        except (TypeError, ValueError):
            generation = None
        interrupted = execution_registry.interrupt(session_id, generation=generation)
        # Cleanup operations are intentionally idempotent
        # because every subscribed node receives the same broadcast.
        agent_session_manager.remove(session_id)
        agent_session_store.clear_pending_tool(session_id)
        if interrupted:
            session_event_registry.emit(session_id, "interrupted", "已停止生成")
        return interrupted

    def _get_settings(self):
        return self._settings or get_settings()

    def _create_client(self, url: str):
        if self._redis_client_factory is not None:
            return self._redis_client_factory(url)
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - guarded by dependency declaration
            raise RuntimeError("Redis 中断广播需要安装 redis 依赖") from exc
        return redis.from_url(url, decode_responses=True)


interrupt_broadcast = RedisInterruptBroadcast()
