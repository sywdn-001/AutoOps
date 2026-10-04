"""会话与命令记录接口（审计回放 + 在线会话管理）。"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, request
from sqlalchemy import or_

from ..access import accessible_targets, serialize_target, target_protocol
from ..audit import close_session, log_command, log_event, new_session
from ..extensions import db
from ..models import CommandLog, SessionRecord
from ..security import (
    admin_required,
    has_permission,
    has_any_permission,
    load_actor,
    login_required,
    permission_required,
)
from ..session_registry import registry
from ..terminal.recorder import TranscriptRecorder, tail_stats
from ..utils import (
    api_error,
    api_list,
    api_ok,
    extract_ids,
    page_args,
    parse_bool,
    parse_int,
    purge_before,
)
from .rdp import RDP_PROTOCOL

log = logging.getLogger("bastion.api.sessions")

bp = Blueprint("sessions", __name__, url_prefix="/api")


DB_EXEC_OUTPUT_LIMIT = 100_000
"""一次性执行的输出入库上限（超长截断，避免撑爆 SQLite）。"""


def _visible_all(actor) -> bool:
    return has_any_permission(actor, "session:view_all", "command:view_all")


def _session_payload(session: SessionRecord, detail: bool = False) -> dict:
    data = session.to_dict(detail=detail)
    live = registry.get(session.sid)
    data["online"] = bool(live and session.status == "active")
    data["transcriptSize"] = TranscriptRecorder.size(session.transcript_path) if session.transcript_path else 0
    return data


def _session_query():
    """列表查询：筛选口径与「清空筛选结果」完全一致（同一实现，避免两边漂移）。"""
    return _session_query_with(request.args)


@bp.get("/sessions")
@permission_required("session:view", "session:view_all")
def list_sessions():
    page, size = page_args()
    query = _session_query()
    total = query.count()
    rows = query.order_by(SessionRecord.id.desc()).offset((page - 1) * size).limit(size).all()
    return api_list([_session_payload(row) for row in rows], total, page, size)


@bp.get("/sessions/online")
@permission_required("session:view", "session:view_all")
def online_sessions():
    actor = load_actor()
    sessions = SessionRecord.query.filter_by(status="active").order_by(SessionRecord.id.desc()).all()
    if not _visible_all(actor):
        sessions = [item for item in sessions if item.user_id == (actor.id if actor else -1)]
    return api_ok([_session_payload(item, detail=False) for item in sessions])


@bp.get("/sessions/<int:session_id>")
@permission_required("session:view", "session:view_all")
def get_session(session_id: int):
    session = db.session.get(SessionRecord, session_id)
    if session is None:
        return api_error("会话不存在", 404, code="NOT_FOUND")
    actor = load_actor()
    if not _visible_all(actor) and session.user_id != (actor.id if actor else -1):
        return api_error("无权查看该会话", 403, code="FORBIDDEN")
    data = _session_payload(session, detail=True)
    if session.transcript_path:
        data["stats"] = tail_stats(session.transcript_path)
    return api_ok(data)


@bp.get("/sessions/<int:session_id>/commands")
@permission_required("command:view", "command:view_all", "session:view")
def session_commands(session_id: int):
    session = db.session.get(SessionRecord, session_id)
    if session is None:
        return api_error("会话不存在", 404, code="NOT_FOUND")
    actor = load_actor()
    if not _visible_all(actor) and session.user_id != (actor.id if actor else -1):
        return api_error("无权查看该会话", 403, code="FORBIDDEN")
    page, size = page_args(default_size=50)
    query = CommandLog.query.filter_by(session_id=session_id)
    action = (request.args.get("action") or "").strip()
    if action:
        query = query.filter(CommandLog.action == action)
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(CommandLog.command.like(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(CommandLog.seq.asc(), CommandLog.id.asc()).offset((page - 1) * size).limit(size).all()
    items = [row.to_dict(with_output=True, output_limit=2000) for row in rows]
    return api_list(items, total, page, size, session=_session_payload(session))


@bp.get("/sessions/<int:session_id>/transcript")
@permission_required("session:replay", "session:view_all", "session:view")
def session_transcript(session_id: int):
    session = db.session.get(SessionRecord, session_id)
    if session is None:
        return api_error("会话不存在", 404, code="NOT_FOUND")
    actor = load_actor()
    if not (has_permission(actor, "session:replay") or _visible_all(actor)):
        if session.user_id != (actor.id if actor else -1):
            return api_error("无权回放该会话", 403, code="FORBIDDEN")
    offset = parse_int(request.args.get("offset"), 0) or 0
    limit = parse_int(request.args.get("limit"), 800) or 800
    limited_only = parse_bool(request.args.get("commandOnly"))
    if not session.transcript_path:
        return api_ok({"events": [], "nextOffset": offset, "eof": True, "stats": {}})
    events, next_offset = TranscriptRecorder.iter_events(session.transcript_path, offset=offset, limit=limit)
    if limited_only:
        events = [item for item in events if item.get("t") in ("command", "deny", "interactive_input", "session_start", "session_end")]
    return api_ok(
        {
            "events": events,
            "nextOffset": next_offset,
            "eof": next_offset <= offset,
            "size": TranscriptRecorder.size(session.transcript_path),
            "stats": tail_stats(session.transcript_path),
        }
    )


@bp.post("/sessions/<int:session_id>/terminate")
@permission_required("session:terminate")
def terminate_session(session_id: int):
    session = db.session.get(SessionRecord, session_id)
    if session is None:
        return api_error("会话不存在", 404, code="NOT_FOUND")
    if session.status != "active":
        return api_error("该会话已经结束", 400, code="INVALID_OPERATION")
    live = registry.get(session.sid)
    if live is not None:
        registry.close(session.sid, reason="管理员强制中断")
    else:
        close_session(session, status="terminated", reason="管理员强制中断")
        db.session.commit()
    log_event(
        "session",
        "terminate_session",
        target_type="session",
        target_id=session.id,
        target_name=session.sid,
        message=f"强制中断会话 {session.sid}（{session.username} → {session.host_name}）",
    )
    return api_ok(None, "会话已中断")


@bp.get("/commands")
@permission_required("command:view", "command:view_all")
def list_commands():
    page, size = page_args()
    query = _filter_commands(CommandLog.query, request.args)
    with_output = parse_bool(request.args.get("withOutput"))
    total = query.count()
    rows = query.order_by(CommandLog.id.desc()).offset((page - 1) * size).limit(size).all()
    items = [row.to_dict(with_output=with_output, output_limit=600) for row in rows]
    return api_list(items, total, page, size)


def _filter_commands(query, params):
    """命令日志筛选（列表接口与「清除筛选结果」共用同一套条件）。"""
    actor = load_actor()
    if not has_any_permission(actor, "command:view_all", "session:view_all"):
        query = query.filter(CommandLog.user_id == (actor.id if actor else -1))

    def get(name: str) -> str:
        value = params.get(name) if hasattr(params, "get") else None
        return "" if value is None else str(value).strip()

    session_id = parse_int(get("sessionId"))
    if session_id:
        query = query.filter(CommandLog.session_id == session_id)
    username = get("username")
    if username:
        query = query.filter(CommandLog.username == username)
    host_id = parse_int(get("hostId"))
    if host_id:
        query = query.filter(CommandLog.host_id == host_id)
    action = get("action")
    if action:
        query = query.filter(CommandLog.action == action)
    risk = get("riskLevel")
    if risk == "high":
        query = query.filter(CommandLog.risk_level.in_(["high", "critical"]))
    elif risk:
        query = query.filter(CommandLog.risk_level == risk)
    keyword = get("keyword")
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(CommandLog.command.like(like), CommandLog.output.like(like)))
    return query


@bp.post("/commands/delete")
@admin_required
def purge_commands():
    """清除命令审计记录（``ids`` 删除选中 / ``all=true`` 清空，可叠加筛选条件与 ``before``）。

    与 `/api/audits/delete` 同一口径：范围必须显式声明，清除动作本身写审计留痕。
    """
    payload = request.get_json(silent=True) or {}
    try:
        ids = extract_ids(payload)
        before = purge_before(payload)
    except ValueError as exc:
        return api_error(str(exc), 400, code="INVALID_ARGUMENT")

    query = CommandLog.query
    if ids is not None:
        query = query.filter(CommandLog.id.in_(ids))
        scope = f"按选择清除 {len(ids)} 条"
    else:
        if not parse_bool(payload.get("all")):
            return api_error(
                "请明确清除范围：传 ids（删除选中）或 all=true（清空，可叠加筛选条件）",
                400,
                code="INVALID_ARGUMENT",
            )
        query = _filter_commands(query, payload)
        if before is not None:
            query = query.filter(CommandLog.started_at < before)
        filters = {
            key: payload.get(key)
            for key in ("username", "hostId", "sessionId", "action", "riskLevel", "keyword")
            if payload.get(key)
        }
        scope = "清空全部" if not filters else "清空筛选结果 " + " ".join(
            f"{k}={v}" for k, v in filters.items()
        )
        if before is not None:
            scope += f"（早于 {before.isoformat()}Z）"

    removed = query.delete(synchronize_session=False)
    db.session.commit()
    log_event(
        "audit",
        "purge_commands",
        target_type="command",
        message=f"清除命令审计 {int(removed)} 条：{scope}",
        detail={"deleted": int(removed), "scope": scope, "ids": ids or []},
    )
    return api_ok({"deleted": int(removed), "scope": scope}, f"已清除 {int(removed)} 条命令记录")


@bp.post("/sessions/delete")
@admin_required
def purge_sessions():
    """清除会话记录（含该会话的命令日志与录像文件）。

    与审计一致：``ids`` 删除选中 / ``all=true`` 清空（可叠加筛选与 ``before``）。
    **进行中的会话（status=active）不会被删** —— 直接删掉在线会话的审计行等于把
    「正在发生的事」抹掉；这里跳过它们并在返回里明确告知，让管理员先中断会话。
    """
    payload = request.get_json(silent=True) or {}
    try:
        ids = extract_ids(payload)
        before = purge_before(payload)
    except ValueError as exc:
        return api_error(str(exc), 400, code="INVALID_ARGUMENT")

    query = SessionRecord.query
    if ids is not None:
        query = query.filter(SessionRecord.id.in_(ids))
        scope = f"按选择清除 {len(ids)} 条"
    else:
        if not parse_bool(payload.get("all")):
            return api_error(
                "请明确清除范围：传 ids（删除选中）或 all=true（清空，可叠加筛选条件）",
                400,
                code="INVALID_ARGUMENT",
            )
        query = _session_query_with(payload, request.args)
        if before is not None:
            query = query.filter(SessionRecord.started_at < before)
        filters = {
            key: payload.get(key)
            for key in ("username", "hostId", "source", "status", "riskLevel", "keyword")
            if payload.get(key)
        }
        scope = "清空全部" if not filters else "清空筛选结果 " + " ".join(
            f"{k}={v}" for k, v in filters.items()
        )
        if before is not None:
            scope += f"（早于 {before.isoformat()}Z）"

    rows = query.order_by(SessionRecord.id.asc()).all()
    active = [row for row in rows if row.status == "active"]
    victims = [row for row in rows if row.status != "active"]
    transcripts = 0
    command_rows = 0
    for row in victims:
        if row.transcript_path:
            transcripts += _remove_transcript(row.transcript_path)
        command_rows += (
            CommandLog.query.filter_by(session_id=row.id).delete(synchronize_session=False)
        )
        db.session.delete(row)
    db.session.commit()
    log_event(
        "audit",
        "purge_sessions",
        target_type="session",
        message=(
            f"清除会话记录 {len(victims)} 条（命令 {command_rows} 条、录像 {transcripts} 个）：{scope}"
        ),
        detail={
            "deleted": len(victims),
            "commands": command_rows,
            "transcripts": transcripts,
            "skippedActive": len(active),
            "scope": scope,
            "ids": ids or [],
        },
    )
    message = f"已清除 {len(victims)} 条会话记录"
    if active:
        message += f"，跳过 {len(active)} 条进行中的会话（请先中断再清除）"
    return api_ok(
        {
            "deleted": len(victims),
            "commands": command_rows,
            "transcripts": transcripts,
            "skippedActive": len(active),
            "scope": scope,
        },
        message,
    )


def _session_query_with(payload: dict, fallback_args=None):
    """按 payload（清空筛选）或 request.args 构造会话查询。"""
    actor = load_actor()
    query = SessionRecord.query
    if not _visible_all(actor):
        query = query.filter(SessionRecord.user_id == (actor.id if actor else -1))

    def get(name: str) -> str:
        value = payload.get(name) if hasattr(payload, "get") else None
        if value in (None, ""):
            return ""
        return str(value).strip()

    keyword = get("keyword")
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                SessionRecord.username.like(like),
                SessionRecord.host_name.like(like),
                SessionRecord.host_address.like(like),
                SessionRecord.sid.like(like),
            )
        )
    username = get("username")
    if username:
        query = query.filter(SessionRecord.username == username)
    host_id = parse_int(get("hostId"))
    if host_id:
        query = query.filter(SessionRecord.host_id == host_id)
    source = get("source")
    if source:
        query = query.filter(SessionRecord.source == source)
    status = get("status")
    if status:
        query = query.filter(SessionRecord.status == status)
    risk = get("riskLevel")
    if risk == "high":
        query = query.filter(SessionRecord.max_risk_level.in_(["high", "critical"]))
    elif risk:
        query = query.filter(SessionRecord.max_risk_level == risk)
    return query


def _remove_transcript(path: str) -> int:
    """删除录像文件；文件本就不存在不算错误，返回实际删掉的个数。"""
    try:
        if os.path.exists(path):
            os.remove(path)
            return 1
    except OSError:
        log.exception("删除录像文件失败 path=%s", path)
    return 0


@bp.get("/commands/<int:command_id>")
@permission_required("command:view", "command:view_all")
def get_command(command_id: int):
    record = db.session.get(CommandLog, command_id)
    if record is None:
        return api_error("命令记录不存在", 404, code="NOT_FOUND")
    actor = load_actor()
    if not has_any_permission(actor, "command:view_all", "session:view_all"):
        if record.user_id != (actor.id if actor else -1):
            return api_error("无权查看该命令记录", 403, code="FORBIDDEN")
    return api_ok(record.to_dict(with_output=True, output_limit=100000))


@bp.get("/terminal/targets")
@permission_required("terminal:use", "rdp:use")
def terminal_targets():
    """当前账号有权访问的主机菜单（网页终端入口，**ssh / winrm / rdp 合并在同一张列表里**）。

    历史上的终端选单只列 ssh 主机，Windows 远程桌面（`protocol="rdp"`）另有一个入口页；
    现在三者合并：同一个列表、同一颗「连接」按钮，按主机的 `protocol` 决定打开哪扇窗口 ——
    ssh 与 winrm 是字符终端（`/terminal/console`），rdp 是远程桌面（`/rdp/console`）。

    谁能看到哪一类，取决于权限码而不是页面：有 `terminal:use`（且账号没被禁用网页终端）
    才返回字符终端主机（Linux 的 ssh 与 Windows 的 winrm 共用这一个权限码），
    有 `rdp:use` 才返回 rdp 主机；两类都没有就是空列表。
    """
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    protocols: list[str] = []
    if has_permission(actor, "terminal:use") and actor.webterm_enabled:
        # 字符终端：Linux 走 ssh、Windows 走 winrm，两者共用一个权限码与同一条前端链路
        protocols.extend(("ssh", "winrm"))
    if has_permission(actor, "rdp:use"):
        protocols.append(RDP_PROTOCOL)
    if not protocols:
        return api_ok([], "当前账号已被禁用网页终端")
    targets = accessible_targets(actor, protocols=tuple(protocols))
    return api_ok([_target_payload(item) for item in targets])


def _target_payload(entry: dict) -> dict:
    """把内部条目转成对外结构（终端选单：账号只暴露 id/name/username/authType，不外传凭据）。

    `authType` 要留着：远程桌面（rdp）只认口令账号，前端据此在合并列表里把
    「密钥账号」的机器标成不可连接，而不是等用户点了以后才报错。
    """
    payload = serialize_target(entry)
    payload["accounts"] = [
        {
            "id": item["id"],
            "name": item["name"],
            "username": item["username"],
            "authType": item.get("authType", "password"),
        }
        for item in payload["accounts"]
    ]
    return payload


@bp.post("/terminal/targets/<int:host_id>/check")
@permission_required("terminal:use")
def check_target(host_id: int):
    """打开终端前的准入校验：返回可用账号与拒绝原因。

    请求体可带 `protocol`（`ssh` / `winrm`）：一台主机可能有多个字符端点，合并成一行
    之后「点哪个按钮就校验哪个端点」；不传则取该主机的第一个字符端点。
    """
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    payload = request.get_json(silent=True) or {}
    wanted = (payload.get("protocol") or "").strip().lower()
    targets = accessible_targets(actor, protocols=("ssh", "winrm"))
    visible = [item for item in targets if item["hostId"] == host_id]
    if wanted:
        target = next((item for item in visible if target_protocol(item) == wanted), None)
        if target is None and visible:
            # 主机在列表里但没有这个端点：比「没权限」更准确的说法
            return api_ok(
                {
                    "allowed": False,
                    "reason": f"该主机没有「{wanted}」这个字符端点",
                    "accounts": [],
                }
            )
    else:
        target = visible[0] if visible else None
    if target is None:
        from ..access import find_access

        if any(item["hostId"] == host_id for item in accessible_targets(actor)):
            return api_ok(
                {
                    "allowed": False,
                    "reason": "该主机是 Windows 远程桌面（RDP）主机，网页终端只跑字符会话；"
                    "请回资产列表点「连接」，会自动用远程桌面窗口打开",
                    "accounts": [],
                }
            )
        resolved = find_access(actor, host_id=host_id, require_login=True)
        reason = "你没有该主机的访问权限"
        if resolved is not None:
            from ..access import in_time_window

            allowed, why = in_time_window(resolved.grant)
            reason = why or reason
        return api_ok({"allowed": False, "reason": reason, "accounts": []})
    # 主机可访问但一个资产账号都没有：连上去只会拿到「该主机下没有可用账号」，
    # 不如在准入校验就明确拒绝，前端据此禁用「连接主机」按钮。
    if not target.get("accounts"):
        payload = _target_payload(target)
        payload["allowed"] = False
        payload["reason"] = "该主机下没有可用账号，请联系管理员配置资产账号"
        return api_ok(payload)
    return api_ok({"allowed": True, "reason": "", **_target_payload(target)})


@bp.post("/terminal/exec")
@permission_required("terminal:use")
def terminal_exec():
    """在授权主机上「一次性执行一条命令」并取回输出（AI 运维的执行工具后端）。

    与网页终端/网关的关系：这是**非交互**执行通道，但审计口径完全一致——

    1. 先用 :func:`find_access` + :func:`in_time_window` 校验授权（含时段、停用、过期）；
    2. 再用该授权绑定的命令策略 :func:`evaluate_policy` 判定放行/拦截；
    3. 拦截时同样写 ``CommandLog(action="deny")``，放行则写 ``CommandLog`` 并回传输出；
    4. 每次执行都会新建一条 ``SessionRecord``（``protocol="exec"``）并立刻关闭，
       所以「谁在什么时间、用哪个账号、在哪台机器上执行了什么、拿到什么输出」都有据可查。

    额外要求 ``ai:tool_exec`` 权限：这条通道是给 AI 用的，不能被普通终端权限顺带拿到。
    """
    import time as _time

    from ..access import check_session_quota, find_access, in_time_window
    from ..config import Config
    from ..policy import evaluate_policy
    from ..session_service import resolve_policy_for
    from ..settings_store import get_int
    from ..ssh_client import SSHError, build_target, close as ssh_close, connect, run_single_command

    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    if not has_permission(actor, "ai:tool_exec"):
        return api_error("你没有「AI 远程执行」权限（ai:tool_exec）", 403, code="FORBIDDEN")

    payload = request.get_json(silent=True) or {}
    host_id = parse_int(payload.get("hostId"))
    account_id = parse_int(payload.get("accountId"))
    command = str(payload.get("command") or "").strip()
    if not host_id or not command:
        return api_error("hostId 与 command 必填", 400, code="INVALID_ARGUMENT")
    if len(command) > 4000:
        return api_error("单条命令长度不能超过 4000 字符", 400, code="INVALID_ARGUMENT")
    requested_timeout = parse_int(payload.get("timeout"), 0) or 0
    timeout = float(
        requested_timeout
        or (get_int("command_timeout", Config.COMMAND_TIMEOUT) or 60)
    )
    timeout = max(1.0, min(timeout, 600.0))
    reason_note = str(payload.get("reason") or "").strip()

    resolved = find_access(actor, host_id=host_id, account_id=account_id, require_login=True)
    if resolved is None:
        return api_error("你没有访问该主机的权限", 403, code="FORBIDDEN")
    allowed, why = in_time_window(resolved.grant)
    if not allowed:
        return api_error(why or "当前不在授权时段内", 403, code="FORBIDDEN")
    grant, host, account = resolved.grant, resolved.host, resolved.account
    if account is None:
        return api_error("该主机下没有可用账号，请联系管理员配置资产账号", 400, code="NO_ACCOUNT")
    quota_ok, quota_reason = check_session_quota(actor, grant)
    if not quota_ok:
        return api_error(quota_reason, 429, code="QUOTA_EXCEEDED")

    policy = resolve_policy_for(grant)
    policy_name = policy.name if policy is not None else "未绑定策略"
    decision = evaluate_policy(policy, command)

    sid = f"exec-{int(_time.time() * 1000)}-{actor.id}"
    record = new_session(
        sid=sid,
        user_id=actor.id,
        username=actor.username,
        role_code=actor.role_code,
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        account_id=account.id,
        account_username=account.username,
        grant_id=grant.id,
        source=str(payload.get("source") or "ai"),
        protocol="exec",
        client_ip=request.remote_addr or "",
        status="active",
        transcript_path="",
    )
    started_at = _time.monotonic()

    if not decision.allowed:
        log_command(
            session=record,
            command=command,
            output="",
            action="deny",
            risk_level=decision.risk_level,
            matched_rule_id=decision.rule_id,
            matched_rule_pattern=decision.rule_pattern or "",
            reason=decision.reason or "命令被策略拦截",
            started_at=record.started_at,
            duration_ms=0,
            exit_status=None,
        )
        close_session(record, status="closed", reason="命令被策略拦截，未执行")
        return api_error(
            f"命令被策略「{policy_name}」拦截：{decision.reason or '命中禁止规则'}",
            403,
            code="COMMAND_DENIED",
            data={
                "allowed": False,
                "policy": policy_name,
                "riskLevel": decision.risk_level,
                "reason": decision.reason,
                "sid": sid,
            },
        )

    target = build_target(
        host,
        account,
        connect_timeout=Config.SSH_CONNECT_TIMEOUT,
        banner_timeout=Config.SSH_BANNER_TIMEOUT,
        keepalive=Config.SSH_KEEPALIVE,
    )
    connection = None
    try:
        connection = connect(target)
        status, out, err = run_single_command(connection, command, timeout=timeout)
    except SSHError as exc:
        log_command(
            session=record,
            command=command,
            output="",
            action="allow",
            risk_level=decision.risk_level,
            matched_rule_id=decision.rule_id,
            matched_rule_pattern=decision.rule_pattern or "",
            reason=f"连接目标机失败：{exc}",
            started_at=record.started_at,
            duration_ms=int((_time.monotonic() - started_at) * 1000),
            exit_status=None,
        )
        close_session(record, status="failed", reason=f"连接失败：{exc}"[:120])
        return api_error(f"连接目标机失败：{exc}", 502, code="SSH_ERROR")
    except Exception as exc:  # noqa: BLE001 - 包含 paramiko 的超时/通道异常
        log_command(
            session=record,
            command=command,
            output="",
            action="allow",
            risk_level=decision.risk_level,
            matched_rule_id=decision.rule_id,
            matched_rule_pattern=decision.rule_pattern or "",
            reason=f"执行失败：{exc}",
            started_at=record.started_at,
            duration_ms=int((_time.monotonic() - started_at) * 1000),
            exit_status=None,
        )
        close_session(record, status="failed", reason=f"执行失败：{exc}"[:120])
        return api_error(f"执行失败：{exc}", 502, code="EXEC_ERROR")
    finally:
        ssh_close(connection)

    duration_ms = int((_time.monotonic() - started_at) * 1000)
    output = out if not err else f"{out}\n{err}" if out else err
    truncated = False
    if len(output) > DB_EXEC_OUTPUT_LIMIT:
        output = output[:DB_EXEC_OUTPUT_LIMIT] + "\n...[输出过长已截断]"
        truncated = True
    log_command(
        session=record,
        command=command,
        output=output,
        action="allow",
        risk_level=decision.risk_level,
        matched_rule_id=decision.rule_id,
        matched_rule_pattern=decision.rule_pattern or "",
        reason=reason_note or "AI 运维一次性执行",
        started_at=record.started_at,
        duration_ms=duration_ms,
        exit_status=status,
        truncated=truncated,
    )
    close_session(record, status="closed", reason="单次命令执行完成")
    return api_ok(
        {
            "allowed": True,
            "sid": sid,
            "hostId": host.id,
            "hostName": host.name,
            "hostAddress": host.address,
            "accountId": account.id,
            "accountUsername": account.username,
            "command": command,
            "exitStatus": status,
            "stdout": out,
            "stderr": err,
            "durationMs": duration_ms,
            "policy": policy_name,
            "riskLevel": decision.risk_level,
            "truncated": truncated,
        }
    )


_ = (admin_required, login_required)
