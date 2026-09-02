from datetime import date

from backend.config import get_settings
from backend.tools import live


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_weather_parser_matches_provider_shape(monkeypatch):
    payload = {
        "current_condition": [
            {
                "temp_C": "25",
                "FeelsLikeC": "26",
                "humidity": "60",
                "windspeedKmph": "8",
                "weatherDesc": [{"value": "Sunny"}],
            }
        ],
        "weather": [
            {
                "date": date.today().isoformat(),
                "maxtempC": "30",
                "mintempC": "20",
                "hourly": [
                    {
                        "time": "600",
                        "tempC": "22",
                        "windspeedKmph": "5",
                        "chanceofrain": "10",
                        "weatherDesc": [{"value": "Clear"}],
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Response(payload))
    result = live.query_weather.invoke({"city": "北京"})
    assert result["source"] == "wttr.in"
    assert result["current"]["tempC"] == "25°C"
    assert result["forecast"][0]["hourly"][0]["time"] == "06:00"


def test_newsdata_normalization(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "news_api_key", "test-key")
    monkeypatch.setattr(
        "httpx.get",
        lambda *args, **kwargs: _Response(
            {
                "results": [
                    {
                        "title": "活动",
                        "description": "展会",
                        "pubDate": "2026-08-31",
                        "source_id": "example",
                    }
                ],
            }
        ),
    )
    result = live.query_destination_news.invoke({"city": "杭州", "topic": "event"})
    assert result["source"] == "newsdata.io"
    assert result["count"] == 1
    assert result["news"][0]["source"] == "example"
