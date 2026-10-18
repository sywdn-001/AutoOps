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

import datetime
import json
import re

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
            "maxToolRounds": ai_service.max_tool_rounds(current_app),
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
        # 收尾事件**不能**放在 finally 里：客户端关页面/断网时 Flask 会对本生成器调
        # close()，GeneratorExit 正好落在这条 yield 上；finally 里再 yield 一次就会抛
        # `RuntimeError: generator ignored GeneratorExit`（控制台那一行
        # 「Exception ignored in: <generator object stream_with_context...>」就是它）。
        # 放在这里：正常结束与异常收尾都会发；对端已经走了就没必要再发。
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
            "maxToolRounds": ai_service.max_tool_rounds(current_app),
            "confirmTtl": int(current_app.config.get("AI_CONFIRM_TTL", 300)),
            "historyLimit": int(current_app.config.get("AI_HISTORY_LIMIT", 40)),
            "timeout": int(current_app.config.get("AI_REQUEST_TIMEOUT", 120)),
            "toolCount": len(tool_registry.TOOLS),
            "envFile": "bastion-backend/.env",
        },
        "AI 配置来自 .env（修改后需重启服务）；「最大调用轮数」改在『系统设置 → 参数设置』里，保存即生效",
    )

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 输入框的示例建议：调 AI 按系统当前状态**实时生成**，24 小时缓存一轮，可手动换一批
# --------------------------------------------------------------------------- #
CHAT_SUGGESTION_COUNT = 4
COMPUTER_USE_SUGGESTION_COUNT = 3
SUGGESTION_PROMPT_VERSION = 2

_SUGGESTION_SYSTEM = """你是 AutoOps 堡垒机的运维助手，用户会在聊天框里看到你给的「快捷建议」。
只输出一个 JSON 数组，不要 markdown 代码块、不要任何解释文字。数组元素形如：
{"zh": "中文建议", "en": "English suggestion"}
每条都是**用户可以一键发给 AI 的具体动作**（查什么、在哪台机器上做什么、建什么策略），
中文 12~40 字、英文对应且同义，不要寒暄、不要编号、不要占位符。"""


def _state_facts() -> dict:
    """抓一份「现在系统长什么样」，抓不到的项就当 0（接口不能因为统计失败而 500）。"""
    facts = {
        "hosts": 0,
        "windows": 0,
        "linux": 0,
        "online": 0,
        "users": 0,
        "denied": 0,
        "cu_ready": 0,
        "cu_failed": 0,
        "hosts_preview": [],
        "sessions_preview": [],
    }
    try:
        from ..models import CommandLog, ComputerUseDeployment, Host, SessionRecord

        facts["hosts"] = Host.query.count()
        try:
            facts["windows"] = Host.query.filter(
                db.func.lower(Host.os_type).like("%windows%")
            ).count()
        except Exception:  # noqa: BLE001
            facts["windows"] = 0
        facts["linux"] = max(facts["hosts"] - facts["windows"], 0)
        facts["online"] = SessionRecord.query.filter_by(status="active").count()
        facts["users"] = User.query.count()
        try:
            facts["denied"] = CommandLog.query.filter_by(result="denied").count()
        except Exception:  # noqa: BLE001
            facts["denied"] = 0
        try:
            facts["cu_ready"] = ComputerUseDeployment.query.filter_by(status="success").count()
            facts["cu_failed"] = ComputerUseDeployment.query.filter_by(status="failed").count()
        except Exception:  # noqa: BLE001
            facts["cu_ready"] = 0
            facts["cu_failed"] = 0
        try:
            facts["hosts_preview"] = [
                {"name": h.name, "os": h.os_type, "address": h.address}
                for h in Host.query.order_by(Host.id.desc()).limit(4)
            ]
        except Exception:  # noqa: BLE001
            pass
        try:
            facts["sessions_preview"] = [
                {"user": s.username, "host": s.host_name, "protocol": s.protocol}
                for s in SessionRecord.query.filter_by(status="active").limit(4)
            ]
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    return facts


_FALLBACK_CHAT = [
    {"zh": "列出当前所有在线会话", "en": "List every live session"},
    {"zh": "有哪些主机？把最近新增的三台列出来", "en": "Which hosts exist? Show the three newest"},
    {"zh": "审计一下最近的敏感命令", "en": "Audit recent sensitive commands"},
    {"zh": "新建一个只读用户，只给查看权限", "en": "Create a read-only user with view-only rights"},
]
_FALLBACK_CU = [
    {"zh": "把当前屏幕截一张给我看", "en": "Take a screenshot of the current screen"},
    {"zh": "看看这台机器装的是什么版本的 Python", "en": "Check which Python version is installed here"},
    {"zh": "列一下正在运行的程序", "en": "List the programs that are running"},
]


def _suggest_key(scope: str) -> str:
    return f"ai_suggestions_v{SUGGESTION_PROMPT_VERSION}_{scope}"


