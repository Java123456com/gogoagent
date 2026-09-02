"""Continuation signals shared by chat routing and recommendation prompts.

These values mirror Java ``ContinuationSignals.ALL``.  Matching is deliberately
exact after trimming/lower-casing; a sentence containing one of the words is a
new request and must go through the normal pipeline.
"""
from __future__ import annotations

CONTINUATION_SIGNALS = frozenset({
    "确定", "确认", "提交", "继续", "是的", "好的", "对", "好", "修改", "补充",
    "不对", "取消", "重新", "再", "ok", "yes", "confirm", "continue",
    "修改一下", "补充一下", "重新来", "再来",
})


def is_continuation_message(message: str | None) -> bool:
    if not message or not message.strip():
        return False
    return message.strip().lower() in CONTINUATION_SIGNALS


__all__ = ["CONTINUATION_SIGNALS", "is_continuation_message"]
