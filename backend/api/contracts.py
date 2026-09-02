from pydantic import BaseModel, Field


class LoginBody(BaseModel):
    username: str
    password: str

class ChatBody(BaseModel):
    message: str = Field(min_length=1)

class ApprovalDecisionBody(BaseModel):
    decision: str
    remark: str | None = None

class OrderBody(BaseModel):
    departure_city: str | None = None
    destination: str
    departure_date: str | None = None
    return_date: str | None = None
    purpose: str | None = None
    plan_html: str | None = None

class BookingBody(BaseModel):
    biz_type: str
    title: str | None = None
    total_amount: str | None = None
    platform: str = "mock"
    external_order_no: str | None = None
    payment_status: str = "PENDING"
