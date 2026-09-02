"""领域模型（对应 Java business/*/entity + schema.sql 的 11 张表）。

使用 SQLAlchemy 2.0 声明式映射，字段与 schema.sql 一一对应；同时提供 ``as_dict()``
便于 API 层返回，避免在路由里手写字段映射。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    def as_dict(self) -> dict[str, Any]:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ---------------- 用户与鉴权 ----------------

class UserProfile(Base):
    __tablename__ = "user_profile"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_city: Mapped[str | None] = mapped_column(String(128))
    level: Mapped[str | None] = mapped_column(String(16))  # P5/P6/P7/P8
    name_pinyin: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(128))
    chinese_name: Mapped[str | None] = mapped_column(String(64))
    id_type: Mapped[int | None] = mapped_column(Integer)
    id_number: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32))
    gender: Mapped[str | None] = mapped_column(String(4))


class UserAccount(Base):
    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password: Mapped[str] = mapped_column(String(128))
    real_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="USER")  # USER/ADMIN
    created_time: Mapped[datetime | None] = mapped_column(DateTime)


class UserApiKey(Base):
    __tablename__ = "user_api_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    api_key_enc: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------- 差旅单 / 审批 / 预订 ----------------

class TravelOrder(Base):
    __tablename__ = "travel_order"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    destination: Mapped[str | None] = mapped_column(String(256))
    departure_city: Mapped[str | None] = mapped_column(String(128))
    departure_date: Mapped[str | None] = mapped_column(String(32))
    return_date: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    approval_id: Mapped[str | None] = mapped_column(String(64))
    plan_html_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApprovalRecord(Base):
    __tablename__ = "approval_record"

    process_instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    approval_form: Mapped[Any | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(String(512))
    submit_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)
    order_id: Mapped[str | None] = mapped_column(String(64))


class BookingRecord(Base):
    __tablename__ = "booking_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64))
    travel_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    biz_type: Mapped[str] = mapped_column(String(16))  # FLIGHT/HOTEL/TRAIN/TICKET/CRUISE/VACATION
    platform: Mapped[str] = mapped_column(String(32))
    external_order_no: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    external_status: Mapped[str | None] = mapped_column(String(64))
    payment_status: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(256))
    total_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8), default="CNY")
    contact_name: Mapped[str | None] = mapped_column(String(64))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    start_time: Mapped[datetime | None] = mapped_column(DateTime)
    end_time: Mapped[datetime | None] = mapped_column(DateTime)
    detail: Mapped[Any | None] = mapped_column(JSON)
    remark: Mapped[str | None] = mapped_column(String(512))
    booked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(Integer, default=0)


# ---------------- 差旅政策 ----------------

class TravelPolicyRule(Base):
    __tablename__ = "travel_policy_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level_min: Mapped[int] = mapped_column(Integer)
    level_max: Mapped[int] = mapped_column(Integer)
    city_tier: Mapped[str] = mapped_column(String(16))  # 一线/新一线/其他
    flight_class: Mapped[str | None] = mapped_column(String(32))
    train_seat_class: Mapped[str | None] = mapped_column(String(16))
    hotel_limit: Mapped[float] = mapped_column(Float, default=0)
    hotel_star_limit: Mapped[int] = mapped_column(Integer, default=4)
    daily_meal_limit: Mapped[float] = mapped_column(Float, default=0)
    daily_transport_limit: Mapped[float] = mapped_column(Float, default=0)
    approval_threshold: Mapped[float] = mapped_column(Float, default=0)
    advance_booking_days: Mapped[int] = mapped_column(Integer, default=3)


# ---------------- 对话历史 ----------------

class ChatConversation(Base):
    __tablename__ = "chat_conversation"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256), default="新对话")
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(Integer, default=0)


class ChatMessage(Base):
    __tablename__ = "chat_message"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))  # user/agent/system
    content: Mapped[str | None] = mapped_column(Text)
    agent_name: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[Any | None] = mapped_column(JSON)
    feedback: Mapped[str | None] = mapped_column(String(16))
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[int] = mapped_column(Integer, default=0)


class UserPreference(Base):
    __tablename__ = "user_preference"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    pref_key: Mapped[str] = mapped_column(String(64))
    pref_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentScopeSession(Base):
    """对应 agentscope_session 表，保存各 Agent 多轮记忆与活跃 Agent 态。"""

    __tablename__ = "agentscope_session"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    state_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    item_index: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    state_data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentLongTermMemory(Base):
    """Durable user-scoped memory entries behind a replaceable memory provider."""

    __tablename__ = "agent_long_term_memory"

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    memory_type: Mapped[str] = mapped_column(String(32), default="preference")
    metadata_json: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class ToolExecutionRecord(Base):
    """幂等工具调用记录，跨 Worker 保存已完成或未决的写操作。"""

    __tablename__ = "tool_execution_record"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_name: Mapped[str | None] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    arguments_hash: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="IN_PROGRESS")
    result_json: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