def _read_suggestion_cache(scope: str) -> dict | None:
    from ..settings_store import get_setting

    raw = (get_setting(_suggest_key(scope), "") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _write_suggestion_cache(scope: str, payload: dict) -> None:
    from ..settings_store import set_setting

    try:
        set_setting(_suggest_key(scope), json.dumps(payload, ensure_ascii=False))
        db.session.commit()
    except Exception:  # noqa: BLE001 - 缓存写失败不影响本次返回
        db.session.rollback()


def _parse_items(text: str, size: int) -> list[dict]:
    """从模型输出里抠出 JSON 数组；抠不出来就返回空，让调用方走兜底。"""
    if not text:
        return []
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    raw = None
    try:
        raw = json.loads(body[start : end + 1])
    except Exception:  # noqa: BLE001 - 有些模型会在数组里塞注释/换行，退回逐个对象抠
        raw = None
    if raw is None:
        objects = re.findall(r"\{[^{}]*\}", body)
        raw = []
        for chunk in objects:
            try:
                raw.append(json.loads(chunk))
            except Exception:  # noqa: BLE001
                continue
    items, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        zh = str(entry.get("zh") or "").strip()
        en = str(entry.get("en") or "").strip()
        if not zh or not en or zh in seen:
            continue
        seen.add(zh)
        items.append({"zh": zh, "en": en})
        if len(items) >= size:
            break
    return items


def _generate_items(scope: str, facts: dict, size: int, avoid: list[str]) -> list[dict]:
    """调 AI 现生成一批建议；未配置 / 调用失败 / 解析失败都返回空，由上层兜底。"""
    client, error = _ai_client()
    if error or client is None:
        return []
    if scope == "computeruse":
        task = (
            f"场景：右侧是 Windows 远程桌面（ComputerUse）的侧边栏，AI 能直接操作这台机器的桌面。"
            f"请给 {size} 条「让 AI 在这台机器上点/看/运行什么」的建议。"
        )
    else:
        task = (
            f"场景：堡垒机后台的「AI 运维」聊天框，AI 能查资产/会话/审计、还能建策略与账号。"
            f"请给 {size} 条「管理员现在最可能想做」的建议。"
        )
    prompt = {
        "task": task,
        "system_state": facts,
        "avoid_previous": avoid,
        "rules": [
            f"正好 {size} 条",
            "每条都必须贴合上面的 system_state（比如有在线会话就问会话、没有主机就问怎么接入）",
            "具体可执行，不要泛泛而谈",
            "只输出 JSON 数组",
        ],
    }
    def _once(temperature: float) -> list[dict]:
        chunks: list[str] = []
        for event in client.stream_chat(
            [
                {"role": "system", "content": _SUGGESTION_SYSTEM},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            temperature=temperature,
            max_tokens=700,
        ):
            if event.get("type") == "content":
                chunks.append(event.get("text") or "")
        text = "".join(chunks)
        parsed = _parse_items(text, size)
        if not parsed:
            current_app.logger.warning("AI 建议解析失败，原始输出：%s", text[:600])
        return parsed

    try:
        items = _once(0.8)
        if len(items) < size:
            # 模型偶尔会少给或带解释文字：把温度调低重试一次
            items = _once(0.3)
        return items
    except Exception:  # noqa: BLE001 - 生成失败就走兜底，不能让输入框空着
        current_app.logger.warning("AI 建议生成失败", exc_info=True)
        return []


@bp.get("/suggestions")
@permission_required("ai:use")
def suggestions():
    """示例建议：调 AI 按当前状态现生成；同一天同一批缓存复用，`nonce` 变则立刻重新生成。"""
    scope = (request.args.get("scope") or "chat").strip().lower()
    nonce = parse_int(request.args.get("nonce"), 0) or 0
    today = datetime.date.today().isoformat()
    size = COMPUTER_USE_SUGGESTION_COUNT if scope == "computeruse" else CHAT_SUGGESTION_COUNT

    cached = _read_suggestion_cache(scope)
    if (
        cached
        and cached.get("bucket") == today
        and int(cached.get("nonce") or 0) == nonce
        and isinstance(cached.get("items"), list)
        and len(cached["items"]) == size
    ):
        return api_ok(cached)

    facts = _state_facts()
    avoid = [i.get("zh") for i in (cached or {}).get("items", []) if isinstance(i, dict)]
    items = _generate_items(scope, facts, size, avoid)
    ai_count = len(items)
    if ai_count < size:
        fallback = _FALLBACK_CU if scope == "computeruse" else _FALLBACK_CHAT
        have = {i["zh"] for i in items}
        for entry in fallback:
            if len(items) >= size:
                break
            if entry["zh"] not in have:
                items.append(entry)
                have.add(entry["zh"])
    source = "ai" if ai_count >= size else ("mixed" if ai_count else "fallback")
    payload = {
        "bucket": today,
        "nonce": nonce,
        "scope": scope,
        "source": source,
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "items": items,
        "facts": facts,
    }
    _write_suggestion_cache(scope, payload)
    return api_ok(payload)
