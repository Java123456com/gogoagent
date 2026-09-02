"""API 请求和响应 DTO。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------- 鉴权 ----------------
class LoginRequest(BaseModel):
    username: str
    password: str


# ---------------- 对话 ----------------
class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatRespondRequest(BaseModel):
    """Human-in-the-Loop：用户对 ask_user 的回复。"""

    response: Any = None


# ---------------- 差旅单 ----------------
class OrderRequest(BaseModel):
    departure_city: str | None = None
    destination: str
    departure_date: str | None = None
    return_date: str | None = None
    purpose: str | None = None


class BookingRequest(BaseModel):
    biz_type: str
    title: str | None = None
    total_amount: float | None = None
    platform: str = "mock"
    external_order_no: str | None = None
    payment_status: str = "PENDING"


class ApprovalDecisionRequest(BaseModel):
    decision: str  # agree / refuse
    remark: str | None = None
