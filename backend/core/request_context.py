"""Context variables replacing the Java Reactor/ThreadLocal request context."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any

_UNSET = object()


@dataclass
class AgentRequestContext:
    user_id: str
    session_id: str
    agent_name: str | None = None
    travel_order_id: str | None = None
    request_id: str | None = None
    deadline_at: float | None = None
    tool_call_id: str | None = None
    idempotency_key: str | None = None

    def derive(self, *, agent_name: str | None | object = _UNSET,
               travel_order_id: str | None | object = _UNSET,
               request_id: str | None | object = _UNSET,
               deadline_at: float | None | object = _UNSET,
               tool_call_id: str | None | object = _UNSET,
               idempotency_key: str | None | object = _UNSET) -> AgentRequestContext:
        """Create a child execution context without changing its identity.

        ``user_id`` and ``session_id`` deliberately have no override arguments:
        nested Agent/Tool calls must inherit the authenticated request identity.
        Passing ``None`` explicitly clears an optional execution field; omitting
        it preserves the parent value.
        """
        values: dict[str, Any] = {}
        for key, value in {
            "agent_name": agent_name,
            "travel_order_id": travel_order_id,
            "request_id": request_id,
            "deadline_at": deadline_at,
            "tool_call_id": tool_call_id,
            "idempotency_key": idempotency_key,
        }.items():
            if value is not _UNSET:
                values[key] = value
        return replace(self, **values)

    def to_payload(self) -> dict[str, Any]:
        """Return the picklable context envelope used by process workers."""
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "travel_order_id": self.travel_order_id,
            "request_id": self.request_id,
            "deadline_at": self.deadline_at,
            "tool_call_id": self.tool_call_id,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AgentRequestContext:
        """Restore a context envelope received from an internal worker queue."""
        return cls(
            user_id=str(payload.get("user_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            agent_name=_optional_text(payload.get("agent_name")),
            travel_order_id=_optional_text(payload.get("travel_order_id")),
            request_id=_optional_text(payload.get("request_id")),
            deadline_at=_optional_float(payload.get("deadline_at")),
            tool_call_id=_optional_text(payload.get("tool_call_id")),
            idempotency_key=_optional_text(payload.get("idempotency_key")),
        )


_current: ContextVar[AgentRequestContext | None] = ContextVar("gogo_agent_context", default=None)


def current_context() -> AgentRequestContext | None:
    return _current.get()


def require_context() -> AgentRequestContext:
    """Return the request context or fail closed at an Agent/Tool boundary."""
    context = current_context()
    if context is None or not context.user_id or not context.session_id:
        raise RuntimeError("缺少可信请求上下文，拒绝执行 Agent/Tool 调用")
    return context


def context_from_state(state: Mapping[str, Any], *, agent_name: str | None = None,
                       parent: AgentRequestContext | None = None) -> AgentRequestContext:
    """Build the effective context, preferring a trusted parent over state.

    API entry points may create the first context from authenticated state. Once
    an Agent/Tool boundary already has a context, values embedded in model-owned
    state can never replace its user or parent session identity.
    """
    parent = parent if parent is not None else current_context()
    if parent is not None:
        return parent.derive(
            agent_name=agent_name if agent_name is not None else parent.agent_name,
            travel_order_id=parent.travel_order_id or _optional_text(
                state.get("travel_order_id") or state.get("order_id")),
            request_id=parent.request_id or _optional_text(state.get("request_id")),
            deadline_at=parent.deadline_at if parent.deadline_at is not None
            else _optional_float(state.get("deadline_at")),
        )
    return AgentRequestContext(
        user_id=str(state.get("user_id") or ""),
        session_id=str(state.get("session_id") or ""),
        agent_name=agent_name or _optional_text(state.get("agent_name")),
        travel_order_id=_optional_text(state.get("travel_order_id") or state.get("order_id")),
        request_id=_optional_text(state.get("request_id")),
        deadline_at=_optional_float(state.get("deadline_at")),
    )


def apply_context_to_state(state: Mapping[str, Any], context: AgentRequestContext) -> dict[str, Any]:
    """Project trusted context fields into legacy business state compatibility keys."""
    result = dict(state)
    result["user_id"] = context.user_id
    result["session_id"] = context.session_id
    if context.request_id is not None:
        result["request_id"] = context.request_id
    else:
        result.pop("request_id", None)
    if context.travel_order_id is not None:
        result["travel_order_id"] = context.travel_order_id
    else:
        result.pop("travel_order_id", None)
    if context.deadline_at is not None:
        result["deadline_at"] = context.deadline_at
    else:
        result.pop("deadline_at", None)
    return result


@contextmanager
def bind_context_value(context: AgentRequestContext) -> Iterator[None]:
    """Bind an existing context object and reliably restore the caller value."""
    token: Token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def bind_context_payload(payload: Mapping[str, Any] | None) -> Iterator[None]:
    """Restore a process-safe context payload when an internal envelope has one."""
    if payload is None:
        yield
        return
    with bind_context_value(AgentRequestContext.from_payload(payload)):
        yield


@contextmanager
def bind_context(user_id: str, session_id: str = "", agent_name: str | None = None,
                 travel_order_id: str | None = None, request_id: str | None = None,
                 deadline_at: float | None = None, tool_call_id: str | None = None,
                 idempotency_key: str | None = None) -> Iterator[None]:
    context = AgentRequestContext(user_id, session_id, agent_name, travel_order_id,
                                  request_id, deadline_at, tool_call_id, idempotency_key)
    with bind_context_value(context):
        yield


@contextmanager
def bind_tool_call_id(tool_call_id: str | None) -> Iterator[None]:
    """Temporarily associate Hook-generated IDs with the current tool call."""
    context = _current.get()
    if context is None:
        yield
        return
    token: Token = _current.set(replace(context, tool_call_id=tool_call_id))
    try:
        yield
    finally:
        _current.reset(token)


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
