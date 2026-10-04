"""SFTP 文件管理器：会话生命周期、访问控制、文件策略与全操作审计。

一次「文件管理器窗口」= 一条 `SessionRecord(source="web", protocol="sftp")`
+ 一个 paramiko SFTP 客户端。每个操作都必须先过访问控制（Grant 开关），
再过文件策略（FilePolicy），最后落一条 `FileLog` —— 放行的、被拦下的、
执行失败的都落库，否则「每一个操作都加入审计」就只是句口号。

会话会注册进 `session_registry`，所以审计页的「强制断开」可以直接断掉
文件管理器连接（registry 会调用本模块 `FileSession.stop(reason)`）。
"""

from __future__ import annotations

import logging
import posixpath
import stat as stat_module
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterator

from ..access import check_session_quota, find_access, grant_accounts
from ..audit import close_session, log_event, log_file_op, new_session
from ..file_policy import (
    DOWNLOAD_OPERATIONS,
    UPLOAD_OPERATIONS,
    WRITE_OPERATIONS,
    FileDecision,
    evaluate_file_policy,
    freeze_file_policy,
    join_path,
    normalize_path,
    operation_label,
    parent_path,
    resolve_file_policy,
)
from ..models import Grant, Host, SessionRecord, gen_sid, utcnow
from ..session_registry import registry as session_registry
from ..ssh_client import SSHError, build_target
from ..ssh_client import close as close_connection
from ..ssh_client import connect

logger = logging.getLogger(__name__)

# 单次文本读取/编辑上限（512 KiB），超过的部分要么截断要么拒绝
MAX_TEXT_BYTES = 512 * 1024
# 单次目录列出的最大条目数（超大目录截断并标记）
MAX_LIST_ENTRIES = 2000
# 打包下载上限：文件数与总字节数
MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
# 目录递归复制上限
MAX_COPY_FILES = 500
MAX_COPY_BYTES = 512 * 1024 * 1024
# SFTP 通道超时（秒）
SFTP_TIMEOUT = 45
# 单个用户同时在线的文件管理器会话上限
MAX_SESSIONS_PER_USER = 8
CHUNK = 256 * 1024


class FileError(RuntimeError):
    """可直接映射成接口错误信封的异常。"""

    def __init__(self, message: str, *, code: str = "FILE_ERROR", status: int = 400, **extra: Any):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra


