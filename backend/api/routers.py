import json
from collections.abc import Callable
from importlib import import_module
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.contracts import ApprovalDecisionBody, BookingBody, ChatBody, LoginBody, OrderBody
from backend.api.dependencies import auth_service, current_user
from backend.core.continuation import is_continuation_message
from backend.core.request_context import bind_context
from backend.infrastructure.object_storage import ObjectStorageError, object_storage
from backend.infrastructure.repositories import approval_repository, travel_order_repository
from backend.memory.session import agent_session_store
from backend.runtime.agent_executor import get_subagent_executor
from backend.services.booking_cancellation import booking_cancellation_service
from backend.services.booking_service import booking_service
from backend.services.chat_service import chat_service
from backend.services.chat_stream import stream_agent_execution
from backend.services.order_service import travel_order_service
from backend.services.preference_service import preference_service
from backend.services.runtime_events import emit_agent_done, emit_agent_start
from backend.services.travel_service import TravelAgentService

router = APIRouter(prefix="/api")
travel = TravelAgentService()

DEBUG_AGENTS = {
    "QueryRewritingAgent": ("问题改写", "backend.agents.query_rewriting", "query_rewriting_agent"),
    "IntentRecognitionAgent": ("意图识别", "backend.agents.intent_recognition", "intent_recognition_agent"),
    "InfoAgent": ("信息查询", "backend.agents.info", "info_agent"),
    "ItineraryPlanAgent": ("行程规划", "backend.agents.itinerary_plan", "itinerary_plan_agent"),
    "ItineraryReviewAgent": ("行程审核", "backend.agents.itinerary_review", "itinerary_review_agent"),
    "ItineraryManageAgent": ("行程管理", "backend.agents.itinerary_manage", "itinerary_manage_agent"),
}


def _resolve_debug_agent(agent_name: str):
    _, module_name, attribute = DEBUG_AGENTS[agent_name]
    return getattr(import_module(module_name), attribute)


