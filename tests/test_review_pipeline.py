import json

from backend.core.request_context import bind_context
from backend.infrastructure.stores import itinerary_plan_store, review_result_store
from backend.tools.review import objective_review, review_itinerary


def _plan():
    return {
        "user_request": {
            "origin": "上海", "destination": "杭州",
            "departure_date": "2099-07-15", "return_date": "2099-07-16",
        },
        "proposals": [{
            "tags": ["综合最佳"],
            "outbound": {"type": "train", "origin": "上海", "destination": "杭州",
                         "departure_time": "2099-07-15T08:00", "arrival_time": "2099-07-15T09:00",
                         "price": 100, "cabin_class": "二等座"},
            "hotel": {"name": "测试酒店", "nights": 1, "price_per_night": 300,
                      "star_rating": 4, "distance_to_dest_km": 2},
            "return": {"type": "train", "origin": "杭州", "destination": "上海",
                       "departure_time": "2099-07-16T18:00", "arrival_time": "2099-07-16T19:00",
                       "price": 100, "cabin_class": "二等座"},
            "metrics": {"total_price": 500, "total_transit_min": 120, "total_transit_hhmm": "2h00m"},
            "scores": {"overall": 90},
        }],
    }


def test_objective_engine_has_java_six_dimensions():
    result = objective_review({
        "origin": "上海", "destination": "杭州", "departure_date": "2099-07-15",
        "return_date": "2099-07-16", "approved_origin": "上海", "approved_destination": "杭州",
        "days": [{"date": "2099-07-15", "outbound_transport": {"type": "train"}, "hotel": {"nights": 1}},
                 {"date": "2099-07-16", "return_transport": {"type": "train"}}],
    }, {})
    assert [item["dimension"] for item in result["checks"]] == [
        "行程完整性", "时间安排合理性", "出发地与目的地核实",
        "预算合规性", "交通舱位合规性", "路径规划合理性",
    ]


def test_review_orchestrator_loads_plan_and_persists_previous_report():
    user_id, session_id = "review_user", "review_session"
    plan = _plan()
    itinerary_plan_store.save(user_id, "上海", "杭州", "2099-07-15", json.dumps(plan, ensure_ascii=False))
    review_result_store.clear(session_id)
    with bind_context(user_id, session_id, "ItineraryPlanAgent"):
        report = review_itinerary.invoke({
            "origin": "上海", "destination": "杭州", "departure_date": "2099-07-15",
            "policy": json.dumps({"hotelLimit": 500, "hotelStarLimit": 4,
                                  "trainSeatClass": "二等座", "advanceBookingDays": 3}),
        })
    assert report["best_proposal_id"] == "P1"
    assert len(report["dimensions"]) == 4
    assert review_result_store.load(session_id)["verdict"] == report["verdict"]
