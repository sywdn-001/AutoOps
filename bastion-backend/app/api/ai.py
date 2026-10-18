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
# 输入框的示例建议：按系统当前状态生成，24 小时换一轮，也可以手动换一批
# --------------------------------------------------------------------------- #
CHAT_SUGGESTION_COUNT = 4
COMPUTER_USE_SUGGESTION_COUNT = 3


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
    }
    try:
        from ..models import CommandLog, ComputerUseDeployment, Host, SessionRecord

        facts["hosts"] = Host.query.count()
        try:
            facts["windows"] = Host.query.filter(
                db.func.lower(Host.os_type).like("%windows%")
            ).count()
        except Exception:  # noqa: BLE001 - 列名变了也不影响主流程
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
    except Exception:  # noqa: BLE001 - 统计失败就全部退回「通用建议」
        pass
    return facts


def _chat_pool(facts: dict) -> list[dict]:
    """AI 运维页的建议池：先放与当前状态强相关的，再放几条通用兜底。"""
    pool: list[dict] = []

    def add(zh: str, en: str, when: bool = True) -> None:
        if when:
            pool.append({"zh": zh, "en": en})

    add(
        f"现在有 {facts['online']} 个在线会话，帮我看看它们分别是谁在用",
        f"There are {facts['online']} live sessions right now — who is using each of them?",
        facts["online"] > 0,
    )
    add(
        "列出所有在线会话，按开始时间排一下",
        "List every live session, ordered by start time",
        facts["online"] == 0,
    )
    add(
        f"纳管了 {facts['hosts']} 台主机（Windows {facts['windows']} / Linux {facts['linux']}），"
        "帮我挑三台做个健康巡检",
        f"{facts['hosts']} hosts are managed (Windows {facts['windows']} / Linux {facts['linux']}) — "
        "pick three and run a health check",
        facts["hosts"] > 0,
    )
    add(
        "帮我接入第一台主机，然后给它建一个只读账号",
        "Help me onboard the first host and create a read-only account for it",
        facts["hosts"] == 0,
    )
    add(
        f"最近有 {facts['denied']} 条命令被拦截，帮我按命令和主机归个类",
        f"{facts['denied']} commands have been blocked so far — group them by command and host",
        facts["denied"] > 0,
    )
    add(
        "审计一下最近的敏感命令，有高危的就标出来",
        "Audit recent sensitive commands and flag anything high risk",
        facts["denied"] == 0,
    )
    add(
        f"系统里现在有 {facts['users']} 个账号，帮我看看谁的权限给大了",
        f"There are {facts['users']} accounts — check which ones are over-privileged",
        facts["users"] > 1,
    )
    add(
        "新建一个只读用户，只给查看权限",
        "Create a read-only user with view-only permissions",
        facts["users"] <= 1,
    )
    add(
        "把最近 24 小时的登录失败和被拦截命令整理成一份小结",
        "Summarize the last 24 hours of failed logins and blocked commands",
    )
    add(
        "有哪些主机还没有分组？帮我整理一下",
        "Which hosts are not in any group yet? Help me tidy them up",
    )
    add(
        "看一下当前 AI 任务的最大调用轮数，并解释这个值怎么调",
        "Show the current max AI tool rounds and explain how to tune it",
    )
    add(
        "帮我写一条「拦截 rm -rf /」的命令策略并说明生效范围",
        "Write a command policy that blocks `rm -rf /` and explain its scope",
    )
    return pool


def _computer_use_pool(facts: dict) -> list[dict]:
    """ComputerUse 侧边栏的建议池：围绕「操作这台机器」给。"""
    pool: list[dict] = []

    def add(zh: str, en: str, when: bool = True) -> None:
        if when:
            pool.append({"zh": zh, "en": en})

    add(
        "把当前屏幕截一张图给我，说明现在停在哪个界面",
        "Take a screenshot and tell me which screen we are on",
    )
    add(
        "打开记事本，写上「今天的巡检结论：一切正常」并保存到桌面",
        'Open Notepad, type "Today\'s inspection: all good" and save it to the desktop',
    )
    add(
        "打开任务管理器，看看哪个进程最占内存",
        "Open Task Manager and find the process using the most memory",
        facts["cu_ready"] > 0,
    )
    add(
        "这台机器装的是什么版本的 Python？顺便看看 .NET 运行时",
        "Which Python version is installed here? Also check the .NET runtime",
    )
    add(
        "列一下现在正在运行的程序，把自启项也一并列出来",
        "List the running programs, including autostart entries",
    )
    add(
        "按 Win+R 打开运行框，执行 msinfo32，把系统摘要读给我",
        "Press Win+R, run msinfo32 and read the system summary to me",
        facts["windows"] > 0,
    )
    add(
        "把我刚打开的窗口关掉，回到桌面",
        "Close the windows I just opened and get back to the desktop",
    )
    add(
        "在开始菜单里找到「设置 → 系统 → 关于」，把设备名称读给我",
        "Open Settings → System → About and read me the device name",
    )
    if facts["cu_failed"] > 0:
        add(
            "这台机器的 MCP 上次部署失败过，帮我看看现在恢复没有",
            "The MCP deployment failed here before — check whether it is healthy now",
        )
    return pool


@bp.get("/suggestions")
@permission_required("ai:use")
def suggestions():
    """示例建议：同一天内稳定，换一天自动换一轮；`nonce` 递增则立刻换一批。"""
    scope = (request.args.get("scope") or "chat").strip().lower()
    nonce = parse_int(request.args.get("nonce"), 0) or 0
    facts = _state_facts()
    if scope == "computeruse":
        pool = _computer_use_pool(facts)
        size = COMPUTER_USE_SUGGESTION_COUNT
    else:
        pool = _chat_pool(facts)
        size = CHAT_SUGGESTION_COUNT
    today = datetime.date.today()
    if pool:
        start = (today.toordinal() + nonce * 7) % len(pool)
        items = [pool[(start + i) % len(pool)] for i in range(min(size, len(pool)))]
    else:  # pragma: no cover - 兜底，正常不会走到
        items = []
    return api_ok(
        {
            "bucket": today.isoformat(),
            "nonce": nonce,
            "scope": scope,
            "items": items,
            "facts": facts,
        }
    )
