"""主机授权接口（谁能碰哪台机器）。"""

from __future__ import annotations

from flask import Blueprint, request

from ..access import in_time_window
from ..audit import log_event
from ..extensions import db
from ..models import CommandPolicy, FilePolicy, Grant, Host, HostAccount, SessionRecord, User
from ..security import admin_required, permission_required
from ..utils import (
    api_error,
    api_list,
    api_ok,
    page_args,
    parse_bool,
    parse_datetime,
    parse_int,
)

bp = Blueprint("grants", __name__, url_prefix="/api/grants")


def _grant_payload(grant: Grant) -> dict:
    data = grant.to_dict()
    user = db.session.get(User, grant.user_id)
    host = db.session.get(Host, grant.host_id)
    policy = db.session.get(CommandPolicy, grant.policy_id) if grant.policy_id else None
    data["username"] = user.username if user else ""
    data["displayName"] = (user.display_name if user else "") or ""
    data["hostName"] = host.name if host else ""
    data["hostAddress"] = host.address if host else ""
    # host_account_username 是 Grant 上的 @property（返回 str），不能当方法调用
    data["accountName"] = grant.account.name if grant.account else "（全部账号）"
    data["accountUsername"] = grant.host_account_username
    data["policyName"] = policy.name if policy else ""
    allowed, reason = in_time_window(grant)
    data["inWindow"] = allowed
    data["windowReason"] = reason
    data["activeSessions"] = SessionRecord.query.filter_by(
        grant_id=grant.id, status="active"
    ).count()
    return data


@bp.get("")
@permission_required("grant:view")
def list_grants():
    page, size = page_args()
    query = Grant.query
    user_id = parse_int(request.args.get("userId"))
    if user_id:
        query = query.filter(Grant.user_id == user_id)
    host_id = parse_int(request.args.get("hostId"))
    if host_id:
        query = query.filter(Grant.host_id == host_id)
    enabled = request.args.get("enabled")
    if enabled not in (None, ""):
        query = query.filter(Grant.enabled == parse_bool(enabled))
    total = query.count()
    rows = query.order_by(Grant.id.desc()).offset((page - 1) * size).limit(size).all()
    return api_list([_grant_payload(row) for row in rows], total, page, size)


@bp.get("/<int:grant_id>")
@permission_required("grant:view")
def get_grant(grant_id: int):
    grant = db.session.get(Grant, grant_id)
    if grant is None:
        return api_error("授权不存在", 404, code="NOT_FOUND")
    return api_ok(_grant_payload(grant))


