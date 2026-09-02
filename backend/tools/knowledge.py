"""Agent-callable wrappers for the three Java InfoAgent Knowledge beans."""
from __future__ import annotations

from typing import Any

from backend.rag.knowledge import travel_knowledge

from ._common import tool


def _query(name: str, query: str, limit: int) -> dict[str, Any]:
    rows = travel_knowledge.retrieve(query, name)
    return {"knowledge": name, "query": query, "results": rows[: max(1, limit)]}


@tool
def query_attraction_knowledge(query: str, limit: int = 4) -> dict[str, Any]:
    """检索百炼/本地景点知识库，返回目的地景点和相关说明。"""
    return _query("attraction", query, limit)


@tool
def query_corporate_travel_policy_knowledge(query: str, limit: int = 4) -> dict[str, Any]:
    """检索企业差旅政策 DOCX 知识库的语义相关片段。"""
    return _query("policy", query, limit)


@tool
def query_corporate_travel_guidelines_knowledge(query: str, limit: int = 4) -> dict[str, Any]:
    """检索企业差旅指南 DOCX 知识库的语义相关片段。"""
    return _query("guideline", query, limit)


def tools():
    return [
        query_attraction_knowledge,
        query_corporate_travel_policy_knowledge,
        query_corporate_travel_guidelines_knowledge,
    ]
