"""差旅单与审批单写操作事务服务。

将「差旅单 + 审批单」多步写操作编排为原子流程：创建并提交 / 取消 / 修改重提 / 审批决策。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.infrastructure.repositories import (
    approval_repository,
    booking_repository,
    travel_order_repository,
)

STATUS_LABELS = {"DRAFT": "草稿", "SUBMITTED": "审批中", "APPROVED": "已通过",
                 "REJECTED": "已驳回", "COMPLETED": "已完成", "CANCELLED": "已取消"}
BOOKING_LABELS = {"FLIGHT": "机票", "HOTEL": "酒店", "TRAIN": "火车票", "TICKET": "门票",
                  "CRUISE": "邮轮", "VACATION": "度假产品"}
BOOKING_STATUS_LABELS = {
    "CREATED": "已创建", "UNPAID": "待支付", "PAID": "已支付", "ISSUED": "已出票",
    "COMPLETED": "已完成", "CANCELLED": "已取消", "FAILED": "失败",
}


class TravelOrderStatus:
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass
class CancelOutcome:
    order_cancelled: bool
    approval_cancelled: bool
    cancelled_approval_id: str | None = None


@dataclass
class ModifyOutcome:
    old_approval_id: str | None
    old_approval_cancelled: bool
    new_approval: Any


class TravelOrderService:
    def create_and_submit(self, user_id: str, values: dict[str, Any]):
        """① 差旅单落库(DRAFT) → ② 提交审批(PENDING) → ③ 差旅单升级 SUBMITTED 并回写审批ID。"""
        values = {**values, "status": TravelOrderStatus.DRAFT}
        order = travel_order_repository.create(user_id, values)

        form = self._build_approval_form(order)
        title = self._build_title(order)
        record = approval_repository.create(user_id, order.order_id, title, form)

        travel_order_repository.update(order.order_id, {
            "approval_id": record.process_instance_id,
            "status": TravelOrderStatus.SUBMITTED,
        })
        return record

    def cancel_with_approval(self, order) -> CancelOutcome:
        order_id = order.order_id
        travel_order_repository.update_status(order_id, TravelOrderStatus.CANCELLED)

        approval_cancelled = False
        cancelled_approval_id = None
        if order.approval_id:
            approval_cancelled = approval_repository.cancel(order.approval_id)
            cancelled_approval_id = order.approval_id
        else:
            latest = approval_repository.find_latest_by_order_id(order_id)
            if latest and latest.status != "CANCELLED":
                approval_cancelled = approval_repository.cancel(latest.process_instance_id)
                cancelled_approval_id = latest.process_instance_id
        return CancelOutcome(True, approval_cancelled, cancelled_approval_id)

    def modify_and_resubmit(self, order, new_values: dict[str, Any]) -> ModifyOutcome:
        old_approval_id = order.approval_id
        old_approval_cancelled = False
        if old_approval_id:
            old_approval_cancelled = approval_repository.cancel(old_approval_id)
        else:
            latest = approval_repository.find_latest_by_order_id(order.order_id)
            if latest:
                old_approval_id = latest.process_instance_id
                old_approval_cancelled = approval_repository.cancel(latest.process_instance_id)

        travel_order_repository.update(order.order_id, {
            **new_values, "status": TravelOrderStatus.DRAFT, "approval_id": None,
        })
        refreshed = travel_order_repository.get(order.order_id)

        form = self._build_approval_form(refreshed)
        title = self._build_title(refreshed)
        new_record = approval_repository.create(refreshed.user_id, refreshed.order_id, title, form)

        travel_order_repository.update(order.order_id, {
            "approval_id": new_record.process_instance_id,
            "status": TravelOrderStatus.SUBMITTED,
        })
        return ModifyOutcome(old_approval_id, old_approval_cancelled, new_record)

    def decide_and_sync_order(self, process_instance_id: str, agree: bool, remark: str | None):
        status = "APPROVED" if agree else "REJECTED"
        record = approval_repository.decide(process_instance_id, status, remark)
        if record is None:
            return None
        if record.order_id:
            travel_order_repository.update_status(record.order_id, status)
        return record

    def list_user_orders(self, user_id: str) -> list[dict[str, Any]]:
        result = []
        for order in travel_order_repository.list_by_user(user_id):
            approval = approval_repository.find_latest_by_order_id(order.order_id)
            bookings = booking_repository.list_by_user(user_id)
            bookings = [b for b in bookings if b.travel_order_id == order.order_id]
            result.append({
                "orderId": order.order_id,
                "destination": order.destination,
                "departureCity": order.departure_city,
                "departureDate": order.departure_date,
                "returnDate": order.return_date,
                "purpose": order.purpose,
                "status": order.status,
                "statusLabel": STATUS_LABELS.get(order.status, order.status),
                "createdAt": _millis(order.created_at),
                "updatedAt": _millis(order.updated_at),
                "international": _is_international(order.destination),
                "approvalId": approval.process_instance_id if approval else None,
                "approvalStatus": approval.status if approval else None,
                "approvalStatusLabel": _approval_status_label(approval.status) if approval else None,
                "approvalRemark": approval.remark if approval else None,
                "approvalSubmitTime": _millis(approval.submit_time) if approval else None,
                "approvalUpdateTime": _millis(approval.update_time) if approval else None,
                "bookingCount": len(bookings),
                "bookings": [self._booking(b) for b in bookings],
                "planHtmlUrl": order.plan_html_url,
            })
        return result

    # ---------------- 私有 ----------------

    @staticmethod
    def _booking(b) -> dict[str, Any]:
        return {"bookingId": b.booking_id, "bizType": b.biz_type,
                "bizTypeLabel": BOOKING_LABELS.get(b.biz_type, b.biz_type),
                "title": b.title, "status": b.status,
                "statusLabel": BOOKING_STATUS_LABELS.get(b.status, b.status),
                "externalStatus": b.external_status,
                "totalAmount": str(b.total_amount) if b.total_amount is not None else None,
                "currency": b.currency, "platform": b.platform,
                "externalOrderNo": b.external_order_no, "paymentStatus": b.payment_status,
                "startTime": _millis(b.start_time), "endTime": _millis(b.end_time),
                "bookedAt": _millis(b.booked_at), "orderUrl": _order_url(b.platform, b.external_order_no)}

    @staticmethod
    def _build_approval_form(order) -> dict[str, Any]:
        return {"orderId": order.order_id, "userId": order.user_id, "purpose": order.purpose,
                "destination": order.destination, "departureCity": order.departure_city,
                "departureDate": order.departure_date, "returnDate": order.return_date}

    @staticmethod
    def _build_title(order) -> str:
        return f"{order.departure_city or '?'}→{order.destination or '?'}-{order.departure_date or '?'}~{order.return_date or '?'}"


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _millis(dt) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def _approval_status_label(status: str | None) -> str | None:
    return {"PENDING": "待审批", "APPROVED": "已通过", "REJECTED": "已拒绝", "CANCELLED": "已撤销"}.get(
        status, status,
    ) if status else None


def _order_url(platform: str | None, order_no: str | None) -> str | None:
    if not platform or not order_no:
        return None
    return {
        "tuniu": f"https://www.tuniu.com/order/detail/{order_no}",
        "rolling-go-hotel": f"https://hotel.rolling-go.com/order/{order_no}",
        "flight-manager": f"https://flight.rolling-go.com/order/{order_no}",
    }.get(platform.lower())


_INTERNATIONAL_KEYWORDS = (
    "日本", "韩国", "新加坡", "泰国", "马来西亚", "美国", "英国", "法国", "德国",
    "澳大利亚", "加拿大", "越南", "菲律宾", "印度", "巴西", "东京", "首尔", "曼谷",
    "新加坡", "纽约", "伦敦", "巴黎", "洛杉矶", "Sydney", "Singapore",
)


def _is_international(destination: str | None) -> bool:
    if not destination:
        return False
    text = str(destination).strip()
    if any(keyword.lower() in text.lower() for keyword in _INTERNATIONAL_KEYWORDS):
        return True
    return any(char.isalpha() and not ("\u4e00" <= char <= "\u9fff") for char in text)


travel_order_service = TravelOrderService()
