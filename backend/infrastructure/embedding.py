"""DashScope text-embedding-v4 adapter with a deterministic local fallback."""
from __future__ import annotations

from typing import Any


class DashScopeEmbeddingError(RuntimeError):
    pass


class DashScopeEmbedding:
    """Minimal HTTP implementation of the DashScope embedding API.

    The Java project uses ``DashScopeTextEmbedding`` with model
    ``text-embedding-v4`` and 1024 dimensions. Keeping this adapter small lets
    the RAG and intent stores share the same provider without coupling either
    module to a specific SDK release.
    """

    def __init__(self, api_key: str, model: str = "text-embedding-v4", dimensions: int = 1024,
                 timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            import httpx

            response = httpx.post(
                "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "input": {"texts": texts},
                      "parameters": {"dimension": self.dimensions}},
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw: Any = response.json()
        except Exception as exc:
            raise DashScopeEmbeddingError(f"DashScope embedding 请求失败: {exc}") from exc

        items = ((raw.get("output") or {}).get("embeddings") if isinstance(raw, dict) else None) or []
        vectors: list[list[float]] = []
        for item in items:
            if isinstance(item, dict):
                vector = item.get("embedding") or item.get("vector") or []
            else:
                vector = item
            if not isinstance(vector, list):
                raise DashScopeEmbeddingError("DashScope embedding 返回格式无效")
            vectors.append([float(value) for value in vector])
        if len(vectors) != len(texts):
            raise DashScopeEmbeddingError("DashScope embedding 返回数量与输入不一致")
        return vectors
