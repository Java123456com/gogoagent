"""预订记录服务（对应 Java BookingRecord + BookingPersistenceHook 落库）。"""
from __future__ import annotations

from typing import Any

from backend.infrastructure.repositories import booking_repository


class BookingService:
    def create(self, user_id: str, values: dict[str, Any]):
        return booking_repository.create(user_id, values).as_dict()

    def list_by_user(self, user_id: str, biz_type: str | None = None,
                     status: str | None = None) -> list[dict[str, Any]]:
        return [b.as_dict() for b in booking_repository.list_by_user(user_id, biz_type, status)]

    def get_by_id(self, booking_id: str) -> dict[str, Any] | None:
        record = booking_repository.find_by_booking_id(booking_id)
        return record.as_dict() if record else None

    def find_by_user_platform_type_external_order(
        self, user_id: str, platform: str, biz_type: str, external_order_no: str,
    ) -> dict[str, Any] | None:
        record = booking_repository.find_by_user_platform_type_external_order(
            user_id, platform, biz_type, external_order_no,
        )
        return record.as_dict() if record else None

    def cancel(self, booking_id: str, reason: str | None = None) -> dict[str, Any] | None:
        values: dict[str, Any] = {"status": "CANCELLED"}
        if reason and reason.strip():
            values["remark"] = reason.strip()
        record = booking_repository.update(booking_id, values)
        return record.as_dict() if record else None

    def delete(self, booking_id: str, user_id: str) -> bool:
        return booking_repository.delete_by_user(booking_id, user_id)


booking_service = BookingService()
