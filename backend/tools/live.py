"""Destination live-information tools matching Java ``DestinationLiveTools``.

Weather uses the free ``wttr.in`` API for today and the next two days. A
configured Weather MCP endpoint is selected for dates beyond that range. News
uses NewsData.io when its key is configured. Orizn Visa remains an MCP tool and
is launched through the configured stdio command, exactly like the Java bean.
All remote failures become structured results so a live integration cannot
break the rest of the travel workflow.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from backend.config import get_settings
from backend.infrastructure.mcp import call_mcp_tool

from ._common import tool

WEATHER_FORECAST_DAYS = 2


def _fallback(city: str, kind: str) -> dict[str, Any]:
    return {
        "city": city,
        "available": False,
        "source": "fallback",
        "message": f"未配置外部{kind}服务，请配置 MCP/API Key",
    }


def _parse_date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip().replace("/", "-"))
    except ValueError:
        return None


def _weather_mcp(city: str, target: str | None) -> dict[str, Any]:
    settings = get_settings()
    target_date = _parse_date(target)
    days = (target_date - datetime.now(UTC).date()).days if target_date else 0
    tool_name = "城市40日预报" if days > 14 else "城市15日预报"
    raw = call_mcp_tool(
        endpoint=settings.weather_mcp_endpoint,
        tool_name=tool_name,
        arguments={"city": city, **({"date": target} if target else {})},
        timeout=settings.weather_mcp_request_timeout_seconds,
        allowed_tools=settings.weather_mcp_enabled_tools,
    )
    return {"city": city, "date": target, "source": "weather-mcp", "tool": tool_name, **raw}


def _weather_wttr(city: str, target: str | None) -> dict[str, Any]:
    import httpx

    response = httpx.get(
        f"https://wttr.in/{quote_plus(city)}?format=j1",
        timeout=8.0,
        headers={"User-Agent": "gogo-agent/1.0"},
    )
    response.raise_for_status()
    raw = response.json()
    target_date = _parse_date(target)
    today = datetime.now(UTC).date()
    result: dict[str, Any] = {"city": city, "source": "wttr.in", "queryDate": target or "today"}

    if target_date in (None, today):
        current = (raw.get("current_condition") or [{}])[0]
        descriptions = current.get("weatherDesc") or [{}]
        result["current"] = {
            "tempC": f"{current.get('temp_C', '')}°C",
            "feelsLikeC": f"{current.get('FeelsLikeC', '')}°C",
            "humidity": f"{current.get('humidity', '')}%",
            "windKmph": f"{current.get('windspeedKmph', '')} km/h",
            "description": descriptions[0].get("value", "") if descriptions else "",
        }

    forecast: list[dict[str, Any]] = []
    for day in raw.get("weather") or []:
        day_date = day.get("date")
        if target_date and target_date != today and day_date != target_date.isoformat():
            continue
        hourly: list[dict[str, Any]] = []
        for slot in day.get("hourly") or []:
            try:
                hour = int(str(slot.get("time", "0")))
                time_value = f"{hour // 100:02d}:{hour % 100:02d}"
            except (TypeError, ValueError):
                time_value = ""
            descriptions = slot.get("weatherDesc") or [{}]
            hourly.append(
                {
                    "time": time_value,
                    "tempC": f"{slot.get('tempC', '')}°C",
                    "windKmph": f"{slot.get('windspeedKmph', '')} km/h",
                    "chanceOfRain": f"{slot.get('chanceofrain', '')}%",
                    "description": descriptions[0].get("value", "") if descriptions else "",
                }
            )
        forecast.append(
            {
                "date": day_date,
                "maxTempC": f"{day.get('maxtempC', '')}°C",
                "minTempC": f"{day.get('mintempC', '')}°C",
                "hourly": hourly,
            }
        )
    result["forecast"] = forecast
    return result


@tool
def query_weather(city: str, date: str | None = None) -> dict[str, Any]:
    """联网查询目的地天气；近三天用 wttr.in，远期用天气 MCP。"""
    settings = get_settings()
    target_date = _parse_date(date)
    if target_date and target_date > datetime.now(UTC).date() + timedelta(days=WEATHER_FORECAST_DAYS):
        if not settings.weather_mcp_endpoint:
            return {
                "city": city,
                "date": date,
                "beyond_range": True,
                "available": False,
                "message": f"{date} 超出 wttr.in 支持范围，请配置天气 MCP 查询。",
            }
        try:
            return _weather_mcp(city, date)
        except Exception as exc:  # noqa: BLE001 - MCP failures degrade to a tool result
            return {
                "city": city,
                "date": date,
                "available": False,
                "source": "weather-mcp",
                "error": f"天气 MCP 请求失败：{exc}",
            }
    try:
        return _weather_wttr(city, date)
    except Exception as exc:  # noqa: BLE001 - remote failures degrade to a tool result
        return {
            "city": city,
            "date": date,
            "available": False,
            "source": "wttr.in",
            "error": f"天气查询暂时不可用：{exc}",
        }


def _news(city: str, topic: str | None) -> dict[str, Any]:
    import httpx

    settings = get_settings()
    query = city + (f" {topic}" if topic and topic.strip() else "")
    response = httpx.get(
        "https://newsdata.io/api/1/news",
        params={"apikey": settings.news_api_key, "q": query, "language": "zh", "size": 5},
        timeout=10.0,
        headers={"User-Agent": "gogo-agent/1.0"},
    )
    response.raise_for_status()
    raw = response.json()
    articles = raw.get("results") or []
    news = [
        {
            "title": item.get("title"),
            "description": item.get("description"),
            "pubDate": item.get("pubDate"),
            "source": item.get("source_id"),
        }
        for item in articles[:5]
        if isinstance(item, dict)
    ]
    return {
        "city": city,
        "topic": topic,
        "source": "newsdata.io",
        "available": True,
        "news": news,
        "count": len(news),
        **({"note": f"暂未检索到 {city} 的相关资讯"} if not news else {}),
    }


@tool
def query_destination_news(city: str, topic: str | None = None) -> dict[str, Any]:
    """查询目的地最新资讯；未配置 NewsData Key 时返回可读降级结果。"""
    settings = get_settings()
    if not settings.news_api_key:
        return {
            "city": city,
            "available": False,
            "source": "newsdata.io",
            "note": "新闻查询需配置 NEWS_API_KEY（https://newsdata.io），当前未启用。",
        }
    try:
        return _news(city, topic)
    except Exception as exc:  # noqa: BLE001 - remote failures degrade to a tool result
        return {
            "city": city,
            "topic": topic,
            "available": False,
            "source": "newsdata.io",
            "error": f"新闻查询请求失败：{exc}",
        }


def _visa(tool_name: str, passport_country: str, destination_country: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        environment = (
            {"ORIZN_API_KEY": settings.orizn_visa_api_key}
            if settings.orizn_visa_api_key
            else {}
        )
        return {
            "source": "orizn-visa-mcp",
            "mode": "full" if settings.orizn_visa_api_key else "free",
            "passport_country": passport_country,
            "destination_country": destination_country,
            **call_mcp_tool(
                command=settings.orizn_mcp_command,
                args=settings.orizn_mcp_args,
                env=environment,
                tool_name=tool_name,
                arguments={"passport": passport_country, "destination": destination_country},
                timeout=settings.orizn_mcp_request_timeout_seconds,
                allowed_tools=settings.orizn_mcp_enabled_tools,
            ),
        }
    except Exception as exc:  # noqa: BLE001 - MCP failures degrade to a tool result
        return {
            "source": "orizn-visa-mcp",
            "available": False,
            "error": f"签证 MCP 请求失败：{exc}",
        }


@tool
def quick_visa_check(passport_country: str, destination_country: str) -> dict[str, Any]:
    """快速查询护照与目的地之间的签证结论（Orizn Visa MCP）。"""
    return _visa("quick_visa_check", passport_country, destination_country)


@tool
def check_visa_requirement(passport_country: str, destination_country: str) -> dict[str, Any]:
    """查询详细签证类型、材料和处理时间（Orizn Visa MCP）。"""
    return _visa("check_visa_requirement", passport_country, destination_country)


def tools():
    return [query_weather, query_destination_news, quick_visa_check, check_visa_requirement]


def destination_tools():
    """DestinationLiveTools equivalent (weather and destination news only)."""
    return [query_weather, query_destination_news]
