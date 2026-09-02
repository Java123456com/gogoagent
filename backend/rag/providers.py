"""Remote RAG providers used by the Java-compatible knowledge facade."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

DEFAULT_BAILIAN_KNOWLEDGE_ENDPOINT = "bailian.cn-beijing.aliyuncs.com"


class BailianKnowledgeError(RuntimeError):
    pass


@dataclass
class BailianKnowledgeProvider:
    """Adapter for AgentScope's ``BailianKnowledge`` integration.

    Java AgentScope uses the official Alibaba Cloud SDK, which signs the
    request with ``accessKeyId``/``accessKeySecret`` and calls
    ``Client.retrieveWithOptions(workspaceId, RetrieveRequest, ...)``.  The
    Python adapter follows that contract.  ``sdk_client`` is injectable for
    tests and for applications that already manage an SDK client.
    """

    endpoint: str = DEFAULT_BAILIAN_KNOWLEDGE_ENDPOINT
    access_key_id: str | None = None
    access_key_secret: str | None = None
    workspace_id: str | None = None
    index_id: str | None = None
    api_key: str | None = None
    timeout: float = 20.0
    provider_name: str = "bailian"
    sdk_client: Any | None = None

    def _client(self) -> Any:
        if self.sdk_client is not None:
            return self.sdk_client
        if not self.access_key_id or not self.access_key_secret:
            raise BailianKnowledgeError(
                "百炼知识库需要 BAILIAN_ACCESS_KEY_ID 和 BAILIAN_ACCESS_KEY_SECRET"
            )
        if not self.workspace_id or not self.index_id:
            raise BailianKnowledgeError(
                "百炼知识库需要 BAILIAN_WORKSPACE_ID 和 BAILIAN_INDEX_ID"
            )
        try:
            from alibabacloud_bailian20231229.client import Client
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise BailianKnowledgeError(
                "需要安装 alibabacloud-bailian20231229 才能访问百炼知识库"
            ) from exc

        endpoint = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        config = open_api_models.Config(
            access_key_id=self.access_key_id,
            access_key_secret=self.access_key_secret,
            endpoint=endpoint,
        )
        self.sdk_client = Client(config)
        return self.sdk_client

    def retrieve(self, query: str, limit: int = 4) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        if not self.workspace_id or not self.index_id:
            raise BailianKnowledgeError("百炼知识库需要 workspace_id 和 index_id")
        try:
            from alibabacloud_bailian20231229 import models as bailian_models
            from alibabacloud_tea_util import models as util_models
        except BailianKnowledgeError:
            raise
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            if self.sdk_client is None:
                raise BailianKnowledgeError(
                    "需要安装 alibabacloud-bailian20231229 才能访问百炼知识库"
                ) from exc
            # A caller-supplied test/double client may not need generated Tea
            # classes.  Keep its request observable without making tests
            # install the production SDK.
            request = SimpleNamespace(index_id=self.index_id, query=query,
                                      dense_similarity_top_k=limit,
                                      sparse_similarity_top_k=limit)
            runtime = SimpleNamespace()
        else:
            request = bailian_models.RetrieveRequest(
                index_id=self.index_id,
                query=query,
                dense_similarity_top_k=limit,
                sparse_similarity_top_k=limit,
            )
            runtime = util_models.RuntimeOptions()
        try:
            response = self._client().retrieve_with_options(
                self.workspace_id, request, {}, runtime
            )
            raw = getattr(response, "body", response)
        except Exception as exc:
            raise BailianKnowledgeError(f"百炼知识库请求失败: {exc}") from exc
        return _normalize_documents(raw)


def _as_mapping(value: Any) -> Any:
    """Convert generated Tea models to plain mappings recursively."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_as_mapping(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_mapping(item) for key, item in value.items()}
    for method_name in ("to_map", "toMap"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _as_mapping(method())
            except (AttributeError, TypeError, ValueError):
                pass
    if hasattr(value, "__dict__"):
        return {
            key: _as_mapping(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _node_content(node: dict[str, Any]) -> str:
    return str(
        node.get("text")
        or node.get("content")
        or node.get("snippet")
        or node.get("textContent")
        or ""
    )


def _node_score(node: dict[str, Any]) -> float:
    value = node.get("score")
    if value is None:
        value = node.get("similarity") or node.get("denseSimilarity") or 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _extract_nodes(raw: Any) -> list[Any]:
    raw = _as_mapping(raw)
    if isinstance(raw, dict):
        data = raw.get("data") or raw.get("Data") or raw
        if isinstance(data, dict):
            for key in ("nodes", "Nodes", "documents", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        return _extract_nodes(data) if data is not raw else []
    return raw if isinstance(raw, list) else []


def _normalize_documents(raw: Any) -> list[dict[str, Any]]:
    """Normalize Bailian ``body.data.nodes`` and compatible test envelopes."""
    docs = _extract_nodes(raw)

    normalized: list[dict[str, Any]] = []
    for item in docs:
        if isinstance(item, str):
            normalized.append({"content": item, "source": "bailian", "provider": "bailian", "score": 0.0})
            continue
        if not isinstance(item, dict):
            continue
        content = _node_content(item)
        normalized.append({
            "content": str(content),
            "source": item.get("source") or item.get("title") or item.get("docId") or "bailian",
            "provider": "bailian",
            "score": _node_score(item),
            "metadata": _node_metadata(item),
            **{key: value for key, value in item.items()
               if key not in {"content", "text", "snippet", "textContent", "source", "title", "score", "similarity", "metadata"}},
        })
    return normalized
