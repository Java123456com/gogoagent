"""旧记忆服务已迁移到 ``backend.memory.long_term``，保留兼容入口。"""
from backend.memory.long_term import LongTermMemory, long_term_memory  # noqa: F401
