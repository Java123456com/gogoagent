"""旧 SQLite 适配层已废弃，统一改用 ``backend.infrastructure.db``。

保留本文件仅为兼容 codex 早期占位实现；新代码请勿 import。
"""
from backend.infrastructure.db import get_engine, get_session, init_db  # noqa: F401
