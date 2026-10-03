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
from .models import (
    AuditLog,
    CommandLog,
    FileLog,
    SessionRecord,
    compute_entry_hash,
)

logger = logging.getLogger("bastion.audit")


def get_app():
    """拿到应用对象，优先用在请求里的 current_app。"""
    try:
        return current_app._get_current_object()
    except RuntimeError:
        from . import get_current_app

        return get_current_app()


def _secret_key() -> str:
    app = get_app()
    return str(app.config.get("SECRET_KEY", ""))


def _last_entry_hash(model_cls, *, before_id: int | None = None) -> str:
    """取上一条（id < before_id）的 entry_hash；before_id 为 None 时取表尾。

    必须通过 ORM query 查询，这样可以看到当前 session 中 flush 过但尚未 commit 的记录。
    """
    query = db.session.query(model_cls)
    if before_id is not None:
        query = query.filter(model_cls.id < before_id)
    last = query.order_by(model_cls.id.desc()).first()
    entry = getattr(last, "entry_hash", "") if last is not None else ""
    return entry or ""


def _assign_chain_hashes(entry, table: str) -> None:
    """给刚 flush 的记录计算 prev_hash / entry_hash，并写回实体。"""
    secret = _secret_key()
    prev_hash = _last_entry_hash(type(entry), before_id=entry.id)
    fields = entry.chain_fields()
    entry_hash = compute_entry_hash(
        table=table, fields=fields, prev_hash=prev_hash, secret_key=secret
    )
    entry.prev_hash = prev_hash or ""
    entry.entry_hash = entry_hash


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
        db.session.flush()
        _assign_chain_hashes(entry, "audit_logs")
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
        db.session.flush()
        _assign_chain_hashes(record, "command_logs")
        if commit:
            db.session.commit()
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
        db.session.flush()
        _assign_chain_hashes(record, "file_logs")
        if commit:
            db.session.commit()
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


# --------------------------------------------------------------------------
# 链式哈希验证：verify_chain 是幂等的，老记录即使 prev_hash/entry_hash
# 为空也当作「链起点之前的历史数据块」。
# --------------------------------------------------------------------------

from .models import AuditLog, CommandLog, FileLog  # noqa: E402

CHAIN_TABLES: list[tuple[str, type]] = [
    ("audit_logs", AuditLog),
    ("command_logs", CommandLog),
    ("file_logs", FileLog),
]