def _iso(value: float | None) -> str:
    if not value:
        return ""
    return (
        datetime.fromtimestamp(float(value), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class FileEntry:
    """目录里的一个条目（对外结构固定，前端不用猜字段）。"""

    name: str
    path: str
    type: str = "file"  # dir | file | link | other
    size: int = 0
    mtime: str = ""
    mode: str = ""
    mode_octal: str = ""
    uid: int = 0
    gid: int = 0

    @property
    def is_dir(self) -> bool:
        return self.type == "dir"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "isDir": self.is_dir,
            "size": self.size,
            "mtime": self.mtime,
            "mode": self.mode,
            "modeOctal": self.mode_octal,
            "uid": self.uid,
            "gid": self.gid,
        }


def _entry_from_attrs(directory: str, name: str, attrs: Any) -> FileEntry:
    mode = int(getattr(attrs, "st_mode", 0) or 0)
    if stat_module.S_ISDIR(mode):
        kind = "dir"
    elif stat_module.S_ISLNK(mode):
        kind = "link"
    elif stat_module.S_ISREG(mode):
        kind = "file"
    else:
        kind = "other"
    return FileEntry(
        name=name,
        path=join_path(directory, name),
        type=kind,
        size=int(getattr(attrs, "st_size", 0) or 0),
        mtime=_iso(getattr(attrs, "st_mtime", None)),
        mode=stat_module.filemode(mode) if mode else "",
        mode_octal=f"{stat_module.S_IMODE(mode):04o}" if mode else "",
        uid=int(getattr(attrs, "st_uid", 0) or 0),
        gid=int(getattr(attrs, "st_gid", 0) or 0),
    )


@dataclass
class FileSession:
    """一个在线文件管理器会话。"""

    sid: str
    record_id: int
    user_id: int
    username: str
    host_id: int
    host_name: str
    host_address: str
    account_id: int | None
    account_username: str
    grant_id: int | None
    can_sftp: bool
    can_upload: bool
    can_download: bool
    can_file_write: bool
    policy_name: str
    policy: Any
    sftp: Any
    connection: Any
    app: Any
    client_ip: str = ""
    client_port: int = 0
    home_dir: str = "/"
    seq: int = 0
    opened_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    closed: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- 会话元信息 ----------------------------------------------------
    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def touch(self) -> None:
        self.last_active = time.time()
        session_registry.touch(self.sid)

    def rights(self) -> dict:
        """文件能力开关以数据库里的授权为准（不是打开窗口那一刻的快照）。

        会话打开时拍平的四个开关只是兜底（超管的临时授权没有持久行）；
        只要授权行还在，每次操作都重新读一次 —— 管理员改授权后，
        **已经打开的窗口立即跟随**，尤其是撤销「改文件」这类高危能力时必须当场生效，
        不能等用户关掉窗口重开。
        """
        snapshot = {
            "canSftp": self.can_sftp,
            "canUpload": self.can_upload,
            "canDownload": self.can_download,
            "canFileWrite": self.can_file_write,
        }
        if not self.grant_id:
            return snapshot
        try:
            from ..extensions import db

            grant = db.session.get(Grant, self.grant_id)
        except Exception:  # pragma: no cover - 脱离请求上下文时用快照
            return snapshot
        if grant is None:
            return {"canSftp": False, "canUpload": False, "canDownload": False, "canFileWrite": False}
        return {
            "canSftp": bool(grant.can_sftp),
            "canUpload": bool(grant.can_upload),
            "canDownload": bool(grant.can_download),
            "canFileWrite": bool(getattr(grant, "can_file_write", False)),
        }

    def to_dict(self) -> dict:
        rights = self.rights()
        return {
            "sid": self.sid,
            "hostId": self.host_id,
            "hostName": self.host_name,
            "hostAddress": self.host_address,
            "accountId": self.account_id,
            "accountUsername": self.account_username,
            "grantId": self.grant_id,
            "policyName": self.policy_name,
            "homeDir": self.home_dir,
            "canSftp": rights["canSftp"],
            "canUpload": rights["canUpload"],
            "canDownload": rights["canDownload"],
            "canFileWrite": rights["canFileWrite"],
            "clientIp": self.client_ip,
            "openedAt": _iso(self.opened_at),
            "lastActive": _iso(self.last_active),
            "operations": self.seq,
        }

    # ---- 生命周期 ------------------------------------------------------
    def stop(self, reason: str = "管理员强制断开") -> None:
        """session_registry.close() 会调用这个方法断开文件管理器。"""
        self.close(reason or "管理员强制断开", status="terminated")

    def close(self, reason: str = "", *, status: str = "closed") -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        session_registry.unregister(self.sid)
        try:
            self.sftp.close()
        except Exception:  # pragma: no cover - 远端可能已经断开
            logger.debug("关闭 SFTP 客户端失败 sid=%s", self.sid, exc_info=True)
        try:
            close_connection(self.connection)
        except Exception:  # pragma: no cover
            logger.debug("关闭 SSH 连接失败 sid=%s", self.sid, exc_info=True)
        try:
            with self.app.app_context():
                record = None
                if self.record_id:
                    from ..extensions import db

                    record = db.session.get(SessionRecord, self.record_id)
                if record is not None:
                    close_session(record, status=status, reason=reason or "文件管理器已关闭")
                log_event(
                    "file",
                    "file_session_close",
                    message=f"{self.username} 关闭 {self.host_name} 文件管理器（{reason or '正常关闭'}）",
                    target_type="session",
                    target_id=self.sid,
                    target_name=self.host_name,
                    detail={"operations": self.seq, "endedAt": _iso(time.time())},
                )
        except Exception:  # pragma: no cover
            logger.exception("收口文件会话记录失败 sid=%s", self.sid)


# ---------------------------------------------------------------------------
# 会话注册表
# ---------------------------------------------------------------------------

_sessions: dict[str, FileSession] = {}
_sessions_lock = threading.Lock()


def _register(session: FileSession) -> None:
    with _sessions_lock:
        _sessions[session.sid] = session


def get_file_session(sid: str) -> FileSession | None:
    with _sessions_lock:
        session = _sessions.get(sid)
    if session is None or session.closed:
        return None
    return session


def require_file_session(sid: str) -> FileSession:
    session = get_file_session(sid)
    if session is None:
        raise FileError("文件管理器会话不存在或已结束", code="SESSION_NOT_FOUND", status=404)
    return session


def list_live_sessions() -> list[dict]:
    with _sessions_lock:
        items = [s.to_dict() for s in _sessions.values() if not s.closed]
    return items


def count_for_user(user_id: int) -> int:
    with _sessions_lock:
        return sum(1 for s in _sessions.values() if not s.closed and s.user_id == user_id)


def close_file_session(sid: str, reason: str = "用户关闭文件管理器") -> bool:
    session = get_file_session(sid)
    if session is None:
        return False
    session.close(reason)
    return True


def open_file_session(
    app,
    *,
    user,
    host_id: int,
    account_id: int | None = None,
    client_ip: str = "",
    client_port: int = 0,
) -> FileSession:
    """打开一条 SFTP 文件管理器会话（与网页终端同一套准入链路）。"""
    from ..extensions import db

    host = db.session.get(Host, int(host_id or 0))
    if host is None:
        raise FileError("主机不存在", code="HOST_NOT_FOUND", status=404)
    if (host.status or "active") != "active":
        raise FileError("主机已停用", code="HOST_DISABLED", status=409)

    resolved = find_access(user, host_id=host.id, account_id=account_id, require_login=False)
    if resolved is None:
        raise FileError("没有该主机的访问授权", code="NO_ACCESS", status=403)
    grant = resolved.grant
    if not grant.can_sftp:
        raise FileError("该授权未开放 SFTP 文件管理", code="SFTP_FORBIDDEN", status=403)

    account = resolved.account
    if account is None:
        accounts = grant_accounts(user, grant)
        if not accounts:
            raise FileError("该主机下没有可用的资产账号", code="NO_ACCOUNT", status=409)
        account = accounts[0]

    allowed, reason = check_session_quota(user, grant)
    if not allowed:
        raise FileError(reason, code="QUOTA_EXCEEDED", status=429)
    if count_for_user(user.id) >= MAX_SESSIONS_PER_USER:
        raise FileError(
            f"同时在线的文件管理器窗口已达上限（{MAX_SESSIONS_PER_USER}）",
            code="QUOTA_EXCEEDED",
            status=429,
        )

    # SFTP 建连必须在 app_context 之外进行，避免长时间占用请求内的会话/连接。
    # 端口取 **ssh 端点**：一台主机可能同时有 winrm/rdp 端点，镜像字段里的 port 可能是
    # 5985 或 3389，拿它去连 SSH 会连到别的服务上。
    ssh_endpoint = host.endpoint_for("ssh")
    if ssh_endpoint is None:
        available = "、".join(host.protocol_names()) or "无"
        raise FileError(
            f"该主机没有 SSH 端点（可用端点：{available}），文件管理器连不上",
            code="NO_SSH_ENDPOINT",
            status=400,
        )
    target = build_target(host, account, port=ssh_endpoint.port)
    try:
        connection = connect(target)
    except SSHError as exc:
        log_event(
            "file",
            "file_connect_failed",
            result="failure",
            message=f"{user.username} 连接 {host.name} 的 SFTP 失败：{exc}",
            target_type="host",
            target_id=host.id,
            target_name=host.name,
        )
        raise FileError(f"SFTP 连接失败：{exc}", code="SFTP_CONNECT_FAILED", status=502) from exc

    try:
        sftp = connection.client.open_sftp()
        channel = sftp.get_channel()
        if channel is not None:
            channel.settimeout(SFTP_TIMEOUT)
    except Exception as exc:  # pragma: no cover - 远端一般不开放 SFTP 时触发
        close_connection(connection)
        log_event(
            "file",
            "file_connect_failed",
            result="failure",
            message=f"{user.username} 打开 {host.name} 的 SFTP 子系统失败：{exc}",
            target_type="host",
            target_id=host.id,
            target_name=host.name,
        )
        raise FileError(f"远端未开放 SFTP：{exc}", code="SFTP_UNAVAILABLE", status=502) from exc

    policy = freeze_file_policy(resolve_file_policy(grant))
    home_dir = "/"
    try:
        home_dir = sftp.normalize(".") or "/"
    except Exception:  # pragma: no cover
        logger.debug("读取远端家目录失败 host=%s", host.name, exc_info=True)

    sid = gen_sid()
    record = new_session(
        sid=sid,
        user_id=user.id,
        username=user.username,
        role_code=user.role_code,
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        account_id=account.id,
        account_username=account.username,
        grant_id=grant.id,
        source="web",
        protocol="sftp",
        client_ip=client_ip,
        client_port=client_port,
        status="active",
        started_at=utcnow(),
    )

    session = FileSession(
        sid=sid,
        record_id=record.id,
        user_id=user.id,
        username=user.username,
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        account_id=account.id,
        account_username=account.username,
        grant_id=grant.id,
        can_sftp=bool(grant.can_sftp),
        can_upload=bool(grant.can_upload),
        can_download=bool(grant.can_download),
        can_file_write=bool(getattr(grant, "can_file_write", False)),
        policy_name=getattr(policy, "name", "") or "默认文件策略",
        policy=policy,
        sftp=sftp,
        connection=connection,
        app=app,
        client_ip=client_ip,
        client_port=client_port,
        home_dir=home_dir,
    )
    _register(session)
    session_registry.register(
        sid,
        kind="file",
        user_id=user.id,
        username=user.username,
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        account_username=account.username,
        grant_id=grant.id,
        source="web",
        protocol="sftp",
        client_ip=client_ip,
        client_port=client_port,
        policy_name=session.policy_name,
        title=f"{host.name} 文件管理器",
        bridge=session,
    )
    log_event(
        "file",
        "file_session_open",
        message=(
            f"{user.username} 打开 {host.name} 的文件管理器"
            f"（账号 {account.username}，策略 {session.policy_name}）"
        ),
        target_type="session",
        target_id=sid,
        target_name=host.name,
        detail={"homeDir": home_dir, "grantId": grant.id},
    )
    return session


# ---------------------------------------------------------------------------
# 访问控制 + 策略闸门
# ---------------------------------------------------------------------------


def _access_reason(session: FileSession, operation: str) -> str:
    """Grant 级别的开关先说话（策略是第二道闸）；开关每次操作都重新读授权，撤销立即生效。"""
    rights = session.rights()
    if not rights["canSftp"]:
        return "该授权已关闭 SFTP 文件管理"
    if operation in UPLOAD_OPERATIONS and not rights["canUpload"]:
        return "该授权未开放上传"
    if operation in DOWNLOAD_OPERATIONS and not rights["canDownload"]:
        return "该授权未开放下载"
    if operation in WRITE_OPERATIONS and not rights["canFileWrite"]:
        return "该授权未开放文件修改（上传/删除/重命名等）"
    return ""


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _human_error(operation: str, message: str) -> str:
    """给失败原因套一层中文框，异常原文一字不改。

    审计事件是**给人看的**：`message` 里全是 `EOF`、`Permission denied` 这类库异常时，
    运维得先猜「这是哪个操作失败了」。这里补上操作名（取自 `FILE_OPERATIONS`），异常原文
    原样留在后面；机器字段 `operation` / `action` / `result` 仍是英文，机器照样好判。
    """
    detail = (message or "").strip()
    if not detail or _has_chinese(detail):
        return message
    return f"{operation_label(operation)}失败：{detail}"


def _audit(
    session: FileSession,
    decision: FileDecision | None,
    *,
    operation: str,
    path: str = "",
    target_path: str = "",
    result: str = "success",
    message: str = "",
    size: int = 0,
    file_count: int = 1,
    started: float | None = None,
) -> None:
    session.touch()
    duration = int((time.time() - started) * 1000) if started else 0
    if result != "success":
        message = _human_error(operation, message)
    log_file_op(
        session=_record_of(session),
        operation=operation,
        path=path,
        target_path=target_path,
        action=(decision.action if decision else ("allow" if result == "success" else "deny")),
        risk_level=(decision.risk_level if decision else "low"),
        matched_rule_id=(decision.rule_id if decision else None),
        matched_rule_pattern=(decision.rule_pattern if decision else ""),
        reason=(decision.reason if decision else ""),
        result=result,
        message=message,
        size=size,
        file_count=file_count,
        started_at=(datetime.fromtimestamp(started, tz=timezone.utc).replace(tzinfo=None) if started else None),
        duration_ms=duration,
        seq=session.next_seq(),
    )


def _record_of(session: FileSession) -> SessionRecord | None:
    from ..extensions import db

    if not session.record_id:
        return None
    try:
        return db.session.get(SessionRecord, session.record_id)
    except Exception:  # pragma: no cover
        return None


def _fail(session: FileSession, decision: FileDecision, *, operation: str, path: str, target_path: str = "") -> None:
    """被拦下的操作也要进审计，然后抛给上层转成 403。"""
    _audit(
        session,
        decision,
        operation=operation,
        path=path,
        target_path=target_path,
        result="denied",
        message=decision.reason,
    )
    log_event(
        "file",
        f"file_{operation}_denied",
        result="failure",
        message=decision.reason or f"文件操作被拦截：{operation}",
        target_type="host",
        target_id=session.host_id,
        target_name=session.host_name,
        actor_id=session.user_id,
        actor_username=session.username,
        detail={
            "sid": session.sid,
            "operation": operation,
            "path": path,
            "targetPath": target_path,
            "ruleId": decision.rule_id,
            "rulePattern": decision.rule_pattern,
        },
    )
    raise FileError(
        decision.reason or "该操作被文件策略拦截",
        code="FILE_DENIED",
        status=403,
        decision=decision.to_dict(),
    )


def check_operation(session: FileSession, operation: str, path: str, *, target_path: str = "") -> dict:
    """策略试算：只判断能不能做，不执行、不落审计（供界面置灰按钮用）。"""
    operation = (operation or "").strip().lower()
    path = normalize_path(path or session.home_dir)
    reason = _access_reason(session, operation)
    if reason:
        return FileDecision(
            allowed=False,
            action="deny",
            risk_level="high",
            reason=reason,
            policy_name=session.policy_name,
            operation=operation,
            path=path,
        ).to_dict()
    return evaluate_file_policy(session.policy, operation, path, target_path=target_path).to_dict()


def _guard(session: FileSession, operation: str, path: str, *, target_path: str = "") -> FileDecision:
    """访问控制 + 文件策略，双闸都过才放行；被拦就审计并抛 FileError。"""
    if session.closed:
        raise FileError("文件管理器会话已结束", code="SESSION_CLOSED", status=409)
    operation = (operation or "").strip().lower()
    reason = _access_reason(session, operation)
    if reason:
        decision = FileDecision(
            allowed=False,
            action="deny",
            risk_level="high",
            reason=reason,
            policy_name=session.policy_name,
            operation=operation,
            path=path,
        )
        _fail(session, decision, operation=operation, path=path, target_path=target_path)
    decision = evaluate_file_policy(session.policy, operation, path, target_path=target_path)
    if not decision.allowed:
        _fail(session, decision, operation=operation, path=path, target_path=target_path)
    return decision


def _wrap_io(exc: Exception, action: str) -> FileError:
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, IOError) and "No such file" in text:
        return FileError("路径不存在", code="FILE_NOT_FOUND", status=404)
    if isinstance(exc, IOError) and "Permission denied" in text:
        return FileError("远端拒绝该操作（权限不足）", code="FILE_PERMISSION_DENIED", status=403)
    return FileError(f"{action}失败：{text}", code="FILE_IO_ERROR", status=502)


