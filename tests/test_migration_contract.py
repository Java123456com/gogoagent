from backend.intent.router import has_multi_intent_signal
from backend.intent.rule_matcher import IntentRuleMatcher, Verdict
from backend.services.circuit_breaker import ToolCircuitBreaker
from backend.services.policy_service import is_cabin_compliant


def test_intent_l1_policy_query():
    result = IntentRuleMatcher().evaluate("查一下上海差旅政策")
    assert result.verdict == Verdict.HIT
    assert result.result.primary_intent == "policy_query"


def test_multi_intent_guard():
    assert has_multi_intent_signal("查下差旅政策，顺便把发票报销了")


def test_cabin_rank_is_policy_compliant():
    assert is_cabin_compliant("经济舱", "经济舱/商务舱")
    assert not is_cabin_compliant("头等舱", "经济舱/商务舱")


def test_circuit_breaker_opens_after_failures():
    breaker = ToolCircuitBreaker(threshold=2, recovery_seconds=60)
    for _ in range(2):
        try: breaker.call("demo", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        except RuntimeError: pass
    try: breaker.call("demo", lambda: "ok")
    except RuntimeError as error: assert "circuit open" in str(error)
    else: raise AssertionError("circuit should be open")
