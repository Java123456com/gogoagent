"""支持片段引用和动态日期变量的提示词加载器。

优先读取项目内 bundled resources，并兼容可选的外部资源目录。
提示词仍然是行为契约，缺失时直接抛错。
"""
from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

_RESOURCE_ROOT = Path(__file__).resolve().parent / "resources"
_PROMPT_DIRS = (_RESOURCE_ROOT / "prompts",)
_INCLUDE_PATTERN = re.compile(r"\{\{include:\s*([\w\-./]+)\s*}}")
_MAX_INCLUDE_DEPTH = 5


def _resolve_existing_path(roots: tuple[Path, ...], name: str) -> Path | None:
    for root in roots:
        path = root / name
        if path.exists():
            return path
    return None


@lru_cache
def load_prompt(name: str) -> str:
    path = _resolve_existing_path(_PROMPT_DIRS, name)
    if path is None:
        raise FileNotFoundError(f"提示词文件不存在：{_PROMPT_DIRS[0] / name}")
    return path.read_text(encoding="utf-8")


def load_static(name: str) -> str:
    """加载并解析 {{fragment}} 引用（对应 PromptLoader.loadStatic）。"""
    text = load_prompt(name)
    text = _expand_fragments(text, {name})
    now = datetime.now().astimezone()
    weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    return (text.replace("{{current_date}}", now.date().isoformat())
            .replace("{{current_weekday}}", weekdays[now.weekday()])
            .replace("{{current_time}}", "（见下方动态注入）"))


def _expand_fragments(text: str, seen: set[str] | None = None, depth: int = 0) -> str:
    seen = seen or set()
    if depth > _MAX_INCLUDE_DEPTH:
        raise ValueError(f"prompt 片段嵌套超过 {_MAX_INCLUDE_DEPTH} 层")

    def _replace(match: re.Match) -> str:
        fragment_name = match.group(1)
        if fragment_name in seen:
            chain = " -> ".join([*seen, fragment_name])
            raise ValueError(f"prompt 片段存在环形引用：{chain}")
        frag_path = _resolve_existing_path(_PROMPT_DIRS, fragment_name)
        if frag_path is None:
            raise FileNotFoundError(f"prompt 片段不存在：{fragment_name}")
        nested = {*seen, fragment_name}
        return _expand_fragments(
            frag_path.read_text(encoding="utf-8").strip(), nested, depth + 1
        )

    return _INCLUDE_PATTERN.sub(_replace, text)
