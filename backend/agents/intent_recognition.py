import json
import re

from backend.agents.base import text_agent
from backend.infrastructure.llm import llm_enabled
from backend.intent.category import IntentCategory
from backend.intent.result import Confidence
from backend.intent.router import intent_router
from backend.intent.rule_matcher import IntentRuleMatcher, Verdict


class IntentRecognitionAgent:
    def invoke(self, question: str) -> dict:
        fast = intent_router.route(question)
        if fast is not None: return fast.to_dict()
        fallback = {"intents": ["GENERAL"], "primary_intent": "GENERAL", "multi_intent": False, "overall_reason": "未命中规则，需要通用问答"}
        if not llm_enabled():
            return self._deterministic_fallback(question)
        text = text_agent("intent-recognition-agent-system.md", question, json.dumps(fallback, ensure_ascii=False))
        try: return json.loads(text)
        except json.JSONDecodeError:
            return self._deterministic_fallback(question)

    @staticmethod
    def _deterministic_fallback(question: str) -> dict:
        """Keep zero-key mode useful for compound requests as well."""
        matcher = IntentRuleMatcher()
        categories = []
        for clause in re.split(r"[，。；！？!?;,、]|然后|接着|顺便|同时|另外|以及", question):
            outcome = matcher.evaluate(clause.strip())
            if outcome.verdict == Verdict.HIT and outcome.result:
                category = outcome.result.primary
                if category not in categories:
                    categories.append(category)
        if not categories:
            categories = [IntentCategory.UNKNOWN]
        items = [
            {
                "intent": category.code,
                "target_agent": category.default_target_agent,
                "confidence": Confidence.MEDIUM.value,
                "reason": "无模型模式下由子句规则补全",
            }
            for category in categories
        ]
        return {
            "intents": items,
            "primary_intent": categories[0].code,
            "multi_intent": len(categories) > 1,
            "overall_reason": "确定性多意图兜底",
        }


intent_recognition_agent = IntentRecognitionAgent()
