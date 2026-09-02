"""Normalize heterogeneous travel-provider search outputs."""
from __future__ import annotations

import json
from typing import Any, ClassVar


class TravelDataNormalizer:
    ARRAY_KEYS: ClassVar[tuple[str, ...]] = (
        "flightList", "flights", "flightInfos", "flightInfoList",
        "hotelList", "hotels", "hotelInformationList",
        "trainList", "trains", "itemList", "items", "list", "data",
    )
    TRAIN_SEAT_NAMES: ClassVar[dict[str, str]] = {
        "swzPrice": "商务座", "tdzPrice": "特等座", "ydzPrice": "一等座",
        "edzPrice": "二等座", "rzPrice": "软座", "yzPrice": "硬座",
        "gjrwPrice": "国际软卧", "rwPrice": "软卧", "ywPrice": "硬卧",
        "dwPrice": "动卧", "ydwPrice": "一等卧", "edwPrice": "二等卧",
        "wzPrice": "无座",
    }

    def normalize(self, raw: Any) -> dict[str, Any] | None:
        root = self._parse_root(raw)
        return self._normalize_root(root) if root else None

    def _normalize_root(self, root: dict[str, Any]) -> dict[str, Any] | None:
        unwrapped = self._unwrap_content_wrapper(root)
        if unwrapped is not None and unwrapped is not root:
            normalized = self._normalize_root(unwrapped)
            if normalized:
                return normalized

        rolling = root.get("hotelInformationList")
        if self._object_list(rolling):
            return self._result("hotel", [self._rolling_hotel(item) for item in rolling])

        for key, kind in (("flights", "flight"), ("hotels", "hotel"), ("trains", "train")):
            values = root.get(key)
            if self._object_list(values):
                return self._result(kind, self._normalize_items(values, kind))

        data = root.get("data")
        if isinstance(data, dict) and self._object_list(data.get("itemList")):
            kind = self._detect_flyai_type(data["itemList"][0])
            if kind:
                return self._result(kind, self._normalize_items(data["itemList"], kind))

        normalized = self._normalize_mcp_result(root)
        return normalized or self._normalize_generic(root)

    def _normalize_mcp_result(self, root: dict[str, Any]) -> dict[str, Any] | None:
        result = root.get("result")
        if isinstance(result, list) and self._object_list(result):
            kind = self._detect_type(result[0])
            return self._result(kind, self._normalize_items(result, kind))
        if not isinstance(result, dict):
            return None

        for key in (
            "flights", "trains", "hotels", "flightList", "trainList",
            "hotelList", "flightInfos", "flightInfoList",
        ):
            values = result.get(key)
            if self._object_list(values):
                kind = self._key_type(key) or self._detect_type(values[0])
                return self._result(kind, self._normalize_items(values, kind))

        structured = result.get("structuredContent")
        payload = None
        if isinstance(structured, dict):
            payload = structured.get("data", structured.get("content"))
        if payload is None:
            payload = result.get("data", result.get("content"))
        if isinstance(payload, str):
            payload = self._parse_root(payload)
        if isinstance(payload, dict):
            return self._normalize_generic(payload)
        if self._object_list(payload):
            kind = self._detect_type(payload[0])
            return self._result(kind, self._normalize_items(payload, kind))
        return None

    def _normalize_generic(self, root: dict[str, Any]) -> dict[str, Any] | None:
        for key in self.ARRAY_KEYS:
            values = root.get(key)
            if self._object_list(values):
                kind = self._key_type(key) or self._detect_type(values[0])
                return self._result(kind, self._normalize_items(values, kind))
        for value in root.values():
            if not isinstance(value, dict):
                continue
            for key in self.ARRAY_KEYS:
                values = value.get(key)
                if self._object_list(values):
                    kind = self._key_type(key) or self._detect_type(values[0])
                    return self._result(kind, self._normalize_items(values, kind))
        return None

    def _unwrap_content_wrapper(self, root: dict[str, Any]) -> dict[str, Any] | None:
        parsed = self._unwrap_blocks(root.get("content"))
        if parsed:
            return parsed
        result = root.get("result")
        if not isinstance(result, dict):
            return None
        parsed = self._unwrap_blocks(result.get("content"))
        if parsed:
            return parsed
        structured = result.get("structuredContent")
        return self._unwrap_blocks(structured.get("content")) if isinstance(structured, dict) else None

    def _unwrap_blocks(self, blocks: Any) -> dict[str, Any] | None:
        if not isinstance(blocks, list) or not blocks:
            return None
        if not all(isinstance(item, dict) and item.get("type") == "text"
                   and isinstance(item.get("text"), str) for item in blocks):
            return None
        return self._parse_root("\n".join(item["text"] for item in blocks))

    def _parse_root(self, raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, dict):
            stdout = raw.get("stdout")
            if isinstance(stdout, str) and stdout.strip():
                parsed = self._parse_root(stdout)
                if parsed:
                    return parsed
            return raw
        if isinstance(raw, list):
            return {"data": raw}
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {"data": value} if isinstance(value, list) else None
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index, char in enumerate(text):
            if char not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append((end, value))
            elif isinstance(value, list):
                candidates.append((end, {"data": value}))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _normalize_items(self, values: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        result = []
        for item in values:
            if item.get("type") == "text" and "text" in item:
                continue
            if kind == "hotel":
                result.append(self._hotel(item))
            elif kind in {"flight", "train"}:
                result.append(self._transport(item, kind))
            else:
                result.append(dict(item))
        return result

    def _hotel(self, source: dict[str, Any]) -> dict[str, Any]:
        target = source.get("info") if isinstance(source.get("info"), dict) else source
        star = self._first(target.get("starName")) or self._star(
            target.get("star"), target.get("starRating"),
        )
        return {
            "name": self._first(target.get("hotelName"), target.get("title"),
                                target.get("name"), source.get("name"), "未知酒店"),
            "address": self._first(target.get("address"), source.get("address")),
            "mainPic": self._first(target.get("firstPic"), target.get("picUrl"),
                                   target.get("mainPic"), target.get("imageUrl"),
                                   source.get("imageUrl")),
            "detailUrl": self._first(target.get("jumpUrl"), target.get("detailUrl"),
                                     target.get("bookingUrl"), source.get("bookingUrl"),
                                     source.get("detailUrl")),
            "price": self._price(target.get("lowestPrice"), target.get("price"), source.get("price")),
            "brandName": self._first(target.get("brandName"), target.get("brand")),
            "score": self._score(target.get("commentScore"), target.get("score")),
            "scoreDesc": target.get("scoreDesc"),
            "star": star,
            "interestsPoi": self._first(target.get("business"), target.get("interestsPoi")),
            "review": self._first(target.get("commentDigest"), target.get("review")),
        }

    def _rolling_hotel(self, hotel: dict[str, Any]) -> dict[str, Any]:
        price = hotel.get("price")
        lowest = price.get("lowestPrice") if isinstance(price, dict) else None
        distance = self._number(hotel.get("distanceInMeters"))
        if distance and distance >= 1000:
            distance_text = f"距目标 {distance / 1000:.1f}km"
        elif distance:
            distance_text = f"距目标 {int(distance)}m"
        else:
            distance_text = None
        rating = int(self._number(hotel.get("starRating")) or 0)
        return {
            "name": hotel.get("name"), "address": hotel.get("address"),
            "mainPic": hotel.get("imageUrl"), "detailUrl": hotel.get("bookingUrl"),
            "price": f"¥{lowest}" if lowest is not None else "",
            **({"star": "⭐" * min(rating, 5)} if rating > 0 else {}),
            **({"interestsPoi": distance_text} if distance_text else {}),
        }

    def _transport(self, source: dict[str, Any], kind: str) -> dict[str, Any]:
        if "journeys" in source:
            return dict(source)
        output: dict[str, Any] = {}
        derived_seat = None
        if isinstance(source.get("price"), dict):
            lowest = self._lowest_train_price(source["price"])
            if lowest:
                output["adultPrice"], derived_seat = lowest
            else:
                output["adultPrice"] = self._price(source.get("adultPrice"), source.get("salePrice"))
        else:
            output["adultPrice"] = self._price(
                source.get("price"), source.get("adultPrice"), source.get("salePrice"),
                source.get("basePrice"),
            )
        output["jumpUrl"] = self._first(
            source.get("jumpUrl"), source.get("detailUrl"), source.get("bookingUrl"),
        )
        segment = self._flat_segment(source, kind)
        if segment:
            if derived_seat and not segment.get("seatClassName"):
                segment["seatClassName"] = derived_seat
            output["journeys"] = [{
                "journeyType": self._first(source.get("journeyType"), source.get("journey_type"), "直达"),
                "totalDuration": self._first(source.get("duration"), source.get("totalDuration"),
                                             source.get("total_duration"), segment.get("duration")),
                "segments": [segment],
            }]
        return {**source, **output}

    def _flat_segment(self, source: dict[str, Any], kind: str) -> dict[str, Any] | None:
        dep_time = self._first(source.get("depDateTime"), source.get("departureTime"),
                              source.get("departTime"), source.get("departsDate"))
        arr_time = self._first(source.get("arrDateTime"), source.get("arrivalTime"),
                              source.get("arriveTime"), source.get("arrivesDate"))
        dep_station = self._first(
            source.get("depStationName"), source.get("departureStationName"),
            source.get("departStationName"), source.get("dep_station_name"),
            source.get("departureAirportName"), source.get("depAirportName"),
            source.get("departureAirport"),
        )
        arr_station = self._first(
            source.get("arrStationName"), source.get("arrivalStationName"),
            source.get("destStationName"), source.get("arr_station_name"),
            source.get("arrivalAirportName"), source.get("arrAirportName"),
            source.get("arrivalAirport"),
        )
        dep_city = self._first(source.get("depCityName"), source.get("departureCityName"),
                              source.get("dep_city_name"), dep_station)
        arr_city = self._first(source.get("arrCityName"), source.get("arrivalCityName"),
                              source.get("arr_city_name"), arr_station)
        if not all((dep_city, arr_city, dep_time, arr_time)):
            return None
        segment = {
            "depCityName": dep_city, "arrCityName": arr_city,
            "depStationName": dep_station, "arrStationName": arr_station,
            "depStationShortName": self._first(
                source.get("depStationShortName"), source.get("departureAirportCode"),
                source.get("depAirportCode"), source.get("depStationCode"),
                source.get("departureTerminal"),
            ),
            "arrStationShortName": self._first(
                source.get("arrStationShortName"), source.get("arrivalAirportCode"),
                source.get("arrAirportCode"), source.get("arrStationCode"),
                source.get("arrivalTerminal"),
            ),
            "depDateTime": dep_time, "arrDateTime": arr_time,
            "duration": self._first(source.get("duration"), source.get("elapsedTime"),
                                    source.get("travelTime"), source.get("totalDuration"),
                                    source.get("flyTime")),
            "transportType": self._first(source.get("transportType"), "飞机" if kind == "flight" else "火车"),
            "marketingTransportName": self._first(
                source.get("marketingTransportName"), source.get("airlineName"),
                source.get("airline"), source.get("airlineCompany"),
            ),
            "marketingTransportNo": self._first(
                source.get("marketingTransportNo"), source.get("flightNo"),
                source.get("trainNo"), source.get("trainNum"), source.get("trainCode"),
                source.get("flightNumber"),
            ),
            "seatClassName": self._first(
                source.get("seatClassName"), source.get("cabinName"), source.get("seatName"),
                source.get("seatClass"), source.get("cabinClass"),
            ),
        }
        for output_key, source_keys in {
            "depTerm": ("depTerm", "departureTerminal"),
            "arrTerm": ("arrTerm", "arrivalTerminal"),
            "depCityCode": ("depCityCode", "departureCityCode"),
            "arrCityCode": ("arrCityCode", "arrivalCityCode"),
        }.items():
            value = self._first(*(source.get(key) for key in source_keys))
            if value:
                segment[output_key] = value
        return segment

    def _detect_flyai_type(self, item: dict[str, Any]) -> str | None:
        target = item.get("info") if isinstance(item.get("info"), dict) else item
        if any(key in target for key in ("mainPic", "picUrl", "score", "star")):
            return "hotel"
        journeys = item.get("journeys")
        if not self._object_list(journeys) or not self._object_list(journeys[0].get("segments")):
            return None
        segment = journeys[0]["segments"][0]
        transport = str(segment.get("transportType") or "").lower()
        if transport in {"飞机", "flight", "flights", "air", "plane", "航班", "airplane"}:
            return "flight"
        if transport in {"火车", "train", "trains", "rail", "高铁", "动车", "railway"}:
            return "train"
        return "flight" if any(key in segment for key in (
            "depStationCode", "depAirportCode", "flightNo", "marketingTransportNo",
        )) else "train"

    @staticmethod
    def _detect_type(item: dict[str, Any]) -> str:
        if "journeys" in item:
            return "flight"
        flight_keys = {
            "flightNo", "flightNumber", "flightInfoId", "marketingFlightNo", "airlineCode",
            "airlineCompany", "departureAirportCode", "depAirportCode", "departureAirport",
            "arrivalAirport", "arrivalAirportCode", "arrAirportCode", "departureTerminal",
            "arrivalTerminal", "flyTime",
        }
        if flight_keys.intersection(item):
            return "flight"
        train_keys = {"trainNo", "trainNum", "trainCode", "departureStationName", "arrivalStationName"}
        time_keys = {"depDateTime", "departureTime", "departsDate", "departTime"}
        if train_keys.intersection(item) and time_keys.intersection(item):
            return "train"
        if {"mainPic", "picUrl", "imageUrl", "starRating", "address", "hotelName"}.intersection(item):
            return "hotel"
        return "unknown"

    @staticmethod
    def _key_type(key: str) -> str | None:
        if key in {"flightList", "flights", "flightInfos", "flightInfoList"}:
            return "flight"
        if key in {"hotelList", "hotels", "hotelInformationList"}:
            return "hotel"
        if key in {"trainList", "trains"}:
            return "train"
        return None

    def _lowest_train_price(self, prices: dict[str, Any]) -> tuple[str, str] | None:
        choices = []
        for key, label in self.TRAIN_SEAT_NAMES.items():
            amount = self._number(prices.get(key))
            if amount and amount > 0:
                choices.append((amount, label))
        if not choices:
            return None
        amount, label = min(choices)
        formatted = str(int(amount)) if amount.is_integer() else str(amount)
        return f"¥{formatted}", label

    @staticmethod
    def _first(*values: Any) -> str:
        return next((str(value).strip() for value in values if value is not None and str(value).strip()), "")

    def _price(self, *values: Any) -> str:
        value = self._first(*values)
        if not value or value.startswith(("¥", "$")):
            return value
        try:
            float(value.replace(",", ""))
            return f"¥{value}"
        except ValueError:
            return value

    def _score(self, *values: Any) -> str:
        value = self._first(*values)
        try:
            float(value)
            return f"{value}分"
        except ValueError:
            return value

    def _star(self, star: Any, rating: Any) -> str | None:
        if self._first(star):
            return self._first(star)
        number = int(self._number(rating) or 0)
        return "⭐" * min(number, 5) if number > 0 else None

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _object_list(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and isinstance(value[0], dict)

    @staticmethod
    def _result(kind: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        return {"type": kind, "items": items} if items else None


travel_data_normalizer = TravelDataNormalizer()
