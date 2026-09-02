"""LLM 工厂（对应 Java 的 ModelConfig：strongModel / stableModel / strongModelWithThinking）。

统一走 LangChain 的 ``ChatOpenAI``（DashScope 兼容 OpenAI 协议），三档模型只是不同
model 名与 temperature；关闭 ``GOGO_USE_LLM`` 或未配置 Key 时 ``get_*_model`` 返回
``None``，调用方走确定性 fallback。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from backend.config import get_settings
from backend.infrastructure.model_profiles import ModelProfile, resolve_model_profile


def _build(profile: ModelProfile) -> BaseChatModel | None:
    settings = get_settings()
    api_key = settings.openai_api_key or settings.dashscope_api_key
    if not settings.gogo_use_llm or not api_key:
        return None
    from langchain_openai import ChatOpenAI

    config = resolve_model_profile(profile)
    kwargs: dict[str, Any] = {
        "model": config.model_name,
        "api_key": api_key,
        "temperature": config.temperature,
        # DashScope's OpenAI-compatible API requires non-standard thinking
        # settings to be forwarded through ``extra_body``.
        "extra_body": config.extra_body(),
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    elif settings.dashscope_api_key:
        kwargs["base_url"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return ChatOpenAI(**kwargs)


@lru_cache
def fast_model() -> BaseChatModel | None:
    """Fast model used by title/recommendation/light extraction services."""
    return _build(ModelProfile.FAST)


@lru_cache
def strong_model() -> BaseChatModel | None:
    """强模型：编排决策、规划、预订（对应 Java strongModel）。"""
    return _build(ModelProfile.STRONG)


@lru_cache
def stable_model() -> BaseChatModel | None:
    """稳模型：信息查询、审核、意图兜底（对应 Java stableModel）。"""
    return _build(ModelProfile.STABLE)


@lru_cache
def strong_model_with_thinking() -> BaseChatModel | None:
    """带深度思考的强模型：ItineraryPlanAgent / BookingAgent。"""
    return _build(ModelProfile.STRONG_THINKING)


def llm_enabled() -> bool:
    settings = get_settings()
    return settings.gogo_use_llm and bool(settings.openai_api_key or settings.dashscope_api_key)


def invoke_text(model: BaseChatModel | None, system: str, user: str, fallback: str) -> str:
    """单次 LLM 调用并返回纯文本；model 为 None 时返回 fallback。"""
    if model is None:
        return fallback
    try:
        resp = model.invoke([("system", system), ("human", user)])
        content = resp.content
        return content if isinstance(content, str) else str(content)
    except Exception:
        return fallback
