from backend.services.booking_cancellation import booking_cancellation_service
from backend.services.booking_service import booking_service

from ._common import current_user_id, tool


@tool
def query_booking_record(user_id: str | None = None, booking_id: str | None = None, travel_order_id: str | None = None, biz_type: str | None = None, status: str | None = None) -> list[dict]:
    """按用户租户隔离查询机票、酒店、火车票等预订记录。"""
    records = booking_service.list_by_user(current_user_id(user_id), biz_type, status)
    return [x for x in records if (not booking_id or x["booking_id"] == booking_id) and (not travel_order_id or x["travel_order_id"] == travel_order_id)]


@tool
def cancel_booking(booking_id: str, user_id: str | None = None, reason: str | None = None) -> dict:
    """取消预订；机票和火车票必须由平台成功确认后才更新本地状态。"""
    return booking_cancellation_service.cancel(current_user_id(user_id), booking_id, reason)


def read_tools():
    return [query_booking_record]


def write_tools():
    return [cancel_booking]


def tools():
    return read_tools() + write_tools()
