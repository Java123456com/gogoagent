from datetime import date, datetime
from typing import Any

from backend.infrastructure.repositories import approval_repository, travel_order_repository
from backend.services.order_service import travel_order_service

from ._common import current_user_id, tool
from .conflict import build_conflict_report


_ACTIVE_ORDER_STATUSES = {"DRAFT", "SUBMITTED", "APPROVED"}
_BLOCKING_CONFLICTS = {"HIGH", "MEDIUM"}


@tool
def query_travel_order(origin: str, destination: str, departure_date: str,
                       user_id: str | None = None) -> dict:
    """按出发地、目的地和出发日期精确查询一条差旅单。"""
    resolved = current_user_id(user_id)
    rows = travel_order_repository.list_by_user(resolved)
    row = next((item for item in rows
                if item.departure_city == origin
                and item.destination == destination
                and item.departure_date == departure_date), None)
    return row.as_dict() if row else {"error": "order not found"}


@tool
def query_travel_order_by_order_id(order_id: str, user_id: str | None = None) -> dict:
    """根据差旅单号查询指定差旅单详情，并校验当前用户归属。"""
    resolved = current_user_id(user_id)
    row = travel_order_repository.get(order_id)
    if row is None or row.user_id != resolved:
        return {"error": "order not found"}
    return row.as_dict()


@tool
def query_travel_orders(status: str | None = None, start_date: str | None = None,
                        end_date: str | None = None, user_id: str | None = None) -> list[dict]:
    """查询当前用户差旅单列表，支持状态和出发/返程日期范围过滤。"""
    rows = travel_order_repository.list_by_user(current_user_id(user_id))
    statuses = {item.strip() for item in (status or "").split(",") if item.strip()}
    return [x.as_dict() for x in rows
            if (not statuses or x.status in statuses)
            and (not start_date or (x.departure_date or "") >= start_date)
            and (not end_date or (x.return_date or "") <= end_date)]


@tool
def query_approval_status(process_instance_id: str | None = None,
                          user_id: str | None = None) -> dict:
    """按审批实例 ID 查询，未传 ID 时查询当前用户最近一条审批。"""
    resolved = current_user_id(user_id)
    row = (approval_repository.get(process_instance_id)
           if process_instance_id else approval_repository.find_latest_by_user_id(resolved))
    if row is None or row.user_id != resolved:
        return {"found": False}
    result = row.as_dict()
    result["query_mode"] = "by_instance" if process_instance_id else "latest"
    return result


@tool
def check_travel_time_validity(order_id: str, user_id: str | None = None) -> dict:
    """检查指定差旅单是否尚未开始或结束，供规划前置校验。"""
    resolved = current_user_id(user_id)
    row = travel_order_repository.get(order_id)
    today = datetime.now().astimezone().date()
    result = {"today": today.isoformat(), "blocked_orders": []}
    if row is None or row.user_id != resolved:
        result.update({"valid": True, "message": "当前尚不存在差旅单，但是仍可直接规划行程。"})
        return result
    reason = None
    try:
        if row.return_date and today > date.fromisoformat(row.return_date):
            reason = f"行程已结束（返回日期 {row.return_date} 已过）"
        elif row.departure_date and today >= date.fromisoformat(row.departure_date):
            reason = f"行程已开始（出发日期 {row.departure_date} 已到达或已过）"
    except ValueError:
        reason = None
    if reason:
        result["blocked_orders"] = [{"order_id": row.order_id, "destination": row.destination,
                                      "departure_date": row.departure_date,
                                      "return_date": row.return_date, "reason": reason}]
        result.update({"valid": False, "message": f"检测到{reason}的差旅单，无法进行规划。请停止规划。"})
    else:
        result.update({"valid": True, "message": "差旅单日期有效，可继续规划。"})
    return result


@tool
def check_travel_order_approval(order_id: str, user_id: str | None = None) -> dict:
    """检查指定差旅单是否已审批通过，供 BookingAgent 预订前置校验。"""
    user_id = current_user_id(user_id)
    order = travel_order_repository.get(order_id)
    if order is None or order.user_id != user_id:
        return {"approved": False, "message": "未查询到您需要预订的差旅单。"}
    approved = order.status == "APPROVED"
    return {"approved": approved, "order_id": order.order_id,
            "destination": order.destination, "departure_date": order.departure_date,
            "return_date": order.return_date,
            "message": "存在已审批差旅单，可以执行预订。" if approved
                       else "您想要预订的差旅尚未完成审批，请先联系管理员审批通过。"}


@tool
def submit_travel_approval(destination: str, departure_city: str, departure_date: str,
                           return_date: str, purpose: str, user_id: str | None = None,
                           ignore_conflicts: bool = False) -> dict:
    """创建差旅单并提交审批，状态从 DRAFT 原子变更为 SUBMITTED。"""
    resolved = current_user_id(user_id)
    values = {"destination": destination, "departure_city": departure_city,
              "departure_date": departure_date, "return_date": return_date, "purpose": purpose}
    duplicate = _matching_active_order(resolved, values)
    if duplicate is not None:
        return {"success": True, "idempotent": True, "order": duplicate.as_dict(),
                "conflictReport": None}

    report = _conflict_report_or_error(resolved, values)
    if report.get("check_status") != "SUCCESS":
        return _conflict_check_failed(report)
    if _requires_conflict_confirmation(report, ignore_conflicts):
        return _conflict_confirmation_required(report)

    record = travel_order_service.create_and_submit(resolved, values)
    return {**record.as_dict(), "success": True, "conflictReport": report}


