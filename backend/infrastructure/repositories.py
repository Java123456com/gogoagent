"""仓储层（对应 Java business/*/repo + mapper）。

用 SQLAlchemy 2.0 会话封装对 11 张表的访问，供 service 层与 tools 层复用。
所有写操作由调用方决定是否 commit（service 层负责事务边界）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from backend.domain.models import (
    AgentLongTermMemory,
    AgentScopeSession,
    ApprovalRecord,
    BookingRecord,
    ChatConversation,
    ChatMessage,
    ToolExecutionRecord,
    TravelOrder,
    TravelPolicyRule,
    UserAccount,
    UserApiKey,
    UserPreference,
    UserProfile,
)
from backend.infrastructure.db import get_session


def _now() -> datetime:
    return datetime.now()  # noqa: DTZ005 - existing schema stores local naive timestamps


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------- 用户 ----------------

class UserRepository:
    def find_by_username(self, username: str) -> UserAccount | None:
        with get_session() as s:
            return s.execute(select(UserAccount).where(UserAccount.username == username)).scalar_one_or_none()

    def find_by_id(self, user_id: str) -> UserAccount | None:
        with get_session() as s:
            return s.execute(select(UserAccount).where(UserAccount.user_id == user_id)).scalar_one_or_none()

    def get_profile(self, user_id: str) -> UserProfile | None:
        with get_session() as s:
            return s.execute(select(UserProfile).where(UserProfile.user_id == user_id)).scalar_one_or_none()

    def upsert_profile(self, user_id: str, values: dict[str, Any]) -> UserProfile:
        with get_session() as s:
            profile = s.get(UserProfile, user_id)
            if profile is None:
                profile = UserProfile(user_id=user_id, **values)
                s.add(profile)
            else:
                for k, v in values.items():
                    setattr(profile, k, v)
            s.commit()
            return profile


# ---------------- 差旅单 ----------------

class TravelOrderRepository:
    def create(self, user_id: str, values: dict[str, Any]) -> TravelOrder:
        with get_session() as s:
            order = TravelOrder(
                order_id=values.get("order_id") or _new_id("to"),
                user_id=user_id,
                destination=values.get("destination"),
                departure_city=values.get("departure_city"),
                departure_date=values.get("departure_date"),
                return_date=values.get("return_date"),
                purpose=values.get("purpose"),
                status=values.get("status", "DRAFT"),
                created_at=_now(),
                updated_at=_now(),
            )
            s.add(order)
            s.commit()
            return order

    def get(self, order_id: str) -> TravelOrder | None:
        with get_session() as s:
            return s.get(TravelOrder, order_id)

    def list_by_user(self, user_id: str) -> list[TravelOrder]:
        with get_session() as s:
            return list(s.execute(select(TravelOrder).where(TravelOrder.user_id == user_id)
                                  .order_by(TravelOrder.created_at.desc())).scalars())

    def update(self, order_id: str, values: dict[str, Any]) -> TravelOrder | None:
        with get_session() as s:
            order = s.get(TravelOrder, order_id)
            if order is None:
                return None
            for k, v in values.items():
                if hasattr(order, k):
                    setattr(order, k, v)
            order.updated_at = _now()
            s.commit()
            return order

    def update_status(self, order_id: str, status: str) -> None:
        self.update(order_id, {"status": status})


# ---------------- 审批 ----------------

class ApprovalRepository:
    def create(self, user_id: str, order_id: str, title: str, form: dict[str, Any]) -> ApprovalRecord:
        with get_session() as s:
            record = ApprovalRecord(
                process_instance_id=_new_id("ap"),
                user_id=user_id,
                order_id=order_id,
                title=title,
                status="PENDING",
                approval_form=json.dumps(form, ensure_ascii=False),
                submit_time=_now(),
                update_time=_now(),
            )
            s.add(record)
            s.commit()
            return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        with get_session() as s:
            return s.get(ApprovalRecord, approval_id)

    def find_latest_by_user_id(self, user_id: str) -> ApprovalRecord | None:
        with get_session() as s:
            return s.execute(
                select(ApprovalRecord).where(ApprovalRecord.user_id == user_id)
                .order_by(ApprovalRecord.submit_time.desc()).limit(1)
            ).scalar_one_or_none()

    def list_all(self, status: str | None = None) -> list[ApprovalRecord]:
        with get_session() as s:
            stmt = select(ApprovalRecord).order_by(ApprovalRecord.submit_time.desc())
            if status:
                stmt = stmt.where(ApprovalRecord.status == status)
            return list(s.execute(stmt).scalars())

    def decide(self, approval_id: str, status: str, remark: str | None) -> ApprovalRecord | None:
        with get_session() as s:
            record = s.get(ApprovalRecord, approval_id)
            if record is None or record.status != "PENDING":
                return None
            record.status = status
            record.remark = remark
            record.update_time = _now()
            s.commit()
            return record

    def cancel(self, approval_id: str) -> bool:
        with get_session() as s:
            record = s.get(ApprovalRecord, approval_id)
            if record is None:
                return False
            if record.status == "CANCELLED":
                return True
            record.status = "CANCELLED"
            record.update_time = _now()
            s.commit()
            return True

    def find_latest_by_order_id(self, order_id: str) -> ApprovalRecord | None:
        with get_session() as s:
            return s.execute(
                select(ApprovalRecord).where(ApprovalRecord.order_id == order_id)
                .order_by(ApprovalRecord.submit_time.desc()).limit(1)
            ).scalar_one_or_none()


# ---------------- 预订 ----------------

class BookingRepository:
    def create(self, user_id: str, values: dict[str, Any]) -> BookingRecord:
        with get_session() as s:
            record = BookingRecord(
                booking_id=_new_id("bk"),
                user_id=user_id,
                travel_order_id=values.get("travel_order_id"),
                conversation_id=values.get("conversation_id"),
                biz_type=values.get("biz_type", "FLIGHT"),
                platform=values.get("platform", "mock"),
                external_order_no=values.get("external_order_no"),
                status=values.get("status", "CREATED"),
                payment_status=values.get("payment_status"),
                title=values.get("title"),
                total_amount=values.get("total_amount"),
                currency=values.get("currency", "CNY"),
                contact_name=values.get("contact_name"),
                contact_phone=values.get("contact_phone"),
                start_time=values.get("start_time"),
                end_time=values.get("end_time"),
                detail=values.get("detail"),
                booked_at=_now(),
                created_at=_now(),
                updated_at=_now(),
            )
            s.add(record)
            s.commit()
            return record

    def list_by_user(self, user_id: str, biz_type: str | None = None,
                     status: str | None = None) -> list[BookingRecord]:
        with get_session() as s:
            stmt = select(BookingRecord).where(BookingRecord.user_id == user_id,
                                               BookingRecord.deleted == 0)
            if biz_type:
                stmt = stmt.where(BookingRecord.biz_type == biz_type)
            if status:
                stmt = stmt.where(BookingRecord.status == status)
            return list(s.execute(stmt.order_by(BookingRecord.booked_at.desc())).scalars())

    def find_by_booking_id(self, booking_id: str) -> BookingRecord | None:
        with get_session() as s:
            return s.execute(
                select(BookingRecord).where(
                    BookingRecord.booking_id == booking_id,
                    BookingRecord.deleted == 0,
                )
            ).scalar_one_or_none()

    def find_by_user_platform_type_external_order(
        self, user_id: str, platform: str, biz_type: str, external_order_no: str,
    ) -> BookingRecord | None:
        """Find a live external booking within one tenant only."""
        with get_session() as s:
            return s.execute(
                select(BookingRecord).where(
                    BookingRecord.user_id == user_id,
                    BookingRecord.platform == platform,
                    BookingRecord.biz_type == biz_type,
                    BookingRecord.external_order_no == external_order_no,
                    BookingRecord.deleted == 0,
                )
            ).scalar_one_or_none()

    def update(self, booking_id: str, values: dict[str, Any]) -> BookingRecord | None:
        with get_session() as s:
            record = s.execute(
                select(BookingRecord).where(
                    BookingRecord.booking_id == booking_id,
                    BookingRecord.deleted == 0,
                )
            ).scalar_one_or_none()
            if record is None:
                return None
            for k, v in values.items():
                if hasattr(record, k):
                    setattr(record, k, v)
            record.updated_at = _now()
            s.commit()
            return record

    def delete_by_user(self, booking_id: str, user_id: str) -> bool:
        with get_session() as s:
            record = s.execute(
                select(BookingRecord).where(
                    BookingRecord.booking_id == booking_id,
                    BookingRecord.user_id == user_id,
                    BookingRecord.deleted == 0,
                )
            ).scalar_one_or_none()
            if record is None:
                return False
            record.deleted = 1
            record.updated_at = _now()
            s.commit()
            return True


# ---------------- 政策 ----------------

class PolicyRepository:
    def find_by_level_and_tier(self, level_num: int, city_tier: str) -> TravelPolicyRule | None:
        with get_session() as s:
            return s.execute(
                select(TravelPolicyRule).where(
                    TravelPolicyRule.level_min <= level_num,
                    TravelPolicyRule.level_max >= level_num,
                    TravelPolicyRule.city_tier == city_tier,
                )
            ).scalar_one_or_none()


# ---------------- 对话历史 ----------------

class ChatRepository:
    def get_conversation(self, conversation_id: str) -> ChatConversation | None:
        with get_session() as s:
            return s.get(ChatConversation, conversation_id)

    def find_recent_messages(self, conversation_id: str, limit: int) -> list[ChatMessage]:
        with get_session() as s:
            stmt = (select(ChatMessage).where(ChatMessage.conversation_id == conversation_id,
                                              ChatMessage.deleted == 0)
                    .order_by(ChatMessage.created_at.desc()).limit(limit))
            rows = list(s.execute(stmt).scalars())
            return list(reversed(rows))

    def save_message(self, conversation_id: str, role: str, content: str,
                     agent_name: str | None = None, extra: dict[str, Any] | None = None) -> ChatMessage:
        with get_session() as s:
            msg = ChatMessage(
                message_id=_new_id("msg"),
                conversation_id=conversation_id,
                role=role,
                content=content,
                agent_name=agent_name,
                extra=extra,
                created_at=_now(),
            )
            s.add(msg)
            s.commit()
            return msg

    def upsert_conversation(self, conversation_id: str, user_id: str,
                            title: str | None = None) -> None:
        with get_session() as s:
            conv = s.get(ChatConversation, conversation_id)
            if conv is None:
                s.add(ChatConversation(conversation_id=conversation_id, user_id=user_id,
                                       title=title or "新对话", created_at=_now(), updated_at=_now()))
            elif title:
                conv.title = title
                conv.updated_at = _now()
            s.commit()

    def list_conversations(self, user_id: str) -> list[ChatConversation]:
        with get_session() as s:
            return list(s.execute(
                select(ChatConversation).where(ChatConversation.user_id == user_id,
                                               ChatConversation.deleted == 0)
                .order_by(ChatConversation.updated_at.desc())).scalars())

    def find_messages(self, conversation_id: str) -> list[ChatMessage]:
        with get_session() as s:
            return list(s.execute(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id, ChatMessage.deleted == 0).order_by(ChatMessage.created_at.asc())).scalars())

    def delete_conversation(self, conversation_id: str) -> None:
        with get_session() as s:
            conv = s.get(ChatConversation, conversation_id)
            if conv:
                conv.deleted = 1
            messages = s.execute(
                select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
            ).scalars()
            for message in messages:
                message.deleted = 1
            s.commit()

    def get_message(self, message_id: str) -> ChatMessage | None:
        with get_session() as s:
            return s.get(ChatMessage, message_id)

    def update_feedback(self, message_id: str, feedback: str | None) -> None:
        with get_session() as s:
            msg = s.get(ChatMessage, message_id)
            if msg is None:
                return
            msg.feedback = feedback.upper() if isinstance(feedback, str) and feedback.strip() else None
            msg.feedback_at = _now() if msg.feedback else None
            s.commit()


class AgentMemoryRepository:
    """Persistence boundary for workflow checkpoints and long-term memories."""

    def save_checkpoint(self, session_id: str, state: dict[str, Any]) -> None:
        now = _now()
        with get_session() as s:
            rows = {
                "state": {key: value for key, value in state.items()
                          if not key.startswith("memory_")},
                "original_messages": state.get("memory_original_messages", []),
                "working_messages": state.get("memory_working_messages") or state.get("messages", []),
                "offload_context": state.get("memory_offload_context", {}),
                "compression_events": state.get("memory_compression_events", []),
                "token_usage": {
                    "before": state.get("memory_token_before", 0),
                    "after": state.get("memory_token_after", 0),
                },
            }
            for state_key, value in rows.items():
                row = s.get(AgentScopeSession, (session_id, state_key, 0))
                payload = json.dumps(value, ensure_ascii=False, default=str)
                if row is None:
                    s.add(AgentScopeSession(session_id=session_id, state_key=state_key, item_index=0,
                                            state_data=payload, created_at=now, updated_at=now))
                else:
                    row.state_data = payload
                    row.updated_at = now
            s.commit()

    def load_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        with get_session() as s:
            rows = s.query(AgentScopeSession).filter(AgentScopeSession.session_id == session_id).all()
            if not rows:
                return None
            values = {}
            for row in rows:
                try:
                    values[row.state_key] = json.loads(row.state_data)
                except (TypeError, json.JSONDecodeError):
                    values[row.state_key] = None
            state = values.get("state")
            if not isinstance(state, dict):
                return None
            state["messages"] = values.get("working_messages") or state.get("messages", [])
            state["memory_working_messages"] = state["messages"]
            state["memory_original_messages"] = values.get("original_messages") or []
            state["memory_offload_context"] = values.get("offload_context") or {}
            state["memory_compression_events"] = values.get("compression_events") or []
            usage = values.get("token_usage") or {}
            state["memory_token_before"] = usage.get("before", 0)
            state["memory_token_after"] = usage.get("after", 0)
            return state

    def delete_checkpoint(self, session_id: str) -> None:
        with get_session() as s:
            rows = s.query(AgentScopeSession).filter(AgentScopeSession.session_id == session_id).all()
            for row in rows:
                s.delete(row)
            s.commit()

    def save_session_field(self, session_id: str, state_key: str, value: Any) -> None:
        """Persist a small router/pending-tool field beside the memory rows."""
        now = _now()
        payload = json.dumps(value, ensure_ascii=False, default=str)
        with get_session() as s:
            row = s.get(AgentScopeSession, (session_id, state_key, 0))
            if row is None:
                s.add(AgentScopeSession(session_id=session_id, state_key=state_key, item_index=0,
                                        state_data=payload, created_at=now, updated_at=now))
            else:
                row.state_data = payload
                row.updated_at = now
            s.commit()

    def load_session_field(self, session_id: str, state_key: str) -> Any:
        with get_session() as s:
            row = s.get(AgentScopeSession, (session_id, state_key, 0))
            if row is None:
                return None
            try:
                return json.loads(row.state_data)
            except (TypeError, json.JSONDecodeError):
                return None

    def delete_session_field(self, session_id: str, state_key: str) -> None:
        with get_session() as s:
            row = s.get(AgentScopeSession, (session_id, state_key, 0))
            if row is not None:
                s.delete(row)
                s.commit()

    def record_memory(self, user_id: str, content: str, memory_type: str = "preference",
                      metadata: dict[str, Any] | None = None) -> str | None:
        if not user_id or not content or not content.strip():
            return None
        memory_id = _new_id("mem")
        now = _now()
        with get_session() as s:
            s.add(AgentLongTermMemory(memory_id=memory_id, user_id=user_id,
                                       content=content.strip(), memory_type=memory_type,
                                       metadata_json=metadata, created_at=now, updated_at=now))
            s.commit()
        return memory_id

    def retrieve_memories(self, user_id: str, limit: int = 10,
                          query: str | None = None) -> list[AgentLongTermMemory]:
        with get_session() as s:
            stmt = (select(AgentLongTermMemory)
                    .where(AgentLongTermMemory.user_id == user_id)
                    .order_by(AgentLongTermMemory.updated_at.desc())
                    .limit(max(1, limit * 3)))
            rows = list(s.execute(stmt).scalars())
        if query:
            terms = {term for term in query.strip().split() if term}
            if terms:
                matched = [row for row in rows if any(term in row.content for term in terms)]
                rows = matched or rows
        return rows[:limit]

    def delete_memory(self, memory_id: str, user_id: str) -> bool:
        with get_session() as s:
            row = s.get(AgentLongTermMemory, memory_id)
            if row is None or row.user_id != user_id:
                return False
            s.delete(row)
            s.commit()
            return True


class ToolExecutionRepository:
    """幂等执行记录仓储；以数据库唯一键作为跨进程并发闸门。"""

    def get(self, idempotency_key: str) -> ToolExecutionRecord | None:
        with get_session() as s:
            return s.get(ToolExecutionRecord, idempotency_key)

    def create_in_progress(self, *, idempotency_key: str, request_id: str | None,
                           user_id: str, agent_name: str | None, tool_name: str,
                           arguments_hash: str, expires_at: datetime | None = None
                           ) -> ToolExecutionRecord | None:
        row = ToolExecutionRecord(
            idempotency_key=idempotency_key, request_id=request_id, user_id=user_id,
            agent_name=agent_name, tool_name=tool_name, arguments_hash=arguments_hash,
            status="IN_PROGRESS", created_at=_now(), updated_at=_now(), expires_at=expires_at,
        )
        with get_session() as s:
            try:
                s.add(row)
                s.commit()
                return row
            except Exception:  # noqa: BLE001 - duplicate key is a normal race
                s.rollback()
                return None

    def complete(self, idempotency_key: str, result: Any) -> None:
        with get_session() as s:
            row = s.get(ToolExecutionRecord, idempotency_key)
            if row is None:
                return
            row.status = "SUCCEEDED"
            row.result_json = json.dumps(result, ensure_ascii=False, default=str)
            row.updated_at = _now()
            s.commit()

    def fail(self, idempotency_key: str, error: Exception) -> None:
        with get_session() as s:
            row = s.get(ToolExecutionRecord, idempotency_key)
            if row is None:
                return
            row.status = "FAILED"
            row.error_type = type(error).__name__
            row.updated_at = _now()
            s.commit()

    @staticmethod
    def decode_result(row: ToolExecutionRecord) -> Any:
        if not row.result_json:
            return None
        try:
            return json.loads(row.result_json)
        except (TypeError, json.JSONDecodeError):
            return row.result_json


# ---------------- 偏好 ----------------

class PreferenceRepository:
    def get_all(self, user_id: str) -> dict[str, str]:
        with get_session() as s:
            rows = s.execute(select(UserPreference).where(UserPreference.user_id == user_id)).scalars()
            return {r.pref_key: r.pref_value for r in rows if r.pref_value is not None}

    def save(self, user_id: str, preferences: dict[str, Any]) -> None:
        with get_session() as s:
            existing = {r.pref_key: r for r in
                        s.execute(select(UserPreference).where(UserPreference.user_id == user_id)).scalars()}
            for key, value in preferences.items():
                encoded = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
                if key in existing:
                    existing[key].pref_value = encoded
                    existing[key].updated_at = _now()
                else:
                    s.add(UserPreference(user_id=user_id, pref_key=key, pref_value=encoded,
                                         created_at=_now(), updated_at=_now()))
            s.commit()


# ---------------- 第三方 API Key ----------------

class ApiKeyRepository:
    def get(self, user_id: str, provider: str) -> UserApiKey | None:
        with get_session() as s:
            return s.execute(select(UserApiKey).where(UserApiKey.user_id == user_id,
                                                      UserApiKey.provider == provider)).scalar_one_or_none()

    def save(self, user_id: str, provider: str, api_key_enc: str) -> None:
        with get_session() as s:
            row = s.execute(select(UserApiKey).where(UserApiKey.user_id == user_id,
                                                     UserApiKey.provider == provider)).scalar_one_or_none()
            if row:
                row.api_key_enc = api_key_enc
                row.updated_at = _now()
            else:
                s.add(UserApiKey(user_id=user_id, provider=provider, api_key_enc=api_key_enc,
                                 created_at=_now(), updated_at=_now()))
            s.commit()

    def delete(self, user_id: str, provider: str) -> bool:
        with get_session() as s:
            row = s.execute(select(UserApiKey).where(UserApiKey.user_id == user_id,
                                                     UserApiKey.provider == provider)).scalar_one_or_none()
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True


# 单例
user_repository = UserRepository()
travel_order_repository = TravelOrderRepository()
approval_repository = ApprovalRepository()
booking_repository = BookingRepository()
policy_repository = PolicyRepository()
chat_repository = ChatRepository()
preference_repository = PreferenceRepository()
api_key_repository = ApiKeyRepository()
agent_memory_repository = AgentMemoryRepository()
tool_execution_repository = ToolExecutionRepository()


def _new_id(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
