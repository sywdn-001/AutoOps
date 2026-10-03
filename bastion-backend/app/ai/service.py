"""AI 运维对话编排：把「用户提问 → 模型流式回答 → 工具调用 → 审计」串成一条链。

一条 AI 回合的生命周期::

    user 消息（入审计）
      └─ 模型流式生成
           ├─ reasoning_content → SSE reasoning 事件（前端折叠展示「思考过程」）
           ├─ content           → SSE content 事件（前端流式 Markdown）
           └─ tool_calls        → 权限判定
                ├─ 无权限     → AiToolCall(denied) + 审计 + 回灌模型
                ├─ 敏感操作   → AiToolCall(pending) + SSE confirm_required（暂停，等管理员密码）
                └─ 允许       → 调自己的 REST API → AiToolCall(success/failure) + 审计 + 回灌模型
      └─ 循环直到模型不再请求工具，或达到 AI_MAX_TOOL_ROUNDS

**所有**环节都落库：``AiMessage`` 存原话与输出，``AiToolCall`` 存工具名/参数/结果/提权人，
``AuditLog(category="ai")`` 存事件级流水，于是「用户说了什么、AI 答了什么、调用了什么」可查。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from ..audit import log_event
from ..extensions import db
from ..models import AiConversation, AiMessage, AiToolCall, User
from ..security import has_permission, is_admin as user_is_admin
from . import tools as tool_registry
from .client import AiError, DeepSeekClient
from .prompt import system_prompt

logger = logging.getLogger("bastion.ai")

CARD_RE = re.compile(r"```ai-card\s*\n(.*?)```", re.S)
CARD_TYPES = {"table", "keyvalue", "alert", "steps", "list", "markdown"}
MAX_ARG_PREVIEW = 600


# --------------------------------------------------------------------------
# 调用者上下文
# --------------------------------------------------------------------------
@dataclass
class Caller:
    """一次 AI 调用的发起者（权限判定与审计都用它）。"""

    id: int
    username: str
    display_name: str = ""
    role_name: str = ""
    role_code: str = ""
    is_admin: bool = False
    permissions: tuple[str, ...] = ()
    token: str = ""
    source: str = "web"
    ip: str = ""
    user_agent: str = ""

    @classmethod
    def from_user(cls, user: User, *, token: str = "", source: str = "web", ip: str = "", user_agent: str = "") -> "Caller":
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name or user.username,
            role_name=getattr(user, "role_name", "") or "",
            role_code=user.role_code or "",
            is_admin=user_is_admin(user),
            permissions=tuple(sorted(user.permission_set())),
            token=token,
            source=source,
            ip=ip,
            user_agent=user_agent,
        )

    def available_tools(self):
        return tool_registry.tools_for_permission(self.permissions, is_admin=self.is_admin)


def _caller_can(caller: Caller, tool) -> bool:
    if caller.is_admin:
        return True
    return tool.permission in caller.permissions


# --------------------------------------------------------------------------
# 卡片解析
# --------------------------------------------------------------------------
def extract_cards(text: str) -> list[dict]:
    """把正文里的 ```ai-card 代码块解析成结构化卡片（非法 JSON 直接丢弃）。"""
    cards: list[dict] = []
    for raw in CARD_RE.findall(text or ""):
        raw = raw.strip()
        if not raw:
            continue
        try:
            card = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(card, dict):
            continue
        card_type = str(card.get("type") or "").lower()
        if card_type not in CARD_TYPES:
            continue
        card["type"] = card_type
        card.setdefault("title", "")
        cards.append(card)
    return cards[:5]


def strip_cards(text: str) -> str:
    """给终端用的正文（去掉卡片块，终端自己渲染成文本表）。"""
    return CARD_RE.sub("", text or "").strip()


# --------------------------------------------------------------------------
# 会话 / 消息
# --------------------------------------------------------------------------
def create_conversation(
    caller: Caller,
    *,
    title: str = "",
    source: str = "web",
    model: str = "",
    host: dict | None = None,
    sid: str = "",
) -> AiConversation:
    conversation = AiConversation(
        title=(title or "").strip()[:128] or "新的运维对话",
        user_id=caller.id,
        username=caller.username,
        source=source or caller.source,
        model=model,
        host_id=(host or {}).get("id"),
        host_name=(host or {}).get("name") or "",
        host_address=(host or {}).get("address") or "",
        sid=sid or "",
    )
    db.session.add(conversation)
    db.session.commit()
    log_event(
        "ai",
        "ai_conversation_start",
        target_type="ai_conversation",
        target_id=conversation.id,
        target_name=conversation.title,
        detail={"source": conversation.source, "model": model, "host": conversation.host_name},
        actor_username=caller.username,
        actor_id=caller.id,
        ip=caller.ip,
        user_agent=caller.user_agent,
    )
    return conversation


def add_message(
    conversation: AiConversation,
    role: str,
    content: str = "",
    *,
    reasoning: str = "",
    tool_calls: list | None = None,
    tool_call_id: str = "",
    cards: list | None = None,
    status: str = "done",
    model: str = "",
    usage: dict | None = None,
    finish_reason: str = "",
    elapsed_ms: int = 0,
) -> AiMessage:
    usage = usage or {}
    message = AiMessage(
        conversation_id=conversation.id,
        role=role,
        content=content or "",
        reasoning=reasoning or "",
        tool_calls=list(tool_calls or []),
        cards=list(cards or []),
        status=status,
        model=model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
        finish_reason=finish_reason or "",
        elapsed_ms=int(elapsed_ms or 0),
    )
    if tool_call_id:
        message.tool_call_id = tool_call_id
    db.session.add(message)
    conversation.message_count = (conversation.message_count or 0) + 1
    if role == "assistant":
        conversation.token_count = (conversation.token_count or 0) + int(usage.get("total_tokens") or 0)
    db.session.commit()
    return message


def _tool_call_ids(tool_calls: list | None) -> list[str]:
    """取出 assistant 消息里上游给的 tool_call id（没有 id 的跳过）。"""
    ids: list[str] = []
    for call in tool_calls or []:
        if isinstance(call, dict) and str(call.get("id") or ""):
            ids.append(str(call["id"]))
    return ids


def _tool_call_name(call: dict) -> str:
    function = (call or {}).get("function") or {}
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def _unanswered_tool_message(call: dict) -> str:
    """给「永远不会有结果」的 tool_call 补一条可读的 tool 响应。"""
    return (
        f"[工具 {_tool_call_name(call) or '未知'}] 未执行："
        "该调用没有执行结果（未获管理员确认、已被取消或已作废）"
    )


def _append_unexecuted_tool_message(tool_call: AiToolCall, content: str) -> bool:
    """把「没执行」的结论写回对话，保证 assistant.tool_calls 一定配得上 tool 响应。

    上游是 OpenAI 兼容协议：assistant 声明了 ``tool_calls``，后面就必须紧跟同
    ``tool_call_id`` 的 ``role=tool`` 消息；缺一条整段历史就是非法报文 —— DeepSeek
    直接回 HTTP 400，而且这个对话之后**每次**请求都会 400（历史是累积的）。
    管理员拒绝/取消敏感操作时最容易踩：那条 tool_call 永远不会有真实结果。
    """
    call_id = tool_call.call_id or ""
    conversation = db.session.get(AiConversation, tool_call.conversation_id)
    if not call_id or conversation is None:
        return False
    exists = AiMessage.query.filter_by(
        conversation_id=conversation.id, role="tool", tool_call_id=call_id
    ).first()
    if exists is not None:
        return False
    add_message(conversation, "tool", content, tool_call_id=call_id, status=tool_call.status)
    return True


def build_messages(
    conversation: AiConversation,
    caller: Caller,
    *,
    limit: int = 40,
    tool_count: int = 0,
) -> list[dict]:
    """把库里存的对话还原成 OpenAI 兼容的消息数组。

    还原时做一次**协议修复**：上游要求 ``assistant.tool_calls`` 里每个 id 后面都紧跟
    同 id 的 ``role=tool`` 响应，反过来 ``role=tool`` 也必须能对上前面某个
    ``assistant.tool_calls``。历史里出现悬空是常态（管理员拒绝、用户没确认就继续说话、
    或 ``limit`` 把窗口切在中间），不修就是 HTTP 400。
    """
    host = None
    if conversation.host_id:
        host = {"id": conversation.host_id, "name": conversation.host_name, "address": conversation.host_address}
    messages: list[dict] = [
        {
            "role": "system",
            "content": system_prompt(
                username=caller.username,
                display_name=caller.display_name,
                role_name=caller.role_name or caller.role_code,
                is_admin=caller.is_admin,
                tool_count=tool_count,
                host=host,
            ),
        }
    ]
    history = conversation.messages[-limit:] if limit else list(conversation.messages)
    declared: set[str] = set()
    for message in history:
        if message.role == "assistant" and message.tool_calls:
            declared.update(_tool_call_ids(message.tool_calls))
    answered = {
        message.tool_call_id
        for message in history
        if message.role == "tool" and message.tool_call_id
    }
    for message in history:
        if message.role == "user":
            messages.append({"role": "user", "content": message.content or ""})
        elif message.role == "assistant":
            if message.status == "error":
                # 「（调用模型失败：…）」是堡垒机自己的报错占位，不是模型说的话，回灌会污染上下文
                continue
            entry: dict = {"role": "assistant", "content": message.content or ""}
            calls = list(message.tool_calls or [])
            if calls:
                entry["tool_calls"] = calls
            messages.append(entry)
            for call in calls:
                call_id = str((call or {}).get("id") or "")
                if call_id and call_id not in answered:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _unanswered_tool_message(call),
                        }
                    )
        elif message.role == "tool":
            if message.tool_call_id and message.tool_call_id not in declared:
                continue  # 窗口切在中间的孤儿 tool 消息，发出去就是 400
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
            )
        elif message.role == "system":
            messages.append({"role": "system", "content": message.content or ""})
    return messages


# --------------------------------------------------------------------------
# 敏感操作确认
# --------------------------------------------------------------------------
def pending_tool_calls(conversation: AiConversation) -> list[AiToolCall]:
    """待处理的敏感操作：**未批准（pending）与已批准待执行（approved）**都算。

    批准后必须仍然能被 ``run_turn(resume=True)`` 捡起来执行，所以这里不能只查
    ``status == "pending"``——否则管理员输完密码，命令就永远躺在库里不执行了。
    """
    rows = (
        AiToolCall.query.filter(
            AiToolCall.conversation_id == conversation.id,
            AiToolCall.status.in_(("pending", "approved")),
        )
        .order_by(AiToolCall.id)
        .all()
    )
    return rows


def _host_key_from_args(tool, args: dict) -> str:
    for key in ("hostId", "host_id", "host"):
        if key in (args or {}):
            return f"{key}={args[key]}"
    if tool.permission == tool_registry.READ:
        return ""
    return ""


def find_fresh_approval(
    conversation: AiConversation, tool, args: dict, *, ttl: int
) -> AiToolCall | None:
    """同一个会话里，同一个工具 + 同一个目标主机，在 TTL 内批准过就直接放行。

    这样管理员输一次密码就能连续执行多条命令，而不是每条命令弹一次窗口。
    """
    if ttl <= 0:
        return None
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=ttl)
    rows = (
        AiToolCall.query.filter(
            AiToolCall.conversation_id == conversation.id,
            AiToolCall.tool_name == tool.name,
            AiToolCall.status.in_(("approved", "success")),
            AiToolCall.confirmed_at.isnot(None),
            AiToolCall.confirmed_at >= since,
        )
        .order_by(AiToolCall.id.desc())
        .limit(10)
        .all()
    )
    key = _host_key_from_args(tool, args)
    for row in rows:
        if _host_key_from_args(tool, row.arguments or {}) == key:
            return row
    return None


def approve_tool_call(
    tool_call: AiToolCall,
    *,
    approved_by: str,
    actor_id: int | None = None,
    ip: str = "",
    user_agent: str = "",
) -> AiToolCall:
    tool_call.status = "approved"
    tool_call.confirmed_by = approved_by
    tool_call.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    log_event(
        "ai",
        "ai_tool_confirm",
        target_type="ai_tool_call",
        target_id=tool_call.id,
        target_name=tool_call.tool_name,
        message=f"管理员 {approved_by} 批准了敏感操作 {tool_call.tool_name}",
        detail={
            "conversationId": tool_call.conversation_id,
            "tool": tool_call.tool_name,
            "arguments": _json_preview(tool_call.arguments),
            "requestedBy": tool_call.username,
        },
        actor_username=approved_by,
        actor_id=actor_id,
        ip=ip,
        user_agent=user_agent,
    )
    return tool_call


def reject_tool_call(
    tool_call: AiToolCall,
    *,
    rejected_by: str,
    reason: str = "",
    actor_id: int | None = None,
    ip: str = "",
    user_agent: str = "",
) -> AiToolCall:
    tool_call.status = "rejected"
    tool_call.confirmed_by = rejected_by
    tool_call.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    tool_call.error = (reason or "管理员拒绝了该操作")[:512]
    # 拒绝同样是这个 tool_call 的**结局**，必须写回一条 tool 消息：否则 assistant 里那条
    # tool_calls 永远悬空，下一次请求会被上游判成非法报文（HTTP 400），整个对话报废。
    _append_unexecuted_tool_message(
        tool_call,
        f"[工具 {tool_call.tool_name}] 未执行：管理员 {rejected_by} 拒绝（或取消）了该操作："
        f"{tool_call.error}",
    )
    db.session.commit()
    log_event(
        "ai",
        "ai_tool_reject",
        result="failure",
        target_type="ai_tool_call",
        target_id=tool_call.id,
        target_name=tool_call.tool_name,
        message=f"管理员 {rejected_by} 拒绝（或取消）了 {tool_call.tool_name}",
        detail={"conversationId": tool_call.conversation_id, "reason": reason},
        actor_username=rejected_by,
        actor_id=actor_id,
        ip=ip,
        user_agent=user_agent,
    )
    return tool_call


# --------------------------------------------------------------------------
# 工具执行
# --------------------------------------------------------------------------
def execute_tool(
    app,
    conversation: AiConversation,
    caller: Caller,
    tool,
    call_id: str,
    args: dict,
    *,
    record: AiToolCall | None = None,
) -> dict:
    """真的调一次自己的 REST API，并把过程落库 + 入审计。

    ``record`` 非空时更新那条既有记录（敏感操作确认后继续执行同一笔调用），
    否则新建一条。
    """
    started = time.time()
    result = tool_registry.invoke(app, tool, args, caller.token)
    duration = int((time.time() - started) * 1000)
    summary, preview, card = tool_registry.summarize(
        tool, result, limit=int(app.config.get("AI_TOOL_RESULT_LIMIT") or 8000)
    )

    if record is None:
        record = AiToolCall(
            conversation_id=conversation.id,
            call_id=call_id or "",
            user_id=caller.id,
            username=caller.username,
            tool_name=tool.name,
            arguments=args or {},
            required_permission=tool.permission,
            sensitive=bool(tool.sensitive),
        )
        db.session.add(record)
    record.status = "success" if result.get("ok") else "failure"
    record.result_summary = summary[:512]
    record.result_preview = preview
    record.error = "" if result.get("ok") else summary[:512]
    record.duration_ms = duration
    conversation.tool_count = (conversation.tool_count or 0) + 1
    db.session.commit()

    log_event(
        "ai",
        "ai_tool_call",
        result="success" if result.get("ok") else "failure",
        message=f"AI 调用工具 {tool.name}：{summary}"[:512],
        target_type="ai_tool_call",
        target_id=record.id,
        target_name=tool.name,
        detail={
            "conversationId": conversation.id,
            "tool": tool.name,
            "arguments": _json_preview(args),
            "status": result.get("status"),
            "http": record.status,
            "durationMs": duration,
            "sensitive": bool(tool.sensitive),
            "confirmedBy": record.confirmed_by,
        },
        actor_username=caller.username,
        actor_id=caller.id,
        ip=caller.ip,
        user_agent=caller.user_agent,
    )

    payload = result.get("data")
    if isinstance(payload, dict) and "data" in payload:
        payload = payload.get("data")
    return {
        "record": record,
        "ok": bool(result.get("ok")),
        "summary": summary,
        "preview": preview,
        "card": card,
        "status": result.get("status"),
        "tool_message": _tool_message(tool, result, summary, preview),
    }


def _tool_message(tool, result: dict, summary: str, preview: str) -> str:
    """回灌给模型的内容：成功给数据，失败给可读错误。"""
    head = f"[工具 {tool.name}] {'成功' if result.get('ok') else '失败'}：{summary}"
    if not result.get("ok"):
        head += f"\nHTTP {result.get('status')}"
    return f"{head}\n{preview}"[: int(12000)]


def _json_preview(value: Any, limit: int = MAX_ARG_PREVIEW) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _deny_tool(app, conversation: AiConversation, caller: Caller, tool, call_id: str, args: dict, reason: str) -> dict:
    record = AiToolCall(
        conversation_id=conversation.id,
        call_id=call_id or "",
        user_id=caller.id,
        username=caller.username,
        tool_name=tool.name,
        arguments=args or {},
        required_permission=tool.permission,
        sensitive=bool(tool.sensitive),
        status="denied",
        result_summary=reason[:512],
        error=reason[:512],
    )
    db.session.add(record)
    db.session.commit()
    log_event(
        "ai",
        "ai_tool_denied",
        result="failure",
        message=f"AI 越权调用被拒绝：{tool.name}（需要 {tool.permission}）",
        target_type="ai_tool_call",
        target_id=record.id,
        target_name=tool.name,
        detail={"conversationId": conversation.id, "reason": reason, "arguments": _json_preview(args)},
        actor_username=caller.username,
        actor_id=caller.id,
        ip=caller.ip,
        user_agent=caller.user_agent,
    )
    return {
        "record": record,
        "ok": False,
        "summary": reason,
        "preview": reason,
        "card": None,
        "status": 403,
        "tool_message": f"[工具 {tool.name}] 被拒绝：{reason}",
    }


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def _client_model(client) -> str:
    """取客户端的模型名；对 duck-typed / 测试替身也安全。"""
    return str(getattr(client, "model", "") or "")


def run_turn(
    app,
    conversation: AiConversation,
    caller: Caller,
    client: DeepSeekClient,
    *,
    user_text: str = "",
    resume: bool = False,
) -> Iterator[dict]:
    """执行一个 AI 回合，产出 SSE 事件字典（由 api 层序列化成 text/event-stream）。"""
    cfg = app.config
    limit = int(cfg.get("AI_HISTORY_LIMIT") or 40)
    max_rounds = max(1, int(cfg.get("AI_MAX_TOOL_ROUNDS") or 8))
    confirm_ttl = int(cfg.get("AI_CONFIRM_TTL") or 300)

    available = caller.available_tools()
    schemas = tool_registry.openai_schemas(available)

    if user_text and not resume:
        add_message(conversation, "user", user_text)
        log_event(
            "ai",
            "ai_chat",
            message=f"用户 {caller.username} 向 AI 提问：{user_text[:120]}",
            target_type="ai_conversation",
            target_id=conversation.id,
            target_name=conversation.title,
            detail={"chars": len(user_text), "source": caller.source},
            actor_username=caller.username,
            actor_id=caller.id,
            ip=caller.ip,
            user_agent=caller.user_agent,
        )
        if (conversation.title or "新的运维对话") == "新的运维对话":
            conversation.title = user_text.strip().splitlines()[0][:60] or "新的运维对话"
            db.session.commit()

    yield {
        "type": "start",
        "conversationId": conversation.id,
        "messageId": 0,
        "model": _client_model(client),
        "tools": len(available),
    }

    messages = build_messages(conversation, caller, limit=limit, tool_count=len(available))

    # ---- 1) 先处理上一轮挂起的敏感操作（管理员刚批过 / 还是没批） ----
    pending = pending_tool_calls(conversation)
    if pending:
        blocked: list[AiToolCall] = []
        for call in pending:
            tool = tool_registry.TOOL_INDEX.get(call.tool_name)
            if tool is None:
                call.status = "failure"
                call.error = "工具已不存在"
                db.session.commit()
                continue
            if call.status == "approved":
                outcome = execute_tool(
                    app, conversation, caller, tool, call.call_id, call.arguments or {}, record=call
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.call_id or "", "content": outcome["tool_message"]}
                )
                add_message(
                    conversation,
                    "tool",
                    outcome["tool_message"],
                    tool_call_id=call.call_id or "",
                    status="done",
                )
                yield {
                    "type": "tool_result",
                    "callId": call.call_id,
                    "name": tool.name,
                    "ok": outcome["ok"],
                    "status": outcome["status"],
                    "summary": outcome["summary"],
                    "preview": outcome["preview"][:4000],
                    "card": outcome["card"],
                    "durationMs": call.duration_ms,
                    "confirmedBy": call.confirmed_by,
                }
            else:
                blocked.append(call)
        if blocked:
            yield _confirm_event(blocked)
            return
        messages = build_messages(conversation, caller, limit=limit, tool_count=len(available))

    # ---- 2) 模型循环 ----
    for round_index in range(max_rounds):
        started = time.time()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_buffer: dict[int, dict] = {}
        usage: dict = {}
        finish_reason = ""
        try:
            for event in client.stream_chat(messages, tools=schemas):
                kind = event.get("type")
                if kind == "content":
                    content_parts.append(event["text"])
                    yield {"type": "content", "delta": event["text"]}
                elif kind == "reasoning":
                    reasoning_parts.append(event["text"])
                    yield {"type": "reasoning", "delta": event["text"]}
                elif kind == "tool_call":
                    index = int(event.get("index") or 0)
                    slot = tool_buffer.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if event.get("id"):
                        slot["id"] = event["id"]
                    if event.get("name"):
                        slot["name"] += event["name"]
                    if event.get("arguments"):
                        slot["arguments"] += event["arguments"]
                elif kind == "usage":
                    usage = event.get("usage") or {}
                elif kind == "finish":
                    finish_reason = event.get("reason") or ""
        except AiError as exc:
            yield {"type": "error", "message": exc.message, "status": exc.status}
            add_message(
                conversation,
                "assistant",
                f"（调用模型失败：{exc.message}）",
                status="error",
                finish_reason="error",
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        calls = [_tool_call_payload(slot, index) for index, slot in sorted(tool_buffer.items())]
        cards = extract_cards(content)
        assistant = add_message(
            conversation,
            "assistant",
            content,
            reasoning=reasoning,
            tool_calls=calls,
            cards=cards,
            status="awaiting_confirm" if calls else "done",
            model=_client_model(client),
            usage=usage,
            finish_reason=finish_reason,
            elapsed_ms=int((time.time() - started) * 1000),
        )

        if cards:
            yield {"type": "cards", "cards": cards}

        if not calls:
            yield {
                "type": "message_end",
                "messageId": assistant.id,
                "content": content,
                "reasoning": reasoning,
                "cards": cards,
                "usage": usage,
                "finishReason": finish_reason,
                "elapsedMs": assistant.elapsed_ms,
                "status": "done",
            }
            return

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})

        blocked: list[AiToolCall] = []
        for call in calls:
            name = call["function"]["name"]
            raw_args = call["function"]["arguments"]
            call_id = call.get("id") or ""
            args, parse_error = _parse_arguments(raw_args)
            if parse_error:
                tool_message = f"[工具 {name}] 参数不是合法 JSON：{raw_args[:200]}"
                messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_message})
                add_message(conversation, "tool", tool_message, tool_call_id=call_id)
                yield {
                    "type": "tool_result",
                    "callId": call_id,
                    "name": name,
                    "ok": False,
                    "summary": "工具参数解析失败",
                    "preview": raw_args[:400],
                    "card": None,
                }
                continue

            tool = tool_registry.TOOL_INDEX.get(name)
            if tool is None:
                tool_message = f"[工具 {name}] 不存在，请从可用工具里选择"
                messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_message})
                add_message(conversation, "tool", tool_message, tool_call_id=call_id)
                yield {
                    "type": "tool_result",
                    "callId": call_id,
                    "name": name,
                    "ok": False,
                    "summary": f"未知工具：{name} 不存在，请从可用工具里选择",
                    "preview": tool_message,
                    "card": None,
                }
                continue

            if tool not in available and not _caller_can(caller, tool):
                reason = f"调用者 {caller.username} 没有 {tool.permission} 权限"
                outcome = _deny_tool(app, conversation, caller, tool, call_id, args, reason)
                messages.append({"role": "tool", "tool_call_id": call_id, "content": outcome["tool_message"]})
                add_message(conversation, "tool", outcome["tool_message"], tool_call_id=call_id)
                yield {
                    "type": "tool_result",
                    "callId": call_id,
                    "name": tool.name,
                    "ok": False,
                    "status": 403,
                    "summary": reason,
                    "preview": reason,
                    "card": None,
                    "denied": True,
                }
                continue

            if tool.sensitive:
                fresh = find_fresh_approval(conversation, tool, args, ttl=confirm_ttl)
                if fresh is None:
                    record = AiToolCall(
                        conversation_id=conversation.id,
                        message_id=assistant.id,
                        call_id=call_id,
                        user_id=caller.id,
                        username=caller.username,
                        tool_name=tool.name,
                        arguments=args,
                        required_permission=tool.permission,
                        sensitive=True,
                        status="pending",
                    )
                    db.session.add(record)
                    db.session.commit()
                    blocked.append(record)
                    yield {
                        "type": "tool_call",
                        "callId": call_id,
                        "name": tool.name,
                        "args": args,
                        "sensitive": True,
                        "permission": tool.permission,
                        "status": "pending",
                        "description": tool.description,
                    }
                    continue

            yield {
                "type": "tool_call",
                "callId": call_id,
                "name": tool.name,
                "args": args,
                "sensitive": bool(tool.sensitive),
                "permission": tool.permission,
                "status": "running",
                "description": tool.description,
            }
            outcome = execute_tool(app, conversation, caller, tool, call_id, args)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": outcome["tool_message"]})
            add_message(conversation, "tool", outcome["tool_message"], tool_call_id=call_id)
            yield {
                "type": "tool_result",
                "callId": call_id,
                "name": tool.name,
                "ok": outcome["ok"],
                "status": outcome["status"],
                "summary": outcome["summary"],
                "preview": outcome["preview"][:4000],
                "card": outcome["card"],
                "durationMs": outcome["record"].duration_ms,
            }

        if blocked:
            yield _confirm_event(blocked)
            return

        if round_index == max_rounds - 1:
            note = f"（已达到单轮工具调用上限 {max_rounds} 次，先停下来，请确认后继续。）"
            add_message(conversation, "assistant", note, status="done")
            yield {"type": "content", "delta": note}
            yield {"type": "message_end", "messageId": assistant.id, "status": "max_rounds"}
            return

    yield {"type": "message_end", "messageId": 0, "status": "done"}


def _confirm_event(rows: list[AiToolCall]) -> dict:
    first = rows[0]
    return {
        "type": "confirm_required",
        "conversationId": first.conversation_id,
        "toolCalls": [
            {
                "id": row.id,
                "callId": row.call_id,
                "name": row.tool_name,
                "args": row.arguments or {},
                "description": (tool_registry.TOOL_INDEX.get(row.tool_name).description if tool_registry.TOOL_INDEX.get(row.tool_name) else ""),
                "permission": row.required_permission,
            }
            for row in rows
        ],
        "toolCallId": first.id,
        "callId": first.call_id,
        "name": first.tool_name,
        "tool": first.tool_name,
        "sensitive": True,
        "permission": first.required_permission,
        "count": len(rows),
        "args": first.arguments or {},
        "reason": "该操作会修改堡垒机配置或在目标机上执行动作，请输入管理员账号密码确认",
    }


def _tool_call_payload(slot: dict, index: int) -> dict:
    return {
        "id": slot.get("id") or f"call_{index}",
        "type": "function",
        "function": {
            "name": slot.get("name") or "",
            "arguments": slot.get("arguments") or "{}",
        },
    }


def _parse_arguments(raw: str | dict) -> tuple[dict, str]:
    if isinstance(raw, dict):
        return raw, ""
    text = (raw or "").strip() or "{}"
    try:
        value = json.loads(text)
    except ValueError as exc:
        return {}, str(exc)
    if not isinstance(value, dict):
        return {}, "参数必须是 JSON 对象"
    return value, ""
