"""AI 运维（AIOps）接口。

* ``GET  /api/ai/status``              能力与配置状态（前端据此决定是否展示入口）
* ``GET  /api/ai/tools``              当前账号可用的 AI 工具目录
* ``POST /api/ai/chat``               流式对话（SSE：reasoning / content / tool_call / tool_result / confirm_required）
* ``POST /api/ai/confirm``            敏感操作确认：校验管理员账号密码后放行挂起的工具调用
* ``GET  /api/ai/conversations``      对话审计列表（含每轮工具调用）
* ``GET  /api/ai/conversations/<id>`` 对话详情（逐条消息 + 工具调用明细）
* ``GET  /api/ai/tool-calls``         AI 工具调用审计明细
* ``DELETE /api/ai/conversations/<id>`` 删除对话（连带消息与工具调用记录）

设计要点：AI 的每个工具都是「带着**调用者自己的 JWT**」去请求本项目自己的 REST API
（见 :mod:`app.ai.tools`），所以权限校验、参数校验、审计全部复用人类路径——
AI 越权不了，也不用维护第二套业务逻辑。
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, current_app, request, stream_with_context
from sqlalchemy import func, or_

from ..ai import service as ai_service
from ..ai import tools as tool_registry
from ..ai.client import AiError, create_client
from ..audit import log_event
from ..extensions import db
from ..models import AiConversation, AiMessage, AiToolCall, User
from ..security import (
    has_any_permission,
    has_permission,
    is_admin,
    load_actor,
    login_required,
    permission_required,
    verify_password,
)
from ..utils import (
    api_error,
    api_list,
    api_ok,
    get_client_ip,
    get_user_agent,
    page_args,
    parse_int,
)

bp = Blueprint("ai", __name__, url_prefix="/api/ai")

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

AI_PERMISSION_LABELS = {
    "ai:view": "查看 AI 对话",
    "ai:use": "使用 AI 助手",
    "ai:view_all": "查看所有人 AI 对话",
    "ai:tool": "AI 只读工具",
    "ai:tool_write": "AI 写入工具",
    "ai:tool_exec": "AI 远程执行工具",
    "ai:manage": "管理 AI 对话",
}


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return header.strip()


def _caller(source: str = "web"):
    actor = load_actor()
    if actor is None:
        return None
    return ai_service.Caller.from_user(
        actor,
        token=_bearer_token(),
        source=source,
        ip=get_client_ip(),
        user_agent=get_user_agent(),
    )


def _ai_client():
    """返回 (client, error)；未启用或未配置 API Key 时给出可操作的中文提示。"""
    if not bool(current_app.config.get("AI_ENABLED")):
        return None, api_error("AI 运维已关闭（.env 里 AI_ENABLED=0）", 403, code="AI_DISABLED")
    if not str(current_app.config.get("DEEPSEEK_API_KEY") or "").strip():
        return None, api_error(
            "尚未配置 DEEPSEEK_API_KEY：请写入 bastion-backend/.env 后重启服务",
            400,
            code="AI_NOT_CONFIGURED",
        )
    try:
        return create_client(current_app.config), None
    except AiError as exc:
        return None, api_error(str(exc), 400, code="AI_NOT_CONFIGURED")


def _conversation_for(conversation_id: int, caller):
    conversation = db.session.get(AiConversation, conversation_id)
    if conversation is None:
        return None, api_error("对话不存在", 404, code="NOT_FOUND")
    view_all = has_any_permission(
        _actor(), "ai:view_all", "ai:manage"
    )
    if not view_all and conversation.user_id != caller.id:
        return None, api_error("无权查看该对话", 403, code="FORBIDDEN")
    return conversation, None


def _actor():
    return load_actor()


def _payload() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------- #
# 状态与工具目录
# --------------------------------------------------------------------------- #
@bp.get("/status")
@login_required
def ai_status():
    actor = load_actor()
    caller = _caller()
    enabled = bool(current_app.config.get("AI_ENABLED")) and bool(
        current_app.config.get("DEEPSEEK_API_KEY")
    )
    return api_ok(
        {
            "enabled": enabled,
            "featureEnabled": bool(current_app.config.get("AI_ENABLED")),
            "configured": bool(current_app.config.get("DEEPSEEK_API_KEY")),
            "model": current_app.config.get("AI_MODEL", ""),
            "baseUrl": current_app.config.get("DEEPSEEK_BASE_URL", ""),
            "canUse": has_permission(actor, "ai:use"),
            "canViewAll": has_any_permission(actor, "ai:view_all", "ai:manage"),
            "canManage": has_permission(actor, "ai:manage"),
            "confirmTtl": int(current_app.config.get("AI_CONFIRM_TTL", 300)),
            "maxToolRounds": int(current_app.config.get("AI_MAX_TOOL_ROUNDS", 8)),
            "historyLimit": int(current_app.config.get("AI_HISTORY_LIMIT", 40)),
            "toolCount": len(caller.available_tools()) if caller else 0,
            "totalToolCount": len(tool_registry.TOOLS),
            "permissions": sorted(
                code for code in (actor.permission_set() if actor else set()) if code.startswith("ai:")
            ),
            "permissionLabels": AI_PERMISSION_LABELS,
        }
    )


@bp.get("/tools")
@login_required
def ai_tools():
    caller = _caller()
    if caller is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    return api_ok(
        {
            "items": caller.available_tools(),
            "groups": tool_registry.grouped_catalog(caller.available_tools()),
            "total": len(tool_registry.TOOLS),
            "available": len(caller.available_tools()),
        }
    )


# --------------------------------------------------------------------------- #
# 流式对话
# --------------------------------------------------------------------------- #
@bp.post("/chat")
@permission_required("ai:use")
def ai_chat():
    caller = _caller(str(_payload().get("source") or "web"))
    if caller is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    client, error = _ai_client()
    if error is not None:
        return error

    payload = _payload()
    text = str(payload.get("message") or "").strip()
    conversation_id = parse_int(payload.get("conversationId"))
    resume = bool(payload.get("resume"))

    conversation = None
    if conversation_id:
        conversation, error = _conversation_for(conversation_id, caller)
        if error is not None:
            return error
    elif not text:
        return api_error("message 不能为空", 400, code="INVALID_ARGUMENT")

    if conversation is None:
        host = None
        host_id = parse_int(payload.get("hostId"))
        if host_id:
            from ..models import Host

            host = db.session.get(Host, host_id)
        conversation = ai_service.create_conversation(
            caller,
            title=text[:60] or "新的对话",
            source=str(payload.get("source") or "web"),
            model=current_app.config.get("AI_MODEL", ""),
            host=host,
            sid=str(payload.get("sid") or ""),
        )

    if resume and not text and not ai_service.pending_tool_calls(conversation):
        return api_error("没有待确认的操作", 400, code="NOTHING_TO_RESUME")

    return Response(
        stream_with_context(_stream_turn(conversation.id, caller, client, text=text, resume=resume)),
        mimetype="text/event-stream",
        headers=SSE_HEADERS,
    )


def _stream_turn(conversation_id: int, caller, client, *, text: str, resume: bool):
    """把编排层的事件序列包成 SSE，并保证任何异常都能以 error 事件收尾。

    这里**只接收 conversation_id 而不是 ORM 对象**：流式响应真正开始产出时，创建
    对话的那个请求上下文往往已经结束（SQLAlchemy 会话被回收），此时读取
    ``conversation.id`` 会抛 ``DetachedInstanceError``。所以在自己的 app context 里
    重新取一次对象，``open`` / ``close`` 与整个 ``run_turn`` 循环都在该上下文内完成。
    """
    app = current_app._get_current_object()
    yield ": aiops stream opened\n\n"
    with app.app_context():
        conversation = db.session.get(AiConversation, conversation_id)
        if conversation is None:
            yield f"data: {json.dumps({'type': 'error', 'message': '对话不存在或已被删除'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'close', 'conversationId': conversation_id}, ensure_ascii=False)}\n\n"
            return
        yield f"data: {json.dumps({'type': 'open', 'conversationId': conversation_id}, ensure_ascii=False)}\n\n"
        try:
            for event in ai_service.run_turn(
                app,
                conversation,
                caller,
                client,
                user_text=text,
                resume=resume,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except AiError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc), 'detail': exc.to_dict()}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 流里必须把异常变成可读事件
            app.logger.exception("AI 对话执行失败 conversation=%s", conversation_id)
            yield f"data: {json.dumps({'type': 'error', 'message': f'AI 执行失败：{exc}'}, ensure_ascii=False)}\n\n"
        finally:
            yield f"data: {json.dumps({'type': 'close', 'conversationId': conversation_id}, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
# 敏感操作确认
# --------------------------------------------------------------------------- #
@bp.post("/confirm")
@permission_required("ai:use")
def ai_confirm():
    caller = _caller()
    if caller is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    payload = _payload()
    conversation, error = _conversation_for(parse_int(payload.get("conversationId")) or 0, caller)
    if error is not None:
        return error

    pending = ai_service.pending_tool_calls(conversation)
    if not pending:
        return api_error("该对话没有待确认的敏感操作", 400, code="NOTHING_TO_APPROVE")

    target_id = parse_int(payload.get("toolCallId"))
    if target_id:
        pending = [row for row in pending if row.id == target_id]
        if not pending:
            return api_error("待确认记录不存在或已处理", 404, code="NOT_FOUND")

    approve = payload.get("approve")
    approve = True if approve is None else str(approve).lower() in ("1", "true", "yes", "on")

    actor = load_actor()
    if not approve:
        for row in pending:
            ai_service.reject_tool_call(
                row,
                rejected_by=actor.username,
                reason=str(payload.get("reason") or "用户在 AI 对话里取消了该操作"),
                actor_id=actor.id,
                ip=get_client_ip(),
                user_agent=get_user_agent(),
            )
        return api_ok({"approved": 0, "rejected": len(pending)}, "已取消该敏感操作")

    username = str(payload.get("adminUsername") or payload.get("username") or "").strip()
    password = str(payload.get("adminPassword") or payload.get("password") or "")
    if not password:
        return api_error("请输入管理员密码", 400, code="INVALID_ARGUMENT")
    # 前端把账号框标成「管理员账号（留空则用当前账号）」：留空就用**当前登录者自己**再验一次口令，
    # 权限判定一点没放松（非管理员照样 403、口令错照样 403），只是不再因为少填一个账号框报 400。
    username = username or actor.username
    checker = User.query.filter(
        func.lower(User.username) == username.strip().lower()
    ).first()
    if checker is None or not checker.is_active() or checker.is_locked():
        log_event(
            "ai",
            "ai_tool_confirm",
            result="failure",
            message=f"敏感操作确认失败：管理员账号 {username} 不可用",
            detail={"conversationId": conversation.id},
            actor_username=username,
            ip=get_client_ip(),
            user_agent=get_user_agent(),
        )
        return api_error("管理员账号不存在、已停用或已锁定", 403, code="FORBIDDEN")
    if not is_admin(checker) or not verify_password(password, checker.password_hash):
        log_event(
            "ai",
            "ai_tool_confirm",
            result="failure",
            message=f"敏感操作确认失败：{username} 密码错误或无管理员权限",
            detail={"conversationId": conversation.id},
            actor_username=username,
            ip=get_client_ip(),
            user_agent=get_user_agent(),
        )
        return api_error("管理员账号或密码错误", 403, code="FORBIDDEN")

    for row in pending:
        ai_service.approve_tool_call(
            row,
            approved_by=checker.username,
            actor_id=checker.id,
            ip=get_client_ip(),
            user_agent=get_user_agent(),
        )
    preview = [
        {"id": row.id, "tool": row.tool_name, "arguments": row.arguments or {}, "sensitive": row.sensitive}
        for row in pending
    ]
    return api_ok({"approved": len(pending), "rejected": 0, "items": preview}, "已确认，正在继续执行")


# --------------------------------------------------------------------------- #
# 对话审计
# --------------------------------------------------------------------------- #
@bp.get("/conversations")
@permission_required("ai:view")
def list_conversations():
    actor = load_actor()
    page, size = page_args(default_size=20)
    query = AiConversation.query
    if not has_any_permission(actor, "ai:view_all", "ai:manage"):
        query = query.filter(AiConversation.user_id == actor.id)
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                AiConversation.title.like(like),
                AiConversation.username.like(like),
                AiConversation.host_name.like(like),
            )
        )
    source = (request.args.get("source") or "").strip()
    if source:
        query = query.filter(AiConversation.source == source)
    username = (request.args.get("username") or "").strip()
    if username:
        query = query.filter(AiConversation.username == username)
    total = query.count()
    rows = (
        query.order_by(AiConversation.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return api_list([item.to_dict() for item in rows], total, page, size)


@bp.get("/conversations/<int:conversation_id>")
@permission_required("ai:view")
def get_conversation(conversation_id: int):
    caller = _caller()
    if caller is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    conversation, error = _conversation_for(conversation_id, caller)
    if error is not None:
        return error
    calls = (
        AiToolCall.query.filter_by(conversation_id=conversation.id)
        .order_by(AiToolCall.id)
        .all()
    )
    data = conversation.to_dict(with_messages=True)
    data["toolCalls"] = [item.to_dict() for item in calls]
    data["pendingConfirm"] = [
        {"id": row.id, "tool": row.tool_name, "arguments": row.arguments or {}, "sensitive": row.sensitive}
        for row in calls
        if row.status == "pending"
    ]
    return api_ok(data)


@bp.delete("/conversations/<int:conversation_id>")
@permission_required("ai:view")
def delete_conversation(conversation_id: int):
    caller = _caller()
    if caller is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    conversation, error = _conversation_for(conversation_id, caller)
    if error is not None:
        return error
    can_manage = has_permission(load_actor(), "ai:manage")
    if conversation.user_id != caller.id and not can_manage:
        return api_error("只能删除自己的对话", 403, code="FORBIDDEN")
    title = conversation.title
    AiMessage.query.filter_by(conversation_id=conversation.id).delete()
    AiToolCall.query.filter_by(conversation_id=conversation.id).delete()
    db.session.delete(conversation)
    db.session.commit()
    log_event(
        "ai",
        "ai_conversation_delete",
        target_type="ai_conversation",
        target_id=conversation_id,
        target_name=title,
        message=f"删除 AI 对话「{title}」",
        actor_username=caller.username,
        actor_id=caller.id,
        ip=get_client_ip(),
        user_agent=get_user_agent(),
    )
    return api_ok({"id": conversation_id}, "已删除")


@bp.get("/tool-calls")
@permission_required("ai:view")
def list_tool_calls():
    actor = load_actor()
    page, size = page_args(default_size=20)
    query = AiToolCall.query
    if not has_any_permission(actor, "ai:view_all", "ai:manage"):
        query = query.filter(AiToolCall.user_id == actor.id)
    conversation_id = parse_int(request.args.get("conversationId"))
    if conversation_id:
        query = query.filter(AiToolCall.conversation_id == conversation_id)
    tool_name = (request.args.get("toolName") or "").strip()
    if tool_name:
        query = query.filter(AiToolCall.tool_name == tool_name)
    status = (request.args.get("status") or "").strip()
    if status:
        query = query.filter(AiToolCall.status == status)
    username = (request.args.get("username") or "").strip()
    if username:
        query = query.filter(AiToolCall.username == username)
    if str(request.args.get("sensitive") or "").lower() in ("1", "true", "yes"):
        query = query.filter(AiToolCall.sensitive.is_(True))
    total = query.count()
    rows = query.order_by(AiToolCall.id.desc()).offset((page - 1) * size).limit(size).all()
    return api_list([item.to_dict() for item in rows], total, page, size)


# --------------------------------------------------------------------------- #
# AI 助手配置（管理员）
# --------------------------------------------------------------------------- #
@bp.get("/settings")
@permission_required("ai:manage")
def ai_settings():
    return api_ok(
        {
            "enabled": bool(current_app.config.get("AI_ENABLED")),
            "configured": bool(current_app.config.get("DEEPSEEK_API_KEY")),
            "model": current_app.config.get("AI_MODEL", ""),
            "baseUrl": current_app.config.get("DEEPSEEK_BASE_URL", ""),
            "maxTokens": int(current_app.config.get("AI_MAX_TOKENS", 8192)),
            "maxToolRounds": int(current_app.config.get("AI_MAX_TOOL_ROUNDS", 8)),
            "confirmTtl": int(current_app.config.get("AI_CONFIRM_TTL", 300)),
            "historyLimit": int(current_app.config.get("AI_HISTORY_LIMIT", 40)),
            "timeout": int(current_app.config.get("AI_REQUEST_TIMEOUT", 120)),
            "toolCount": len(tool_registry.TOOLS),
            "envFile": "bastion-backend/.env",
        },
        "AI 配置来自 .env（修改后需重启服务）",
    )