# ---------------------------------------------------------------------------
# 文件操作
# ---------------------------------------------------------------------------


def capabilities(session: FileSession) -> dict:
    rights = session.rights()
    return {
        "sid": session.sid,
        "hostId": session.host_id,
        "hostName": session.host_name,
        "hostAddress": session.host_address,
        "accountUsername": session.account_username,
        "grantId": session.grant_id,
        "policyName": session.policy_name,
        "homeDir": session.home_dir,
        "canSftp": rights["canSftp"],
        "canUpload": rights["canUpload"],
        "canDownload": rights["canDownload"],
        "canFileWrite": rights["canFileWrite"],
        "operations": list(operation_labels().keys()),
        "limits": {
            "maxTextBytes": MAX_TEXT_BYTES,
            "maxListEntries": MAX_LIST_ENTRIES,
            "maxArchiveFiles": MAX_ARCHIVE_FILES,
            "maxArchiveBytes": MAX_ARCHIVE_BYTES,
        },
    }


def operation_labels() -> dict[str, str]:
    from ..file_policy import FILE_OPERATIONS

    return dict(FILE_OPERATIONS)


def list_dir(session: FileSession, path: str, *, show_hidden: bool = True) -> dict:
    target = normalize_path(path or session.home_dir)
    decision = _guard(session, "list", target)
    started = time.time()
    with session.lock:
        try:
            attrs_list = session.sftp.listdir_attr(target)
        except Exception as exc:
            _audit(
                session,
                decision,
                operation="list",
                path=target,
                result="failure",
                message=str(exc),
                started=started,
            )
            raise _wrap_io(exc, "读取目录") from exc
    entries = [_entry_from_attrs(target, attrs.filename, attrs) for attrs in attrs_list]
    if not show_hidden:
        entries = [item for item in entries if not item.name.startswith(".")]
    entries.sort(key=lambda item: (0 if item.is_dir else 1, item.name.casefold()))
    truncated = len(entries) > MAX_LIST_ENTRIES
    if truncated:
        entries = entries[:MAX_LIST_ENTRIES]
    _audit(
        session,
        decision,
        operation="list",
        path=target,
        file_count=len(entries),
        message=f"列出目录 {target}（{len(entries)} 项{'，已截断' if truncated else ''}）",
        started=started,
    )
    return {
        "path": target,
        "parent": parent_path(target),
        "entries": [item.to_dict() for item in entries],
        "count": len(entries),
        "truncated": truncated,
    }


