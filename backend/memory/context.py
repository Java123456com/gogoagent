"""Layered request/session context management and compression hooks."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


class RequestMemoryCache:
    """Single-request cache for deterministic tool and preference lookups."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> Any:
        self._values[key] = value
        return value

    def get_or_set(self, key: str, factory: Callable[[], Any]) -> Any:
        if key not in self._values:
            self._values[key] = factory()
        return self._values[key]


@dataclass(frozen=True)
class ContextCompressionConfig:
    message_threshold: int = 60
    token_ratio: float = 0.75
    max_context_tokens: int = 128_000
    keep_last: int = 20
    large_payload_chars: int = 4_096
    preview_chars: int = 300


@dataclass
class CompressionResult:
    working_messages: list[Any]
    original_messages: list[Any]
    offload_context: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    token_before: int = 0
    token_after: int = 0


class ContextCompressionHook:
    """Compress old/large messages while retaining an auditable original copy."""

    def __init__(self, config: ContextCompressionConfig | None = None) -> None:
        self.config = config or ContextCompressionConfig()

    def before_model(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        result = self.compress(messages)
        return {
            **state,
            "messages": result.working_messages,
            "memory_working_messages": result.working_messages,
            "memory_original_messages": result.original_messages,
            "memory_offload_context": result.offload_context,
            "memory_compression_events": result.events,
            "memory_token_before": result.token_before,
            "memory_token_after": result.token_after,
        }

    def compress(self, messages: list[Any]) -> CompressionResult:
        original = deepcopy(messages)
        working = deepcopy(messages)
        offload: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        token_before = _estimate_tokens(working)
        cutoff = max(0, len(working) - self.config.keep_last)

        for index, message in enumerate(working[:cutoff]):
            content = _message_content(message)
            if len(content) <= self.config.large_payload_chars:
                continue
            key = _offload_key(content, index)
            offload[key] = message
            working[index] = _replace_message(message,
                                               f"[上下文已卸载:{key}] {content[:self.config.preview_chars]}")
            events.append({"event_type": "large_message_offload", "message_index": index,
                           "offload_key": key, "original_chars": len(content)})

        should_summarize = (
            len(working) > self.config.message_threshold
            or token_before >= self.config.max_context_tokens * self.config.token_ratio
        )
        if should_summarize and len(working) > self.config.keep_last:
            summary_end = len(working) - self.config.keep_last
            older = working[:summary_end]
            summary_text = _summarize(older)
            working = [_replace_message(older[0] if older else ("system", ""),
                                        f"[历史上下文摘要]\n{summary_text}")] + working[summary_end:]
            events.append({"event_type": "history_summary", "compressed_message_count": summary_end,
                           "token_before": token_before})

        token_after = _estimate_tokens(working)
        return CompressionResult(working, original, offload, events, token_before, token_after)

    @staticmethod
    def restore_offloaded(state: dict[str, Any], key: str) -> Any:
        value = (state.get("memory_offload_context") or {}).get(key)
        return deepcopy(value)


class LayeredContextManager:
    """Coordinates request cache, session state and agent-controlled memory.

    AgentScope's long-term-memory mode is ``AGENT_CONTROL``: agents decide
    when to call ``retrieve_from_memory``.  ``auto_retrieve_long_term`` is an
    explicit deterministic-fallback option for keyless local demos, not part
    of the LLM path.
    """

    def __init__(self, session_store, long_term_memory,
                 compression_hook: ContextCompressionHook | None = None,
                 auto_retrieve_long_term: bool = False):
        self.session_store = session_store
        self.long_term_memory = long_term_memory
        self.compression_hook = compression_hook or ContextCompressionHook()
        self.auto_retrieve_long_term = auto_retrieve_long_term

    def load(self, session_id: str) -> dict[str, Any]:
        return self.session_store.get(session_id)

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        user_id = state.get("user_id", "")
        request_cache = RequestMemoryCache()
        if self.auto_retrieve_long_term:
            preferences = request_cache.get_or_set(
                f"preferences:{user_id}", lambda: self.long_term_memory.retrieve(user_id)
            )
        else:
            preferences = list(state.get("long_term_preferences") or [])
        prepared = {**state, "long_term_preferences": preferences,
                    "request_memory_cache": request_cache._values}
        return self.compression_hook.before_model(prepared)

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        clean = {key: value for key, value in state.items() if key != "request_memory_cache"}
        self.session_store.save(session_id, clean)


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content", ""))
    if isinstance(message, (tuple, list)) and len(message) > 1:
        return str(message[1])
    return str(getattr(message, "content", message))


def _replace_message(message: Any, content: str) -> Any:
    if isinstance(message, dict):
        result = dict(message)
        result["content"] = content
        return result
    if isinstance(message, (tuple, list)) and len(message) > 1:
        return (message[0], content)
    message_type = getattr(message, "type", "human")
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        cls = {"ai": AIMessage, "system": SystemMessage}.get(message_type, HumanMessage)
        return cls(content=content)
    except ImportError:
        return content


def _summarize(messages: list[Any]) -> str:
    lines = []
    for message in messages:
        content = _message_content(message).replace("\n", " ").strip()
        if content:
            lines.append(content[:500])
    return "；".join(lines)[:4_000] or "无可用历史上下文"


def _offload_key(content: str, index: int) -> str:
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    return f"ctx_{index}_{digest}"


def _estimate_tokens(messages: list[Any]) -> int:
    """Count model input tokens when tiktoken is available, else use a stable estimate."""
    text = "\n".join(_message_content(message) for message in messages)
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        # Chinese text is close to one token per character; English averages
        # about four characters per token. This is only a fallback threshold.
        chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
        return chinese + max(0, (len(text) - chinese + 3) // 4)
