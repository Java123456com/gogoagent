"""Conservative city-to-city transit estimates used by conflict detection."""

from __future__ import annotations

import logging

from backend.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 240
SAME_CITY_MINUTES = 0
TIER_OTHER_MINUTES = 360
TIER2_MINUTES = 300
TIER1_MINUTES = 270


def normalize_city(city: str | None) -> str:
    value = str(city or "").strip()
    if value.endswith("市") and len(value) > 1:
        value = value[:-1]
    return value.casefold()


def _configured_minutes(from_city: str, to_city: str) -> int | None:
    for raw_pair, raw_minutes in get_settings().city_pair_minutes.items():
        parts = str(raw_pair).split("-", 1)
        if len(parts) != 2:
            continue
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError):
            continue
        if minutes <= 0:
            continue
        first, second = (normalize_city(part) for part in parts)
        if (first, second) in {(from_city, to_city), (to_city, from_city)}:
            return minutes
    return None


def _tier(city: str) -> int:
    settings = get_settings()
    if city in {normalize_city(value) for value in settings.tier1_cities}:
        return 0
    if city in {normalize_city(value) for value in settings.new_tier1_cities}:
        return 1
    if city in {normalize_city(value) for value in settings.tier2_cities}:
        return 2
    return 3


def estimate_minutes(from_city: str | None, to_city: str | None) -> int:
    """Estimate the full minimum transfer time, including station/airport access."""
    origin, destination = normalize_city(from_city), normalize_city(to_city)
    if not origin or not destination:
        logger.warning("City transit input is blank; using conservative default")
        return DEFAULT_MINUTES
    if origin == destination:
        return SAME_CITY_MINUTES
    if configured := _configured_minutes(origin, destination):
        return configured
    max_tier = max(_tier(origin), _tier(destination))
    if max_tier >= 3:
        return TIER_OTHER_MINUTES
    if max_tier == 2:
        return TIER2_MINUTES
    return TIER1_MINUTES