def stat_path(session: FileSession, path: str) -> dict:
    target = normalize_path(path or session.home_dir)
    decision = _guard(session, "list", target)
    started = time.time()
    with session.lock:
        try:
            attrs = session.sftp.stat(target)
        except Exception as exc:
            _audit(session, decision, operation="list", path=target, result="failure", message=str(exc), started=started)
            raise _wrap_io(exc, "读取属性") from exc
    entry = _entry_from_attrs(parent_path(target), posixpath.basename(target) or target, attrs)
    _audit(session, decision, operation="list", path=target, message=f"查看属性 {target}", started=started)
    return entry.to_dict()


def _exists(session: FileSession, path: str) -> bool:
    try:
        with session.lock:
            session.sftp.stat(path)
        return True
    except IOError:
        return False
    except Exception:  # pragma: no cover
        return False


def mkdir(session: FileSession, path: str, *, mode: int = 0o755, parents: bool = False) -> dict:
    target = normalize_path(path)
    decision = _guard(session, "mkdir", target)
    started = time.time()
    created = 0
    try:
        if _exists(session, target):
            raise FileError("目录已存在", code="FILE_EXISTS", status=409)
        pending: list[str] = []
        if parents:
            current = parent_path(target)
            while current and current != "/" and not _exists(session, current):
                pending.append(current)
                current = parent_path(current)
        for directory in reversed(pending):
            # 递归创建出来的父目录同样要过策略闸门，别让 /tmp/x 变成绕过口子
            _guard(session, "mkdir", directory)
            with session.lock:
                session.sftp.mkdir(directory)
            created += 1
        with session.lock:
            session.sftp.mkdir(target)
        created += 1
    except FileError:
        raise
    except Exception as exc:
        _audit(session, decision, operation="mkdir", path=target, result="failure", message=str(exc), started=started)
        raise _wrap_io(exc, "新建目录") from exc
    _audit(
        session,
        decision,
        operation="mkdir",
        path=target,
        file_count=created,
        message=f"新建目录 {target}（权限 {mode:04o}）",
        started=started,
    )
    return {"path": target, "created": created}


