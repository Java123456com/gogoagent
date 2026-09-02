import time

from backend.core.request_context import current_context
from backend.infrastructure.object_storage import ObjectStorageError, object_storage
from backend.infrastructure.repositories import travel_order_repository
from backend.services.runtime_events import emit_event

from ._common import current_user_id, tool


@tool
def save_plan_html(
    html: str, title: str = "差旅行程方案", order_id: str | None = None,
    user_id: str | None = None,
) -> dict:
    """将完整行程 HTML 保存到 MinIO，并关联当前差旅单。"""
    user_id = current_user_id(user_id)
    context = current_context()
    order_id = order_id or (context.travel_order_id if context else None)
    session_id = context.session_id if context else "unknown"
    existing = travel_order_repository.get(order_id) if order_id else None
    if existing is not None and existing.user_id != user_id:
        return {"saved": False, "order_id": order_id, "title": title, "error": "订单不存在"}
    identifier = order_id or f"session_{session_id}"
    object_key = f"plans/{identifier}/{int(time.time() * 1000)}.html"
    emit_event("plan_html", {"type": "plan_html", "html": html, "title": title})
    if object_storage.enabled:
        try:
            object_storage.put_html(object_key, html)
        except ObjectStorageError as exc:
            return {"saved": False, "order_id": order_id, "title": title, "error": str(exc)}
        if existing is not None:
            travel_order_repository.update(order_id, {"plan_html_url": object_key})
        return {"saved": True, "minio_saved": True, "object_key": object_key,
                "order_id": order_id, "title": title, "html_length": len(html)}
    # Local development fallback: preserve the old inline value so the
    # frontend remains usable before MinIO credentials are supplied.
    if existing is None:
        return {"saved": False, "minio_saved": False, "order_id": order_id,
                "title": title, "error": "MinIO 未配置且未关联差旅单"}
    order = travel_order_repository.update(order_id, {"plan_html_url": html})
    return {"saved": order is not None, "minio_saved": False, "order_id": order_id,
            "title": title, "html_length": len(html)}


def tools(): return [save_plan_html]
