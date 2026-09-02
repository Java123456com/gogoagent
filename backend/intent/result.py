"""三层意图识别统一结果（对应 Java IntentRecognitionResult）。

无论 L1 规则、L2 向量还是 L3 LLM，最终都生成该结构，并输出与 LLM 同构的 JSON，
下游 MasterAgent / 流水线零改动。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.intent.category import IntentCategory


class Source(str, Enum):
    RULE = "RULE"
    VECTOR = "VECTOR"
    LLM = "LLM"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def from_wire(cls, value: str | None) -> Confidence:
        if value is None:
            return cls.LOW
        v = value.lower()
        if v == "high":
            return cls.HIGH
        if v == "medium":
            return cls.MEDIUM
        return cls.LOW


@dataclass
class IntentItem:
    category: IntentCategory
    target_agent: str
    confidence: Confidence
    reason: str

    @property
    def intent(self) -> str:
        return self.category.code

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "target_agent": self.target_agent or "",
            "confidence": self.confidence.value,
            "reason": self.reason or "",
        }


@dataclass
class IntentRecognitionResult:
    source: Source
    intents: list[IntentItem]
    primary: IntentCategory
    multi_intent: bool
    overall_reason: str
    score: float | None = None

    @classmethod
    def single(cls, source: Source, category: IntentCategory, confidence: Confidence,
               reason: str, score: float | None = None) -> IntentRecognitionResult:
        target = category.default_target_agent
        item = IntentItem(category, target, confidence, reason)
        return cls(source, [item], category, False, reason or "", score)

    @property
    def primary_intent(self) -> str:
        return self.primary.code

    def to_dict(self) -> dict[str, Any]:
        return {
            "intents": [i.to_dict() for i in self.intents],
            "primary_intent": self.primary_intent,
            "multi_intent": self.multi_intent,
            "overall_reason": self.overall_reason,
            "source": self.source.value,
            "score": self.score,
        }
