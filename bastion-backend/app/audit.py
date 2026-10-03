"""审计落库：控制台操作流水、命令流水、会话流水。

网关 / Web 终端跑在非请求线程里，没有 Flask 请求上下文，
因此这里自己解析应用对象并推上下文，保证审计永不丢。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from flask import current_app, g

from .extensions import db
from .models import AuditLog, CommandLog, FileLog, SessionRecord

logger = logging.getLogger("bastion.audit")


def get_app():
    """拿到应用对象，优先用在请求里的 current_app。"""
    try:
        return current_app._get_current_object()
    except RuntimeError:
        from . import get_current_app

        return get_current_app()


def _actor_from_request():
    user = getattr(g, "actor", None)
    if user is None:
        return None
    return user


def log_event(
    category: str,
    action: str,
    *,
    result: str = "success",
    message: str = "",
    target_type: str = "",
    target_id: Any = "",
    target_name: str = "",
    detail: dict | None = None,
    actor=None,
    actor_username: str = "",
    actor_role: str = "",
    actor_id: int | None = None,
    ip: str = "",
    user_agent: str = "",
) -> None:
    """写一条审计流水。任何异常都不能影响主流程。"""
    try:
        if actor is None:
            actor = _actor_from_request()
        if actor is not None:
            actor_id = actor.id
            actor_username = actor.username
            actor_role = actor.role_code
        entry = AuditLog(
            ts=datetime.now(timezone.utc).replace(tzinfo=None),
            category=category,
            action=action,
            actor_id=actor_id,
            actor_username=actor_username or "",
            actor_role=actor_role or "",
            target_type=target_type,
            target_id=str(target_id or ""),
            target_name=target_name or "",
            result=result,
            message=(message or "")[:512],
            detail=detail or {},
            ip=ip or "",
            user_agent=(user_agent or "")[:255],
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:  # pragma: no cover - 审计不能反噬业务
        db.session.rollback()
        logger.exception("写入审计流水失败 category=%s action=%s", category, action)


def log_command(
    *,
    session: SessionRecord | None,
    command: str,
    output: str = "",
    action: str = "allow",
    risk_level: str = "low",
    matched_rule_id: int | None = None,
    matched_rule_pattern: str = "",
    reason: str = "",
    started_at: datetime | None = None,
    duration_ms: int = 0,
    exit_status: int | None = None,
    truncated: bool = False,
    seq: int = 0,
    commit: bool = True,
) -> CommandLog | None:
    """写一条命令审计记录，并同步会话说数。"""
    try:
        started = started_at or datetime.now(timezone.utc).replace(tzinfo=None)
        record = CommandLog(
            session_id=session.id if session else None,
            sid=session.sid if session else "",
            seq=seq,
            user_id=session.user_id if session else None,
            username=session.username if session else "",
            host_id=session.host_id if session else None,
            host_name=session.host_name if session else "",
            command=command or "",
            output=output or "",
            action=action,
            risk_level=risk_level or "low",
            matched_rule_id=matched_rule_id,
            matched_rule_pattern=matched_rule_pattern or "",
            reason=reason or "",
            started_at=started,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None),
            duration_ms=duration_ms,
            exit_status=exit_status,
            truncated=truncated,
        )
        db.session.add(record)
        if session is not None:
            session.command_count = (session.command_count or 0) + 1
            if action == "deny":
                session.denied_count = (session.denied_count or 0) + 1
            from .policy import RISK_ORDER

            if RISK_ORDER.get(record.risk_level, 0) > RISK_ORDER.get(
                session.max_risk_level or "low", 0
            ):
                session.max_risk_level = record.risk_level
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return record
    except Exception:  # pragma: no cover
        db.session.rollback()
        logger.exception("写入命令审计失败 command=%s", command[:80])
        return None


def log_file_op(
    *,
    session: SessionRecord | None,
    operation: str,
    path: str = "",
    target_path: str = "",
    action: str = "allow",
    risk_level: str = "low",
    matched_rule_id: int | None = None,
    matched_rule_pattern: str = "",
    reason: str = "",
    result: str = "success",
    message: str = "",
    size: int = 0,
    file_count: int = 1,
    started_at: datetime | None = None,
    duration_ms: int = 0,
    seq: int = 0,
    commit: bool = True,
) -> FileLog | None:
    """写一条文件操作审计，并同步会话的状态与告警计数。

    每一次文件管理器操作都必须走这里 —— 放行的、被拦的、执行失败的都要落，
    否则「全操作审计」就只是句口号。
    """
    try:
        started = started_at or datetime.now(timezone.utc).replace(tzinfo=None)
        record = FileLog(
            session_id=session.id if session else None,
            sid=session.sid if session else "",
            seq=seq,
            user_id=session.user_id if session else None,
            username=session.username if session else "",
            host_id=session.host_id if session else None,
            host_name=session.host_name if session else "",
            operation=operation or "",
            path=path or "",
            target_path=target_path or "",
            action=action,
            risk_level=risk_level or "low",
            matched_rule_id=matched_rule_id,
            matched_rule_pattern=matched_rule_pattern or "",
            reason=(reason or "")[:255],
            result=result,
            message=(message or "")[:512],
            size=int(size or 0),
            file_count=int(file_count or 1),
            started_at=started,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None),
            duration_ms=duration_ms,
        )
        db.session.add(record)
        if session is not None:
            session.command_count = (session.command_count or 0) + 1
            if action == "deny":
                session.denied_count = (session.denied_count or 0) + 1
            if result == "failure" and session.status == "active":
                session.status = "active"
            from .file_policy import RISK_ORDER

            if RISK_ORDER.get(record.risk_level, 0) > RISK_ORDER.get(
                session.max_risk_level or "low", 0
            ):
                session.max_risk_level = record.risk_level
            if size:
                session.bytes_out = (session.bytes_out or 0) + int(size)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return record
    except Exception:  # pragma: no cover
        db.session.rollback()
        logger.exception("写入文件操作审计失败 operation=%s path=%s", operation, path[:80])
        return None


def new_session(**kwargs) -> SessionRecord:
    record = SessionRecord(**kwargs)
    db.session.add(record)
    db.session.commit()
    return record


def close_session(
    session: SessionRecord,
    *,
    status: str = "closed",
    reason: str = "",
    bytes_in: int | None = None,
    bytes_out: int | None = None,
) -> None:
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session.status = status
        session.end_reason = (reason or "")[:128]
        session.ended_at = now
        if session.started_at:
            started = session.started_at
            if started.tzinfo is not None:
                started = started.replace(tzinfo=None)
            session.duration_seconds = int((now - started).total_seconds())
        if bytes_in is not None:
            session.bytes_in = bytes_in
        if bytes_out is not None:
            session.bytes_out = bytes_out
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        logger.exception("关闭会话记录失败 sid=%s", getattr(session, "sid", "?"))
