"""Deterministic candidate-search boundary used by the itinerary graph.

The first provider wraps the existing restricted Tuniu CLI.  Keeping it behind
this interface makes the planning graph independent of shell/Skill details and
allows an HTTP/MCP provider to replace it without changing remediation logic.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha1
from typing import Any, ClassVar, Protocol

from backend.config import get_settings
from backend.runtime.resilient_tool import ResilientToolHook
from backend.services.api_key_service import api_key_service
from backend.services.tool_result_side_effects import (
    build_search_candidates,
    tool_result_side_effects,
)
from backend.services.travel_data import travel_data_normalizer
from backend.tools.skills import execute_shell_command, skill_command_available


class CandidateSearchProvider(Protocol):
    def search(
        self,
        *,
        user_id: str,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str,
        requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


class TuniuCliCandidateSearchProvider:
    """Build allow-listed CLI calls from validated structured constraints."""

    def search(
        self,
        *,
        user_id: str,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str,
        requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized = requests or [
            {"target_type": "transport", "constraints": {"direction": "outbound"}},
            {"target_type": "transport", "constraints": {"direction": "return"}},
            {"target_type": "hotel", "constraints": {}},
        ]
        calls: list[dict[str, Any]] = []
        for request in normalized:
            calls.extend(
                self._commands(
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date,
                    return_date=return_date,
                    target_type=str(request.get("target_type") or "transport"),
                    constraints=request.get("constraints") or {},
                )
            )
        results = []
        for call in _dedupe_calls(calls):
            command = call["command"]
            try:
                result = ResilientToolHook("ItineraryPlanAgent").invoke(
                    execute_shell_command,
                    {"command": command, "timeout_seconds": 180, "user_id": user_id},
                )
            except Exception as exc:  # noqa: BLE001 - provider failures keep old candidates
                result = {"ok": False, "error": str(exc)}
            tool_result_side_effects.process(
                "execute_shell_command",
                {"command": command, "user_id": user_id},
                result,
            )
            results.append(
                {
                    "kind": call["kind"],
                    "ok": _result_ok(result),
                    "error": result.get("error") if isinstance(result, dict) else None,
                }
            )
        candidates = build_search_candidates(
            user_id,
            origin,
            destination,
            departure_date,
            return_date,
        ) or {"transport_options": [], "hotel_options": []}
        return {
            "ok": bool(candidates.get("transport_options") or candidates.get("hotel_options")),
            "calls": results,
            "candidates": candidates,
        }

    def _commands(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str,
        target_type: str,
        constraints: dict[str, Any],
    ) -> list[dict[str, str]]:
        target_type = (
            target_type if target_type in {"transport", "flight", "train", "hotel"} else "transport"
        )
        direction = str(constraints.get("direction") or "both")
        routes = []
        if direction in {"outbound", "both"}:
            routes.append((origin, destination, departure_date))
        if direction in {"return", "both"}:
            routes.append((destination, origin, return_date))
        commands: list[dict[str, str]] = []
        if target_type in {"transport", "flight", "train"}:
            for source, target, travel_date in routes:
                common = {
                    "departureCityName": _safe_text(source),
                    "arrivalCityName": _safe_text(target),
                    "departureDate": _safe_date(travel_date),
                }
                if constraints.get("departure_time"):
                    common["departureTime"] = _safe_time_range(constraints["departure_time"])
                if target_type in {"transport", "flight"}:
                    commands.append(
                        _command(
                            "flight",
                            "searchLowestPriceFlight",
                            common,
                        )
                    )
                if target_type in {"transport", "train"}:
                    commands.append(
                        _command(
                            "train",
                            "searchLowestPriceTrain",
                            common,
                        )
                    )
        if target_type == "hotel":
            args: dict[str, Any] = {
                "cityName": _safe_text(destination),
                "checkIn": _safe_date(departure_date),
                "checkOut": _safe_date(return_date),
            }
            if constraints.get("keyword"):
                args["keyword"] = _safe_text(constraints["keyword"])
            if constraints.get("max_price") is not None:
                maximum = max(1, int(float(constraints["max_price"])))
                args["prices"] = f"0-{maximum}"
            commands.append(_command("hotel", "tuniuHotelSearch", args))
        return commands


def _command(server: str, method: str, args: dict[str, Any]) -> dict[str, str]:
    payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return {"kind": server, "command": f"tuniu call {server} {method} -a '{payload}'"}


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9 _.\-]{1,40}", text):
        raise ValueError(f"搜索参数包含非法字符: {text!r}")
    return text


def _safe_date(value: Any) -> str:
    text = str(value or "").replace("/", "-")
    if not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        raise ValueError(f"搜索日期格式错误: {text!r}")
    return text


def _safe_time_range(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?", text):
        raise ValueError(f"搜索时段格式错误: {text!r}")
    return text


def _dedupe_calls(calls: list[dict[str, str]]) -> list[dict[str, str]]:
    seen, result = set(), []
    for call in calls:
        if call["command"] not in seen:
            seen.add(call["command"])
            result.append(call)
    return result


def _result_ok(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("ok", result.get("success", result.get("exit_code") == 0)))


class FlyAiCandidateSearchProvider:
    name = "flyai"

    def search(self, *, user_id: str, origin: str, destination: str,
               departure_date: str, return_date: str,
               requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not skill_command_available("flyai"):
            return _unavailable(self.name, "未安装 @fly-ai/flyai-cli")
        wanted = _wanted_types(requests)
        calls: list[dict[str, Any]] = []
        candidates = _empty_candidates()
        if "transport" in wanted:
            for direction, source, target, travel_date in (
                ("outbound", origin, destination, departure_date),
                ("return", destination, origin, return_date),
            ):
                for kind, subcommand in (("flight", "search-flight"), ("train", "search-train")):
                    command = (
                        f'flyai {subcommand} --origin "{_safe_text(source)}" '
                        f'--destination "{_safe_text(target)}" --dep-date {_safe_date(travel_date)}'
                    )
                    raw = _shell(user_id, command)
                    calls.append(_call_status(self.name, kind, raw))
                    _merge_normalized(candidates, travel_data_normalizer.normalize(raw), direction)
        if "hotel" in wanted:
            nights = _stay_nights(departure_date, return_date)
            query = _safe_text(f"{destination} {departure_date}到{return_date} 商务酒店")
            raw = _shell(user_id, f'flyai ai-search --query "{query}"')
            calls.append(_call_status(self.name, "hotel", raw))
            _merge_normalized(
                candidates,
                travel_data_normalizer.normalize(raw),
                "stay",
                nights=nights,
            )
        return _provider_result(self.name, calls, candidates)


class FlightManagerCandidateSearchProvider:
    name = "flight-manager"
    _CITY_CODES: ClassVar[dict[str, str]] = {
        "北京": "BJS", "上海": "SHA", "广州": "CAN", "深圳": "SZX",
        "成都": "CTU", "杭州": "HGH", "重庆": "CKG", "武汉": "WUH",
        "西安": "SIA", "南京": "NKG", "长沙": "CSX", "青岛": "TAO",
        "厦门": "XMN", "昆明": "KMG", "天津": "TSN", "郑州": "CGO",
    }

    def search(self, *, user_id: str, origin: str, destination: str,
               departure_date: str, return_date: str,
               requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        key = api_key_service.get(user_id, self.name)
        if not key:
            return _unavailable(self.name, "当前用户未配置 flight-manager API Key")
        if "transport" not in _wanted_types(requests):
            return _provider_result(self.name, [], _empty_candidates())
        calls, candidates = [], _empty_candidates()
        for direction, source, target, travel_date in (
            ("outbound", origin, destination, departure_date),
            ("return", destination, origin, return_date),
        ):
            source_code, target_code = self._CITY_CODES.get(source), self._CITY_CODES.get(target)
            if not source_code or not target_code:
                calls.append({"provider": self.name, "kind": "flight", "ok": False,
                              "error": f"缺少城市三字码: {source}/{target}"})
                continue
            payload = {
                "jsonrpc": "2.0", "id": f"gogo-{direction}", "method": "tools/call",
                "params": {"name": "search_flights", "arguments": {
                    "departureCityCode": source_code, "arrivalCityCode": target_code,
                    "date": travel_date, "adultNum": "1", "pageSize": 20,
                }},
            }
            try:
                import httpx

                response = httpx.post(
                    "https://fly.huoli.com/mcp/flight_server",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=get_settings().external_request_timeout_seconds,
                )
                response.raise_for_status()
                raw = response.json()
                calls.append({"provider": self.name, "kind": "flight", "ok": True})
                _merge_normalized(candidates, travel_data_normalizer.normalize(raw), direction)
            except Exception as exc:  # noqa: BLE001 - one provider must not break planning
                calls.append({"provider": self.name, "kind": "flight", "ok": False,
                              "error": str(exc)})
        return _provider_result(self.name, calls, candidates)


class RollingGoCandidateSearchProvider:
    name = "rolling-go-hotel"

    def search(self, *, user_id: str, origin: str, destination: str,
               departure_date: str, return_date: str,
               requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if "hotel" not in _wanted_types(requests):
            return _provider_result(self.name, [], _empty_candidates())
        if not skill_command_available("rgh"):
            return _unavailable(self.name, "未安装 RollingGo rgh CLI")
        nights = _stay_nights(departure_date, return_date)
        command = (
            f'rgh search-hotels --origin-query "{_safe_text(destination)}商务酒店" '
            f'--place "{_safe_text(destination)}" --place-type "城市" '
            f'--check-in-date {_safe_date(departure_date)} --stay-nights {nights} --size 5'
        )
        raw = _shell(user_id, command)
        candidates = _empty_candidates()
        _merge_normalized(
            candidates,
            travel_data_normalizer.normalize(raw),
            "stay",
            nights=nights,
        )
        return _provider_result(self.name, [_call_status(self.name, "hotel", raw)], candidates)


class MultiSourceCandidateSearchProvider:
    """Provider registry equivalent to Java SkillBox discovery and routing."""

    def __init__(self) -> None:
        self.providers: dict[str, CandidateSearchProvider] = {
            "tuniu-cli": TuniuCliCandidateSearchProvider(),
            "flyai": FlyAiCandidateSearchProvider(),
            "flight-manager": FlightManagerCandidateSearchProvider(),
            "rolling-go-hotel": RollingGoCandidateSearchProvider(),
        }

    def search(self, **kwargs: Any) -> dict[str, Any]:
        selected = [
            self.providers[name]
            for name in get_settings().travel_search_providers
            if name in self.providers
        ]
        if not selected:
            return {"ok": False, "calls": [], "candidates": _empty_candidates(),
                    "error": "未配置旅行搜索 Provider"}
        with ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="travel-search") as pool:
            futures = {
                pool.submit(provider.search, **kwargs): getattr(provider, "name", "tuniu-cli")
                for provider in selected
            }
            results = []
            for future, provider_name in futures.items():
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - isolate each external provider
                    results.append(_unavailable(provider_name, f"Provider 执行失败: {exc}"))
        candidates = _empty_candidates()
        calls: list[dict[str, Any]] = []
        for result in results:
            calls.extend(result.get("calls") or [])
            candidates = _merge_candidate_sets(candidates, result.get("candidates") or {})
        return {
            "ok": bool(candidates["transport_options"] or candidates["hotel_options"]),
            "calls": calls,
            "candidates": candidates,
            "providers": [getattr(provider, "name", "tuniu-cli") for provider in selected],
        }


def _shell(user_id: str, command: str) -> Any:
    try:
        return ResilientToolHook("ItineraryPlanAgent").invoke(
            execute_shell_command,
            {"command": command, "timeout_seconds": 180, "user_id": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _empty_candidates() -> dict[str, list[dict[str, Any]]]:
    return {"transport_options": [], "hotel_options": []}


def _wanted_types(requests: list[dict[str, Any]] | None) -> set[str]:
    if not requests:
        return {"transport", "hotel"}
    values = {str(item.get("target_type") or "transport") for item in requests}
    result = set()
    if values.intersection({"transport", "flight", "train"}):
        result.add("transport")
    if "hotel" in values:
        result.add("hotel")
    return result


def _merge_normalized(
    target: dict[str, list[dict[str, Any]]],
    normalized: dict[str, Any] | None,
    direction: str,
    *,
    nights: int = 1,
) -> None:
    if not normalized:
        return
    kind = normalized.get("type")
    for index, item in enumerate(normalized.get("items") or []):
        if not isinstance(item, dict):
            continue
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        candidate_id = f"{kind[:1].upper()}_{sha1(identity.encode('utf-8')).hexdigest()[:12]}"
        if kind == "hotel":
            target["hotel_options"].append({
                "id": candidate_id, "name": item.get("name") or "酒店",
                "brand": item.get("brandName"), "star_rating": _number(item.get("star")),
                "price_per_night": _number(item.get("price")), "nights": nights,
                "booking_url": item.get("detailUrl"), "raw": item,
            })
            continue
        journeys = item.get("journeys") or []
        segment = ((journeys[0].get("segments") or [{}])[0]
                   if journeys and isinstance(journeys[0], dict) else {})
        target["transport_options"].append({
            "id": candidate_id, "type": kind, "direction": direction,
            "origin": segment.get("depCityName"), "destination": segment.get("arrCityName"),
            "departure_time": segment.get("depDateTime"), "arrival_time": segment.get("arrDateTime"),
            "duration": segment.get("duration"), "price": _number(item.get("adultPrice")),
            "cabin_class": segment.get("seatClassName"),
            "carrier": segment.get("marketingTransportName"),
            "code": segment.get("marketingTransportNo"), "booking_url": item.get("jumpUrl"),
            "raw": item,
        })


def _number(value: Any) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else 0.0


def _stay_nights(departure_date: str, return_date: str) -> int:
    return max(1, (date.fromisoformat(return_date) - date.fromisoformat(departure_date)).days)


def _merge_candidate_sets(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = _empty_candidates()
    for field in result:
        merged = {}
        for index, item in enumerate([*(left.get(field) or []), *(right.get(field) or [])]):
            if isinstance(item, dict):
                merged[str(item.get("id") or f"{field}_{index}")] = item
        result[field] = list(merged.values())
    return result


def _provider_result(name: str, calls: list[dict[str, Any]], candidates: dict[str, Any]) -> dict[str, Any]:
    return {"provider": name, "ok": bool(candidates["transport_options"] or candidates["hotel_options"]),
            "calls": calls, "candidates": candidates}


def _unavailable(name: str, error: str) -> dict[str, Any]:
    return {"provider": name, "ok": False, "calls": [{"provider": name, "ok": False, "error": error}],
            "candidates": _empty_candidates()}


def _call_status(provider: str, kind: str, result: Any) -> dict[str, Any]:
    return {"provider": provider, "kind": kind, "ok": _result_ok(result),
            "error": result.get("error") if isinstance(result, dict) else None}


candidate_search_provider: CandidateSearchProvider = MultiSourceCandidateSearchProvider()
