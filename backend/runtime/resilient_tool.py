"""Unified timeout/retry/backoff/idempotency wrapper for Agent tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from backend.core.request_context import current_context
from backend.infrastructure.repositories import tool_execution_repository
from backend.runtime.retry import (
    backoff_seconds,
    call_async,
    call_sync,
    is_transient_error,
    remaining,
)
from backend.runtime.tool_policy import ToolEffect, ToolPolicy, tool_policy_registry
from backend.services.circuit_breaker import tool_circuit_breaker
from backend.services.runtime_events import (
    emit_tool_circuit_open,
    emit_tool_retry,
    emit_tool_timeout,
)


class ToolDeadlineExceeded(TimeoutError):
    """The tool or its Agent request exceeded its deadline."""


class ToolUnknownOutcome(RuntimeError):
    """A non-idempotent operation may have completed before timing out."""


class ToolIdempotencyConflict(RuntimeError):
    """Another Worker currently owns the same idempotent write key."""


class RetryableToolResult(RuntimeError):
    """A tool returned a structured temporary failure instead of raising."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.retry_after = result.get("retry_after", result.get("Retry-After"))
        super().__init__(str(result.get("error") or "temporary tool failure"))


class ResilientToolHook:
    def __init__(self, agent_name: str, registry=tool_policy_registry) -> None:
        self.agent_name = agent_name
        self.registry = registry

    def wrap_tools(self, tools: list[Any]) -> list[Any]:
        try:
            from langchain_core.tools import StructuredTool
        except ImportError:  # pragma: no cover
            return tools
        wrapped: list[Any] = []
        for original in tools:
            if not isinstance(original, StructuredTool) or (original.func is None and original.coroutine is None):
                wrapped.append(original)
                continue
            if original.func is not None:
                def sync_wrapper(*args, _func=original.func, _name=original.name, **kwargs):
                    return self.execute(_name, _func, args, kwargs)
            else:
                sync_wrapper = None
            if original.coroutine is not None:
                async def async_wrapper(*args, _coroutine=original.coroutine,
                                        _name=original.name, **kwargs):
                    return await self.aexecute(_name, _coroutine, args, kwargs)
            else:
                async_wrapper = None
            wrapped.append(StructuredTool.from_function(
                func=sync_wrapper, coroutine=async_wrapper, name=original.name,
                description=original.description, args_schema=original.args_schema,
                return_direct=original.return_direct,
            ))
        return wrapped

    def invoke(self, tool_obj: Any, payload: dict[str, Any]) -> Any:
        """Invoke a tool from deterministic/fallback code through this policy."""
        name = str(getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", "tool"))
        invoke = getattr(tool_obj, "invoke", None)
        if callable(invoke):
            return self.execute(name, lambda: invoke(payload), (), {})
        if callable(tool_obj):
            return self.execute(name, lambda: tool_obj(**payload), (), {})
        raise TypeError(f"Tool is not callable: {tool_obj!r}")

    def execute(self, name: str, fn, args: tuple, kwargs: dict) -> Any:
        policy = self.registry.policy(name)
        if not self._enabled(policy):
            return fn(*args, **kwargs)
        call_id = self._call_id()
        idem_key, _arg_hash, owner = self._idempotency(name, args, kwargs, policy)
        cached = self._load_cached(idem_key)
        if cached is not None:
            return cached
        if idem_key and not owner:
            raise ToolIdempotencyConflict(f"工具 {name} 的相同幂等请求正在执行或已失败")
        try:
            result = tool_circuit_breaker.call(
                name,
                lambda: self._attempts(name, policy, fn, args, kwargs, call_id),
                force=policy.circuit_breaker,
            )
        except RetryableToolResult as exc:
            # Preserve the tool's existing structured fallback after the
            # retry budget is exhausted while allowing the circuit to count
            # the logical call as a failure.
            return exc.result
        except RuntimeError as exc:
            if str(exc).startswith("circuit open:"):
                emit_tool_circuit_open(self.agent_name, name, call_id)
            if idem_key:
                tool_execution_repository.fail(idem_key, exc)
            raise
        except Exception as exc:
            if idem_key:
                tool_execution_repository.fail(idem_key, exc)
            if isinstance(exc, (ToolDeadlineExceeded, TimeoutError)) \
                    and policy.effect is ToolEffect.NON_IDEMPOTENT_WRITE:
                raise ToolUnknownOutcome(
                    f"工具 {name} 超时，无法确认非幂等操作是否已执行",
                ) from exc
            raise
        if idem_key:
            tool_execution_repository.complete(idem_key, result)
        return result

    async def aexecute(self, name: str, fn, args: tuple, kwargs: dict) -> Any:
        policy = self.registry.policy(name)
        if not self._enabled(policy):
            return await fn(*args, **kwargs)
        call_id = self._call_id()
        idem_key, _, owner = self._idempotency(name, args, kwargs, policy)
        cached = self._load_cached(idem_key)
        if cached is not None:
            return cached
        if idem_key and not owner:
            raise ToolIdempotencyConflict(f"工具 {name} 的相同幂等请求正在执行或已失败")
        try:
            result = await tool_circuit_breaker.acall(
                name,
                lambda: self._aattempts(name, policy, fn, args, kwargs, call_id),
                force=policy.circuit_breaker,
            )
        except RetryableToolResult as exc:
            return exc.result
        except RuntimeError as exc:
            if str(exc).startswith("circuit open:"):
                emit_tool_circuit_open(self.agent_name, name, call_id)
            if idem_key:
                tool_execution_repository.fail(idem_key, exc)
            raise
        except Exception as exc:
            if idem_key:
                tool_execution_repository.fail(idem_key, exc)
            if isinstance(exc, (ToolDeadlineExceeded, TimeoutError)) \
                    and policy.effect is ToolEffect.NON_IDEMPOTENT_WRITE:
                raise ToolUnknownOutcome(
                    f"工具 {name} 超时，无法确认非幂等操作是否已执行",
                ) from exc
            raise
        if idem_key:
            tool_execution_repository.complete(idem_key, result)
        return result

    @staticmethod
    def _enabled(policy: ToolPolicy) -> bool:
        from backend.config import get_settings
        return bool(get_settings().tool_policy_enabled)

    @staticmethod
    def _call_id() -> str:
        context = current_context()
        return str(context.tool_call_id if context and context.tool_call_id else f"tool_{uuid4().hex}")

    @staticmethod
    def _idempotency(name: str, args: tuple, kwargs: dict,
                     policy: ToolPolicy) -> tuple[str | None, str, bool]:
        payload = json.dumps({"args": args, "kwargs": kwargs}, ensure_ascii=False,
                             sort_keys=True, default=str, separators=(",", ":"))
        arg_hash = hashlib.sha256(payload.encode()).hexdigest()
        context = current_context()
        if policy.effect is not ToolEffect.IDEMPOTENT_WRITE or context is None:
            return None, arg_hash, True
        request_id = context.request_id or context.session_id or "local"
        call_suffix = f":{context.tool_call_id}" if context.tool_call_id else ""
        key = context.idempotency_key or f"{request_id}:{context.agent_name or 'agent'}:{name}{call_suffix}:{arg_hash}"
        existing = tool_execution_repository.get(key)
        if existing is not None:
            return key, arg_hash, False
        row = tool_execution_repository.create_in_progress(
            idempotency_key=key, request_id=context.request_id, user_id=context.user_id,
            agent_name=context.agent_name, tool_name=name, arguments_hash=arg_hash,
            expires_at=datetime.now() + timedelta(hours=24),
        )
        # A duplicate create means another worker won the database race.
        return key, arg_hash, row is not None

    @staticmethod
    def _load_cached(key: str | None) -> Any:
        if not key:
            return None
        row = tool_execution_repository.get(key)
        if row is not None and row.status == "SUCCEEDED":
            return tool_execution_repository.decode_result(row)
        if row is not None and row.status == "IN_PROGRESS":
            # The owner may be this call (create_in_progress is intentionally
            # idempotent); execution proceeds.  A concurrent duplicate is
            # guarded by the database unique key and should be reconciled by
            # the domain service if both calls race.
            return None
        return None

    def _attempts(self, name: str, policy: ToolPolicy, fn, args, kwargs, call_id: str) -> Any:
        last: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            timeout = self._timeout(policy)
            if timeout is not None and timeout <= 0:
                raise ToolDeadlineExceeded(f"工具 {name} 已超过请求截止时间")
            try:
                result = call_sync(lambda: fn(*args, **kwargs), timeout)
                if self._retryable_result(result):
                    raise RetryableToolResult(result)
                return result
            except TimeoutError as exc:
                last = ToolDeadlineExceeded(str(exc))
                emit_tool_timeout(self.agent_name, name, call_id, attempt, timeout)
            except Exception as exc:
                last = exc
            if attempt >= policy.max_attempts or not self._retryable(policy, last):
                break
            delay = self._retry_delay(policy, last, attempt)
            rem = remaining(self._deadline())
            if rem is not None and rem <= delay:
                break
            emit_tool_retry(self.agent_name, name, call_id, attempt, delay, type(last).__name__)
            time.sleep(delay)
        assert last is not None
        raise last

    async def _aattempts(self, name: str, policy: ToolPolicy, fn, args, kwargs, call_id: str) -> Any:
        last: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            timeout = self._timeout(policy)
            if timeout is not None and timeout <= 0:
                raise ToolDeadlineExceeded(f"工具 {name} 已超过请求截止时间")
            try:
                result = await call_async(lambda: fn(*args, **kwargs), timeout)
                if self._retryable_result(result):
                    raise RetryableToolResult(result)
                return result
            except TimeoutError as exc:
                last = ToolDeadlineExceeded(str(exc))
                emit_tool_timeout(self.agent_name, name, call_id, attempt, timeout)
            except Exception as exc:
                last = exc
            if attempt >= policy.max_attempts or not self._retryable(policy, last):
                break
            delay = self._retry_delay(policy, last, attempt)
            rem = remaining(self._deadline())
            if rem is not None and rem <= delay:
                break
            emit_tool_retry(self.agent_name, name, call_id, attempt, delay, type(last).__name__)
            await asyncio.sleep(delay)
        assert last is not None
        raise last

    @staticmethod
    def _retryable(policy: ToolPolicy, error: Exception | None) -> bool:
        if error is None or not policy.retryable:
            return False
        if isinstance(error, RetryableToolResult):
            return True
        if policy.retry_exceptions:
            return isinstance(error, policy.retry_exceptions)
        return is_transient_error(error)

    @staticmethod
    def _retryable_result(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("retryable") is True:
            return True
        try:
            if int(result.get("status_code", 0)) in {408, 429, 502, 503, 504}:
                return True
        except (TypeError, ValueError):
            pass
        return result.get("available") is False and bool(result.get("error"))

    @staticmethod
    def _retry_delay(policy: ToolPolicy, error: Exception | None, attempt: int) -> float:
        delay = backoff_seconds(policy.initial_backoff_seconds, policy.backoff_multiplier,
                                policy.max_backoff_seconds, policy.jitter_ratio, attempt)
        hint = getattr(error, "retry_after", None) if error is not None else None
        try:
            if hint is not None:
                delay = min(policy.max_backoff_seconds, max(delay, float(hint)))
        except (TypeError, ValueError):
            pass
        return delay

    @staticmethod
    def _deadline() -> float | None:
        context = current_context()
        return context.deadline_at if context else None

    def _timeout(self, policy: ToolPolicy) -> float | None:
        rem = remaining(self._deadline())
        if rem is None:
            return policy.timeout_seconds
        return min(policy.timeout_seconds, rem)
