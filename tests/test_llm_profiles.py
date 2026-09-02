import pytest

from backend.config import get_settings
from backend.infrastructure import llm


@pytest.fixture(autouse=True)
def clear_model_factory_caches():
    factories = (llm.fast_model, llm.stable_model, llm.strong_model, llm.strong_model_with_thinking)
    for factory in factories:
        factory.cache_clear()
    yield
    for factory in factories:
        factory.cache_clear()


def test_thinking_profile_is_forwarded_to_dashscope_compatible_client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "gogo_use_llm", True)
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    model = llm.strong_model_with_thinking()

    assert isinstance(model, FakeChatOpenAI)
    assert captured["extra_body"] == {"enable_thinking": True, "thinking_budget": 2048}


def test_non_thinking_profile_explicitly_disables_thinking(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "gogo_use_llm", True)
    monkeypatch.setattr(settings, "dashscope_api_key", "test-key")
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    llm.stable_model()

    assert captured["extra_body"] == {"enable_thinking": False}
