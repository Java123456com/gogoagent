from backend.config import get_settings
from backend.infrastructure.stores import CircuitBreakerStore


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    def hincrby(self, key, field, amount):
        self.operations.append(("hincrby", (key, field, amount)))
        return self

    def hset(self, key, field, value):
        self.operations.append(("hset", (key, field, value)))
        return self

    def expire(self, key, seconds):
        self.operations.append(("expire", (key, seconds)))
        return self

    def execute(self):
        return [getattr(self.redis, name)(*args) for name, args in self.operations]


class _RedisStub:
    def __init__(self):
        self.values = {}
        self.hashes = {}

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)

    def expire(self, _key, _seconds):
        return True

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = int(bucket.get(field, 0)) + amount
        return bucket[field]

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hexists(self, key, field):
        return field in self.hashes.get(key, {})

    def pipeline(self, transaction=True):
        assert transaction is True
        return _Pipeline(self)


def test_circuit_breaker_state_is_shared_and_uses_generation_backoff(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "circuit_breaker_initial_cooldown_seconds", 2)
    monkeypatch.setattr(settings, "circuit_breaker_backoff_multiplier", 2.0)
    monkeypatch.setattr(settings, "circuit_breaker_max_cooldown_seconds", 10)
    clock = {"now": 100.0}
    monkeypatch.setattr("backend.infrastructure.stores.time.time", lambda: clock["now"])

    redis = _RedisStub()
    first, second = CircuitBreakerStore(), CircuitBreakerStore()
    first._redis = redis
    second._redis = redis

    assert first.increment_failure_count("query_weather") == 1
    assert second.get_failure_count("query_weather") == 1
    assert first.open_with_next_generation("query_weather") == 2
    assert second.is_open("query_weather") is True
    assert second.get_generation("query_weather") == 1
    assert second.is_cooldown_expired("query_weather") is False

    clock["now"] = 102.1
    assert second.is_cooldown_expired("query_weather") is True
    assert second.open_with_next_generation("query_weather") == 4
    assert first.get_generation("query_weather") == 2

    second.clear_open("query_weather")
    second.reset_failure_count("query_weather")
    assert first.is_open("query_weather") is False
    assert first.get_failure_count("query_weather") == 0
