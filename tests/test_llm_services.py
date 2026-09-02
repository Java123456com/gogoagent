from backend.services.llm_services import ConversationTitleService, QuestionRecommendationService


def test_recommendations_parse_structured_output(monkeypatch):
    service = QuestionRecommendationService()
    monkeypatch.setattr(
        "backend.services.llm_services.invoke_text",
        lambda *_: '{"questions": ["确定", "修改方案", "查询政策", "继续", "忽略"]}',
    )
    assert service.generate("规划杭州行程", "已生成三套方案") == ["确定", "修改方案", "查询政策", "继续"]


def test_recommendations_fail_closed_on_invalid_json(monkeypatch):
    service = QuestionRecommendationService()
    monkeypatch.setattr("backend.services.llm_services.invoke_text", lambda *_: "not json")
    assert service.generate("问题", "回答") == []


def test_title_service_only_updates_default_conversation(monkeypatch):
    service = ConversationTitleService()
    updated = []

    class Conversation:
        deleted = 0
        user_id = "u_001"
        title = "新对话"

    monkeypatch.setattr("backend.services.llm_services.chat_repository.get_conversation", lambda _: Conversation())
    monkeypatch.setattr(service, "generate", lambda *_: "杭州行程规划")
    monkeypatch.setattr(
        "backend.services.llm_services.chat_service.update_title",
        lambda *args: updated.append(args),
    )
    service.update_if_default("s_001", "u_001", "去杭州出差", {"primary_intent": "itinerary_planning"})

    assert updated == [("s_001", "u_001", "杭州行程规划")]
