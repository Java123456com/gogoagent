from __future__ import annotations

import json

from backend.core.request_context import bind_context
from backend.infrastructure.stores import search_candidate_store
from backend.services import tool_result_side_effects as side_effect_module
from backend.services.runtime_events import bind_event_sink
from backend.services.tool_result_side_effects import (
    build_search_candidates,
    tool_result_side_effects,
)
from backend.services.travel_data import travel_data_normalizer


def _shell_result(payload: dict) -> dict:
    return {
        "ok": True,
        "exit_code": 0,
        "stdout": json.dumps({"success": True, "result": payload}, ensure_ascii=False),
        "stderr": "",
    }


def test_travel_data_normalizes_mcp_train_and_rolling_go_hotel():
    train = travel_data_normalizer.normalize({
        "result": {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "trains": [{
                        "trainNum": "G1",
                        "departureStationName": "北京南",
                        "arrivalStationName": "上海虹桥",
                        "departureTime": "2026-09-10 08:00",
                        "arrivalTime": "2026-09-10 12:30",
                        "price": {"edzPrice": "553", "ydzPrice": "933"},
                    }],
                }, ensure_ascii=False),
            }],
        },
    })
    assert train is not None
    assert train["type"] == "train"
    assert train["items"][0]["adultPrice"] == "¥553"
    segment = train["items"][0]["journeys"][0]["segments"][0]
    assert segment["marketingTransportNo"] == "G1"
    assert segment["seatClassName"] == "二等座"

    hotel = travel_data_normalizer.normalize({
        "hotelInformationList": [{
            "name": "示例酒店",
            "address": "人民路 1 号",
            "price": {"lowestPrice": 499},
            "starRating": 5,
            "distanceInMeters": 1500,
        }],
    })
    assert hotel == {
        "type": "hotel",
        "items": [{
            "name": "示例酒店",
            "address": "人民路 1 号",
            "mainPic": None,
            "detailUrl": None,
            "price": "¥499",
            "star": "⭐⭐⭐⭐⭐",
            "interestsPoi": "距目标 1.5km",
        }],
    }


def test_search_capture_uses_normalized_fields_and_builds_candidates():
    user_id = "capture-user"
    search_candidate_store.delete(search_candidate_store.key_of(user_id))
    outbound_command = (
        "tuniu call flight searchLowestPriceFlight -a "
        "'{\"departureCityName\":\"北京\",\"arrivalCityName\":\"上海\","
        "\"departureDate\":\"2026-09-10\"}'"
    )
    return_command = (
        "tuniu call train searchLowestPriceTrain -a "
        "'{\"departureCityName\":\"上海\",\"arrivalCityName\":\"北京\","
        "\"departureDate\":\"2026-09-12\"}'"
    )
    hotel_command = (
        "tuniu call hotel tuniuHotelSearch -a "
        "'{\"cityName\":\"上海\",\"checkInDate\":\"2026-09-10\","
        "\"checkOutDate\":\"2026-09-12\"}'"
    )
    flight = {
        "flightNumber": "MU5101", "departureTime": "2026-09-10 08:00",
        "arrivalTime": "2026-09-10 10:20", "basePrice": "600", "totalTax": "50",
        "cabinClass": "经济舱", "type": "直飞", "airlineCompany": "东方航空",
    }
    train = {
        "trainNum": "G2", "departureTime": "2026-09-12 15:00",
        "arrivalTime": "2026-09-12 19:30", "trainType": "direct",
        "price": {"edzPrice": "553"}, "seatAvailable": {"edzNum": 5},
    }
    hotel = {
        "hotelId": "H100", "hotelName": "上海商务酒店", "brandName": "示例品牌",
        "starName": "高档型", "roomName": "大床房", "lowestPrice": 450,
        "meal": "双早餐", "refund": "入住前可退",
    }

    with bind_context(user_id, "capture-session", "ItineraryPlanAgent"):
        tool_result_side_effects.process(
            "execute_shell_command", {"command": outbound_command}, _shell_result({"data": [flight]}),
        )
        newer = {**flight, "basePrice": "580"}
        tool_result_side_effects.process(
            "execute_shell_command", {"command": outbound_command}, _shell_result({"data": [newer]}),
        )
        tool_result_side_effects.process(
            "execute_shell_command", {"command": return_command}, _shell_result({"data": [train]}),
        )
        tool_result_side_effects.process(
            "execute_shell_command", {"command": hotel_command}, _shell_result({"hotels": [hotel]}),
        )

    entries = search_candidate_store.load_by_trip(
        user_id, "北京", "上海", "2026-09-10", "2026-09-12",
    )
    assert set(entries) == {
        "flight_北京_上海_2026-09-10",
        "train_上海_北京_2026-09-12",
        "hotel_上海_2026-09-10_2026-09-12",
    }
    assert entries["flight_北京_上海_2026-09-10"]["data"]["data"] == [newer]

    candidates = build_search_candidates(
        user_id, "北京", "上海", "2026-09-10", "2026-09-12",
    )
    assert candidates is not None
    assert len(candidates["transport_options"]) == 2
    assert candidates["transport_options"][0]["price"] == 630
    assert candidates["hotel_options"][0]["nights"] == 2