def _normalize_weekdays(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        items = [item for item in value.replace(",", " ").split() if item]
    else:
        items = list(value)
    days = []
    for item in items:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return days


@bp.post("")
@admin_required
def create_grant():
    payload = request.get_json(silent=True) or {}
    user_id = parse_int(payload.get("userId"))
    host_id = parse_int(payload.get("hostId"))
    if not user_id or db.session.get(User, user_id) is None:
        return api_error("请选择有效的堡垒机账号", 400, code="INVALID_ARGUMENT")
    if not host_id or db.session.get(Host, host_id) is None:
        return api_error("请选择有效的主机", 400, code="INVALID_ARGUMENT")
    account_id = parse_int(payload.get("hostAccountId"))
    if account_id:
        account = db.session.get(HostAccount, account_id)
        if account is None or account.host_id != host_id:
            return api_error("指定的主机登录账号不属于该主机", 400, code="INVALID_ARGUMENT")
    policy_id = parse_int(payload.get("policyId"))
    if policy_id and db.session.get(CommandPolicy, policy_id) is None:
        return api_error("指定的命令策略不存在", 400, code="INVALID_ARGUMENT")
    file_policy_id = parse_int(payload.get("filePolicyId"))
    if file_policy_id and db.session.get(FilePolicy, file_policy_id) is None:
        return api_error("指定的文件策略不存在", 400, code="INVALID_ARGUMENT")

    duplicated = Grant.query.filter_by(
        user_id=user_id, host_id=host_id, host_account_id=account_id
    ).first()
    if duplicated is not None:
        return api_error("该账号对这台主机的相同授权已存在", 409, code="DUPLICATED")

    grant = Grant(
        user_id=user_id,
        host_id=host_id,
        host_account_id=account_id,
        policy_id=policy_id,
        file_policy_id=file_policy_id,
        can_login=parse_bool(payload.get("canLogin"), True),
        can_sftp=parse_bool(payload.get("canSftp")),
        can_upload=parse_bool(payload.get("canUpload")),
        can_download=parse_bool(payload.get("canDownload")),
        can_file_write=parse_bool(payload.get("canFileWrite")),
        can_port_forward=parse_bool(payload.get("canPortForward")),
        can_webterm=parse_bool(payload.get("canWebterm"), True),
        time_start=(payload.get("timeStart") or "").strip() or None,
        time_end=(payload.get("timeEnd") or "").strip() or None,
        weekdays=_normalize_weekdays(payload.get("weekdays")),
        expire_at=parse_datetime(payload.get("expireAt")),
        max_sessions=parse_int(payload.get("maxSessions"), 0) or 0,
        enabled=parse_bool(payload.get("enabled"), True),
        remark=(payload.get("remark") or "").strip(),
    )
    from ..security import load_actor

    actor = load_actor()
    grant.created_by = actor.id if actor else None
    db.session.add(grant)
    db.session.commit()
    log_event(
        "grant",
        "create_grant",
        target_type="grant",
        target_id=grant.id,
        target_name=f"user={user_id} host={host_id}",
        message="新增主机授权",
        detail={"userId": user_id, "hostId": host_id, "accountId": account_id},
    )
    return api_ok(_grant_payload(grant), "授权已创建")


@bp.put("/<int:grant_id>")
@admin_required
def update_grant(grant_id: int):
    grant = db.session.get(Grant, grant_id)
    if grant is None:
        return api_error("授权不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    if "hostAccountId" in payload:
        account_id = parse_int(payload.get("hostAccountId"))
        if account_id:
            account = db.session.get(HostAccount, account_id)
            if account is None or account.host_id != grant.host_id:
                return api_error("指定的主机登录账号不属于该主机", 400, code="INVALID_ARGUMENT")
        grant.host_account_id = account_id
    if "policyId" in payload:
        policy_id = parse_int(payload.get("policyId"))
        if policy_id and db.session.get(CommandPolicy, policy_id) is None:
            return api_error("指定的命令策略不存在", 400, code="INVALID_ARGUMENT")
        grant.policy_id = policy_id
    if "filePolicyId" in payload:
        file_policy_id = parse_int(payload.get("filePolicyId"))
        if file_policy_id and db.session.get(FilePolicy, file_policy_id) is None:
            return api_error("指定的文件策略不存在", 400, code="INVALID_ARGUMENT")
        grant.file_policy_id = file_policy_id
    for field, key, caster in (
        ("can_login", "canLogin", lambda v: parse_bool(v, True)),
        ("can_sftp", "canSftp", parse_bool),
        ("can_upload", "canUpload", parse_bool),
        ("can_download", "canDownload", parse_bool),
        ("can_file_write", "canFileWrite", parse_bool),
        ("can_port_forward", "canPortForward", parse_bool),
        ("can_webterm", "canWebterm", lambda v: parse_bool(v, True)),
        ("enabled", "enabled", lambda v: parse_bool(v, True)),
    ):
        if key in payload:
            setattr(grant, field, caster(payload.get(key)))
    if "timeStart" in payload:
        grant.time_start = (payload.get("timeStart") or "").strip() or None
    if "timeEnd" in payload:
        grant.time_end = (payload.get("timeEnd") or "").strip() or None
    if "weekdays" in payload:
        grant.weekdays = _normalize_weekdays(payload.get("weekdays"))
    if "expireAt" in payload:
        grant.expire_at = parse_datetime(payload.get("expireAt"))
    if "maxSessions" in payload:
        grant.max_sessions = parse_int(payload.get("maxSessions"), 0) or 0
    if "remark" in payload:
        grant.remark = (payload.get("remark") or "").strip()
    db.session.commit()
    log_event(
        "grant",
        "update_grant",
        target_type="grant",
        target_id=grant.id,
        message="更新主机授权",
    )
    return api_ok(_grant_payload(grant), "授权已更新")


@bp.delete("/<int:grant_id>")
@admin_required
def delete_grant(grant_id: int):
    grant = db.session.get(Grant, grant_id)
    if grant is None:
        return api_error("授权不存在", 404, code="NOT_FOUND")
    active = SessionRecord.query.filter_by(grant_id=grant_id, status="active").count()
    if active:
        return api_error("该授权下还有在线会话，请先中断", 409, code="SESSION_ACTIVE")
    info = f"user={grant.user_id} host={grant.host_id}"
    db.session.delete(grant)
    db.session.commit()
    log_event("grant", "delete_grant", target_type="grant", target_id=grant_id, target_name=info)
    return api_ok(None, "授权已删除")


@bp.post("/batch")
@admin_required
def batch_grant():
    """批量授权：一个账号 × 多台主机，策略/时间窗/开关取同一套。"""
    payload = request.get_json(silent=True) or {}
    user_id = parse_int(payload.get("userId"))
    host_ids = payload.get("hostIds") or []
    if not user_id or db.session.get(User, user_id) is None:
        return api_error("请选择有效的堡垒机账号", 400, code="INVALID_ARGUMENT")
    host_ids = [parse_int(item) for item in host_ids if parse_int(item)]
    if not host_ids:
        return api_error("请至少选择一台主机", 400, code="INVALID_ARGUMENT")
    policy_id = parse_int(payload.get("policyId"))
    if policy_id and db.session.get(CommandPolicy, policy_id) is None:
        return api_error("指定的命令策略不存在", 400, code="INVALID_ARGUMENT")

    created, skipped = [], []
    for host_id in host_ids:
        host = db.session.get(Host, host_id)
        if host is None:
            skipped.append({"hostId": host_id, "reason": "主机不存在"})
            continue
        duplicated = Grant.query.filter_by(
            user_id=user_id, host_id=host_id, host_account_id=None
        ).first()
        if duplicated is not None:
            skipped.append({"hostId": host_id, "reason": "授权已存在"})
            continue
        grant = Grant(
            user_id=user_id,
            host_id=host_id,
            host_account_id=None,
            policy_id=policy_id,
            can_login=parse_bool(payload.get("canLogin"), True),
            can_sftp=parse_bool(payload.get("canSftp")),
            can_upload=parse_bool(payload.get("canUpload")),
            can_download=parse_bool(payload.get("canDownload")),
            can_port_forward=parse_bool(payload.get("canPortForward")),
            can_webterm=parse_bool(payload.get("canWebterm"), True),
            time_start=(payload.get("timeStart") or "").strip() or None,
            time_end=(payload.get("timeEnd") or "").strip() or None,
            weekdays=_normalize_weekdays(payload.get("weekdays")),
            expire_at=parse_datetime(payload.get("expireAt")),
            max_sessions=parse_int(payload.get("maxSessions"), 0) or 0,
            enabled=True,
            remark=(payload.get("remark") or "").strip(),
        )
        db.session.add(grant)
        created.append(host_id)
    db.session.commit()
    log_event(
        "grant",
        "batch_grant",
        target_type="user",
        target_id=user_id,
        message=f"批量授权：新增 {len(created)} 条，跳过 {len(skipped)} 条",
        detail={"created": created, "skipped": skipped},
    )
    return api_ok(
        {"created": created, "skipped": skipped},
        f"批量授权完成：新增 {len(created)} 条，跳过 {len(skipped)} 条",
    )


@bp.post("/preview")
@permission_required("grant:view", "policy:view")
def preview_grant():
    """在授权前试算：这条命令在该策略下会不会被拦。

    无副作用的试算端点按 ``*:view`` 权限开放（与 ``/api/policies/<id>/test`` 一致）；
    任何**写**操作仍然一律 ``@admin_required``。
    """
    payload = request.get_json(silent=True) or {}
    command = payload.get("command") or ""
    if not command.strip():
        return api_error("请输入要试算的命令", 400, code="INVALID_ARGUMENT")
    from ..models import CommandPolicy
    from ..policy import evaluate_policy, freeze_policy
    from ..settings_store import get_int

    policy = None
    policy_id = parse_int(payload.get("policyId")) or get_int("default_policy_id", 0)
    if policy_id:
        policy = db.session.get(CommandPolicy, policy_id)
    if policy is None:
        policy = CommandPolicy.query.filter_by(is_default=True).first()
    decision = evaluate_policy(freeze_policy(policy), command)
    return api_ok(
        {
            "policyId": policy.id if policy else None,
            "policyName": policy.name if policy else "未绑定策略（按审计放行）",
            "allowed": decision.allowed,
            "action": decision.action,
            "riskLevel": decision.risk_level,
            "reason": decision.reason,
            "ruleId": decision.rule_id,
            "rulePattern": decision.rule_pattern,
            "segments": [
                {
                    "segment": seg.segment,
                    "allowed": seg.allowed,
                    "action": seg.action,
                    "riskLevel": seg.risk_level,
                    "rulePattern": seg.rule_pattern,
                    "reason": seg.reason,
                }
                for seg in (decision.segments or [])
            ],
        }
    )


@bp.get("/matrix")
@permission_required("grant:view")
def grant_matrix():
    """账号 × 主机 授权矩阵，供管理端一眼看清谁能碰谁。"""
    page, size = page_args(default_size=50, max_size=200)
    query = User.query.order_by(User.id.asc())
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(User.username.like(f"%{keyword}%"))
    total = query.count()
    users = query.offset((page - 1) * size).limit(size).all()
    hosts = Host.query.order_by(Host.id.asc()).all()
    # 单元格显示实际生效的策略名：授权未绑定策略时回落到系统默认策略
    policy_names = {policy.id: policy.name for policy in CommandPolicy.query.all()}
    _default_policy = CommandPolicy.query.filter_by(is_default=True).first()
    fallback_policy = _default_policy.name if _default_policy else ""
    rows = []
    for user in users:
        grants = {grant.host_id: grant for grant in Grant.query.filter_by(user_id=user.id).all()}
        cells = []
        for host in hosts:
            grant = grants.get(host.id)
            cells.append(
                {
                    "hostId": host.id,
                    "hostName": host.name,
                    "granted": grant is not None,
                    "enabled": bool(grant.enabled) if grant else False,
                    "canWebterm": bool(grant.can_webterm) if grant else False,
                    "policyName": (
                        (policy_names.get(grant.policy_id) or fallback_policy) if grant else ""
                    ),
                    "grantId": grant.id if grant else None,
                }
            )
        rows.append(
            {
                "userId": user.id,
                "username": user.username,
                "displayName": user.display_name,
                "roleCode": user.role_code,
                "status": user.status,
                "grantCount": len(grants),
                "cells": cells,
            }
        )
    return api_list(rows, total, page, size, hosts=[{"id": h.id, "name": h.name} for h in hosts])


_ = parse_bool
