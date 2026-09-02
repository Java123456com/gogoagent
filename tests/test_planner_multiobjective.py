import json

from backend.infrastructure.stores import search_candidate_store
from backend.services import preference_scoring
from backend.tools.planner import plan_itinerary


def _save(user_id: str, transports: list[dict], hotels: list[dict]) -> None:
    search_candidate_store.save(
        user_id,
        json.dumps({
            "transport_options": transports,
            "hotel_options": hotels,
        }, ensure_ascii=False),
    )


def _leg(candidate_id: str, direction: str, price: float = 500,
         duration: int = 120) -> dict:
    outbound = direction == "OUTBOUND"
    return {
        "id": candidate_id,
        "direction": direction,
        "type": "flight",
        "from": "北京" if outbound else "上海",
        "to": "上海" if outbound else "北京",
        "price": price,
        "duration": duration,
        "departure_time": "2099-07-15 08:00" if outbound else "2099-07-17 18:00",
        "arrival_time": "2099-07-15 10:00" if outbound else "2099-07-17 20:00",
        "cabin_class": "经济舱",
        "is_direct": True,
    }


def test_auto_preference_scoring_selects_matching_hotel(monkeypatch):
    monkeypatch.setattr(preference_scoring, "strong_model", lambda: None)
    user_id = "planner-auto-preference"
    _save(
        user_id,
        [_leg("T_OUT", "OUTBOUND"), _leg("T_BACK", "INBOUND")],
        [
            {"id": "H_MATCH", "name": "全季人民广场店", "brand": "全季",
             "room_type": "大床房", "price_per_night": 400, "nights": 2},
            {"id": "H_OTHER", "name": "其他酒店", "brand": "其他",
             "room_type": "双床房", "price_per_night": 400, "nights": 2},
        ],
    )

    result = plan_itinerary.invoke({
        "user_id": user_id, "origin": "北京", "destination": "上海",
        "departure_date": "2099-07-15", "return_date": "2099-07-17",
        "preferences": json.dumps({"hotel_brand": ["全季"], "hotel_room": "大床房"},
                                  ensure_ascii=False),
        "scores": "auto",
    })

    preferred = next(item for item in result["proposals"] if "最符合偏好" in item["tags"])
    assert preferred["hotel"]["id"] == "H_MATCH"
    assert result["preference_scoring"]["mode"] == "deterministic_fallback"
    assert any("全季" in basis for basis in preferred["preference_basis"])


def test_policy_soft_penalty_changes_comprehensive_winner():
    user_id = "planner-policy-penalty"
    _save(
        user_id,
        [_leg("T_OUT", "OUTBOUND"), _leg("T_BACK", "INBOUND")],
        [
            {"id": "H_VIOLATE", "name": "偏好五星", "star_rating": 5,
             "price_per_night": 600, "nights": 1},
            {"id": "H_COMPLIANT", "name": "合规酒店", "star_rating": 4,
             "price_per_night": 500, "nights": 1},
        ],
    )
    scores = {
        "transport_scores": {"T_OUT": 50, "T_BACK": 50},
        "hotel_scores": {"H_VIOLATE": 100, "H_COMPLIANT": 0},
    }
    policy = {
        "hotelLimit": 500, "hotelStarLimit": 4, "flightClass": "经济舱",
    }

    result = plan_itinerary.invoke({
        "user_id": user_id, "origin": "北京", "destination": "上海",
        "departure_date": "2099-07-15", "return_date": "2099-07-17",
        "scores": json.dumps(scores), "policy": json.dumps(policy, ensure_ascii=False),
    })

    best = next(item for item in result["proposals"] if "综合最佳" in item["tags"])
    preferred = next(item for item in result["proposals"] if "最符合偏好" in item["tags"])
    assert best["hotel"]["id"] == "H_COMPLIANT"
    assert preferred["hotel"]["id"] == "H_VIOLATE"
    assert preferred["scores"]["policy_penalty"] > 0
    assert best["scores"]["policy_penalty"] == 0


def test_large_cartesian_space_is_pruned_by_more_than_ninety_percent(monkeypatch):
    monkeypatch.setattr(preference_scoring, "strong_model", lambda: None)
    user_id = "planner-pruning"
    outbound = [_leg(f"O{i}", "OUTBOUND", 300 + i, 90 + i) for i in range(20)]
    inbound = [_leg(f"R{i}", "INBOUND", 280 + i, 100 + i) for i in range(20)]
    hotels = [
        {"id": f"H{i}", "name": f"酒店{i}", "price_per_night": 300 + i,
         "distance_to_dest_km": i / 2, "nights": 2}
        for i in range(20)
    ]
    _save(user_id, [*outbound, *inbound], hotels)

    result = plan_itinerary.invoke({
        "user_id": user_id, "origin": "北京", "destination": "上海",
        "departure_date": "2099-07-15", "return_date": "2099-07-17",
    })

    filtering = result["meta"]["filtering"]
    assert filtering["raw_combo_count"] == 8000
    assert filtering["enumerated_combo_count"] <= 800
    assert filtering["pruning_rate"] >= 90
    assert filtering["filter_rate"] >= 90
    assert result["candidate_count"] <= 800


def test_llm_scores_are_validated_and_missing_ids_use_fallback(monkeypatch):
    monkeypatch.setattr(preference_scoring, "strong_model", lambda: object())
    monkeypatch.setattr(
        preference_scoring,
        "invoke_text",
        lambda *_args, **_kwargs: json.dumps({
            "transport_scores": {"T1": {"score": 97, "basis": ["命中偏好航司"]}},
            "hotel_scores": {},
        }, ensure_ascii=False),
    )
    result = preference_scoring.score_candidate_preferences(
        {"flight_airline": ["国航"], "hotel_brand": ["全季"]},
        {
            "transport_options": [{"id": "T1", "carrier": "国航"}],
            "hotel_options": [{"id": "H1", "brand": "全季", "name": "全季酒店"}],
        },
    )

    assert result["mode"] == "llm"
    assert result["scores"]["transport_scores"]["T1"]["score"] == 97
    assert result["scores"]["hotel_scores"]["H1"]["score"] > 50
