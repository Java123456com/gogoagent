"""应用启动引导：建表 + 种子数据（等价于 schema.sql 初始化）。"""
from __future__ import annotations

from backend.infrastructure.db import init_db
from backend.infrastructure.security import install_sensitive_logging


def bootstrap() -> None:
    install_sensitive_logging()
    init_db()
