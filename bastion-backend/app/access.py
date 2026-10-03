"""授权解析：谁能访问哪台机器、用哪个账号、什么时间段、几个并发。

**超级管理员例外**：`is_superuser` 的账号在授权表里没有匹配记录时，仍可访问全部
启用状态的主机（否则新部署的堡垒机里管理员自己打不开网页终端 / 网关菜单，
演示与首次排障都会被挡住）。例外只放宽「能不能进」，不放宽审计与策略：
会话记录、每一条命令与输出照样落库，命令策略仍然按系统默认策略强制执行。
``_superuser_access`` 返回的是一条**未入库的临时 Grant**（不加进 session，
不会污染 grants 表），会话记录里的 `grant_id` 为 NULL。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .extensions import db
from .models import CommandPolicy, FilePolicy, Grant, Host, HostAccount, SessionRecord, User


@dataclass
class ResolvedAccess:
    grant: Grant
    host: Host
    account: HostAccount


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def in_time_window(grant: Grant, now: datetime | None = None) -> tuple[bool, str]:
    """校验授权的时间窗 / 星期 / 到期时间。默认按服务器本地时间判断。"""
    if not grant.enabled:
        return False, "授权已被停用"

    if grant.expire_at and _aware(grant.expire_at) <= datetime.now(timezone.utc):
        return False, "授权已过期"

    local_now = now or datetime.now()
    weekdays = list(grant.weekdays or [])
    if weekdays and local_now.weekday() not in weekdays:
        return False, "当前星期不在授权时段内"

    start, end = grant.time_start, grant.time_end
    if start or end:
        current = local_now.time()
        if start and end:
            if start <= end:
                ok = start <= current <= end
            else:  # 跨零点，例如 22:00 - 06:00
                ok = current >= start or current <= end
        elif start:
            ok = current >= start
        else:
            ok = current <= end
        if not ok:
            window = f"{start.strftime('%H:%M') if start else '00:00'}-{end.strftime('%H:%M') if end else '24:00'}"
            return False, f"当前时间不在授权时段 {window} 内"
    return True, ""


def active_grants(user: User) -> list[Grant]:
    """该用户当前生效（未停用/未过期/时段内/主机可用）的授权。"""
    grants = (
        db.session.query(Grant)
        .filter(Grant.user_id == user.id, Grant.enabled.is_(True))
        .all()
    )
    result = []
    for grant in grants:
        host = grant.host
        if host is None or host.status != "active":
            continue
        ok, _reason = in_time_window(grant)
        if ok:
            result.append(grant)
    return result


def _default_policy_name() -> str:
    policy = CommandPolicy.query.filter_by(is_default=True).first()
    return policy.name if policy is not None else "未绑定策略"


def _default_file_policy_name() -> str:
    policy = FilePolicy.query.filter_by(is_default=True).first()
    return policy.name if policy is not None else "默认文件策略"


def _superuser_access(
    user: User,
    *,
    host_id: int | None = None,
    host_name: str | None = None,
    account_id: int | None = None,
) -> ResolvedAccess | None:
    """超级管理员的兜底准入：不查授权表，直接给一台启用主机 + 一个可用账号。

    返回的 Grant 是**临时对象**：只填业务字段、不设置任何 relationship
    （否则 `host.grants` 的反向引用会把这条临时授权级联进 session 并真的写库），
    因此 `grant.id is None`、会话记录里 `grant_id` 为 NULL。

    账号选择规则与普通用户一致：显式指定 `account_id` 时必须是该主机的账号，
    否则取该主机下 id 最小的账号；主机不存在 / 没有账号 → 返回 None。
    """
    query = Host.query.filter(Host.status == "active")
    if host_id is not None:
        query = query.filter(Host.id == host_id)
    if host_name is not None:
        query = query.filter(Host.name == host_name)
    host = query.order_by(Host.id).first()
    if host is None:
        return None

    if account_id is not None:
        account = db.session.get(HostAccount, account_id)
        if account is None or account.host_id != host.id:
            return None
    else:
        account = (
            HostAccount.query.filter_by(host_id=host.id).order_by(HostAccount.id).first()
        )
        if account is None:
            return None

    grant = Grant(
        user_id=user.id,
        host_id=host.id,
        can_login=True,
        can_sftp=True,
        can_upload=True,
        can_download=True,
        can_port_forward=True,
        can_webterm=True,
        can_file_write=True,
        enabled=True,
        weekdays=[],
        max_sessions=0,
    )
    return ResolvedAccess(grant=grant, host=host, account=account)


def find_access(
    user: User,
    *,
    host_id: int | None = None,
    host_name: str | None = None,
    account_id: int | None = None,
    require_login: bool = True,
) -> ResolvedAccess | None:
    """找到最贴合的一条授权。

    账号维度的优先级：

    * 显式指定 ``account_id`` 时：精确绑定该账号的授权 > 覆盖整机的通配授权；
      两者都没有 → 返回 ``None``（**绝不静默换用别的账号**，否则会变成越权）。
    * 未指定 ``account_id`` 时：优先精确账号授权，否则用通配授权 + 该主机的第一个账号。

    超级管理员（``user.is_superuser``）在授权表里找不到匹配时，回落到
    :func:`_superuser_access`（详见模块 docstring）。
    """
    candidates: list[ResolvedAccess] = []
    for grant in active_grants(user):
        host = grant.host
        if host_id is not None and host.id != host_id:
            continue
        if host_name is not None and host.name != host_name:
            continue
        if require_login and not grant.can_login:
            continue
        candidates.append(ResolvedAccess(grant=grant, host=host, account=grant.account))
    if not candidates:
        if user.is_superuser:
            return _superuser_access(
                user, host_id=host_id, host_name=host_name, account_id=account_id
            )
        return None

    if account_id is not None:
        for candidate in candidates:
            if candidate.grant.host_account_id == account_id:
                account = db.session.get(HostAccount, account_id)
                if account is not None:
                    return ResolvedAccess(
                        grant=candidate.grant, host=candidate.host, account=account
                    )
        # 通配授权（host_account_id 为空）代表管理员把「整机」授权给了该用户
        for candidate in candidates:
            if candidate.grant.host_account_id is None:
                account = db.session.get(HostAccount, account_id)
                if account is not None and account.host_id == candidate.host.id:
                    return ResolvedAccess(
                        grant=candidate.grant, host=candidate.host, account=account
                    )
        if user.is_superuser:
            return _superuser_access(
                user, host_id=host_id, host_name=host_name, account_id=account_id
            )
        return None

    exact_accounts = [c for c in candidates if c.grant.host_account_id is not None]
    if exact_accounts:
        return exact_accounts[0]

    wildcard = candidates[0]
    if wildcard.account is None:
        accounts = grant_accounts(user, wildcard.grant)
        if not accounts:
            return wildcard
        return ResolvedAccess(grant=wildcard.grant, host=wildcard.host, account=accounts[0])
    return wildcard


def grant_accounts(user: User, grant: Grant) -> list[HostAccount]:
    """某条授权可用的资产账号。

    - 授权绑定了具体账号 -> 只有那一个；
    - 授权未绑定账号 -> 该主机下全部账号（管理员在授权里给「整机」权限）。
    """
    if grant.host_account_id:
        account = db.session.get(HostAccount, grant.host_account_id)
        return [account] if account else []
    return list(grant.host.accounts)


def accessible_targets(user: User) -> list[dict]:
    """网关菜单 / Web 终端选单：用户当前可访问的主机与账号。

    注意：返回的是**内部原始条目**（含 `host` ORM 对象，`address` 已拼上端口，
    没有 `port`/`osType`）。任何对外输出（菜单渲染、API、Socket.IO）都必须再走一遍
    `serialize_target()`，否则会 KeyError。

    超级管理员额外获得「全部启用主机」（授权表里已有的主机保留其账号范围，
    没有授权记录的主机按全部账号列出），与 :func:`_superuser_access` 的口径一致。
    """
    merged: dict[int, dict] = {}
    for grant in active_grants(user):
        host = grant.host
        entry = merged.setdefault(
            host.id,
            {
                "host": host,
                "hostId": host.id,
                "hostName": host.name,
                "address": f"{host.address}:{host.port}",
                "groupName": host.group.name if host.group else "",
                "description": host.description,
                "canSftp": False,
                "canWebterm": False,
                "canUpload": False,
                "canDownload": False,
                "canFileWrite": False,
                "accounts": [],
                "grantIds": [],
                "policyName": grant.policy.name if grant.policy else "未绑定策略",
                "filePolicyName": grant.file_policy.name if grant.file_policy else "默认文件策略",
                "maxSessions": grant.max_sessions or 0,
            },
        )
        entry["grantIds"].append(grant.id)
        entry["canSftp"] = entry["canSftp"] or grant.can_sftp
        entry["canUpload"] = entry["canUpload"] or grant.can_upload
        entry["canDownload"] = entry["canDownload"] or grant.can_download
        entry["canFileWrite"] = entry["canFileWrite"] or bool(getattr(grant, "can_file_write", False))
        entry["canWebterm"] = entry["canWebterm"] or grant.can_webterm
        entry["maxSessions"] = max(entry["maxSessions"], grant.max_sessions or 0)
        for account in grant_accounts(user, grant):
            if all(a["id"] != account.id for a in entry["accounts"]):
                entry["accounts"].append(
                    {
                        "id": account.id,
                        "name": account.name,
                        "username": account.username,
                        "authType": account.auth_type,
                    }
                )

    if user.is_superuser:
        policy_name = _default_policy_name()
        file_policy_name = _default_file_policy_name()
        for host in Host.query.filter_by(status="active").order_by(Host.name).all():
            if host.id in merged:
                continue
            merged[host.id] = {
                "host": host,
                "hostId": host.id,
                "hostName": host.name,
                "address": f"{host.address}:{host.port}",
                "groupName": host.group.name if host.group else "",
                "description": host.description,
                "canSftp": True,
                "canWebterm": True,
                "canUpload": True,
                "canDownload": True,
                "canFileWrite": True,
                "accounts": [
                    {
                        "id": account.id,
                        "name": account.name,
                        "username": account.username,
                        "authType": account.auth_type,
                    }
                    for account in sorted(host.accounts, key=lambda a: a.id)
                ],
                "grantIds": [],
                "policyName": policy_name,
                "filePolicyName": file_policy_name,
                "maxSessions": 0,
            }

    return sorted(merged.values(), key=lambda e: e["hostName"])


def current_session_count(user_id: int, grant_id: int | None = None) -> int:
    query = db.session.query(SessionRecord).filter(
        SessionRecord.user_id == user_id, SessionRecord.status == "active"
    )
    if grant_id is not None:
        query = query.filter(SessionRecord.grant_id == grant_id)
    return query.count()


def check_session_quota(user: User, grant: Grant) -> tuple[bool, str]:
    limit = grant.max_sessions or 0
    if limit <= 0:
        return True, ""
    used = current_session_count(user.id, grant.id)
    if used >= limit:
        return False, f"该授权并发会话已达上限（{used}/{limit}）"
    return True, ""


def serialize_target(entry: dict) -> dict:
    host: Host = entry["host"]
    return {
        "hostId": host.id,
        "hostName": host.name,
        "address": host.address,
        "port": host.port,
        "groupName": entry["groupName"],
        "description": entry["description"],
        "osType": host.os_type,
        "canSftp": entry["canSftp"],
        "canUpload": entry["canUpload"],
        "canDownload": entry["canDownload"],
        "canFileWrite": entry.get("canFileWrite", False),
        "canWebterm": entry["canWebterm"],
        "policyName": entry["policyName"],
        "filePolicyName": entry.get("filePolicyName", ""),
        "maxSessions": entry["maxSessions"],
        "accounts": entry["accounts"],
    }
