"""给第三方模块用的 ``autoops.bastion`` SDK 门面。

设计取舍：
* 所有触库/触网的方法都是 ``async def``，协程本身只做上下文快照与线程切换，
  真正的同步代码在 ``ThreadPoolExecutor`` 里跑，并显式 ``with app.app_context()``。
  这样模块作者不会不小心在事件循环线程里做 SQLAlchemy 查询。
* executor 线程里没有 Flask 的 ``g``，所以调用线程先把 actor 快照成普通字典，
  再带进线程；审计写的是用户快照，不是 ORM 对象。
* 凭据明文只出现在返回值里：``Credentials.__repr__`` 掩码，审计 detail 不含明文。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from flask import g

from .. import audit as audit_service
from .. import settings_store, ssh_client
from ..access import find_access
from ..crypto import decrypt
from ..extensions import db, socketio
from ..models import Host, HostAccount, SessionRecord, User
from ..policy import evaluate_policy
from ..session_service import SessionError, open_session, resolve_policy_for
from .context import ModuleApiError, ModuleError

logger = logging.getLogger("bastion.modules.sdk")

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="module-io")
_APP = None
_CURRENT_MODULE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "autoops_module_id", default=""
)
_LOADED_MODULES: set[str] = set()


def bind_app(app) -> None:
    global _APP
    _APP = app


def _require_app():
    if _APP is None:
        raise ModuleError("模块 SDK 尚未绑定 Flask 应用")
    return _APP


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
def _call_context() -> dict:
    """在调用线程里快照 actor 与模块 id；executor 线程拿不到 g。"""
    context: dict[str, Any] = {"module_id": _CURRENT_MODULE.get()}
    try:
        user = getattr(g, "actor", None)
    except RuntimeError:  # 不在 Flask 应用/请求上下文里
        user = None
    if user is not None:
        try:
            context.update(
                actor_id=int(user.id),
                actor_username=str(user.username or ""),
                actor_role=str(user.role_code or ""),
            )
        except Exception:  # pragma: no cover - detached ORM 实例
            pass
    return context


@contextmanager
def module_scope(module_id: str):
    token = _CURRENT_MODULE.set(str(module_id))
    try:
        yield
    finally:
        _CURRENT_MODULE.reset(token)


def register_loaded_module(module_id: str) -> None:
    _LOADED_MODULES.add(str(module_id))


def unregister_loaded_module(module_id: str) -> None:
    _LOADED_MODULES.discard(str(module_id))


def reset_loaded_modules() -> None:
    _LOADED_MODULES.clear()


def _module_id(ctx: dict) -> str:
    module_id = str(ctx.get("module_id") or "")
    if module_id:
        return module_id
    if len(_LOADED_MODULES) == 1:
        return next(iter(_LOADED_MODULES))
    raise ModuleError(
        "无法确定当前模块上下文；请从模块路由/钩子中调用，或使用显式模块设置 key"
    )


def _ctx_user(ctx: dict) -> User | None:
    actor_id = ctx.get("actor_id")
    if not actor_id:
        return None
    return db.session.get(User, int(actor_id))


def _require_user(ctx: dict) -> User:
    user = _ctx_user(ctx)
    if user is None or not user.is_active():
        raise ModuleError("该操作需要已登录且启用的用户上下文")
    return user


def _full_setting_key(ctx: dict, key: str) -> str:
    text = str(key or "").strip()
    if not text:
        raise ModuleError("设置项 key 不能为空")
    module_id = _module_id(ctx)
    if text.startswith("mod."):
        # 完整 key 也仅限当前模块，避免模块 A 读到模块 B 的设置。
        if not text.startswith(f"mod.{module_id}."):
            raise ModuleError("不能访问其他模块的设置项")
        return text
    return settings_store.module_setting_key(module_id, text)


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------
async def _run(func: Callable[[dict], Any]) -> Any:
    loop = asyncio.get_running_loop()
    app = _require_app()
    context = _call_context()

    def target():
        with app.app_context():
            return func(context)

    return await loop.run_in_executor(_EXECUTOR, target)


def _run_sync(func: Callable[[dict], Any]) -> Any:
    app = _require_app()
    context = _call_context()
    with app.app_context():
        return func(context)


def _jsonable(value: Any, depth: int = 0) -> Any:
    """审计 detail 必须是 JSON 能落库的形状；截断文本避免把大对象写进 DB。"""
    if depth > 6:
        return "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict):
        return {str(key)[:80]: _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth + 1) for item in list(value)[:200]]
    return str(value)[:4000]


def _audit(
    ctx: dict,
    action: str,
    *,
    target_type: str = "",
    target_id: Any = "",
    message: str = "",
    detail: dict | None = None,
    result: str = "success",
) -> None:
    if not action:
        raise ModuleError("审计 action 不能为空")
    audit_service.log_event(
        "module",
        str(action)[:64],
        result=result,
        message=str(message or "")[:500],
        target_type=str(target_type or "")[:32],
        target_id=str(target_id if target_id is not None else ""),
        detail=_jsonable(detail) if detail is not None else None,
        actor_id=ctx.get("actor_id"),
        actor_username=str(ctx.get("actor_username") or ""),
        actor_role=str(ctx.get("actor_role") or ""),
    )


def _audit_credential(ctx: dict, account: HostAccount, field_name: str) -> None:
    """凭据访问审计：只记录元数据，不记录任何明文/密文。"""
    _audit(
        ctx,
        "module_credential",
        target_type="host_account",
        target_id=account.id,
        message=f"模块读取资产账号凭据（{field_name}）",
        detail={
            "moduleId": ctx.get("module_id") or "",
            "hostId": account.host_id,
            "accountId": account.id,
            "username": account.username,
            "field": field_name,
            "hasPassword": bool(account.secret_enc),
            "hasPrivateKey": bool(account.private_key_enc),
        },
    )


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------
@dataclass
class Credentials:
    host_id: int | None
    account_id: int | None
    host_name: str = ""
    address: str = ""
    protocol: str = ""
    port: int = 0
    username: str = ""
    password: str = field(default="", repr=False)
    private_key: str = field(default="", repr=False)
    passphrase: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return (
            "Credentials("
            f"host_id={self.host_id!r}, account_id={self.account_id!r}, "
            f"host_name={self.host_name!r}, address={self.address!r}, "
            f"protocol={self.protocol!r}, port={self.port!r}, "
            f"username={self.username!r}, password=<redacted>, "
            "private_key=<redacted>, passphrase=<redacted>)"
        )

    __str__ = __repr__


def _host_protocol(host: Host) -> tuple[str, int]:
    protocol = str(host.protocol or "ssh")
    endpoint = host.endpoint_for(protocol)
    port = int(endpoint.port if endpoint is not None else host.port or 0)
    return protocol, port


def _host_dict(host: Host) -> dict:
    data = host.to_dict()
    # SDK 只承诺契约里的基础字段；to_dict 已有的字段保持原样。
    data.setdefault("protocols", [])
    return data


def _account_dict(account: HostAccount) -> dict:
    return account.to_dict()


# ---------------------------------------------------------------------------
# 同步实现（在 app context 线程里执行）
# ---------------------------------------------------------------------------
def _credentials_get_sync(
    ctx: dict, host_id: int, account_id: int | None = None
) -> Credentials:
    host = db.session.get(Host, int(host_id))
    if host is None:
        raise ModuleError(f"资产不存在：{host_id}")
    query = HostAccount.query.filter_by(host_id=host.id)
    if account_id is not None:
        query = query.filter_by(id=int(account_id))
    account = query.order_by(HostAccount.id).first()
    if account is None:
        raise ModuleError(f"资产 {host.id} 下不存在指定账号")
    password = decrypt(account.secret_enc) if account.secret_enc else ""
    private_key = decrypt(account.private_key_enc) if account.private_key_enc else ""
    passphrase = decrypt(account.passphrase_enc) if account.passphrase_enc else ""
    if account.auth_type == "password" and account.secret_enc and not password:
        raise ModuleError("凭据解密失败，请管理员重新保存账号口令")
    protocol, port = _host_protocol(host)
    _audit_credential(ctx, account, "password" if password else "private_key")
    return Credentials(
        host_id=host.id,
        account_id=account.id,
        host_name=host.name,
        address=host.address,
        protocol=protocol,
        port=port,
        username=account.username,
        password=password,
        private_key=private_key,
        passphrase=passphrase,
    )


def _credentials_list_sync(ctx: dict, host_id: int | None = None) -> list[dict]:
    query = HostAccount.query
    if host_id is not None:
        query = query.filter_by(host_id=int(host_id))
    result = []
    for account in query.order_by(HostAccount.id).all():
        item = _account_dict(account)
        host = db.session.get(Host, account.host_id) if account.host_id else None
        if host is not None:
            protocol, port = _host_protocol(host)
            item.update(
                hostName=host.name,
                hostAddress=host.address,
                hostProtocol=protocol,
                hostPort=port,
            )
        result.append(item)
    return result


def _hosts_list_sync(ctx: dict, protocols=None, keyword: str | None = None) -> list[dict]:
    wanted = set()
    if protocols:
        if isinstance(protocols, str):
            protocols = [protocols]
        wanted = {str(item).strip().lower() for item in protocols if str(item).strip()}
    keyword_text = str(keyword or "").strip().lower()
    result = []
    for host in Host.query.order_by(Host.id).all():
        if wanted and not wanted.intersection(host.protocol_names()):
            continue
        if keyword_text:
            haystack = " ".join(
                [
                    str(host.name or ""),
                    str(host.address or ""),
                    str(host.description or ""),
                    " ".join(str(tag) for tag in (host.tags or [])),
                ]
            ).lower()
            if keyword_text not in haystack:
                continue
        result.append(_host_dict(host))
    return result


def _hosts_get_sync(ctx: dict, host_id: int) -> dict | None:
    host = db.session.get(Host, int(host_id))
    return _host_dict(host) if host is not None else None


def _accounts_list_sync(ctx: dict, host_id: int | None = None) -> list[dict]:
    query = HostAccount.query
    if host_id is not None:
        query = query.filter_by(host_id=int(host_id))
    return [_account_dict(item) for item in query.order_by(HostAccount.id).all()]


def _account_by_id(account_id: int) -> HostAccount:
    account = db.session.get(HostAccount, int(account_id))
    if account is None:
        raise ModuleError(f"资产账号不存在：{account_id}")
    return account


def _accounts_reveal_password_sync(ctx: dict, account_id: int) -> str:
    account = _account_by_id(account_id)
    value = decrypt(account.secret_enc) if account.secret_enc else ""
    if account.secret_enc and not value:
        raise ModuleError("口令解密失败，请管理员重新保存账号口令")
    _audit_credential(ctx, account, "password")
    return value


def _accounts_reveal_private_key_sync(ctx: dict, account_id: int) -> str:
    account = _account_by_id(account_id)
    value = decrypt(account.private_key_enc) if account.private_key_enc else ""
    if account.private_key_enc and not value:
        raise ModuleError("私钥解密失败，请管理员重新保存账号私钥")
    _audit_credential(ctx, account, "private_key")
    return value


def _sessions_list_active_sync(ctx: dict) -> list[dict]:
    records = (
        SessionRecord.query.filter_by(status="active")
        .order_by(SessionRecord.started_at.desc(), SessionRecord.id.desc())
        .all()
    )
    return [record.to_dict() for record in records]


def _sessions_open_sync(
    ctx: dict,
    host_id: int,
    account_id: int | None,
    protocol: str,
    source: str,
) -> dict:
    user = _require_user(ctx)
    module_id = _module_id(ctx)
    source = str(source or f"module:{module_id}")
    if not source.startswith("module:"):
        source = f"module:{module_id}"
    try:
        opened = open_session(
            _require_app(),
            user_id=user.id,
            host_id=int(host_id),
            account_id=int(account_id) if account_id is not None else None,
            protocol=str(protocol or "ssh"),
            source=source,
        )
    except SessionError as exc:
        raise ModuleError(str(exc)) from exc
    return {
        "sid": opened.sid,
        "recordId": opened.record_id,
        "hostName": opened.host_name,
        "accountUsername": opened.account_username,
        "policyName": opened.policy_name,
        "protocol": opened.meta.get("protocol", protocol),
        "source": source,
        "segmented": bool(opened.segmented),
    }


def _audit_log_sync(
    ctx: dict,
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    message: str = "",
    detail: dict | None = None,
) -> None:
    _audit(
        ctx,
        action,
        target_type=target_type or "",
        target_id=target_id if target_id is not None else "",
        message=message,
        detail=detail,
    )


def _find_session(session_id: Any) -> SessionRecord | None:
    text = str(session_id if session_id is not None else "").strip()
    if not text:
        return None
    record = SessionRecord.query.filter_by(sid=text).first()
    if record is not None:
        return record
    try:
        return db.session.get(SessionRecord, int(text))
    except (TypeError, ValueError):
        return None


def _audit_log_command_sync(
    ctx: dict,
    session_id: Any,
    command: str,
    output: str | None = None,
    allowed: bool = True,
    reason: str = "",
) -> None:
    session = _find_session(session_id)
    audit_service.log_command(
        session=session,
        command=str(command or ""),
        output=str(output or ""),
        action="allow" if allowed else "deny",
        risk_level="low" if allowed else "medium",
        reason=str(reason or ""),
        commit=True,
    )
    _audit(
        ctx,
        "module_command",
        target_type="session",
        target_id=session.sid if session is not None else str(session_id or ""),
        message=f"模块记录命令（{'放行' if allowed else '拒绝'}）",
        detail={"allowed": bool(allowed), "moduleId": ctx.get("module_id") or ""},
    )


def _settings_get_sync(ctx: dict, key: str) -> str:
    full = _full_setting_key(ctx, key)
    return str(settings_store.get_setting(full) or "")


def _settings_set_sync(ctx: dict, key: str, value: Any) -> None:
    full = _full_setting_key(ctx, key)
    if settings_store.default_meta(full) is None:
        raise ModuleError(f"模块没有声明设置项：{key}")
    settings_store.set_setting(full, value)
    db.session.commit()


def _users_list_sync(ctx: dict) -> list[dict]:
    return [user.to_dict() for user in User.query.order_by(User.id).all()]


def _policy_check_sync(ctx: dict, account_id: int, command: str) -> dict:
    account = _account_by_id(account_id)
    user = _ctx_user(ctx)
    if user is None or not user.is_active():
        return {"allowed": False, "matched_rule": None, "action": "deny"}
    resolved = find_access(
        user,
        host_id=account.host_id,
        account_id=account.id,
        require_login=True,
    )
    if resolved is None:
        return {"allowed": False, "matched_rule": None, "action": "deny"}
    policy = resolve_policy_for(resolved.grant)
    decision = evaluate_policy(policy, str(command or ""))
    return {
        "allowed": bool(decision.allowed),
        "matched_rule": decision.rule_pattern or None,
        "action": decision.action,
    }


def _ssh_run_sync(
    ctx: dict,
    host_id: int,
    command: str,
    account_id: int | None,
    timeout: int | float,
) -> dict:
    user = _require_user(ctx)
    resolved = find_access(
        user,
        host_id=int(host_id),
        account_id=int(account_id) if account_id is not None else None,
        require_login=True,
    )
    if resolved is None or resolved.host is None or resolved.account is None:
        raise ModuleError("你没有访问该主机/账号的权限")
    policy = resolve_policy_for(resolved.grant)
    decision = evaluate_policy(policy, str(command or ""))
    if not decision.allowed:
        raise ModuleError(decision.reason or "命令被策略拒绝")
    target = ssh_client.build_target(
        resolved.host,
        resolved.account,
        connect_timeout=min(float(timeout or 30), 60),
    )
    started = time.perf_counter()
    connection = None
    try:
        connection = ssh_client.connect(target)
        code, stdout, stderr = ssh_client.run_single_command(
            connection, str(command or ""), timeout=float(timeout or 30)
        )
    except ssh_client.SSHError as exc:
        raise ModuleError(str(exc)) from exc
    finally:
        if connection is not None:
            try:
                ssh_client.close(connection)
            except Exception:
                pass
    elapsed = round(time.perf_counter() - started, 3)
    _audit(
        ctx,
        "module_ssh_run",
        target_type="host",
        target_id=resolved.host.id,
        message="模块执行远程命令",
        detail={
            "moduleId": ctx.get("module_id") or "",
            "hostId": resolved.host.id,
            "accountId": resolved.account.id,
            "exitCode": code,
            "elapsed": elapsed,
        },
    )
    return {"code": code, "stdout": stdout, "stderr": stderr, "elapsed": elapsed}


def _events_emit_sync(ctx: dict, topic: str, payload: Any) -> None:
    module_id = _module_id(ctx)
    name = str(topic or "").strip()
    if not name or ":" in name or len(name) > 64:
        raise ModuleError("事件 topic 不能为空、不能含冒号且最长 64 字符")
    socketio.emit(f"module:{module_id}:{name}", _jsonable(payload))


# ---------------------------------------------------------------------------
# 公开门面
# ---------------------------------------------------------------------------
class _CredentialsAPI:
    async def get(self, host_id: int, account_id: int | None = None) -> Credentials:
        return await _run(lambda ctx: _credentials_get_sync(ctx, host_id, account_id))

    def get_sync(self, host_id: int, account_id: int | None = None) -> Credentials:
        return _run_sync(lambda ctx: _credentials_get_sync(ctx, host_id, account_id))

    async def list(self, host_id: int | None = None) -> list[dict]:
        return await _run(lambda ctx: _credentials_list_sync(ctx, host_id))

    def list_sync(self, host_id: int | None = None) -> list[dict]:
        return _run_sync(lambda ctx: _credentials_list_sync(ctx, host_id))


class _HostsAPI:
    async def list(self, protocols=None, keyword: str | None = None) -> list[dict]:
        return await _run(lambda ctx: _hosts_list_sync(ctx, protocols, keyword))

    def list_sync(self, protocols=None, keyword: str | None = None) -> list[dict]:
        return _run_sync(lambda ctx: _hosts_list_sync(ctx, protocols, keyword))

    async def get(self, host_id: int) -> dict | None:
        return await _run(lambda ctx: _hosts_get_sync(ctx, host_id))

    def get_sync(self, host_id: int) -> dict | None:
        return _run_sync(lambda ctx: _hosts_get_sync(ctx, host_id))


class _AccountsAPI:
    async def list(self, host_id: int | None = None) -> list[dict]:
        return await _run(lambda ctx: _accounts_list_sync(ctx, host_id))

    def list_sync(self, host_id: int | None = None) -> list[dict]:
        return _run_sync(lambda ctx: _accounts_list_sync(ctx, host_id))

    async def reveal_password(self, account_id: int) -> str:
        return await _run(lambda ctx: _accounts_reveal_password_sync(ctx, account_id))

    def reveal_password_sync(self, account_id: int) -> str:
        return _run_sync(lambda ctx: _accounts_reveal_password_sync(ctx, account_id))

    async def reveal_private_key(self, account_id: int) -> str:
        return await _run(lambda ctx: _accounts_reveal_private_key_sync(ctx, account_id))

    def reveal_private_key_sync(self, account_id: int) -> str:
        return _run_sync(lambda ctx: _accounts_reveal_private_key_sync(ctx, account_id))


class _SessionsAPI:
    async def list_active(self) -> list[dict]:
        return await _run(_sessions_list_active_sync)

    def list_active_sync(self) -> list[dict]:
        return _run_sync(_sessions_list_active_sync)

    async def open(
        self,
        host_id: int,
        account_id: int | None,
        protocol: str = "ssh",
        source: str = "",
    ) -> dict:
        return await _run(
            lambda ctx: _sessions_open_sync(
                ctx, host_id, account_id, protocol, source
            )
        )

    def open_sync(
        self,
        host_id: int,
        account_id: int | None,
        protocol: str = "ssh",
        source: str = "",
    ) -> dict:
        return _run_sync(
            lambda ctx: _sessions_open_sync(
                ctx, host_id, account_id, protocol, source
            )
        )


class _AuditAPI:
    async def log(
        self,
        action: str,
        target_type: str | None = None,
        target_id: Any = None,
        message: str = "",
        detail: dict | None = None,
    ) -> None:
        return await _run(
            lambda ctx: _audit_log_sync(
                ctx, action, target_type, target_id, message, detail
            )
        )

    def log_sync(self, *args, **kwargs) -> None:
        return _run_sync(lambda ctx: _audit_log_sync(ctx, *args, **kwargs))

    async def log_command(
        self,
        session_id: Any,
        command: str,
        output: str | None = None,
        allowed: bool = True,
        reason: str = "",
    ) -> None:
        return await _run(
            lambda ctx: _audit_log_command_sync(
                ctx, session_id, command, output, allowed, reason
            )
        )

    def log_command_sync(self, *args, **kwargs) -> None:
        return _run_sync(lambda ctx: _audit_log_command_sync(ctx, *args, **kwargs))


class _SettingsAPI:
    async def get(self, key: str) -> str:
        return await _run(lambda ctx: _settings_get_sync(ctx, key))

    def get_sync(self, key: str) -> str:
        return _run_sync(lambda ctx: _settings_get_sync(ctx, key))

    async def set(self, key: str, value: Any) -> None:
        return await _run(lambda ctx: _settings_set_sync(ctx, key, value))

    def set_sync(self, key: str, value: Any) -> None:
        return _run_sync(lambda ctx: _settings_set_sync(ctx, key, value))


class _UsersAPI:
    async def list(self) -> list[dict]:
        return await _run(_users_list_sync)

    def list_sync(self) -> list[dict]:
        return _run_sync(_users_list_sync)


class _PolicyAPI:
    async def check(self, account_id: int, command: str) -> dict:
        return await _run(lambda ctx: _policy_check_sync(ctx, account_id, command))

    def check_sync(self, account_id: int, command: str) -> dict:
        return _run_sync(lambda ctx: _policy_check_sync(ctx, account_id, command))


class _SSHAPI:
    async def run(
        self,
        host_id: int,
        command: str,
        account_id: int | None = None,
        timeout: int | float = 30,
    ) -> dict:
        return await _run(
            lambda ctx: _ssh_run_sync(ctx, host_id, command, account_id, timeout)
        )

    def run_sync(
        self,
        host_id: int,
        command: str,
        account_id: int | None = None,
        timeout: int | float = 30,
    ) -> dict:
        return _run_sync(
            lambda ctx: _ssh_run_sync(ctx, host_id, command, account_id, timeout)
        )


class _EventsAPI:
    async def emit(self, topic: str, payload: Any = None) -> None:
        return await _run(lambda ctx: _events_emit_sync(ctx, topic, payload))

    def emit_sync(self, topic: str, payload: Any = None) -> None:
        return _run_sync(lambda ctx: _events_emit_sync(ctx, topic, payload))


class _UtilAPI:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    async def run_blocking(fn: Callable, *args, **kwargs) -> Any:
        app = _require_app()
        loop = asyncio.get_running_loop()

        def target():
            with app.app_context():
                return fn(*args, **kwargs)

        return await loop.run_in_executor(_EXECUTOR, target)


credentials = _CredentialsAPI()
hosts = _HostsAPI()
accounts = _AccountsAPI()
sessions = _SessionsAPI()
audit = _AuditAPI()
settings = _SettingsAPI()
users = _UsersAPI()
policy = _PolicyAPI()
ssh = _SSHAPI()
events = _EventsAPI()
util = _UtilAPI()

__all__ = [
    "Credentials",
    "ModuleApiError",
    "ModuleError",
    "accounts",
    "audit",
    "bind_app",
    "credentials",
    "events",
    "hosts",
    "policy",
    "sessions",
    "settings",
    "ssh",
    "users",
    "util",
]
