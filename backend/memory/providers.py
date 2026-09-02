"""Long-term memory providers.

The remote implementation delegates preference storage to Bailian's
``BailianLongTermMemory``.  Its 1.0.12 client uses the DashScope memory API
with two fixed paths and a Bearer API key; this module mirrors those request
models while keeping a local fallback available when the service is disabled.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class LongTermMemoryProvider(Protocol):
    provider_name: str

    def record(self, user_id: str, content: str, memory_type: str,
               metadata: dict[str, Any] | None = None) -> str | None: ...

    def retrieve(self, user_id: str, limit: int = 10,
                 query: str | None = None) -> list[str]: ...


class BailianProviderError(RuntimeError):
    """Raised when the configured Bailian service cannot be reached."""


@dataclass
class BailianLongTermMemoryProvider:
    """HTTP adapter matching AgentScope 1.0.12's ``BailianMemoryClient``."""

    endpoint: str = "https://dashscope.aliyuncs.com"
    api_key: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    workspace_id: str | None = None
    memory_library_id: str | None = None
    project_id: str | None = None
    profile_schema: str | None = None
    timeout: float = 20.0
    provider_name: str = "bailian"
    http_post: Any | None = None

    def _request(self, operation: str, payload: dict[str, Any]) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise BailianProviderError("需要安装 httpx 才能访问百炼长期记忆") from exc

        paths = {
            "add": "/api/v2/apps/memory/add",
            "search": "/api/v2/apps/memory/memory_nodes/search",
        }
        try:
            url = f"{self.endpoint.rstrip('/')}{paths[operation]}"
        except KeyError as exc:
            raise BailianProviderError(f"不支持的百炼记忆操作: {operation}") from exc
        if not self.api_key:
            raise BailianProviderError("百炼长期记忆需要 DASHSCOPE_API_KEY")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            post = self.http_post or httpx.post
            response = post(url, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise BailianProviderError(f"百炼长期记忆请求失败: {exc}") from exc

    def _base_payload(self, user_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"userId": user_id}
        for key, value in {
            "memoryLibraryId": self.memory_library_id,
            "projectId": self.project_id,
            "profileSchema": self.profile_schema,
        }.items():
            if value:
                payload[key] = value
        return payload

    def record(self, user_id: str, content: str, memory_type: str,
               metadata: dict[str, Any] | None = None) -> str | None:
        payload = self._base_payload(user_id)
        payload.update({
            "messages": [{"role": "user", "content": content}],
            "metadata": {
                "source": "gogo-travel-agent",
                "memory_type": memory_type,
                **(metadata or {}),
            },
        })
        result = self._request("add", payload)
        if isinstance(result, dict):
            data = result.get("data") or result.get("result") or result
            if isinstance(data, dict):
                nodes = data.get("memoryNodes") or data.get("memory_nodes") or []
                if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
                    node_id = nodes[0].get("memoryNodeId") or nodes[0].get("memory_node_id")
                    if node_id:
                        return str(node_id)
                return str(data.get("requestId") or data.get("request_id") or data.get("id") or "remote")
        # Bailian's record endpoint returns no identifier. A successful
        # response without an id is still a remote write, so return a stable
        # sentinel and do not duplicate it in the local fallback store.
        return "remote"

    def retrieve(self, user_id: str, limit: int = 10,
                 query: str | None = None) -> list[str]:
        payload = self._base_payload(user_id)
        payload.update({
            "messages": [{"role": "user", "content": query or "旅行偏好"}],
            "topK": limit,
        })
        if self.project_id:
            payload["projectIds"] = [self.project_id]
        result = self._request("search", payload)
        return _extract_memory_text(result)


def _extract_memory_text(result: Any) -> list[str]:
    """Normalize common SDK/HTTP response shapes into memory text entries."""
    if result is None:
        return []
    if isinstance(result, str):
        return [result] if result.strip() else []
    if isinstance(result, list):
        values = result
    elif isinstance(result, dict):
        values = result.get("memoryNodes") or result.get("memory_nodes") or result.get("memories") or result.get("items")
        if values is None:
            nested = result.get("data") or result.get("result")
            if nested is not result:
                return _extract_memory_text(nested)
            values = result.get("content")
        if values is None:
            values = []
    else:
        return []

    normalized: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            normalized.append(value.strip())
        elif isinstance(value, dict):
            text = value.get("content") or value.get("text") or value.get("memory")
            if text is not None and str(text).strip():
                normalized.append(str(text).strip())
        else:
            normalized.append(json.dumps(value, ensure_ascii=False))
    return normalized
