"""操作审计日志与控制台统计接口。"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, request
from sqlalchemy import func, or_

from ..audit import log_event, verify_all_chains, verify_table_chain
from ..extensions import db
from ..models import (
    AuditLog,
    CommandLog,
    CommandPolicy,
    FileLog,
    FilePolicy,
    Grant,
    Host,
    HostAccount,
    SessionRecord,
    User,
    utcnow,
)
from ..security import (
    admin_required,
    has_any_permission,
    load_actor,
    permission_required,
)
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

bp = Blueprint("audits", __name__, url_prefix="/api")


def _filter_audits(query, params):
    """把筛选条件套到 AuditLog 查询上（列表接口与「清除筛选结果」共用同一套条件）。

    要求：两边口径必须一致 —— 否则「清空当前筛选」会删掉用户没看到的数据。
    """
    def get(name: str) -> str:
        value = params.get(name) if hasattr(params, "get") else None
        if value is None:
            return ""
        return str(value).strip()

    category = get("category")
    if category:
        query = query.filter(AuditLog.category == category)
    action = get("action")
    if action:
        query = query.filter(AuditLog.action == action)
    result = get("result")
    if result:
        query = query.filter(AuditLog.result == result)
    actor_username = get("actorUsername")
    if actor_username:
        query = query.filter(AuditLog.actor_username == actor_username)
    target_type = get("targetType")
    if target_type:
        query = query.filter(AuditLog.target_type == target_type)
    keyword = get("keyword")
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                AuditLog.message.like(like),
                AuditLog.target_name.like(like),
                AuditLog.actor_username.like(like),
                AuditLog.ip.like(like),
            )
        )
    return query


@bp.get("/audits")
@permission_required("audit:view")
def list_audits():
    page, size = page_args()
    query = _filter_audits(AuditLog.query, request.args)
    total = query.count()
    rows = query.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size).all()
    return api_list([row.to_dict() for row in rows], total, page, size)


@bp.post("/audits/delete")
@admin_required
def purge_audits():
    """清除审计日志（需求：审计可以清除、可以按选择清除）。

    两种口径：
    * ``{"ids": [1,2,3]}`` —— 删除勾选的行；
    * ``{"all": true, ...筛选条件, "before": "2026-09-01"}`` —— 清空全部 / 清空当前筛选结果 / 只清某个时间点之前。

    ``all`` 必须显式传 ``true``：空 body 或只有筛选条件一律 400，避免误清空。
    **清除动作本身会写一条新的审计日志**（`purge_audits`）—— 数据可以被清，
    但「谁在什么时候清了多少条、按什么条件清的」必须留痕。
    """
    payload = request.get_json(silent=True) or {}
    try:
        ids = extract_ids(payload)
        before = purge_before(payload)
    except ValueError as exc:
        return api_error(str(exc), 400, code="INVALID_ARGUMENT")

    scope = ""
    query = AuditLog.query
    if ids is not None:
        query = query.filter(AuditLog.id.in_(ids))
        scope = f"按选择清除 {len(ids)} 条"
    else:
        if not parse_bool(payload.get("all")):
            return api_error(
                "请明确清除范围：传 ids（删除选中）或 all=true（清空，可叠加筛选条件）",
                400,
                code="INVALID_ARGUMENT",
            )
        query = _filter_audits(query, payload)
        if before is not None:
            query = query.filter(AuditLog.ts < before)
        filters = {
            key: payload.get(key)
            for key in ("category", "action", "result", "actorUsername", "targetType", "keyword")
            if payload.get(key)
        }
        scope = "清空全部"
        if filters:
            scope = "清空筛选结果 " + " ".join(f"{k}={v}" for k, v in filters.items())
        if before is not None:
            scope += f"（早于 {before.isoformat()}Z）"

    removed = query.delete(synchronize_session=False)
    db.session.commit()
    log_event(
        "audit",
        "purge_audits",
        target_type="audit",
        message=f"清除审计日志 {int(removed)} 条：{scope}",
        detail={"deleted": int(removed), "scope": scope, "ids": ids or []},
    )
    return api_ok({"deleted": int(removed), "scope": scope}, f"已清除 {int(removed)} 条审计日志")


@bp.get("/audits/<int:audit_id>")
@permission_required("audit:view")
def get_audit(audit_id: int):
    row = db.session.get(AuditLog, audit_id)
    if row is None:
        from ..utils import api_error

        return api_error("审计记录不存在", 404, code="NOT_FOUND")
    return api_ok(row.to_dict())


@bp.get("/audits/options")
@permission_required("audit:view")
def audit_options():
    categories = [
        row[0]
        for row in db.session.query(AuditLog.category).distinct().order_by(AuditLog.category).all()
        if row[0]
    ]
    actions = [
        row[0]
        for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
        if row[0]
    ]
    return api_ok({"categories": categories, "actions": actions})


def _filter_file_logs(query, params):
    """文件操作记录筛选（列表与「清除筛选结果」共用，口径必须一致）。"""
    def get(name: str) -> str:
        value = params.get(name) if hasattr(params, "get") else None
        if value is None:
            return ""
        return str(value).strip()

    operation = get("operation")
    if operation:
        query = query.filter(FileLog.operation == operation)
    action = get("action")
    if action:
        query = query.filter(FileLog.action == action)
    result = get("result")
    if result:
        query = query.filter(FileLog.result == result)
    risk_level = get("riskLevel")
    if risk_level:
        query = query.filter(FileLog.risk_level == risk_level)
    username = get("username")
    if username:
        query = query.filter(FileLog.username == username)
    host_name = get("hostName")
    if host_name:
        query = query.filter(FileLog.host_name == host_name)
    sid = get("sid")
    if sid:
        query = query.filter(FileLog.sid.like(f"{sid}%"))
    keyword = get("keyword")
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                FileLog.path.like(like),
                FileLog.target_path.like(like),
                FileLog.reason.like(like),
                FileLog.message.like(like),
                FileLog.username.like(like),
                FileLog.host_name.like(like),
            )
        )
    return query


def _visible_file_logs(query, actor, params):
    """没有 command:view_all 的账号只能看自己的文件操作记录（与命令记录同口径）。"""
    if actor is not None and not has_any_permission(actor, "command:view_all", "session:view_all"):
        query = query.filter(FileLog.user_id == actor.id)
    return _filter_file_logs(query, params)


@bp.get("/audits/files")
@permission_required("command:view")
def list_file_logs():
    actor = load_actor()
    page, size = page_args()
    query = _visible_file_logs(FileLog.query, actor, request.args)
    total = query.count()
    rows = query.order_by(FileLog.id.desc()).offset((page - 1) * size).limit(size).all()
    return api_list([row.to_dict() for row in rows], total, page, size)


@bp.post("/audits/files/delete")
@permission_required("command:view_all")
def purge_file_logs():
    """清除文件操作记录（与命令记录清除同口径：ids 或 all=true + 筛选条件）。"""
    actor = load_actor()
    payload = request.get_json(silent=True) or {}
    try:
        ids = extract_ids(payload)
        before = purge_before(payload)
    except ValueError as exc:
        return api_error(str(exc), 400, code="INVALID_ARGUMENT")

    scope = ""
    query = FileLog.query
    if ids is not None:
        query = query.filter(FileLog.id.in_(ids))
        scope = f"按选择清除 {len(ids)} 条"
    else:
        if not parse_bool(payload.get("all")):
            return api_error(
                "请明确清除范围：传 ids（删除选中）或 all=true（清空，可叠加筛选条件）",
                400,
                code="INVALID_ARGUMENT",
            )
        query = _visible_file_logs(query, actor, payload)
        if before is not None:
            query = query.filter(FileLog.started_at < before)
        filters = {
            key: payload.get(key)
            for key in ("operation", "action", "result", "riskLevel", "username", "hostName", "keyword")
            if payload.get(key)
        }
        scope = "清空全部"
        if filters:
            scope = "清空筛选结果 " + " ".join(f"{k}={v}" for k, v in filters.items())
        if before is not None:
            scope += f"（早于 {before.isoformat()}Z）"

    removed = query.delete(synchronize_session=False)
    db.session.commit()
    log_event(
        "audit",
        "purge_file_logs",
        target_type="file",
        message=f"清除文件操作记录 {int(removed)} 条：{scope}",
        detail={"deleted": int(removed), "scope": scope, "ids": ids or []},
    )
    return api_ok({"deleted": int(removed), "scope": scope}, f"已清除 {int(removed)} 条文件操作记录")


@bp.get("/audits/files/options")
@permission_required("command:view")
def file_log_options():
    def distinct(column):
        return [
            row[0]
            for row in db.session.query(column).distinct().order_by(column).all()
            if row[0]
        ]

    return api_ok(
        {
            "operations": distinct(FileLog.operation),
            "actions": distinct(FileLog.action),
            "results": distinct(FileLog.result),
            "riskLevels": distinct(FileLog.risk_level),
            "usernames": distinct(FileLog.username),
            "hosts": distinct(FileLog.host_name),
        }
    )


def _count(model, *criteria) -> int:
    query = db.session.query(func.count(model.id))
    if criteria:
        query = query.filter(*criteria)
    return int(query.scalar() or 0)


@bp.get("/dashboard/overview")
@permission_required("dashboard:view")
def dashboard_overview():
    actor = load_actor()
    now = utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)
    can_see_all = has_any_permission(actor, "session:view_all", "user:manage")
    my_id = actor.id if actor else -1

    session_filter = [SessionRecord.started_at >= day_ago]
    command_filter = [CommandLog.started_at >= day_ago]
    file_filter = [FileLog.started_at >= day_ago]
    if not can_see_all:
        session_filter.append(SessionRecord.user_id == my_id)
        command_filter.append(CommandLog.user_id == my_id)
        file_filter.append(FileLog.user_id == my_id)

    cards = {
        "hosts": _count(Host, Host.status == "active"),
        "hostAccounts": _count(HostAccount),
        "users": _count(User, User.status == "active"),
        "grants": _count(Grant, Grant.enabled.is_(True)),
        "policies": _count(CommandPolicy),
        "filePolicies": _count(FilePolicy),
        "onlineSessions": _count(SessionRecord, SessionRecord.status == "active"),
        "sessions24h": _count(SessionRecord, *session_filter),
        "commands24h": _count(CommandLog, *command_filter),
        "denied24h": _count(CommandLog, CommandLog.action == "deny", *command_filter),
        "fileOps24h": _count(FileLog, *file_filter),
        # 文件操作被拦有两条路径：策略判 deny（action=deny）或准入/IO 失败（result!=success）。
        "fileDenied24h": _count(
            FileLog,
            or_(
                FileLog.action == "deny",
                FileLog.result.in_(["denied", "failure"]),
            ),
            *file_filter,
        ),
        "risky24h": _count(
            CommandLog, CommandLog.risk_level.in_(["high", "critical"]), *command_filter
        ),
        "sessions7d": _count(SessionRecord, SessionRecord.started_at >= week_ago),
        # AuditLog 的时间列叫 ts（不是 created_at），别想当然。
        "audits24h": _count(AuditLog, AuditLog.ts >= day_ago),
    }

    trend = _session_trend(days=14, user_id=None if can_see_all else my_id)
    top_hosts = [
        {"hostName": name or "-", "count": int(count or 0)}
        for name, count in db.session.query(SessionRecord.host_name, func.count(SessionRecord.id))
        .filter(SessionRecord.started_at >= week_ago)
        .group_by(SessionRecord.host_name)
        .order_by(func.count(SessionRecord.id).desc())
        .limit(6)
        .all()
    ]
    recent_commands = [
        row.to_dict(with_output=False)
        for row in CommandLog.query.order_by(CommandLog.id.desc()).limit(8).all()
    ]
    recent_files_query = FileLog.query
    if not can_see_all:
        recent_files_query = recent_files_query.filter(FileLog.user_id == my_id)
    recent_files = [
        row.to_dict() for row in recent_files_query.order_by(FileLog.id.desc()).limit(8).all()
    ]
    recent_audits = [
        row.to_dict() for row in AuditLog.query.order_by(AuditLog.id.desc()).limit(8).all()
    ]
    return api_ok(
        {
            "cards": cards,
            "trend": trend,
            "topHosts": top_hosts,
            "recentCommands": recent_commands,
            "recentFiles": recent_files,
            "recentAudits": recent_audits,
        }
    )


def _session_trend(days: int = 14, user_id=None):
    now = utcnow()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    query = db.session.query(
        func.strftime("%Y-%m-%d", SessionRecord.started_at).label("day"),
        func.count(SessionRecord.id),
    ).filter(SessionRecord.started_at >= start)
    if user_id is not None:
        query = query.filter(SessionRecord.user_id == user_id)
    rows = {day: int(count or 0) for day, count in query.group_by("day").all()}

    command_query = db.session.query(
        func.strftime("%Y-%m-%d", CommandLog.started_at).label("day"),
        func.count(CommandLog.id),
    ).filter(CommandLog.started_at >= start)
    if user_id is not None:
        command_query = command_query.filter(CommandLog.user_id == user_id)
    commands = {day: int(count or 0) for day, count in command_query.group_by("day").all()}

    deny_query = db.session.query(
        func.strftime("%Y-%m-%d", CommandLog.started_at).label("day"),
        func.count(CommandLog.id),
    ).filter(CommandLog.started_at >= start, CommandLog.action == "deny")
    if user_id is not None:
        deny_query = deny_query.filter(CommandLog.user_id == user_id)
    denies = {day: int(count or 0) for day, count in deny_query.group_by("day").all()}

    trend = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        trend.append(
            {
                "date": day,
                "sessions": rows.get(day, 0),
                "commands": commands.get(day, 0),
                "denied": denies.get(day, 0),
            }
        )
    return trend


@bp.get("/dashboard/mine")
@permission_required("dashboard:view")
def dashboard_mine():
    """普通用户视角：我有权访问哪些机器、最近做了什么。"""
    actor = load_actor()
    from ..access import accessible_targets

    targets = accessible_targets(actor) if actor else []
    my_id = actor.id if actor else -1
    recent_sessions = [
        row.to_dict()
        for row in SessionRecord.query.filter_by(user_id=my_id)
        .order_by(SessionRecord.id.desc())
        .limit(10)
        .all()
    ]
    recent_commands = [
        row.to_dict(with_output=False)
        for row in CommandLog.query.filter_by(user_id=my_id)
        .order_by(CommandLog.id.desc())
        .limit(10)
        .all()
    ]
    return api_ok(
        {
            "targets": [
                {
                    "hostId": item["hostId"],
                    "hostName": item["hostName"],
                    "address": item["address"],
                    "groupName": item["groupName"],
                    "policyName": item["policyName"],
                    "accountCount": len(item["accounts"]),
                    "canWebterm": item["canWebterm"],
                }
                for item in targets
            ],
            "targetCount": len(targets),
            "recentSessions": recent_sessions,
            "recentCommands": recent_commands,
            "onlineSessions": _count(
                SessionRecord, SessionRecord.user_id == my_id, SessionRecord.status == "active"
            ),
            "denied7d": _count(
                CommandLog,
                CommandLog.user_id == my_id,
                CommandLog.action == "deny",
                CommandLog.started_at >= utcnow() - timedelta(days=7),
            ),
        }
    )


_ = parse_int


@bp.get("/audits/chain")
@permission_required("audit:view")
def audits_chain_status():
    """链状态：三张表的总行数、已哈希数、链尾哈希、是否健康的概览。

    - ``counts``：``{total, hashed, pending, pendingPrefix, pendingInside}``；
      ``pendingPrefix`` 是链起步前的合法遗留行，``pendingInside`` 是链内的空哈希行（异常）
    - ``heads``：``{lastId, lastEntryHash, verifiedHead}``；抄库外锚点请用 ``verifiedHead``
    - ``healthy`` 只看 ``pendingInside`` + 最新样本 ``prev_hash`` 自洽，不做逐行重算（逐行走 /verify）
    """
    heads = {}
    counts = {}
    for name, model in [
        ("audit_logs", AuditLog),
        ("command_logs", CommandLog),
        ("file_logs", FileLog),
    ]:
        total = int(db.session.query(func.count(model.id)).scalar() or 0)
        hashed = int(
            db.session.query(func.count(model.id))
            .filter(model.entry_hash != "")
            .scalar()
            or 0
        )
        # 「链起步前」的遗留行是合法的（升级前数据补不出哈希）；「链内的空哈希行」非法。
        # 用「第一条已哈希行的 id」把两者切开，不再一律当成 pending。
        first_hashed_id = (
            db.session.query(func.min(model.id)).filter(model.entry_hash != "").scalar()
        )
        if first_hashed_id is None:
            # 整表都没有哈希：全部算链起步前的遗留块（是不是被清空过，只有库外锚点能裁决）
            pending_prefix = total
        else:
            pending_prefix = int(
                db.session.query(func.count(model.id))
                .filter(model.id < first_hashed_id)
                .scalar()
                or 0
            )
        last = db.session.query(model).order_by(model.id.desc()).first()
        last_hashed = (
            db.session.query(model)
            .filter(model.entry_hash != "")
            .order_by(model.id.desc())
            .first()
        )
        heads[name] = {
            "lastId": last.id if last else None,
            "lastEntryHash": (last.entry_hash or "") if last and getattr(last, "entry_hash", None) else "",
            # 链尾 = 最后一个**非空** entry_hash。末行哈希被清空时，它才是该抄走的库外锚点
            "verifiedHead": (last_hashed.entry_hash or "") if last_hashed else "",
        }
        counts[name] = {
            "total": total,
            "hashed": hashed,
            "pending": total - hashed,
            "pendingPrefix": pending_prefix,
            "pendingInside": max(0, total - hashed - pending_prefix),
        }
    # healthy 只对「链内空洞」亮红：链起步前的遗留行不该把完好的链判成不健康
    # （老库升级完第一眼就全红的话，运维会习惯性忽略这个徽标）
    healthy = True
    for name, model in [
        ("audit_logs", AuditLog),
        ("command_logs", CommandLog),
        ("file_logs", FileLog),
    ]:
        if counts[name]["pendingInside"] > 0:
            healthy = False
            break
        sample = db.session.query(model).filter(model.entry_hash != "").order_by(model.id.desc()).first()
        if sample is None:
            continue
        # 只用样本做快速自洽：prev_hash 对上一条的 entry_hash
        prev = (
            db.session.query(model)
            .filter(model.id < sample.id)
            .order_by(model.id.desc())
            .first()
        )
        if sample.prev_hash != ((prev.entry_hash or "") if prev else ""):
            healthy = False
            break
    return api_ok(
        {
            "healthy": healthy,
            "counts": counts,
            "heads": heads,
        }
    )


@bp.post("/audits/chain/verify")
@permission_required("audit:view")
def audits_chain_verify():
    """逐行重算并校验三张表的哈希链。返回每表的详细错误（最多前 32 条）。

    权限口径：**审计查看权限即可**（`audit:view`）—— 完整性校验是只读的，审计员也该能自证清白；
    运维专用、能校验单表的调用方用 CLI `tools/verify_audit_chain.py`。

    - `?table=audit_logs|command_logs|file_logs` 只校验单张表
    - `?expected_head=<hash>` 传入库外锚点比对链尾（对不上即判「被截断」，见 CLI `--print-head`）
    """
    table = (request.args.get("table") or "").strip()
    expected_head = (request.args.get("expected_head") or "").strip() or None
    mapping = {
        "audit_logs": AuditLog,
        "command_logs": CommandLog,
        "file_logs": FileLog,
    }
    if table:
        if table not in mapping:
            return api_error("UNKNOWN_TABLE", f"未知表名：{table}", code=400)
        result = verify_table_chain(mapping[table], expected_head=expected_head)
        return api_ok({"ok": result["ok"], "tables": {table: result}})
    if expected_head:
        return api_error(
            "ANCHOR_NEEDS_TABLE",
            "expected_head 锚点必须与 table= 一起用（每张表的链尾不同）",
            code=400,
        )
    return api_ok(verify_all_chains())