def _sse_response(
    execute: Callable[[], dict[str, Any]],
    *,
    session_id: str,
    latest_user_text: str,
    user_id: str,
    persist_user_reply: bool = False,
) -> StreamingResponse:
    stream = stream_agent_execution(
        execute,
        session_id=session_id,
        latest_user_text=latest_user_text,
        user_id=user_id,
        persist_user_reply=persist_user_reply,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@router.post("/auth/login")
def login(payload: LoginBody):
    try: return auth_service.login(payload.username, payload.password)
    except ValueError as error: raise HTTPException(401, str(error)) from error

@router.post("/auth/logout")
def logout(authorization: str | None = Header(default=None), user: dict = Depends(current_user)):
    if authorization: auth_service.logout(authorization)
    return {"message": "退出成功"}

@router.get("/auth/info")
def info(user: dict = Depends(current_user)): return {"userId": user["user_id"], "admin": user["admin"]}

@router.get("/preferences/options")
def preference_options(user: dict = Depends(current_user)): return preference_service.options()

@router.get("/preferences")
def get_preferences(user: dict = Depends(current_user)): return preference_service.get(user["user_id"])

@router.post("/preferences")
def save_preferences(payload: dict, user: dict = Depends(current_user)):
    preference_service.save(user["user_id"], payload); return {"saved": True}

@router.post("/travel/orders")
def create_order(payload: OrderBody, user: dict = Depends(current_user)):
    return travel_order_service.create_and_submit(user["user_id"], payload.model_dump()).as_dict()

@router.post("/travel/orders/{order_id}/cancel")
def cancel_order(order_id: str, force: bool = False, user: dict = Depends(current_user)):
    order = travel_order_repository.get(order_id)
    if not order or order.user_id != user["user_id"]: raise HTTPException(404, "差旅单不存在")
    if order.status == "APPROVED" and not force: raise HTTPException(409, "已通过订单取消需要 force=true")
    return travel_order_service.cancel_with_approval(order).__dict__

@router.post("/travel/orders/{order_id}/bookings")
def create_booking(order_id: str, payload: BookingBody, user: dict = Depends(current_user)):
    order = travel_order_repository.get(order_id)
    if not order or order.user_id != user["user_id"]: raise HTTPException(404, "差旅单不存在")
    return booking_service.create(user["user_id"], {**payload.model_dump(), "travel_order_id": order_id})

@router.get("/my-travel/orders")
def my_orders(user: dict = Depends(current_user)): return travel_order_service.list_user_orders(user["user_id"])

@router.get("/my-travel/plan-html/{order_id}")
def plan_html(order_id: str, user: dict = Depends(current_user)):
    order = travel_order_repository.get(order_id)
    if not order:
        raise HTTPException(404, "方案不存在")
    if order.user_id != user["user_id"]:
        raise HTTPException(403, "无权访问该差旅单")
    if not order.plan_html_url:
        raise HTTPException(404, "方案不存在")
    html = order.plan_html_url
    if html.startswith("plans/"):
        try:
            html = object_storage.get_html(html)
        except ObjectStorageError as error:
            raise HTTPException(502, str(error)) from error
    return Response(html, media_type="text/html")

@router.delete("/my-travel/bookings/{booking_id}")
def delete_booking(booking_id: str, user: dict = Depends(current_user)):
    if not booking_service.delete(booking_id, user["user_id"]):
        return JSONResponse(
            status_code=404,
            content={"deleted": False, "message": "预订记录不存在或无权删除"},
        )
    return {"deleted": True}

@router.post("/my-travel/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, payload: dict | None = None, user: dict = Depends(current_user)):
    result = booking_cancellation_service.cancel(
        user["user_id"], booking_id, (payload or {}).get("reason"),
    )
    if result["success"]:
        return result
    status = {"BOOKING_NOT_FOUND": 404, "PERMISSION_DENIED": 403}.get(result.get("error_code"), 400)
    return JSONResponse(status_code=status, content=result)

@router.get("/admin/approvals")
def approvals(status: str = "", user: dict = Depends(current_user)):
    if not user["admin"]: raise HTTPException(403, "需要管理员权限")
    return [_approval_view(x) for x in approval_repository.list_all(status or None)]

@router.post("/admin/approvals/{approval_id}/decision")
def decide(approval_id: str, payload: ApprovalDecisionBody, user: dict = Depends(current_user)):
    if not user["admin"]: raise HTTPException(403, "需要管理员权限")
    decision = payload.decision.lower()
    if decision not in {"agree", "refuse"}:
        raise HTTPException(400, "decision 只能为 agree 或 refuse")
    record = travel_order_service.decide_and_sync_order(approval_id, decision == "agree", payload.remark)
    if not record: raise HTTPException(409, "审批单不存在或已被处理")
    return _approval_view(record)


def _approval_view(record) -> dict[str, Any]:
    """Build the camel-case approval response consumed by the frontend."""
    form: Any = record.approval_form
    if isinstance(form, str):
        try:
            form = json.loads(form)
        except (TypeError, ValueError):
            pass
    account = None
    try:
        from backend.infrastructure.repositories import user_repository
        account = user_repository.find_by_id(record.user_id)
    except Exception:
        account = None
    return {
        "processInstanceId": record.process_instance_id,
        "userId": record.user_id,
        "userName": account.real_name if account else None,
        "title": record.title,
        "status": record.status,
        "statusLabel": {"PENDING": "待审批", "APPROVED": "已通过",
                         "REJECTED": "已拒绝", "CANCELLED": "已撤销"}.get(record.status, record.status),
        "orderId": record.order_id,
        "remark": record.remark,
        "submitTime": record.submit_time.timestamp() * 1000 if record.submit_time else None,
        "updateTime": record.update_time.timestamp() * 1000 if record.update_time else None,
        "form": form,
    }

@router.post("/chat/respond")
def respond(payload: dict, user: dict = Depends(current_user)):
    session_id = payload.get("sessionId") or payload.get("session_id")
    if not session_id:
        raise HTTPException(422, "缺少 sessionId")
    answer = payload.get("response", payload.get("answer", payload.get("value")))
    tool_use_id = payload.get("toolUseId") or payload.get("tool_use_id")
    reply_text = answer if isinstance(answer, str) else json.dumps(
        answer, ensure_ascii=False, default=str,
    )
    return _sse_response(
        lambda: travel.resume(session_id, answer, user["user_id"], tool_use_id),
        session_id=session_id,
        latest_user_text=reply_text,
        user_id=user["user_id"],
        persist_user_reply=True,
    )


@router.post("/chat/{session_id}")
def chat(session_id: str, payload: ChatBody, user: dict = Depends(current_user)):
    travel.interrupt_previous(session_id)
    chat_service.save_user_message(session_id, user["user_id"], payload.message)
    active_agent = agent_session_store.get_active_agent(session_id)
    if active_agent and active_agent != "MasterAgent" and is_continuation_message(payload.message):
        execute = lambda: travel.run_active(payload.message, user["user_id"], session_id)
    else:
        execute = lambda: travel.run(payload.message, user["user_id"], session_id)
    return _sse_response(
        execute,
        session_id=session_id,
        latest_user_text=payload.message,
        user_id=user["user_id"],
    )

@router.post("/chat/{session_id}/interrupt")
def interrupt(session_id: str, user: dict = Depends(current_user)):
    try:
        running = travel.interrupt(session_id, user["user_id"])
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    return {"interrupted": True, "running": running, "sessionId": session_id}

@router.post("/chat/{session_id}/confirm")
def confirm(session_id: str, payload: dict, user: dict = Depends(current_user)):
    decision = payload.get("decision")
    answer = decision == "approve" if decision in {"approve", "reject"} else payload.get(
        "confirmed", payload.get("answer", False),
    )
    reply_text = "确认" if answer is True else "取消"
    return _sse_response(
        lambda: travel.resume(session_id, answer, user["user_id"]),
        session_id=session_id,
        latest_user_text=reply_text,
        user_id=user["user_id"],
        persist_user_reply=True,
    )

@router.get("/chat/conversations")
def conversations(user: dict = Depends(current_user)): return [x.as_dict() for x in chat_service.list_conversations(user["user_id"])]

@router.get("/chat/{session_id}/messages")
def messages(session_id: str, user: dict = Depends(current_user)):
    try:
        rows = chat_service.find_messages_for_user(session_id, user["user_id"])
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    return [{"id": x.message_id, "role": x.role, "content": x.content,
             "agentName": x.agent_name,
             "timestamp": x.created_at.timestamp() * 1000 if x.created_at else None,
             "extra": x.extra or {}, "feedback": x.feedback,
             "feedbackAt": x.feedback_at.timestamp() * 1000 if x.feedback_at else None}
            for x in rows]

@router.put("/chat/{session_id}/title")
def title(session_id: str, payload: dict, user: dict = Depends(current_user)):
    try:
        chat_service.update_title(session_id, user["user_id"], payload.get("title", "新对话"))
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    return {"updated": True}

@router.delete("/chat/{session_id}")
def delete_conversation(session_id: str, user: dict = Depends(current_user)):
    try:
        chat_service.delete_conversation(session_id, user["user_id"])
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    return {"deleted": True}

@router.put("/chat/{session_id}/messages/{message_id}/feedback")
def feedback(session_id: str, message_id: str, payload: dict, user: dict = Depends(current_user)):
    try:
        chat_service.update_feedback(message_id, user["user_id"], payload.get("feedback"))
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    return {"updated": True}


@router.post("/callback/dingtalk/approval")
def dingtalk_approval_callback(payload: dict):
    """Receive the idempotent DingTalk approval callback."""
    instance_id = payload.get("processInstanceId") or payload.get("process_instance_id")
    if not instance_id:
        raise HTTPException(422, "缺少 processInstanceId")
    result = str(payload.get("result") or "")
    travel_order_service.decide_and_sync_order(
        instance_id, result.lower() == "agree", payload.get("remark"),
    )
    # The callback is intentionally idempotent and always acknowledges with
    # a plain success body, including already-processed instances.
    return "success"

@router.get("/debug/agent/list")
def debug_agents(user: dict = Depends(current_user)):
    return [{"name": name, "label": item[0]} for name, item in DEBUG_AGENTS.items()]


@router.post("/debug/agent/{agent_name}")
def debug_agent(agent_name: str, payload: dict, user: dict = Depends(current_user)):
    item = DEBUG_AGENTS.get(agent_name)
    if item is None:
        raise HTTPException(404, f"不允许直连的智能体: {agent_name}")
    session_id = payload.get("sessionId") or payload.get("session_id") or (
        f"debug-{agent_name}-{user['user_id']}"
    )
    message = str(payload.get("message") or "")
    if not message.strip():
        raise HTTPException(422, "message 不能为空")
    travel.interrupt_previous(session_id)

    def execute() -> dict[str, Any]:
        state = {
            "request": message,
            "original_question": message,
            "user_id": user["user_id"],
            "session_id": session_id,
            "messages": [],
            "trace": [],
        }
        if agent_name == "QueryRewritingAgent":
            target = _resolve_debug_agent(agent_name)
            emit_agent_start(agent_name)
            try:
                with bind_context(user["user_id"], session_id, agent_name):
                    rewritten = target.invoke(message)
                return {"final": rewritten, "active_agent": agent_name, "trace": []}
            finally:
                emit_agent_done(agent_name)
        if agent_name == "IntentRecognitionAgent":
            target = _resolve_debug_agent(agent_name)
            emit_agent_start(agent_name)
            try:
                with bind_context(user["user_id"], session_id, agent_name):
                    recognized = target.invoke(message)
                return {
                    "final": json.dumps(recognized, ensure_ascii=False),
                    "active_agent": agent_name,
                    "trace": [],
                }
            finally:
                emit_agent_done(agent_name)
        if agent_name in {"InfoAgent", "ItineraryPlanAgent", "ItineraryManageAgent", "BookingAgent"}:
            return get_subagent_executor().execute(agent_name, state)
        target = _resolve_debug_agent(agent_name)
        return target.invoke(state)

    return _sse_response(
        execute,
        session_id=session_id,
        latest_user_text=message,
        user_id=user["user_id"],
        persist_user_reply=True,
    )