def verify_table_chain(  # noqa: C901
    model_cls,
    *,
    secret_key: str | None = None,
    expected_head: str | None = None,
):
    """逐条按 id 升序验证一条链。返回 ``(ok, total, verified, prefix, pending_inside, head, first_bad, errors)``。

    - ``ok``: 是否完整通过（没有坏记录）
    - ``total``: 表总行数
    - ``verified``: 实际上参与链式哈希校验的行数（即 ``entry_hash != ""`` 的行）
    - ``prefix``: 链**起步之前**的空哈希行数 —— 升级前遗留数据，合法，不报错
    - ``pending_inside``: 链**起步之后**出现的空哈希行数 —— 非法：把某行的哈希清空再冒充
      「升级前遗留」是藏篡改的常用手法（清掉尾行哈希后，库内自洽的校验会判 clean）
    - ``head``: 链尾（最后一个非空 ``entry_hash``）
    - ``expected_head``: 库外锚点（CLI 每天抄走的那串）；对不上即判截断，errors 里点名「锚点」
    - ``first_bad``: 首个坏记录的 ``id`` 或 ``None``
    - ``errors``: 最多前 32 条错误（``{id, reason, expected, actual}``）

    边界（诚实声明）：整张表**一行都没有哈希**时，它与「升级前的纯遗留表」在库内不可区分，
    需要 ``expected_head`` 锚点才能裁决 —— 别把「库内自洽」当成「没被截断」。
    """
    from .models import compute_entry_hash  # noqa: E402

    if secret_key is None:
        secret_key = _secret_key()
    errors: list[dict] = []
    total = 0
    verified = 0
    prefix = 0
    pending_inside = 0
    first_bad = None
    chain_started = False
    prev_hash = ""
    for row in db.session.query(model_cls).order_by(model_cls.id).yield_per(2000):
        total += 1
        current_entry = (row.entry_hash or "") if hasattr(row, "entry_hash") else ""
        if not current_entry:
            if chain_started:
                # 链一旦起步，任何空 entry_hash 都是异常：要么被清空（藏篡改），要么被插了伪造的遗留行
                pending_inside += 1
                errors.append(
                    {
                        "id": row.id,
                        "reason": "链内出现空 entry_hash（疑似清空哈希冒充升级前遗留）",
                        "expected": "非空 entry_hash",
                        "actual": "",
                    }
                )
                if first_bad is None:
                    first_bad = row.id
            else:
                # 链起步之前的遗留块：既无 prev 也无 entry，合法
                prefix += 1
            continue
        chain_started = True
        verified += 1
        if (row.prev_hash or "") != prev_hash:
            reason = "prev_hash 不匹配（与上一条 entry_hash 不符）"
            errors.append(
                {
                    "id": row.id,
                    "reason": reason,
                    "expected": prev_hash,
                    "actual": row.prev_hash or "",
                }
            )
            if first_bad is None:
                first_bad = row.id
        fields = row.chain_fields()
        expected = compute_entry_hash(
            table=model_cls.__tablename__,
            fields=fields,
            prev_hash=row.prev_hash or "",
            secret_key=secret_key,
        )
        if expected != current_entry:
            reason = "entry_hash 校验失败（字段被篡改）"
            errors.append(
                {
                    "id": row.id,
                    "reason": reason,
                    "expected": expected,
                    "actual": current_entry,
                }
            )
            if first_bad is None:
                first_bad = row.id
        # 无论本条是否损坏，下一条 prev 都取本条存储的 entry_hash（不是期望的）
        prev_hash = current_entry
        if len(errors) >= 32:
            break
    head = prev_hash
    anchor_checked = expected_head is not None
    if anchor_checked and (expected_head or "") != head:
        # 库内自洽看不出「被砍掉尾巴」，只有库外锚点能裁决截断
        errors.append(
            {
                "id": None,
                "reason": "链尾与库外锚点不符（链被截断或表被替换）",
                "expected": expected_head or "",
                "actual": head,
            }
        )
    ok = len(errors) == 0
    return {
        "ok": ok,
        "total": total,
        "verified": verified,
        "prefix": prefix,
        "pending_inside": pending_inside,
        "head": head,
        "anchor_checked": anchor_checked,
        "first_bad": first_bad,
        "errors": errors,
    }


def verify_all_chains(
    *,
    secret_key: str | None = None,
    expected_heads: dict[str, str] | None = None,
) -> dict:
    """一次性验证三张审计流水表。返回 ``{ok, tables: {name: result}, heads}``。

    ``expected_heads`` 是「库外锚点」字典（表名 → 上次抄走的链尾哈希），传了就比对链尾。
    """
    if secret_key is None:
        secret_key = _secret_key()
    tables: dict[str, dict] = {}
    head: dict[str, dict] = {}
    all_ok = True
    for name, model in CHAIN_TABLES:
        res = verify_table_chain(
            model,
            secret_key=secret_key,
            expected_head=(expected_heads or {}).get(name),
        )
        tables[name] = res
        if not res["ok"]:
            all_ok = False
        # 表头信息（最后一条 id/hash）
        last = (
            db.session.query(model)
            .order_by(model.id.desc())
            .first()
        )
        head[name] = {
            "lastId": last.id if last else None,
            "lastEntryHash": last.entry_hash if last and getattr(last, "entry_hash", None) else "",
            # 链尾 = 最后一个「非空 entry_hash」，锚点比对与 / --print-head 都以它为准
            "verifiedHead": res["head"],
        }
    return {"ok": all_ok, "tables": tables, "heads": head}
