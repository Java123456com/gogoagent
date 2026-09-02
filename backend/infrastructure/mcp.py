"""Managed MCP clients for Streamable HTTP and stdio transports."""
from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from backend.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ClientSpec:
    endpoint: str | None
    command: str | None
    args: tuple[str, ...]
    env_fingerprint: str


class McpClientManager:
    """Own one event loop and reuse initialized MCP sessions across calls."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mcp-client-loop", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        self._clients: dict[_ClientSpec, tuple[AsyncExitStack, Any]] = {}
        self._closed = False

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        endpoint: str | None,
        command: str | None,
        args: list[str],
        env: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("MCP client manager is closed")
        future = asyncio.run_coroutine_threadsafe(
            self._call(
                tool_name=tool_name,
                arguments=arguments,
                endpoint=endpoint,
                command=command,
                args=args,
                env=env,
                timeout=timeout,
            ),
            self._loop,
        )
        return future.result(timeout=max(1.0, timeout + 5.0))

    async def _call(self, **kwargs: Any) -> dict[str, Any]:
        env = kwargs["env"]
        fingerprint = hashlib.sha256(
            json.dumps(env, sort_keys=True).encode("utf-8")
        ).hexdigest()
        spec = _ClientSpec(
            kwargs["endpoint"], kwargs["command"], tuple(kwargs["args"]), fingerprint,
        )
        stack, session = await self._get_or_create(spec, env, kwargs["timeout"])
        try:
            result = await asyncio.wait_for(
                session.call_tool(kwargs["tool_name"], kwargs["arguments"]),
                timeout=kwargs["timeout"],
            )
            return normalize_mcp_result(result)
        except Exception:
            self._clients.pop(spec, None)
            await stack.aclose()
            raise

    async def _get_or_create(
        self, spec: _ClientSpec, env: dict[str, str], timeout: float,
    ) -> tuple[AsyncExitStack, Any]:
        existing = self._clients.get(spec)
        if existing is not None:
            return existing
        from mcp import ClientSession, StdioServerParameters

        stack = AsyncExitStack()
        try:
            if spec.endpoint:
                from mcp.client.streamable_http import streamablehttp_client

                streams = await stack.enter_async_context(
                    streamablehttp_client(
                        spec.endpoint,
                        timeout=timeout,
                        sse_read_timeout=timeout,
                    )
                )
                read_stream, write_stream = streams[0], streams[1]
            else:
                from mcp.client.stdio import stdio_client

                parameters = StdioServerParameters(
                    command=spec.command or "npx",
                    args=list(spec.args),
                    env={**os.environ, **env},
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=timeout)
        except Exception:
            await stack.aclose()
            raise
        self._clients[spec] = (stack, session)
        return stack, session

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def shutdown() -> None:
            clients = list(self._clients.values())
            self._clients.clear()
            for stack, _ in reversed(clients):
                try:
                    await stack.aclose()
                except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                    logger.debug("MCP 会话关闭失败: %s", exc)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)


_manager: McpClientManager | None = None
_manager_lock = threading.RLock()


def get_mcp_client_manager() -> McpClientManager:
    global _manager
    with _manager_lock:
        if _manager is None or _manager._closed:
            _manager = McpClientManager()
        return _manager


def close_mcp_clients() -> None:
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.close()
            _manager = None


atexit.register(close_mcp_clients)


def call_mcp_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    endpoint: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    allowed_tools: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    """Call an allow-listed MCP tool over a managed or short-lived session."""
    if not endpoint and not command:
        raise ValueError("MCP endpoint or command is required")
    if allowed_tools is not None and tool_name not in set(allowed_tools):
        raise PermissionError(f"MCP 工具未在白名单中: {tool_name}")
    payload = arguments or {}
    command_args = args or []
    environment = env or {}
    if get_settings().mcp_persistent_sessions:
        return get_mcp_client_manager().call(
            tool_name=tool_name,
            arguments=payload,
            endpoint=endpoint,
            command=command,
            args=command_args,
            env=environment,
            timeout=timeout,
        )
    return asyncio.run(_call_once(
        tool_name=tool_name,
        arguments=payload,
        endpoint=endpoint,
        command=command,
        args=command_args,
        env=environment,
        timeout=timeout,
    ))


async def _call_once(**kwargs: Any) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters

    if kwargs["endpoint"]:
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            kwargs["endpoint"], timeout=kwargs["timeout"], sse_read_timeout=kwargs["timeout"],
        ) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await asyncio.wait_for(
                session.call_tool(kwargs["tool_name"], kwargs["arguments"]),
                timeout=kwargs["timeout"],
            )
            return normalize_mcp_result(result)
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=kwargs["command"] or "npx",
        args=kwargs["args"],
        env={**os.environ, **kwargs["env"]},
    )
    async with stdio_client(parameters) as streams, ClientSession(
        streams[0], streams[1]
    ) as session:
        await session.initialize()
        result = await asyncio.wait_for(
            session.call_tool(kwargs["tool_name"], kwargs["arguments"]),
            timeout=kwargs["timeout"],
        )
        return normalize_mcp_result(result)


def normalize_mcp_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        raw = result.model_dump(mode="json")
    elif isinstance(result, dict):
        raw = result
    else:
        raw = {"result": str(result)}
    content = raw.get("content") or []
    texts = [
        str(item.get("text"))
        for item in content
        if isinstance(item, dict) and item.get("text") is not None
    ]
    payload: dict[str, Any] = {"content": "\n".join(texts)}
    structured = raw.get("structuredContent") or raw.get("structured_content")
    if structured is not None:
        payload["structuredContent"] = structured
    if raw.get("isError") or raw.get("is_error"):
        payload["isError"] = True
    return payload
