"""轻量 LLM 服务（对应 Java ConversationTitleService / QuestionRecommendationService）。

二者均为「单次 LLM 调用、无需工具、无多轮推理」的纯文本生成，不套 ReActAgent。
"""
from __future__ import annotations

import json
from threading import Thread

from backend.core.continuation import CONTINUATION_SIGNALS
from backend.infrastructure.llm import fast_model, invoke_text
from backend.infrastructure.repositories import chat_repository
from backend.prompts import load_prompt
from backend.services.chat_service import chat_service


class ConversationTitleService:
    """根据用户问题 + 意图识别结果异步生成对话标题。"""

    def generate(self, question: str, intent_json: str) -> str:
        system = load_prompt("conversation-title-agent-system.md")
        user = f"用户问题：{question}\n意图识别结果：{intent_json}"
        fallback = (question[:18] + ("…" if len(question) > 18 else "")) or "新对话"
        return invoke_text(fast_model(), system, user, fallback).strip()

    def update_if_default(self, session_id: str, user_id: str, question: str,
                          intent_json: dict | str | None) -> None:
        """Update only an untouched default title; failures never affect chat."""
        try:
            conversation = chat_repository.get_conversation(session_id)
            if conversation is None or conversation.deleted or conversation.user_id != user_id:
                return
            if conversation.title != "新对话":
                return
            encoded = (json.dumps(intent_json, ensure_ascii=False)
                       if isinstance(intent_json, dict) else str(intent_json or "{}"))
            title = self.generate(question, encoded).strip()[:24]
            if title:
                chat_service.update_title(session_id, user_id, title)
        except Exception:
            return

    def schedule_update(self, session_id: str, user_id: str, question: str,
                        intent_json: dict | str | None) -> None:
        Thread(
            target=self.update_if_default,
            args=(session_id, user_id, question, intent_json),
            name=f"conversation-title-{session_id}",
            daemon=True,
        ).start()


class QuestionRecommendationService:
    """MasterAgent 返回后生成下一步推荐问题。"""

    def generate(self, user_question: str, assistant_reply: str) -> list[str]:
        system = load_prompt("question-recommendation-agent-system.md")
        signals = "、".join(sorted(CONTINUATION_SIGNALS))
        user = (
            f"用户问题：{user_question}\n助手回复：{assistant_reply}\n\n"
            f"可直接继续当前步骤的关键词：{signals}"
        )
        fallback = '{"questions": ["查询我的差旅", "查看差旅政策", "设置出行偏好"]}'
        text = invoke_text(fast_model(), system, user, fallback)
        try:
            start, end = text.find("{"), text.rfind("}")
            payload = json.loads(text[start:end + 1] if start >= 0 and end > start else text)
            questions = payload.get("questions", []) if isinstance(payload, dict) else []
            return [str(item).strip() for item in questions if str(item).strip()][:4]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []


conversation_title_service = ConversationTitleService()
question_recommendation_service = QuestionRecommendationService()
