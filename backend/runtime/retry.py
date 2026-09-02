"""Retry/deadline primitives shared by sync and async tool wrappers."""
from __future__ import annotations

import asyncio
import contextvars
import errno
import random
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def is_transient_error(error: BaseException) -> bool:
    """Classify common HTTP/MCP transport failures without hard dependencies."""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OSError) and getattr(error, "errno", None) in {
        errno.ECONNRESET, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH,
        errno.ENETUNREACH, errno.ENETRESET, errno.EPIPE, socket.EAI_AGAIN, socket.EAI_FAIL,
    }:
        return True
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    try:
        if int(status_code) in {408, 429, 502, 503, 504}:
            return True
    except (TypeError, ValueError):
        pass
    return type(error).__name__ in {
        "TimeoutException", "ConnectError", "ReadTimeout", "WriteTimeout",
        "ConnectTimeout", "RemoteProtocolError", "MCPConnectionError",
        "ServiceUnavailableError", "TooManyRequestsError",
    }


class RetryExhausted(Exception):
    def __init__(self, last_error: Exception, attempts: int) -> None:
        self.last_error = last_error
        self.attempts = attempts
        super().__init__(str(last_error))


def remaining(deadline_at: float | None) -> float | None:
    if deadline_at is None:
        return None
    return max(0.0, deadline_at - time.monotonic())


def backoff_seconds(initial: float, multiplier: float, maximum: float,
                    jitter_ratio: float, attempt: int,
                    rng: Callable[[], float] = random.random) -> float:
    base = min(maximum, initial * (multiplier ** max(0, attempt - 1)))
    if base <= 0 or jitter_ratio <= 0:
        return base
    # Symmetric jitter keeps the expected delay near the configured base.
    return max(0.0, base * (1.0 + (rng() * 2.0 - 1.0) * jitter_ratio))


def call_sync(fn: Callable[[], T], timeout: float | None) -> T:
    """Execute a sync function with a soft timeout using a daemon thread.

    Python cannot safely kill a running thread.  The process Worker supervisor
    supplies the hard timeout; this helper prevents the caller from waiting
    indefinitely in local mode and lets the daemon thread die with its process.
    """
    if timeout is None:
        return fn()
    result: list[tuple[bool, Any]] = []
    context = contextvars.copy_context()

    def run() -> None:
        try:
            result.append((True, context.run(fn)))
        except BaseException as exc:
            result.append((False, exc))

    worker = threading.Thread(target=run, name="gogo-tool-call", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        raise TimeoutError(f"tool call exceeded {timeout:.3f}s")
    if not result:
        raise RuntimeError("tool call returned without a result")
    ok, value = result[0]
    if ok:
        return value
    raise value


async def call_async(fn: Callable[[], Awaitable[T]], timeout: float | None) -> T:
    if timeout is None:
        return await fn()
    return await asyncio.wait_for(fn(), timeout=max(0.0, timeout))
