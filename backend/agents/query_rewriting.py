from backend.agents.base import text_agent


class QueryRewritingAgent:
    def invoke(self, question: str, history: str = "") -> str:
        fallback = question.strip()
        return text_agent("query-rewriting-agent-system.md", f"历史：{history}\n当前问题：{question}", fallback)


query_rewriting_agent = QueryRewritingAgent()
