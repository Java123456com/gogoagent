"""Post-tool side effects for search, booking, and progress events."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from typing import Any, ClassVar

from backend.core.request_context import current_context
from backend.infrastructure.stores import search_candidate_store
from backend.observability import record_event
from backend.services.booking_service import booking_service
from backend.services.runtime_events import emit_event
from backend.services.travel_data import travel_data_normalizer

logger = logging.getLogger(__name__)


class ToolResultSideEffects:
    SEARCH_COMMANDS: ClassVar[dict[str, str]] = {
        "call flight searchLowestPriceFlight": "FLIGHT",
        "call train searchLowestPriceTrain": "TRAIN",
        "call hotel tuniuHotelSearch": "HOTEL",
    }
    BOOKING_COMMANDS: ClassVar[dict[str, tuple[str, str]]] = {
        "call flight saveOrder": ("CREATE", "FLIGHT"),
        "call hotel tuniuHotelCreateOrder": ("CREATE", "HOTEL"),
        "call train bookTrain": ("CREATE", "TRAIN"),
        "call flight cancelOrder": ("CANCEL", "FLIGHT"),
        "call train cancelOrder": ("CANCEL", "TRAIN"),
    }

    def process(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        """Run optional side effects without ever failing the agent tool call."""
        try:
            self._track_travel_order(tool_name, arguments, result)
            normalized = travel_data_normalizer.normalize(result)
            if normalized:
                normalized["items"] = normalized.get("items", [])[:5]
                emit_event("travel_data", normalized)
            if tool_name != "execute_shell_command":
                return
            command = str(arguments.get("command") or "")
            if not command:
                return
            self._capture_search(command, result)
            self._persist_booking(command, result)
        except Exception:
            logger.exception("Post-tool side effect failed: tool_name=%s", tool_name)

    def _capture_search(self, command: str, result: Any) -> None:
        search_type = next(
            (kind for marker, kind in self.SEARCH_COMMANDS.items() if marker in command),
            None,
        )
        if search_type is None:
            return
        business = _business_result(result)
        context = current_context()
        if business is None or context is None:
            return
        arguments = _command_arguments(command)
        field = _search_field(search_type, arguments)
        entry = {"type": search_type, "args": arguments, "data": business}
        old = search_candidate_store.get_entry(context.user_id, field)
        search_candidate_store.put(
            context.user_id,
            field,
            _merge_search_entries(old, entry, search_type) if old else entry,
        )

    def _persist_booking(self, command: str, result: Any) -> None:
        operation = next(
            (value for marker, value in self.BOOKING_COMMANDS.items() if marker in command),
            None,
        )
        if operation is None:
            return
        business = _business_result(result, require_business_success=True)
        context = current_context()
        if business is None or context is None:
            return
        action, biz_type = operation
        arguments = _command_arguments(command)
        if action == "CANCEL":
            # Compatibility for a successful legacy raw cancellation command only. The
            # public cancel_booking tool owns authorization and platform-first semantics.
            if business.get("success") is True or business.get("successCode") is True:
                self._cancel_booking(context.user_id, biz_type, arguments)
            return

        source = business.get("data") if isinstance(business.get("data"), dict) else business
        external_order_no = _first(
            source,
            "orderId",
            "orderNo",
            "mainOrderNo",
            "bookingOrderId",
            "order_id",
        )
        if not external_order_no:
            return
        existing = _find_booking(context.user_id, biz_type, external_order_no)
        if existing:
            return

        values = _booking_values(biz_type, arguments)
        values.update(
            {
                "conversation_id": context.session_id,
                "travel_order_id": context.travel_order_id,
                "biz_type": biz_type,
                "platform": "tuniu-cli",
                "external_order_no": external_order_no,
                "status": "CREATED",
                "payment_status": "UNPAID",
                "detail": {"args": arguments, "result": business},
            }
        )
        record = booking_service.create(context.user_id, values)
        record_event(
            "booking.persisted",
            booking_type=biz_type,
            result_class="success",
            external_result_code="ORDER_CREATED",
        )
        pay_url = _first(
            source,
            "payUrl",
            "paymentUrl",
            "cashierUrl",
            "orderDetailH5Url",
            "orderDetailUrl",
            "jumpUrl",
        )
        emit_event(
            "booking_result",
            {
                "type": "booking_result",
                "bizType": biz_type.lower(),
                "bizTypeLabel": {"FLIGHT": "机票", "HOTEL": "酒店", "TRAIN": "火车票"}.get(
                    biz_type,
                    "预订",
                ),
                "orderId": external_order_no,
                "title": record.get("title"),
                "amount": str(record["total_amount"])
                if record.get("total_amount") is not None
                else None,
                "currency": record.get("currency"),
                "payUrl": pay_url,
                "statusLabel": "待支付",
            },
        )

    @staticmethod
    def _cancel_booking(user_id: str, biz_type: str, arguments: dict[str, Any]) -> None:
        external_order_no = _first(arguments, "orderId", "order_id", "orderNo")
        if not external_order_no:
            return
        existing = _find_booking(user_id, biz_type, external_order_no)
        if existing and existing.get("status") != "CANCELLED":
            booking_service.cancel(existing["booking_id"])

    @staticmethod
    def _track_travel_order(
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> None:
        if tool_name not in {"check_travel_order_approval", "query_travel_order_by_order_id"}:
            return
        context = current_context()
        order_id = arguments.get("order_id")
        if context is None or not order_id:
            return
        if tool_name == "check_travel_order_approval":
            parsed = _parse_json_value(result)
            if isinstance(parsed, dict) and parsed.get("approved") is not True:
                return
        context.travel_order_id = str(order_id)


def build_search_candidates(
    user_id: str,
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str,
) -> dict[str, list[dict[str, Any]]] | None:
    """Convert the five cached search fields into planner transport/hotel options."""
    entries = search_candidate_store.load_by_trip(
        user_id,
        origin,
        destination,
        departure_date,
        return_date,
    )
    transports: list[dict[str, Any]] = []
    hotels: list[dict[str, Any]] = []
    for field in sorted(entries):
        entry = entries[field]
        kind, arguments, data = entry.get("type"), entry.get("args") or {}, entry.get("data") or {}
        if kind == "FLIGHT":
            for item in data.get("data") or []:
                base = _number(item.get("basePrice"))
                tax = _number(item.get("totalTax"))
                transports.append(
                    {
                        "id": _candidate_id(
                            "F",
                            item,
                            "flightNumber",
                            "departureTime",
                            "cabinClass",
                        ),
                        "type": "flight",
                        "departure_time": item.get("departureTime"),
                        "arrival_time": item.get("arrivalTime"),
                        "origin": arguments.get("departureCityName")
                        or item.get("departureAirport"),
                        "destination": arguments.get("arrivalCityName")
                        or item.get("arrivalAirport"),
                        "price": round(base + tax, 2),
                        "cabin_class": item.get("cabinClass"),
                        "is_direct": item.get("type") == "直飞",
                        "refund_policy": "不可退",
                        "carrier": item.get("airlineCompany"),
                        "code": item.get("flightNumber"),
                    }
                )
        elif kind == "TRAIN":
            for item in data.get("data") or []:
                prices = item.get("price") or {}
                availability = item.get("seatAvailable") or {}
                for code, label in (("edz", "二等座"), ("ydz", "一等座"), ("swz", "商务座")):
                    price = prices.get(f"{code}Price")
                    if price in (None, "") or availability.get(f"{code}Num") == 0:
                        continue
                    transports.append(
                        {
                            "id": _candidate_id("T", item, "trainNum", "departureTime")
                            + f"_{code}",
                            "type": "train",
                            "departure_time": item.get("departureTime"),
                            "arrival_time": item.get("arrivalTime"),
                            "origin": arguments.get("departureCityName")
                            or item.get("departStationName"),
                            "destination": arguments.get("arrivalCityName")
                            or item.get("destStationName"),
                            "price": round(_number(price), 2),
                            "cabin_class": label,
                            "is_direct": item.get("trainType") == "direct",
                            "refund_policy": "可退",
                            "carrier": item.get("trainNum"),
                            "code": item.get("trainNum"),
                        }
                    )
        elif kind == "HOTEL":
            nights = _nights(arguments, departure_date, return_date)
            for item in data.get("hotels") or []:
                hotels.append(
                    {
                        "id": _candidate_id("H", item, "hotelId", "hotelName", "roomName"),
                        "name": item.get("hotelName"),
                        "brand": item.get("brandName"),
                        "star_rating": _star_rating(item.get("starName")),
                        "room_type": item.get("roomName"),
                        "price_per_night": _number(item.get("lowestPrice")),
                        "nights": nights,
                        "distance_to_dest_km": 0,
                        "breakfast_included": "早餐" in str(item.get("meal") or ""),
                        "cancel_policy": item.get("refund"),
                    }
                )
    return (
        {"transport_options": transports, "hotel_options": hotels} if transports or hotels else None
    )


def _candidate_id(prefix: str, item: dict[str, Any], *keys: str) -> str:
    identity = "|".join(str(item.get(key) or "") for key in keys)
    if not identity.strip("|"):
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:12]}"


def _business_result(result: Any, *, require_business_success: bool = False) -> dict[str, Any] | None:
    """Validate the shell envelope and unwrap existing Tuniu/MCP response shapes.

    A platform order is durable only after both the shell and Tuniu outer envelope
    explicitly succeeded.  Create/cancel paths additionally require an explicit
    business success signal, preventing error payloads from being persisted.
    """
    if not isinstance(result, dict) or result.get("ok") is not True or result.get("exit_code") != 0:
        return None
    parsed = _parse_json_value(result.get("stdout"))
    if not isinstance(parsed, dict) or parsed.get("success") is not True or parsed.get("isError") is True:
        return None

    value = parsed.get("structuredContent")
    if isinstance(value, dict):
        if value.get("isError") is True:
            return None
        value = value.get("result", value)
    else:
        value = parsed.get("result", parsed.get("data"))
    business = _unwrap_business(value)
    if not isinstance(business, dict) or _explicit_failure(business):
        return None
    if require_business_success and not _explicit_success(business):
        return None
    return business


def _parse_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    text = str(value).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _unwrap_business(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        return {"data": value}
    if not isinstance(value, dict) or value.get("isError") is True:
        return None
    if _explicit_failure(value):
        return None
    structured = value.get("structuredContent")
    if isinstance(structured, dict):
        return _unwrap_business(structured.get("result", structured))
    content = value.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") not in (None, "text"):
                continue
            parsed = _parse_json_value(item.get("text"))
            if isinstance(parsed, dict):
                return _unwrap_business(parsed)
        return None
    nested = value.get("result")
    if isinstance(nested, (dict, list)):
        return _unwrap_business(nested)
    return value


def _explicit_failure(value: dict[str, Any]) -> bool:
    return value.get("isError") is True or value.get("success") is False or value.get("successCode") is False


def _explicit_success(value: dict[str, Any]) -> bool:
    return value.get("success") is True or value.get("successCode") is True


def _find_booking(user_id: str, biz_type: str, external_order_no: str) -> dict[str, Any] | None:
    finder = getattr(booking_service, "find_by_user_platform_type_external_order", None)
    if callable(finder):
        return finder(user_id, "tuniu-cli", biz_type, external_order_no)
    # Compatibility with lightweight test doubles and custom service adapters.
    return next(
        (item for item in booking_service.list_by_user(user_id, biz_type)
         if item.get("platform") == "tuniu-cli"
         and item.get("external_order_no") == external_order_no),
        None,
    )


def _command_arguments(command: str) -> dict[str, Any]:
    start, end = command.find("{"), command.rfind("}")
    if start < 0 or end <= start:
        return {}
    parsed = _parse_json_value(command[start : end + 1])
    return parsed if isinstance(parsed, dict) else {}


def _search_field(kind: str, arguments: dict[str, Any]) -> str:
    if kind in {"FLIGHT", "TRAIN"}:
        values = (
            str(arguments.get("departureCityName") or "unknown").strip(),
            str(arguments.get("arrivalCityName") or "unknown").strip(),
            str(arguments.get("departureDate") or "unknown").strip(),
        )
        factory = (
            search_candidate_store.flight_field
            if kind == "FLIGHT"
            else search_candidate_store.train_field
        )
        return factory(*values)
    return search_candidate_store.hotel_field(
        str(arguments.get("cityName") or "unknown").strip(),
        str(arguments.get("checkInDate") or arguments.get("checkIn") or "unknown").strip(),
        str(arguments.get("checkOutDate") or arguments.get("checkOut") or "unknown").strip(),
    )


def _merge_search_entries(
    old: dict[str, Any],
    new: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    field = "hotels" if kind == "HOTEL" else "data"
    old_items = (old.get("data") or {}).get(field) or []
    new_items = (new.get("data") or {}).get(field) or []
    deduplicated: dict[str, dict[str, Any]] = {}
    for item in [*old_items, *new_items]:
        if not isinstance(item, dict):
            continue
        if kind == "FLIGHT" and item.get("flightNumber") and item.get("departureTime"):
            key = f"{item['flightNumber']}|{item['departureTime']}"
        elif kind == "TRAIN" and item.get("trainNum") and item.get("departureTime"):
            key = f"{item['trainNum']}|{item['departureTime']}"
        elif kind == "HOTEL" and item.get("hotelId"):
            key = str(item["hotelId"])
        elif kind == "HOTEL" and item.get("hotelName"):
            key = f"name_{item['hotelName']}"
        else:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        deduplicated[key] = item
    merged_data = {**(new.get("data") or {}), field: list(deduplicated.values())}
    return {**new, "data": merged_data}


def _booking_values(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {"currency": "CNY"}
    if kind == "FLIGHT":
        route = "→".join(
            filter(
                None,
                (
                    arguments.get("departureCityName"),
                    arguments.get("arrivalCityName"),
                ),
            )
        )
        values.update(
            {
                "title": " ".join(filter(None, (route, arguments.get("flightNo")))) or None,
                "start_time": _date_time(arguments.get("departureDate")),
            }
        )
        contact = arguments.get("contactTourist") or {}
        values.update({"contact_name": contact.get("name"), "contact_phone": contact.get("mobile")})
    elif kind == "HOTEL":
        values.update(
            {
                "title": "酒店预订",
                "start_time": _date_time(arguments.get("checkInDate")),
                "end_time": _date_time(arguments.get("checkOutDate")),
                "contact_name": arguments.get("contactName"),
                "contact_phone": arguments.get("contactPhone"),
            }
        )
    elif kind == "TRAIN":
        resources = arguments.get("resources") or []
        first = resources[0] if resources and isinstance(resources[0], dict) else {}
        contact = arguments.get("contact") or {}
        values.update(
            {
                "title": "火车票预订",
                "start_time": _date_time(first.get("departsDate")),
                "total_amount": _number(first.get("adultPrice")) or None,
                "contact_phone": contact.get("tel"),
            }
        )
    return values


def _first(mapping: dict[str, Any], *keys: str) -> str | None:
    return next((str(mapping[key]) for key in keys if mapping.get(key) not in (None, "")), None)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _date_time(value: Any) -> datetime | None:
    try:
        return datetime.combine(date.fromisoformat(str(value).strip()), datetime.min.time())
    except (TypeError, ValueError):
        return None


def _nights(arguments: dict[str, Any], departure: str, returning: str) -> int:
    check_in = arguments.get("checkInDate") or arguments.get("checkIn") or departure
    check_out = arguments.get("checkOutDate") or arguments.get("checkOut") or returning
    try:
        return max(1, (date.fromisoformat(str(check_out)) - date.fromisoformat(str(check_in))).days)
    except ValueError:
        return 1


def _star_rating(value: Any) -> int:
    text = str(value or "")
    return 5 if "豪华" in text else 4 if "高档" in text else 2 if "经济" in text else 3


tool_result_side_effects = ToolResultSideEffects()