@tool
def cancel_travel_order(
    order_id: str, user_id: str | None = None, reason: str | None = None, force: bool = False
) -> dict:
    """取消差旅单并同步撤销关联审批；已通过订单需 force=true。"""
    user_id = current_user_id(user_id)
    order = travel_order_repository.get(order_id)
    if not order or order.user_id != user_id:
        return {"success": False, "error": "订单不存在"}
    if order.status == "APPROVED" and not force: return {"success": False, "requires_confirmation": True, "error": "已通过订单取消需要确认"}
    return {"success": True, "outcome": travel_order_service.cancel_with_approval(order).__dict__, "reason": reason}


@tool
def modify_travel_order(
    order_id: str,
    user_id: str | None = None,
    destination: str | None = None,
    departure_city: str | None = None,
    departure_date: str | None = None,
    return_date: str | None = None,
    purpose: str | None = None,
    force: bool = False,
    ignore_conflicts: bool = False,
) -> dict:
    """修改差旅单并撤销旧审批后重新提交；已通过订单需 force=true。"""
    user_id = current_user_id(user_id)
    order = travel_order_repository.get(order_id)
    if order is None or order.user_id != user_id:
        return {"success": False, "error": "订单不存在"}
    if order.status == "APPROVED" and not force:
        return {"success": False, "requires_confirmation": True, "error": "已通过订单修改需要确认"}
    values = {
        key: value for key, value in {
            "destination": destination, "departure_city": departure_city,
            "departure_date": departure_date, "return_date": return_date, "purpose": purpose,
        }.items() if value is not None
    }
    if not values:
        return {"success": True, "idempotent": True, "noChanges": True,
                "order": order.as_dict(), "conflictReport": None}

    candidate = {
        "destination": values.get("destination", order.destination),
        "departure_city": values.get("departure_city", order.departure_city),
        "departure_date": values.get("departure_date", order.departure_date),
        "return_date": values.get("return_date", order.return_date),
        "purpose": values.get("purpose", order.purpose),
    }
    report = _conflict_report_or_error(user_id, candidate, exclude_order_id=order.order_id)
    if report.get("check_status") != "SUCCESS":
        return _conflict_check_failed(report)
    if _requires_conflict_confirmation(report, ignore_conflicts):
        return _conflict_confirmation_required(report)
    outcome = travel_order_service.modify_and_resubmit(order, values)
    return {"success": True, "old_approval_id": outcome.old_approval_id,
            "old_approval_cancelled": outcome.old_approval_cancelled,
            "new_approval": outcome.new_approval.as_dict(), "conflictReport": report}


def _matching_active_order(user_id: str, values: dict[str, Any]):
    for order in travel_order_repository.list_by_user(user_id):
        if order.status not in _ACTIVE_ORDER_STATUSES:
            continue
        if all(getattr(order, key) == value for key, value in values.items()):
            return order
    return None


def _conflict_report_or_error(user_id: str, values: dict[str, Any],
                              exclude_order_id: str | None = None) -> dict[str, Any]:
    return build_conflict_report(
        user_id=user_id,
        departure_city=values.get("departure_city"),
        destination=values.get("destination"),
        departure_date=values.get("departure_date"),
        return_date=values.get("return_date"),
        exclude_order_id=exclude_order_id,
    )


def _requires_conflict_confirmation(report: dict[str, Any], ignore_conflicts: bool) -> bool:
    return not ignore_conflicts and any(
        item.get("severity") in _BLOCKING_CONFLICTS for item in report.get("conflicts", [])
    )


def _highest_severity(report: dict[str, Any]) -> str | None:
    for severity in ("HIGH", "MEDIUM", "LOW"):
        if any(item.get("severity") == severity for item in report.get("conflicts", [])):
            return severity
    return None


def _conflict_confirmation_required(report: dict[str, Any]) -> dict[str, Any]:
    highest = _highest_severity(report)
    return {"success": False, "needConfirm": True, "errorCode": "TRAVEL_CONFLICT",
            "highestSeverity": highest, "conflictReport": report,
            "message": f"检测到 {highest} 级差旅冲突，请向用户展示冲突明细并取得明确确认后再提交。"}


def _conflict_check_failed(report: dict[str, Any]) -> dict[str, Any]:
    return {"success": False, "needConfirm": False, "errorCode": "CONFLICT_CHECK_FAILED",
            "highestSeverity": None, "conflictReport": report,
            "message": report.get("summary") or "冲突检查失败，未执行写入。"}


def read_tools():
    """TravelOrderReadTools equivalent used by Plan/Booking/Manage agents."""
    return [query_travel_order, query_travel_order_by_order_id, query_travel_orders,
            query_approval_status, check_travel_time_validity, check_travel_order_approval]


def write_tools():
    """TravelOrderWriteTools equivalent used only by ItineraryManageAgent."""
    return [submit_travel_approval, cancel_travel_order, modify_travel_order]


def tools():
    return read_tools() + write_tools()