class _BookingServiceStub:
    def __init__(self) -> None:
        self.records = []

    def list_by_user(self, user_id: str, biz_type: str | None = None):
        return [item for item in self.records if item["user_id"] == user_id
                and (biz_type is None or item["biz_type"] == biz_type)]

    def create(self, user_id: str, values: dict):
        record = {"booking_id": "bk-1", "user_id": user_id, **values}
        self.records.append(record)
        return record

    def cancel(self, booking_id: str):
        record = next(item for item in self.records if item["booking_id"] == booking_id)
        record["status"] = "CANCELLED"
        return record


def test_booking_hook_persists_once_and_emits_payment_card(monkeypatch):
    service = _BookingServiceStub()
    monkeypatch.setattr(side_effect_module, "booking_service", service)
    events = []
    command = (
        "tuniu call flight saveOrder -a "
        "'{\"departureCityName\":\"北京\",\"arrivalCityName\":\"上海\","
        "\"departureDate\":\"2026-09-10\",\"flightNo\":\"MU5101\","
        "\"contactTourist\":{\"name\":\"张三\",\"mobile\":\"13800000000\"}}'"
    )
    result = _shell_result({
        "success": True, "orderId": "TN-001", "payUrl": "https://pay.example/TN-001",
    })
    with (
        bind_event_sink(lambda event, data: events.append((event, data))),
        bind_context("u001", "session-book", "BookingAgent", "to-001"),
    ):
        tool_result_side_effects.process("execute_shell_command", {"command": command}, result)
        tool_result_side_effects.process("execute_shell_command", {"command": command}, result)

    assert len(service.records) == 1
    assert service.records[0]["travel_order_id"] == "to-001"
    booking_events = [data for name, data in events if name == "booking_result"]
    assert booking_events == [{
        "type": "booking_result",
        "bizType": "flight",
        "bizTypeLabel": "机票",
        "orderId": "TN-001",
        "title": "北京→上海 MU5101",
        "amount": None,
        "currency": "CNY",
        "payUrl": "https://pay.example/TN-001",
        "statusLabel": "待支付",
    }]


def test_booking_hook_ignores_failed_or_malformed_order_results(monkeypatch):
    service = _BookingServiceStub()
    monkeypatch.setattr(side_effect_module, "booking_service", service)
    command = "tuniu call flight saveOrder -a '{\"flightNo\":\"MU5101\"}'"
    failures = [
        {"ok": False, "exit_code": 1, "stdout": "", "stderr": "failed"},
        {"ok": True, "exit_code": 0, "stdout": "not-json", "stderr": ""},
        _shell_result({"success": False, "orderId": "TN-should-not-persist"}),
        _shell_result({"orderId": "TN-missing-business-success"}),
        _shell_result({"message": "missing order id"}),
    ]
    with bind_context("u001", "session-book", "BookingAgent", "to-001"):
        for result in failures:
            tool_result_side_effects.process("execute_shell_command", {"command": command}, result)
    assert service.records == []


def test_booking_hook_accepts_mcp_content_but_rejects_error_envelopes(monkeypatch):
    service = _BookingServiceStub()
    monkeypatch.setattr(side_effect_module, "booking_service", service)
    command = "tuniu call flight saveOrder -a '{\"flightNo\":\"MU5101\"}'"
    valid = _shell_result({
        "structuredContent": {"result": {"content": [{
            "type": "text",
            "text": json.dumps({"successCode": True, "orderId": "TN-content"}),
        }]}},
    })
    rejected = [
        {"ok": True, "exit_code": 1, "stdout": json.dumps({"success": True, "result": {}})},
        _shell_result({"isError": True, "success": True, "orderId": "TN-error"}),
        {"ok": True, "exit_code": 0,
         "stdout": json.dumps({"success": True, "isError": True, "result": {}})},
    ]
    with bind_context("u001", "session-book", "BookingAgent", "to-001"):
        tool_result_side_effects.process("execute_shell_command", {"command": command}, valid)
        for result in rejected:
            tool_result_side_effects.process("execute_shell_command", {"command": command}, result)
    assert [record["external_order_no"] for record in service.records] == ["TN-content"]
