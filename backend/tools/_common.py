from typing import Any

try:
    from langchain_core.tools import BaseTool, tool
except ImportError:  # source inspection fallback; install project deps to execute
    BaseTool = Any
    def tool(fn): return fn


def current_user_id(explicit: str | None = None, default: str = "u_001") -> str:
    """Resolve the request-scoped user id for Python tools."""
    try:
        from backend.core.request_context import current_context
        context = current_context()
        if context and context.user_id:
            return context.user_id
    except ImportError:
        pass
    # Explicit ids are useful for trusted internal calls and isolated unit tests,
    # but must never override the authenticated request tenant.
    if explicit:
        return explicit
    return default