def _rename_remote(sftp: Any, source: str, target: str) -> None:
    """重命名/移动远端路径。

    优先用 OpenSSH 的 ``posix-rename@openssh.com`` 扩展（允许覆盖目标，符合「改名」
    的直觉）；若服务端不实现该扩展（paramiko 的 ``SFTPServer``、部分精简 SFTP
    服务端都会回 ``Operation unsupported``），退回标准 ``SSH_FXP_RENAME``。
    标准 RENAME 在目标已存在时会被服务端拒绝——调用方已提前拦下同名情况。
    """
    posix_rename = getattr(sftp, "posix_rename", None)
    if posix_rename is not None:
        try:
            posix_rename(source, target)
            return
        except OSError as exc:
            if "unsupported" not in str(exc).lower():
                raise
    sftp.rename(source, target)


def rename_path(session: FileSession, path: str, new_name: str, *, target_path: str | None = None) -> dict:
    source = normalize_path(path)
    if target_path:
        target = normalize_path(target_path)
    else:
        name = (new_name or "").strip()
        if not name:
            raise FileError("新名称不能为空", code="INVALID_ARGUMENT", status=400)
        if "/" in name:
            target = normalize_path(name)
        else:
            target = join_path(parent_path(source), name)
    if target == source:
        raise FileError("新名称与原名称相同", code="INVALID_ARGUMENT", status=400)
    if target.startswith(source.rstrip("/") + "/"):
        raise FileError("不能把目录移入它自己", code="INVALID_ARGUMENT", status=400)
    operation = "rename" if parent_path(source) == parent_path(target) else "move"
    decision = _guard(session, operation, source, target_path=target)
    if _exists(session, target):
        raise FileError("目标已存在，请先处理同名文件", code="FILE_EXISTS", status=409)
    started = time.time()
    with session.lock:
        try:
            _rename_remote(session.sftp, source, target)
        except Exception as exc:
            _audit(
                session,
                decision,
                operation=operation,
                path=source,
                target_path=target,
                result="failure",
                message=str(exc),
                started=started,
            )
            raise _wrap_io(exc, "移动/重命名") from exc
    _audit(
        session,
        decision,
        operation=operation,
        path=source,
        target_path=target,
        message=f"{operation_label(operation)} {source} → {target}",
        started=started,
    )
    return {"operation": operation, "path": source, "targetPath": target}


