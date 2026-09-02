"""Authoritative, platform-first cancellation workflow for booking records."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from backend.infrastructure.security import mask_sensitive
from backend.services.api_key_service import api_key_service
from backend.services.booking_service import booking_service
from backend.tools.skills import execute_shell_command

logger = logging.getLogger(__name__)

_PLATFORM_COMMANDS = {
    "FLIGHT": "flight cancelOrder",
    "TRAIN": "train cancelOrder",
}
_SAFE_EXTERNAL_ORDER_NO = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _error(error_code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error_code": error_code, "message": message}


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _business_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Read current MCP wrapping and legacy direct business payloads."""
    result = payload.get("result")
    if isinstance(result, dict):
        if result.get("isError") is True:
            return None
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            parsed = _json_object(structured.get("result"))
            if parsed is not None:
                return parsed
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            parsed = _json_object(content[0].get("text"))
            if parsed is not None:
                return parsed
        if "success" in result or "successCode" in result:
            return result
    return payload


def _platform_message(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "平台响应格式异常"
    for key in ("msg", "errorMessage", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return mask_sensitive(value.strip())
    return "平台返回取消失败（未提供具体原因）"


class BookingCancellationService:
    def cancel(self, user_id: str, booking_id: str, reason: str | None = None) -> dict[str, Any]:
        normalized_booking_id = str(booking_id or "").strip()
        if not normalized_booking_id:
            return _error("INVALID_PARAM", "booking_id 不能为空")

        record = booking_service.get_by_id(normalized_booking_id)
        if record is None:
            return _error("BOOKING_NOT_FOUND", "预订记录不存在")
        if record.get("user_id") != user_id:
            return _error("PERMISSION_DENIED", "无权操作该预订记录，归属用户不匹配。")
        biz_type = str(record.get("biz_type") or "").upper()
        if record.get("status") == "CANCELLED":
            return self._success(record, False, "该预订记录已处于取消状态，无需重复取消。")

        if biz_type in _PLATFORM_COMMANDS:
            outcome = self._cancel_on_platform(user_id, biz_type, record.get("external_order_no"))
            if not outcome[0]:
                logger.warning("Booking platform cancellation failed: booking_id=%s biz_type=%s", normalized_booking_id, biz_type)
                return _error("PLATFORM_CANCEL_FAILED", "外部平台取消失败，内部预订状态未变更。原因：" + outcome[1])
            platform_cancelled = True
            message = "预订已取消。平台取消请求已发送。"
        else:
            platform_cancelled = False
            message = (
                "酒店预订暂不支持线上自动取消，已标记内部状态为已取消，请联系酒店前台处理。"
                if biz_type == "HOTEL" else "该业务类型暂不支持自动取消，已标记内部状态为已取消。"
            )

        updated = booking_service.cancel(normalized_booking_id, reason)
        if updated is None:
            return _error("BOOKING_UPDATE_FAILED", "取消状态保存失败，请稍后重试。")
        logger.info("Booking cancelled: booking_id=%s biz_type=%s platform_cancelled=%s", normalized_booking_id, biz_type, platform_cancelled)
        return self._success(updated, platform_cancelled, message)

    def _cancel_on_platform(self, user_id: str, biz_type: str, external_order_no: Any) -> tuple[bool, str]:
        if not api_key_service.get(user_id, "tuniu-cli"):
            return False, "用户未配置途牛 API Key，无法调用平台取消接口。"
        order_no = str(external_order_no or "").strip()
        if not _SAFE_EXTERNAL_ORDER_NO.fullmatch(order_no):
            return False, "外部平台订单号缺失或格式异常。"
        command = f"tuniu call {_PLATFORM_COMMANDS[biz_type]} -a '{{\"orderId\":\"{order_no}\"}}'"
        try:
            response = execute_shell_command.invoke({
                "command": command,
                "timeout_seconds": 30,
                "user_id": user_id,
            })
        except Exception:
            logger.exception("Booking platform cancellation command failed: biz_type=%s", biz_type)
            return False, "平台取消接口调用异常，请稍后重试。"
        if not isinstance(response, dict) or response.get("ok") is not True or response.get("exit_code") not in (0, None):
            return False, "平台接口调用异常或超时，请稍后重试。"
        payload = _business_payload(_json_object(response.get("stdout")) or {})
        if payload and (payload.get("success") is True or payload.get("successCode") is True):
            return True, "平台取消请求已发送。"
        return False, _platform_message(payload)

    @staticmethod
    def _success(record: dict[str, Any], platform_cancelled: bool, message: str) -> dict[str, Any]:
        return {
            "success": True,
            "booking_id": record.get("booking_id"),
            "biz_type": record.get("biz_type"),
            "new_status": "CANCELLED",
            "platform_cancelled": platform_cancelled,
            "message": message,
        }


booking_cancellation_service = BookingCancellationService()
