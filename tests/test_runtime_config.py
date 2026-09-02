from backend.config.settings import Settings
from backend.memory.store import InMemorySessionStore, PersistentSessionStore
from sqlalchemy.exc import SQLAlchemyError


def test_gogo_database_url_alias_is_supported(monkeypatch):
    monkeypatch.setenv("GOGO_DATABASE_URL", "mysql+pymysql://user:pass@db/gogo")
    settings = Settings()
    assert settings.database_url == "mysql+pymysql://user:pass@db/gogo"


def test_in_memory_session_store_supports_delete():
    store = InMemorySessionStore()
    store.save("session-1", {"value": 1})
    store.delete("session-1")
    assert store.get("session-1") == {}


def test_persistent_store_delete_falls_back_when_database_is_unavailable(monkeypatch):
    store = PersistentSessionStore()
    store._fallback.save("session-2", {"value": 2})
    monkeypatch.setattr(
        "backend.memory.store.agent_memory_repository.delete_checkpoint",
        lambda _session_id: (_ for _ in ()).throw(SQLAlchemyError("database unavailable")),
    )
    store.delete("session-2")
    assert store._fallback.get("session-2") == {}
