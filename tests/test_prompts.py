from backend.prompts import load_static


def test_prompt_include_syntax_and_date_variables_are_rendered():
    prompt = load_static("itinerary-manage-agent-system.md")
    assert "{{include:" not in prompt
    assert "{{current_date}}" not in prompt
    assert "### 时间处理规则" in prompt
    assert "星期" in prompt
