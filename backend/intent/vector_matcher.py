"""L2 向量相似度匹配器。

支持 DashScope Embedding 与本地字符 n-gram 两种 Top-2 检索路径；二者共享
命中/歧义判定逻辑（阈值 + margin），但 embedding 默认用「字符 n-gram 哈希」在本地
零依赖计算，配置 ``GOGO_USE_LLM`` + 模型 Key 后自动切换为真实 Embedding。
"""
from __future__ import annotations

import itertools
import math

from backend.config import get_settings
from backend.infrastructure.embedding import DashScopeEmbedding, DashScopeEmbeddingError
from backend.intent.category import IntentCategory
from backend.intent.result import Confidence, IntentRecognitionResult, Source
from backend.intent.seed import SEED_EXAMPLES

DEFAULT_SCORE_THRESHOLD = 0.75
DEFAULT_SCORE_MARGIN = 0.05
TOP_K = 2

_VECTOR_DIM = 512


def _embed(text: str, dim: int = _VECTOR_DIM) -> list[float]:
    """字符 n-gram（1~2 元）哈希向量，UTF-8 友好，零外部依赖。"""
    vec = [0.0] * dim
    text = text.lower().strip()
    tokens: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        tokens.append(ch)
    tokens += [a + b for a, b in itertools.pairwise(tokens)]
    if not tokens:
        return vec
    for tok in tokens:
        h = hash(tok) & 0x7FFFFFFF
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # 向量已归一化


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class IntentVectorMatcher:
    def __init__(self, score_threshold: float = DEFAULT_SCORE_THRESHOLD,
                 score_margin: float = DEFAULT_SCORE_MARGIN) -> None:
        self.score_threshold = score_threshold
        self.score_margin = score_margin
        self._corpus: list[tuple[IntentCategory, str, list[float]]] = []
        for category, examples in SEED_EXAMPLES.items():
            for example in examples:
                self._corpus.append((category, example, _embed(example)))
        self._remote_ready = False
        settings = get_settings()
        self._remote = (
            DashScopeEmbedding(
                settings.dashscope_api_key,
                settings.dashscope_embedding_model,
                settings.dashscope_embedding_dimensions,
                settings.external_request_timeout_seconds,
            )
            if settings.dashscope_embedding_enabled and settings.dashscope_api_key
            else None
        )

    def _ensure_remote_corpus(self) -> None:
        if not self._remote or self._remote_ready:
            return
        try:
            texts = [example for _, example, _ in self._corpus]
            vectors = self._remote.embed(texts)
            self._corpus = [
                (category, example, _normalize(vector))
                for (category, example, _), vector in zip(self._corpus, vectors)
            ]
            self._remote_ready = True
        except DashScopeEmbeddingError:
            # A remote outage must not disable deterministic intent routing.
            self._remote = None

    def match(self, text: str | None) -> IntentRecognitionResult | None:
        if not text or not text.strip():
            return None
        normalized = text.strip()
        self._ensure_remote_corpus()
        if self._remote:
            try:
                query_vec = _normalize(self._remote.embed([normalized])[0])
            except DashScopeEmbeddingError:
                self._remote = None
                query_vec = _embed(normalized)
        else:
            query_vec = _embed(normalized)

        scored: list[tuple[float, IntentCategory, str]] = []
        for category, example, vec in self._corpus:
            scored.append((_cosine(query_vec, vec), category, example))
        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return None

        top_score, top_category, top_example = scored[0]
        if top_score < self.score_threshold:
            return None

        # margin 校验：top-1/top-2 分属不同意图且分差过小时放行 L3
        if len(scored) >= 2:
            second_score, second_category, _ = scored[1]
            if (second_category != top_category
                    and top_score - second_score < self.score_margin):
                return None

        confidence = self._classify(top_score)
        reason = f"L2 向量命中：相似度={top_score:.3f}，匹配样本「{top_example}」"
        return IntentRecognitionResult.single(Source.VECTOR, top_category, confidence, reason, top_score)

    @staticmethod
    def _classify(score: float) -> Confidence:
        if score >= 0.85:
            return Confidence.HIGH
        if score >= 0.75:
            return Confidence.MEDIUM
        return Confidence.LOW