def _copy_tree(
    session: FileSession,
    source: str,
    target: str,
    *,
    counter: dict,
) -> None:
    with session.lock:
        attrs = session.sftp.stat(source)
    if stat_module.S_ISDIR(attrs.st_mode):
        if not _exists(session, target):
            with session.lock:
                session.sftp.mkdir(target)
            counter["dirs"] += 1
        with session.lock:
            children = session.sftp.listdir_attr(source)
        for child in children:
            _copy_tree(session, join_path(source, child.filename), join_path(target, child.filename), counter=counter)
        return
    counter["files"] += 1
    if counter["files"] > MAX_COPY_FILES:
        raise FileError(f"一次最多复制 {MAX_COPY_FILES} 个文件", code="TOO_MANY_FILES", status=413)
    with session.lock:
        with session.sftp.open(source, "rb") as reader:
            with session.sftp.open(target, "wb") as writer:
                try:
                    writer.set_pipelined(True)
                except Exception:  # pragma: no cover
                    pass
                while True:
                    chunk = reader.read(CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    counter["bytes"] += len(chunk)
                    if counter["bytes"] > MAX_COPY_BYTES:
                        raise FileError("复制内容超过上限", code="TOO_LARGE", status=413)


def copy_path(session: FileSession, path: str, target_path: str, *, recursive: bool = True) -> dict:
    source = normalize_path(path)
    target = normalize_path(target_path)
    if target == source:
        raise FileError("源路径与目标路径相同", code="INVALID_ARGUMENT", status=400)
    if target.startswith(source.rstrip("/") + "/"):
        raise FileError("不能把目录复制进它自己", code="INVALID_ARGUMENT", status=400)
    decision = _guard(session, "copy", source, target_path=target)
    if _exists(session, target):
        raise FileError("目标已存在，请先处理同名文件", code="FILE_EXISTS", status=409)
    with session.lock:
        try:
            attrs = session.sftp.stat(source)
        except Exception as exc:
            _audit(session, decision, operation="copy", path=source, target_path=target, result="failure", message=str(exc))
            raise _wrap_io(exc, "复制") from exc
    if stat_module.S_ISDIR(attrs.st_mode) and not recursive:
        raise FileError("目录复制需要递归开关", code="INVALID_ARGUMENT", status=400)
    counter = {"files": 0, "dirs": 0, "bytes": 0}
    started = time.time()
    try:
        _copy_tree(session, source, target, counter=counter)
    except FileError:
        raise
    except Exception as exc:
        _audit(session, decision, operation="copy", path=source, target_path=target, result="failure", message=str(exc), started=started)
        raise _wrap_io(exc, "复制") from exc
    _audit(
        session,
        decision,
        operation="copy",
        path=source,
        target_path=target,
        size=counter["bytes"],
        file_count=counter["files"] or 1,
        message=f"复制 {source} → {target}（{counter['files']} 个文件，{counter['dirs']} 个目录）",
        started=started,
    )
    return {"path": source, "targetPath": target, **counter}


def delete_paths(session: FileSession, paths: list[str], *, recursive: bool = True) -> dict:
    targets = [normalize_path(item) for item in (paths or []) if str(item or "").strip()]
    if not targets:
        raise FileError("请选择要删除的文件", code="INVALID_ARGUMENT", status=400)
    # 先整体过闸：批量删除里只要有一条被策略拦下，就整批不执行，
    # 避免出现「删了一半才发现被拦」的半成品状态。
    decisions: dict[str, FileDecision] = {}
    for target in targets:
        decisions[target] = _guard(session, "delete", target)

    deleted: list[str] = []
    failed: list[dict] = []
    for target in targets:
        decision = decisions[target]
        started = time.time()
        with session.lock:
            try:
                attrs = session.sftp.stat(target)
            except Exception as exc:
                _audit(
                    session,
                    decision,
                    operation="delete",
                    path=target,
                    result="failure",
                    message=str(exc),
                    started=started,
                )
                failed.append({"path": target, "message": str(exc)})
                continue
        is_dir = stat_module.S_ISDIR(attrs.st_mode)
        if is_dir and not recursive:
            raise FileError("删除目录需要递归开关", code="INVALID_ARGUMENT", status=400)
        counter = {"files": 0, "dirs": 0, "bytes": 0}
        try:
            _remove_tree(session, target, counter=counter, remove_root=True)
        except FileError:
            raise
        except Exception as exc:
            _audit(session, decision, operation="delete", path=target, result="failure", message=str(exc), started=started)
            failed.append({"path": target, "message": str(exc)})
            continue
        deleted.append(target)
        _audit(
            session,
            decision,
            operation="delete",
            path=target,
            file_count=max(1, counter["files"] + counter["dirs"]),
            message=(f"删除目录 {target}（{counter['files']} 个文件，{counter['dirs']} 个目录）" if is_dir else f"删除文件 {target}"),
            started=started,
        )
    return {"deleted": deleted, "failed": failed}


def _remove_tree(session: FileSession, path: str, *, counter: dict, remove_root: bool) -> None:
    with session.lock:
        attrs = session.sftp.stat(path)
    if stat_module.S_ISDIR(attrs.st_mode):
        with session.lock:
            children = session.sftp.listdir_attr(path)
        for child in children:
            _remove_tree(session, join_path(path, child.filename), counter=counter, remove_root=True)
        if remove_root:
            with session.lock:
                session.sftp.rmdir(path)
            counter["dirs"] += 1
        return
    with session.lock:
        session.sftp.remove(path)
    counter["files"] += 1
    counter["bytes"] += int(getattr(attrs, "st_size", 0) or 0)


def chmod_path(session: FileSession, path: str, mode: int) -> dict:
    target = normalize_path(path)
    if not isinstance(mode, int) or mode < 0 or mode > 0o7777:
        raise FileError("权限值不合法（0 - 07777）", code="INVALID_ARGUMENT", status=400)
    decision = _guard(session, "chmod", target)
    started = time.time()
    with session.lock:
        try:
            before = session.sftp.stat(target)
            session.sftp.chmod(target, mode)
        except Exception as exc:
            _audit(session, decision, operation="chmod", path=target, result="failure", message=str(exc), started=started)
            raise _wrap_io(exc, "修改权限") from exc
    old = f"{stat_module.S_IMODE(before.st_mode):04o}"
    # 回读真实权限：某些平台（如 Windows 上的演示目标机）chmod 不会真正生效，
    # 此时如实返回实际值，避免审计记录里出现「声称改了、其实没改」的假成功。
    try:
        with session.lock:
            after = f"{stat_module.S_IMODE(session.sftp.stat(target).st_mode):04o}"
    except Exception:  # noqa: BLE001 - 回读失败时退回请求值，不掩盖成功的 chmod
        after = f"{mode:04o}"
    _audit(
        session,
        decision,
        operation="chmod",
        path=target,
        message=f"修改权限 {target}：{old} → {after}",
        started=started,
    )
    return {"path": target, "before": old, "after": after, "requested": f"{mode:04o}"}


def read_text(session: FileSession, path: str, *, max_bytes: int = MAX_TEXT_BYTES) -> dict:
    target = normalize_path(path)
    decision = _guard(session, "read", target)
    started = time.time()
    with session.lock:
        try:
            attrs = session.sftp.stat(target)
            if stat_module.S_ISDIR(attrs.st_mode):
                raise FileError("这是目录，请展开浏览", code="INVALID_ARGUMENT", status=400)
            size = int(getattr(attrs, "st_size", 0) or 0)
            with session.sftp.open(target, "rb") as handle:
                raw = handle.read(min(size, max_bytes) or max_bytes)
        except FileError:
            raise
        except Exception as exc:
            _audit(session, decision, operation="read", path=target, result="failure", message=str(exc), started=started)
            raise _wrap_io(exc, "读取文件") from exc
    if b"\x00" in raw[:8192]:
        _audit(
            session,
            decision,
            operation="read",
            path=target,
            size=len(raw),
            result="failure",
            message=f"文件是二进制，已拒绝文本预览：{target}",
            started=started,
        )
        raise FileError("该文件是二进制文件，请下载后查看", code="FILE_BINARY", status=415)
    try:
        content = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        try:
            content = raw.decode("latin-1")
            encoding = "latin-1"
        except Exception:
            content = ""
            encoding = "unknown"
    truncated = size > len(raw)
    _audit(
        session,
        decision,
        operation="read",
        path=target,
        size=len(raw),
        message=f"读取文件 {target}（{len(raw)} 字节{'，已截断' if truncated else ''}）",
        started=started,
    )
    return {
        "path": target,
        "content": content,
        "size": size,
        "readBytes": len(raw),
        "truncated": truncated,
        "encoding": encoding,
        "mtime": _iso(getattr(attrs, "st_mtime", None)),
        "modeOctal": f"{stat_module.S_IMODE(attrs.st_mode):04o}",
    }


def write_text(
    session: FileSession,
    path: str,
    content: str,
    *,
    expected_mtime: int | None = None,
) -> dict:
    target = normalize_path(path)
    decision = _guard(session, "write", target)
    payload = (content or "").encode("utf-8")
    if len(payload) > MAX_TEXT_BYTES:
        raise FileError(f"单次保存不能超过 {MAX_TEXT_BYTES // 1024} KiB", code="TOO_LARGE", status=413)
    started = time.time()
    existed = _exists(session, target)
    if existed and expected_mtime:
        with session.lock:
            try:
                current = int(session.sftp.stat(target).st_mtime)
            except Exception:  # pragma: no cover
                current = None
        if current is not None and int(expected_mtime) != current:
            raise FileError(
                "远端文件已被其他人修改，请重新打开后再保存",
                code="FILE_CONFLICT",
                status=409,
            )
    with session.lock:
        try:
            with session.sftp.open(target, "wb") as handle:
                handle.write(payload)
        except Exception as exc:
            _audit(session, decision, operation="write", path=target, result="failure", message=str(exc), started=started)
            raise _wrap_io(exc, "保存文件") from exc
    _audit(
        session,
        decision,
        operation="write",
        path=target,
        size=len(payload),
        message=f"{'覆盖保存' if existed else '新建'}文件 {target}（{len(payload)} 字节）",
        started=started,
    )
    return {"path": target, "size": len(payload), "created": not existed}


def upload_file(
    session: FileSession,
    path: str,
    stream: BinaryIO,
    *,
    overwrite: bool = True,
    declared_size: int | None = None,
) -> dict:
    target = normalize_path(path)
    decision = _guard(session, "upload", target)
    started = time.time()
    existed = _exists(session, target)
    if existed and not overwrite:
        raise FileError("目标已存在，未允许覆盖", code="FILE_EXISTS", status=409)
    with session.lock:
        try:
            session.sftp.putfo(stream, target, confirm=False)
            attrs = session.sftp.stat(target)
        except Exception as exc:
            _audit(session, decision, operation="upload", path=target, result="failure", message=str(exc), started=started)
            raise _wrap_io(exc, "上传文件") from exc
    size = int(getattr(attrs, "st_size", 0) or 0)
    _audit(
        session,
        decision,
        operation="upload",
        path=target,
        size=size,
        message=f"上传文件 {target}（{size} 字节{'，覆盖原文件' if existed else ''}）",
        started=started,
    )
    return {"path": target, "size": size, "overwritten": existed, "declaredSize": declared_size or 0}


def download_file(session: FileSession, path: str) -> dict:
    """打开远端文件供流式下载；审计在流开始前落一条。"""
    target = normalize_path(path)
    decision = _guard(session, "download", target)
    started = time.time()
    with session.lock:
        try:
            attrs = session.sftp.stat(target)
            if stat_module.S_ISDIR(attrs.st_mode):
                raise FileError("目录请使用「打包下载」", code="INVALID_ARGUMENT", status=400)
            handle = session.sftp.open(target, "rb")
        except FileError:
            raise
        except Exception as exc:
            _audit(session, decision, operation="download", path=target, result="failure", message=str(exc), started=started)
            raise _wrap_io(exc, "下载文件") from exc
    size = int(getattr(attrs, "st_size", 0) or 0)
    _audit(
        session,
        decision,
        operation="download",
        path=target,
        size=size,
        message=f"下载文件 {target}（{size} 字节）",
        started=started,
    )
    return {
        "path": target,
        "size": size,
        "mtime": getattr(attrs, "st_mtime", 0),
        "handle": handle,
        "lock": session.lock,
    }


def _iter_remote(handle: Any) -> Iterator[bytes]:
    while True:
        chunk = handle.read(CHUNK)
        if not chunk:
            break
        yield chunk


def stream_download(payload: dict) -> Iterator[bytes]:
    handle = payload["handle"]
    lock = payload["lock"]
    lock.acquire()
    try:
        yield from _iter_remote(handle)
    finally:
        try:
            handle.close()
        except Exception:  # pragma: no cover
            pass
        lock.release()


def _collect_archive_members(session: FileSession, targets: list[str]) -> tuple[list[tuple[str, str, int]], int]:
    members: list[tuple[str, str, int]] = []
    total = 0
    for target in targets:
        with session.lock:
            try:
                attrs = session.sftp.stat(target)
            except Exception as exc:
                # 路径不存在/不允许读时给 404/403，不能把 SFTP 异常抛成 500
                raise _wrap_io(exc, f"读取 {target}") from exc
        if stat_module.S_ISDIR(attrs.st_mode):
            root = posixpath.basename(target.rstrip("/")) or target.strip("/")
            stack = [(target, root)]
            while stack:
                current, arc = stack.pop()
                members.append((current, arc.rstrip("/") + "/", 0))
                with session.lock:
                    try:
                        children = session.sftp.listdir_attr(current)
                    except Exception as exc:
                        raise _wrap_io(exc, f"读取目录 {current}") from exc
                for child in children:
                    child_path = join_path(current, child.filename)
                    child_arc = f"{arc.rstrip('/')}/{child.filename}"
                    if stat_module.S_ISDIR(child.st_mode):
                        stack.append((child_path, child_arc))
                    else:
                        size = int(getattr(child, "st_size", 0) or 0)
                        members.append((child_path, child_arc, size))
                        total += size
        else:
            size = int(getattr(attrs, "st_size", 0) or 0)
            members.append((target, posixpath.basename(target), size))
            total += size
        if len(members) > MAX_ARCHIVE_FILES:
            raise FileError(f"一次最多打包 {MAX_ARCHIVE_FILES} 个条目", code="TOO_MANY_FILES", status=413)
        if total > MAX_ARCHIVE_BYTES:
            raise FileError("打包内容超过上限", code="TOO_LARGE", status=413)
    return members, total


def archive_paths(session: FileSession, paths: list[str]) -> dict:
    """把选中的文件/目录打成一个 zip 临时文件（供流式发送后删除）。"""
    targets = [normalize_path(item) for item in (paths or []) if str(item or "").strip()]
    if not targets:
        raise FileError("请选择要打包的文件", code="INVALID_ARGUMENT", status=400)
    decisions: dict[str, FileDecision] = {}
    for target in targets:
        decisions[target] = _guard(session, "archive", target)
    collect_started = time.time()
    try:
        members, total = _collect_archive_members(session, targets)
    except FileError as exc:
        # 打包失败（路径不存在、目录不可读…）同样是「一次操作」，必须留痕
        _audit(
            session,
            decisions[targets[0]],
            operation="archive",
            path=targets[0],
            result="failure",
            message=exc.message,
            file_count=len(targets),
            started=collect_started,
        )
        raise
    started = time.time()
    handle = tempfile.NamedTemporaryFile(prefix="bastion-archive-", suffix=".zip", delete=False)
    temp_path = handle.name
    handle.close()
    written = 0
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for remote_path, arc_name, _size in members:
                if arc_name.endswith("/"):
                    archive.writestr(zipfile.ZipInfo(arc_name), b"")
                    continue
                with session.lock:
                    remote = session.sftp.open(remote_path, "rb")
                try:
                    with archive.open(zipfile.ZipInfo(arc_name), "w") as target_handle:
                        while True:
                            chunk = remote.read(CHUNK)
                            if not chunk:
                                break
                            target_handle.write(chunk)
                            written += len(chunk)
                            if written > MAX_ARCHIVE_BYTES:
                                raise FileError("打包内容超过上限", code="TOO_LARGE", status=413)
                finally:
                    try:
                        remote.close()
                    except Exception:  # pragma: no cover
                        pass
    except FileError:
        _cleanup_temp(temp_path)
        raise
    except Exception as exc:
        _cleanup_temp(temp_path)
        raise _wrap_io(exc, "打包下载") from exc
    decision = next(iter(decisions.values()), None)
    for target in targets:
        row = decisions[target]
        _audit(
            session,
            row,
            operation="archive",
            path=target,
            size=written,
            file_count=len(members),
            message=f"打包下载 {target}（{len(members)} 个条目，{written} 字节）",
            started=started,
        )
    if decision is None:  # pragma: no cover - 上面 targets 非空
        raise FileError("打包失败", code="FILE_IO_ERROR", status=500)
    name = posixpath.basename(targets[0].rstrip("/")) or "download"
    if len(targets) > 1:
        name = f"files-{len(targets)}"
    return {
        "tempPath": temp_path,
        "filename": f"{name}.zip",
        "entries": len(members),
        "bytes": written,
    }


def _cleanup_temp(path: str) -> None:
    import os

    try:
        os.unlink(path)
    except OSError:  # pragma: no cover
        logger.debug("清理临时打包文件失败 path=%s", path, exc_info=True)
