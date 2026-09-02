"""对话历史服务（对应 Java ChatHistoryService）。

负责 chat_conversation / chat_message 持久化与最近消息加载，供问题改写与前端历史展示。
"""
from __future__ import annotations

from typing import Any

from backend.domain.models import ChatMessage
from backend.infrastructure.repositories import chat_repository


class ChatService:
    def save_user_message(self, conversation_id: str, user_id: str, content: str) -> None:
        chat_repository.upsert_conversation(conversation_id, user_id)
        chat_repository.save_message(conversation_id, "user", content)

    def save_agent_message(self, conversation_id: str, content: str, agent_name: str | None = None,
                           extra: dict[str, Any] | None = None) -> ChatMessage:
        return chat_repository.save_message(conversation_id, "agent", content, agent_name, extra)

    def find_recent_messages(self, conversation_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return [{"role": m.role, "content": m.content} for m in
                chat_repository.find_recent_messages(conversation_id, limit)]

    def update_title(self, conversation_id: str, user_id: str, title: str) -> None:
        conversation = chat_repository.get_conversation(conversation_id)
        if conversation is None or conversation.deleted:
            raise ValueError("会话不存在")
        if conversation.user_id != user_id:
            raise PermissionError("无权访问该会话")
        title = (title or "新对话").strip()[:24]
        chat_repository.upsert_conversation(conversation_id, user_id, title)

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        return [c.as_dict() for c in chat_repository.list_conversations(user_id)]

    def find_messages(self, conversation_id: str):
        return chat_repository.find_messages(conversation_id)

    def find_messages_for_user(self, conversation_id: str, user_id: str):
        conversation = chat_repository.get_conversation(conversation_id)
        if conversation is None or conversation.deleted:
            raise ValueError("会话不存在")
        if conversation.user_id != user_id:
            raise PermissionError("无权访问该会话")
        return chat_repository.find_messages(conversation_id)

    def delete_conversation(self, conversation_id: str, user_id: str) -> None:
        conversation = chat_repository.get_conversation(conversation_id)
        if conversation is None or conversation.deleted:
            raise ValueError("会话不存在")
        if conversation.user_id != user_id:
            raise PermissionError("无权访问该会话")
        chat_repository.delete_conversation(conversation_id)

    def update_feedback(self, message_id: str, user_id: str, feedback: str | None) -> None:
        message = chat_repository.get_message(message_id)
        if message is None or message.deleted:
            raise ValueError("消息不存在")
        conversation = chat_repository.get_conversation(message.conversation_id)
        if conversation is None or conversation.deleted:
            raise ValueError("会话不存在")
        if conversation.user_id != user_id:
            raise PermissionError("无权访问该消息")
        if isinstance(feedback, str):
            normalized = feedback.strip()
            feedback = None if normalized.lower() in {"", "null", "clear"} else normalized.upper()
        chat_repository.update_feedback(message_id, feedback)


chat_service = ChatService()
