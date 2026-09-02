"""三层意图识别编排器（对应 Java IntentRecognitionRouter）。

按 L0（结构启发）→ L1（规则）→ L2（向量）顺序尝试，命中即短路；未命中返回 ``None``，
由调用方兜底 L3（LLM）。
"""
from __future__ import annotations

import logging
import re

from backend.intent.result import IntentRecognitionResult
from backend.intent.rule_matcher import STRONG_CONJUNCTIONS, IntentRuleMatcher, Verdict
from backend.intent.vector_matcher import IntentVectorMatcher

logger = logging.getLogger(__name__)

MULTI_INTENT_CONJUNCTION = re.compile(STRONG_CONJUNCTIONS)
MULTI_INTENT_MIN_LENGTH = 10
MULTI_INTENT_MIN_CONJUNCTION_OFFSET = 4


class IntentRecognitionRouter:
    def __init__(self, rule_matcher: IntentRuleMatcher | None = None,
                 vector_matcher: IntentVectorMatcher | None = None) -> None:
        self.rule_matcher = rule_matcher or IntentRuleMatcher()
        self.vector_matcher = vector_matcher or IntentVectorMatcher()

    def route(self, question: str | None) -> IntentRecognitionResult | None:
        if not question or not question.strip():
            return None
        normalized = question.strip()

        # -------- L0：多意图结构启发 --------
        if has_multi_intent_signal(normalized):
            logger.info("[INTENT_ROUTER] L0 检测到并列/顺承连词，疑似多意图，跳过 L1/L2 交给 L3")
            return None

        # -------- L1 --------
        outcome = self.rule_matcher.evaluate(normalized)
        if outcome.verdict == Verdict.HIT:
            return outcome.result
        if outcome.verdict == Verdict.AMBIGUOUS:
            logger.info("[INTENT_ROUTER] L1 子句级多类命中，疑似多意图，跳过 L2 交给 L3")
            return None

        # -------- L2 --------
        try:
            return self.vector_matcher.match(normalized)
        except Exception as e:  # embedding/检索失败 → 降级 L3
            logger.warning("[INTENT_ROUTER] L2 异常，降级到 L3: %s", e)
            return None


def has_multi_intent_signal(text: str | None) -> bool:
    if not text or len(text) < MULTI_INTENT_MIN_LENGTH:
        return False
    m = MULTI_INTENT_CONJUNCTION.search(text)
    return bool(m) and m.start() >= MULTI_INTENT_MIN_CONJUNCTION_OFFSET


# 进程级单例
intent_router = IntentRecognitionRouter()
