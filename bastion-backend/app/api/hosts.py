"""主机资产管理接口。"""

from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy import or_

from ..audit import log_event
from ..crypto import decrypt, encrypt
from ..extensions import db
from ..models import Grant, Host, HostAccount, HostGroup, HostProtocol, SessionRecord
from ..rdp.proxy import RDP_SECURITY_CHOICES, RDP_SECURITY_DEFAULT
from ..winrm.client import WINRM_TRANSPORTS, WINRM_TRANSPORT_DEFAULT
from ..security import admin_required, permission_required
from ..ssh_client import SSHError, build_target, connect, run_single_command
from ..utils import (
    api_error,
    api_list,
    api_ok,
    page_args,
    parse_bool,
    parse_int,
)

bp = Blueprint("hosts", __name__, url_prefix="/api/hosts")

ALLOWED_PROTOCOLS = {"ssh", "rdp", "winrm"}

#: 新建主机时不填端口时的默认值（ssh 22 / rdp 3389 / winrm 5985）
DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "winrm": 5985}
ALLOWED_AUTH_TYPES = {"password", "key"}


def _host_payload(host: Host, with_accounts: bool = True) -> dict:
    data = host.to_dict(with_accounts=with_accounts)
    if with_accounts:
        for account in data.get("accounts", []):
            account["sessionCount"] = SessionRecord.query.filter_by(
                account_id=account["id"]
            ).count()
    data["grantCount"] = Grant.query.filter_by(host_id=host.id).count()
    data["groupName"] = host.group.name if host.group else ""
    return data


@bp.get("")
@permission_required("host:view", "grant:view")
def list_hosts():
    page, size = page_args()
    query = Host.query
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(Host.name.like(like), Host.address.like(like), Host.description.like(like))
        )
    group_id = parse_int(request.args.get("groupId"))
    if group_id:
        query = query.filter(Host.group_id == group_id)
    status = (request.args.get("status") or "").strip()
    if status:
        query = query.filter(Host.status == status)
    total = query.count()
    rows = (
        query.order_by(Host.id.desc()).offset((page - 1) * size).limit(size).all()
    )
    return api_list(
        [_host_payload(row, with_accounts=False) for row in rows],
        total,
        page,
        size,
    )


@bp.get("/options")
@permission_required("grant:view", "host:view", "policy:view")
def host_options():
    rows = Host.query.order_by(Host.name.asc()).all()
    return api_ok(
        [
            {
                "label": f"{row.name}（{row.address}:{row.port}）",
                "value": row.id,
                "name": row.name,
                "address": row.address,
                "port": row.port,
                "groupId": row.group_id,
                "status": row.status,
            }
            for row in rows
        ]
    )


@bp.get("/<int:host_id>")
@permission_required("host:view", "grant:view")
def get_host(host_id: int):
    host = db.session.get(Host, host_id)
    if host is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    data = _host_payload(host)
    grants = Grant.query.filter_by(host_id=host_id).all()
    data["grants"] = [grant.to_dict() for grant in grants]
    return api_ok(data)


def _same_credential(left: HostAccount, right: HostAccount) -> bool:
    """两份账号是不是同一把登录凭据（口令 / 私钥 / 私钥口令都要一致）。

    `secret_enc` 一类字段是 Fernet 密文，每次加密都带随机 IV —— 直接比密文永远不相等，
    只能解密后再比。密文解不开（换过 `fernet.key`）时按「不是同一把」处理：宁可多留一份
    账号，也不能把来源主机的凭据悄悄丢掉。
    """
    for field in ("secret_enc", "private_key_enc", "passphrase_enc"):
        left_value = getattr(left, field, "") or ""
        right_value = getattr(right, field, "") or ""
        if not left_value and not right_value:
            continue
        try:
            if decrypt(left_value) != decrypt(right_value):
                return False
        except Exception:  # noqa: BLE001 - 密文不可解一律当成两份不同凭据
            return False
    return True


def _parse_endpoints(payload: dict) -> tuple[list[dict] | None, str | None]:
    """解析请求里的 `protocols`（协议端点列表）。

    支持两种写法，都是`同一台机器多个入口`的表达：

    * `["ssh", "winrm"]` —— 只给协议名，端口取该协议默认值；
    * `[{"protocol": "winrm", "port": 5985, "winrmTransport": "ntlm"}, …]` —— 带上端口与
      WinRM 认证方式（前端表单用这种）。

    返回 `(endpoints, error)`；`payload` 里没有 `protocols` 时返回 `(None, None)`，调用方
    按老的「单协议主机」路径处理（老客户端与老测试都走这条路）。
    """
    raw = payload.get("protocols")
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, api_error("protocols 必须是数组", 400, code="INVALID_ARGUMENT")
    if not raw:
        return None, api_error("至少要为这台主机选一个协议", 400, code="INVALID_ARGUMENT")
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"protocol": entry}
        if not isinstance(entry, dict):
            return None, api_error("protocols 里的每一项必须是协议名或对象", 400, code="INVALID_ARGUMENT")
        protocol = str(entry.get("protocol") or "").strip().lower()
        if protocol not in ALLOWED_PROTOCOLS:
            return None, api_error(
                "主机协议只能是 ssh（Linux 网页终端）/ rdp（Windows 远程桌面）"
                "/ winrm（Windows 网页终端）",
                400,
                code="INVALID_ARGUMENT",
            )
        if protocol in seen:
            return None, api_error(f"协议「{protocol}」重复了", 400, code="INVALID_ARGUMENT")
        seen.add(protocol)
        raw_port = entry.get("port")
        if raw_port is None or (isinstance(raw_port, str) and not raw_port.strip()):
            port = DEFAULT_PORTS.get(protocol, 22)
        else:
            # 显式写了端口就必须合法：不能像老路径那样把 0 / 乱填悄悄回落成默认端口，
            # 否则「填错了」会变成「配了个默认端口」，用户与审计都看不出问题。
            port = parse_int(raw_port)
            if not (1 <= port <= 65535):
                return None, api_error(
                    f"{protocol} 的端口必须在 1-65535 之间（收到 {raw_port!r}）",
                    400,
                    code="INVALID_ARGUMENT",
                )
        transport = str(
            entry.get("winrmTransport")
            or entry.get("winrm_transport")
            or WINRM_TRANSPORT_DEFAULT
        ).strip().lower()
        if protocol == "winrm" and transport not in WINRM_TRANSPORTS:
            return None, api_error(
                f"WinRM 认证方式只能是 {' / '.join(WINRM_TRANSPORTS)}（收到 {transport or '空值'}）",
                400,
                code="INVALID_ARGUMENT",
            )
        enabled = entry.get("enabled")
        status = str(entry.get("status") or ("disabled" if enabled is False else "active")).strip().lower()
        if status not in ("active", "disabled"):
            return None, api_error("端点状态只能是 active / disabled", 400, code="INVALID_ARGUMENT")
        items.append(
            {
                "protocol": protocol,
                "port": port,
                "winrm_transport": transport,
                "status": status,
            }
        )
    return items, None


def _primary_endpoint(items: list[dict], requested: str | None) -> dict:
    """主端点：请求里 `protocol` 指定的那条，否则第一条。"""
    wanted = (requested or "").strip().lower()
    return next((item for item in items if item["protocol"] == wanted), items[0])


def _apply_endpoints(host: Host, items: list[dict]) -> None:
    """按请求把端点表对齐（增/改/删），随后把主机镜像字段重新对齐到主端点。"""
    rows = {row.protocol: row for row in list(host.protocols or [])}
    wanted = {item["protocol"]: item for item in items}
    for protocol, row in rows.items():
        if protocol not in wanted:
            db.session.delete(row)
    for protocol, item in wanted.items():
        row = rows.get(protocol)
        if row is None:
            db.session.add(
                HostProtocol(
                    host_id=host.id,
                    protocol=protocol,
                    port=item["port"],
                    winrm_transport=item["winrm_transport"],
                    status=item["status"],
                )
            )
        else:
            row.port = item["port"]
            row.winrm_transport = item["winrm_transport"]
            row.status = item["status"]
    db.session.flush()
    # 端点表变了，镜像字段（老代码路径还在读 host.protocol/port）要跟着走
    db.session.expire(host, ["protocols"])
    host.mirror_primary_endpoint()


def _validate_host_payload(payload: dict, *, host_id: int | None = None):
    name = (payload.get("name") or "").strip()
    address = (payload.get("address") or "").strip()
    endpoints, endpoint_error = _parse_endpoints(payload)
    if endpoint_error is not None:
        return None, endpoint_error
    protocol = (payload.get("protocol") or "ssh").strip().lower()
    if endpoints:
        # 有端点表就以它为准：主端点决定镜像字段（老代码路径、审计文案还在读）
        primary = _primary_endpoint(endpoints, payload.get("protocol"))
        protocol = primary["protocol"]
    if protocol not in ALLOWED_PROTOCOLS:
        return None, api_error(
            "主机协议只能是 ssh（Linux 网页终端）/ rdp（Windows 远程桌面）"
            "/ winrm（Windows 网页终端）",
            400,
            code="INVALID_ARGUMENT",
        )
    default_port = DEFAULT_PORTS.get(protocol, 22)
    port = parse_int(payload.get("port"), default_port) or default_port
    if endpoints:
        port = _primary_endpoint(endpoints, payload.get("protocol"))["port"]
    if not name:
        return None, api_error("主机别名不能为空", 400, code="INVALID_ARGUMENT")
    if not address:
        return None, api_error("主机地址不能为空", 400, code="INVALID_ARGUMENT")
    if not (1 <= int(port) <= 65535):
        return None, api_error("端口必须在 1-65535 之间", 400, code="INVALID_ARGUMENT")
    duplicated = Host.query.filter_by(name=name).first()
    if duplicated is not None and duplicated.id != host_id:
        return None, api_error(f"主机别名「{name}」已存在", 409, code="DUPLICATED")
    group_id = parse_int(payload.get("groupId"))
    if group_id and db.session.get(HostGroup, group_id) is None:
        return None, api_error("指定的主机分组不存在", 400, code="INVALID_ARGUMENT")
    rdp_security = str(
        payload.get("rdpSecurity") or payload.get("rdp_security") or RDP_SECURITY_DEFAULT
    ).strip().lower()
    if rdp_security not in RDP_SECURITY_CHOICES:
        return None, api_error(
            f"RDP 安全层只能是 {' / '.join(RDP_SECURITY_CHOICES)}（收到 {rdp_security or '空值'}）",
            400,
            code="INVALID_ARGUMENT",
        )
    winrm_transport = str(
        payload.get("winrmTransport") or payload.get("winrm_transport") or WINRM_TRANSPORT_DEFAULT
    ).strip().lower()
    if endpoints:
        # 主端点的认证方式才是镜像字段该有的值（例如主端点是 winrm + basic）
        winrm_transport = _primary_endpoint(endpoints, payload.get("protocol"))["winrm_transport"]
    if winrm_transport not in WINRM_TRANSPORTS:
        return None, api_error(
            f"WinRM 认证方式只能是 {' / '.join(WINRM_TRANSPORTS)}（收到 {winrm_transport or '空值'}）",
            400,
            code="INVALID_ARGUMENT",
        )
    return {
        "name": name,
        "address": address,
        "port": int(port),
        "protocol": protocol,
        "rdp_security": rdp_security,
        "winrm_transport": winrm_transport,
        "os_type": (payload.get("osType") or payload.get("os_type") or "linux").strip(),
        "group_id": group_id,
        "description": (payload.get("description") or "").strip(),
        "status": (payload.get("status") or "active").strip(),
        "tags": payload.get("tags") or [],
    }, None


@bp.post("")
@admin_required
def create_host():
    payload = request.get_json(silent=True) or {}
    data, error = _validate_host_payload(payload)
    if error is not None:
        return error
    if data["protocol"] not in ALLOWED_PROTOCOLS:
        return api_error(
            "主机协议只能是 ssh（Linux 网页终端）/ rdp（Windows 远程桌面）/ winrm（Windows 网页终端）",
            400,
            code="INVALID_ARGUMENT",
        )
    host = Host(**data)
    db.session.add(host)
    db.session.flush()
    endpoints, endpoint_error = _parse_endpoints(payload)
    if endpoint_error is not None:
        return endpoint_error
    if endpoints:
        for item in endpoints:
            db.session.add(
                HostProtocol(
                    host_id=host.id,
                    protocol=item["protocol"],
                    port=item["port"],
                    winrm_transport=item["winrm_transport"],
                    status=item["status"],
                )
            )
    db.session.commit()
    log_event(
        "host",
        "create_host",
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        message=f"新增主机 {host.name}（{host.address}:{host.port}）",
    )
    return api_ok(_host_payload(host), "主机已添加")


@bp.put("/<int:host_id>")
@admin_required
def update_host(host_id: int):
    host = db.session.get(Host, host_id)
    if host is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    merged = {
        "name": payload.get("name", host.name),
        "address": payload.get("address", host.address),
        "port": payload.get("port", host.port),
        "protocol": payload.get("protocol", host.protocol),
        "rdpSecurity": payload.get("rdpSecurity", host.rdp_security or RDP_SECURITY_DEFAULT),
        "winrmTransport": payload.get(
            "winrmTransport", host.winrm_transport or WINRM_TRANSPORT_DEFAULT
        ),
        "osType": payload.get("osType", host.os_type),
        "groupId": payload.get("groupId", host.group_id),
        "description": payload.get("description", host.description),
        "status": payload.get("status", host.status),
        "tags": payload.get("tags", host.tags or []),
        # 协议端点列表（可选）：给了就整表对齐，没给就走老的「单协议主机」路径
        "protocols": payload.get("protocols"),
    }
    data, error = _validate_host_payload(merged, host_id=host_id)
    if error is not None:
        return error
    endpoints, endpoint_error = _parse_endpoints(merged)
    if endpoint_error is not None:
        return endpoint_error
    for key, value in data.items():
        setattr(host, key, value)
    if endpoints is not None:
        _apply_endpoints(host, endpoints)
    db.session.commit()
    log_event(
        "host",
        "update_host",
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        message=f"更新主机 {host.name}",
    )
    return api_ok(_host_payload(host), "主机已更新")


@bp.delete("/<int:host_id>")
@admin_required
def delete_host(host_id: int):
    host = db.session.get(Host, host_id)
    if host is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    active = SessionRecord.query.filter_by(host_id=host_id, status="active").count()
    if active:
        return api_error(f"该主机还有 {active} 个在线会话，请先中断", 409, code="SESSION_ACTIVE")
    name = host.name
    Grant.query.filter_by(host_id=host_id).delete()
    HostAccount.query.filter_by(host_id=host_id).delete()
    db.session.delete(host)
    db.session.commit()
    log_event(
        "host",
        "delete_host",
        target_type="host",
        target_id=host_id,
        target_name=name,
        message=f"删除主机 {name}（连带其账号与授权）",
    )
    return api_ok(None, "主机已删除")


@bp.post("/<int:host_id>/merge")
@admin_required
def merge_host(host_id: int):
    """把另一台主机（`sourceId`）并进这台。

    同一条机器因为历史原因被登记成两条（例如 `win-75`（rdp）与 `win-75-winrm`（winrm））
    时，管理员用它收成一台多协议主机：账号、授权、协议端点全部搬到本机，来源主机删掉。

    两条硬约束：地址必须一致（否则不是「同一台机器」）、来源主机不能还有在线会话
    （会话记录里的 `host_id` 是历史审计，不随之改写）。
    """
    target = db.session.get(Host, host_id)
    if target is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    source_id = parse_int(payload.get("sourceId") or payload.get("sourceHostId"), 0)
    if not source_id:
        return api_error("缺少 sourceId（要合并进来的那台主机）", 400, code="INVALID_ARGUMENT")
    if int(source_id) == int(host_id):
        return api_error("不能把主机并进它自己", 400, code="INVALID_ARGUMENT")
    source = db.session.get(Host, source_id)
    if source is None:
        return api_error("要合并的主机不存在", 404, code="NOT_FOUND")
    if source.address != target.address:
        return api_error(
            f"两台主机的地址不一样（{source.address} ≠ {target.address}），不能合并",
            400,
            code="INVALID_ARGUMENT",
        )
    active = SessionRecord.query.filter_by(host_id=source.id, status="active").count()
    if active:
        return api_error(f"要合并的主机还有 {active} 个在线会话，请先中断", 409, code="SESSION_ACTIVE")

    source_name = source.name
    moved_accounts = 0
    reused_accounts = 0
    moved_grants = 0
    dropped_grants = 0
    moved_endpoints: list[str] = []
    kept_endpoints: list[str] = []

    # 1) 账号：同名**且同一把凭据**的才复用本机账号；同名不同凭据的改名搬过来，
    #    绝不因为重名就丢掉来源主机的登录凭据。
    target_accounts = {row.name: row for row in list(target.accounts or [])}
    account_map: dict[int, int] = {}
    for account in list(source.accounts or []):
        twin = target_accounts.get(account.name)
        if twin is not None and (
            twin.username == account.username
            and twin.auth_type == account.auth_type
            and _same_credential(twin, account)
        ):
            account_map[account.id] = twin.id
            reused_accounts += 1
            continue
        if twin is not None:
            account.name = f"{account.name}-{source.id}"
        # 用关系搬家（而不是只改 host_id）：关系一换，行就从 source.accounts 上摘掉了，
        # 不会在删除来源主机时被 cascade="all, delete-orphan" 连带删掉。
        target.accounts.append(account)
        target_accounts[account.name] = account
        account_map[account.id] = account.id
        moved_accounts += 1

    # 2) 授权：改挂到本机与新账号 id；与本机已有授权完全重复的直接丢掉
    existing_grants = {
        (row.user_id, row.host_account_id, row.policy_id)
        for row in Grant.query.filter_by(host_id=target.id).all()
    }
    for grant in Grant.query.filter_by(host_id=source.id).all():
        mapped = account_map.get(grant.host_account_id or 0)
        grant.host_id = target.id
        if mapped:
            grant.host_account_id = mapped
        key = (grant.user_id, grant.host_account_id, grant.policy_id)
        if key in existing_grants:
            db.session.delete(grant)
            dropped_grants += 1
            continue
        existing_grants.add(key)
        moved_grants += 1

    # 3) 协议端点：本机没有的搬过来，本机已有的（同协议）保留本机端口
    have = {row.protocol for row in list(target.protocols or [])}
    for endpoint in list(source.protocols or []):
        if endpoint.protocol in have:
            kept_endpoints.append(endpoint.protocol)
            db.session.delete(endpoint)
            continue
        # 同账号：用关系搬家，避免 cascade="all, delete-orphan" 把刚搬过去的端点删掉
        target.protocols.append(endpoint)
        have.add(endpoint.protocol)
        moved_endpoints.append(endpoint.protocol)

    db.session.delete(source)
    db.session.flush()
    # 端点表刚变过，镜像字段要重新对齐（协议、端口、WinRM 认证方式）
    db.session.expire(target, ["protocols"])
    target.mirror_primary_endpoint()
    db.session.commit()
    log_event(
        "host",
        "merge_host",
        target_type="host",
        target_id=target.id,
        target_name=target.name,
        message=(
            f"合并主机 {source_name} → {target.name}："
            f"账号 +{moved_accounts}（复用 {reused_accounts}）、授权 +{moved_grants}"
            f"（去重 {dropped_grants}）、端点 +{moved_endpoints or '无'}"
            f"（本机已有保留 {'/'.join(kept_endpoints) or '无'}）"
        ),
        detail={
            "sourceHostId": source_id,
            "sourceName": source_name,
            "accounts": moved_accounts,
            "reusedAccounts": reused_accounts,
            "grants": moved_grants,
            "droppedGrants": dropped_grants,
            "endpoints": moved_endpoints,
            "keptEndpoints": kept_endpoints,
        },
    )
    return api_ok(
        _host_payload(target),
        f"已把 {source_name} 合并到 {target.name}（账号 +{moved_accounts}、授权 +{moved_grants}）",
    )


# --------------------------------------------------------------------------
# 主机登录账号
# --------------------------------------------------------------------------@bp.get("/<int:host_id>/accounts")
@permission_required("account:view", "host:view", "grant:view")
def list_accounts(host_id: int):
    host = db.session.get(Host, host_id)
    if host is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    rows = HostAccount.query.filter_by(host_id=host_id).order_by(HostAccount.id.asc()).all()
    hide = not _can_read_secret()
    items = []
    for row in rows:
        data = row.to_dict(with_secret=not hide)
        data["grantCount"] = Grant.query.filter_by(host_account_id=row.id).count()
        items.append(data)
    return api_ok(items)


@bp.post("/<int:host_id>/accounts")
@admin_required
def create_account(host_id: int):
    host = db.session.get(Host, host_id)
    if host is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    username = (payload.get("username") or "").strip()
    auth_type = (payload.get("authType") or "password").strip().lower()
    if not name or not username:
        return api_error("账号别名和登录名都不能为空", 400, code="INVALID_ARGUMENT")
    if auth_type not in ALLOWED_AUTH_TYPES:
        return api_error("认证方式只支持 password（口令）或 key（密钥）", 400, code="INVALID_ARGUMENT")
    secret = payload.get("password") or payload.get("secret") or ""
    private_key = payload.get("privateKey") or ""
    if auth_type == "password" and not secret:
        return api_error("口令认证必须填写登录密码", 400, code="INVALID_ARGUMENT")
    if auth_type == "key" and not private_key:
        return api_error("密钥认证必须粘贴私钥内容", 400, code="INVALID_ARGUMENT")
    duplicated = HostAccount.query.filter_by(
        host_id=host_id, name=name
    ).first()
    if duplicated is not None:
        return api_error(f"该主机下账号别名「{name}」已存在", 409, code="DUPLICATED")

    from ..crypto import encrypt

    account = HostAccount(
        host_id=host_id,
        name=name,
        username=username,
        auth_type=auth_type,
        secret_enc=encrypt(secret) if secret else "",
        private_key_enc=encrypt(private_key) if private_key else "",
        passphrase_enc=encrypt(payload.get("passphrase") or "")
        if payload.get("passphrase")
        else "",
        sudo_command=(payload.get("sudoCommand") or "").strip(),
        description=(payload.get("description") or "").strip(),
    )
    db.session.add(account)
    db.session.commit()
    log_event(
        "host",
        "create_account",
        target_type="host_account",
        target_id=account.id,
        target_name=f"{host.name}/{account.name}",
        message=f"为主机 {host.name} 新增登录账号 {account.name}（{username}）",
    )
    return api_ok(account.to_dict(with_secret=False), "主机登录账号已添加")


@bp.put("/<int:host_id>/accounts/<int:account_id>")
@admin_required
def update_account(host_id: int, account_id: int):
    account = db.session.get(HostAccount, account_id)
    if account is None or account.host_id != host_id:
        return api_error("主机登录账号不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    from ..crypto import encrypt

    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return api_error("账号别名不能为空", 400, code="INVALID_ARGUMENT")
        duplicated = HostAccount.query.filter(
            HostAccount.host_id == host_id,
            HostAccount.name == name,
            HostAccount.id != account_id,
        ).first()
        if duplicated is not None:
            return api_error(f"该主机下账号别名「{name}」已存在", 409, code="DUPLICATED")
        account.name = name
    if "username" in payload:
        account.username = (payload.get("username") or "").strip()
    if "authType" in payload:
        auth_type = (payload.get("authType") or "password").strip().lower()
        if auth_type not in ALLOWED_AUTH_TYPES:
            return api_error("认证方式只支持 password 或 key", 400, code="INVALID_ARGUMENT")
        account.auth_type = auth_type
    secret = payload.get("password") or payload.get("secret")
    if secret:
        account.secret_enc = encrypt(secret)
    private_key = payload.get("privateKey")
    if private_key:
        account.private_key_enc = encrypt(private_key)
    passphrase = payload.get("passphrase")
    if passphrase is not None:
        account.passphrase_enc = encrypt(passphrase) if passphrase else ""
    if "sudoCommand" in payload:
        account.sudo_command = (payload.get("sudoCommand") or "").strip()
    if "description" in payload:
        account.description = (payload.get("description") or "").strip()
    db.session.commit()
    log_event(
        "host",
        "update_account",
        target_type="host_account",
        target_id=account.id,
        target_name=account.name,
        message=f"更新主机登录账号 {account.name}",
    )
    return api_ok(account.to_dict(with_secret=False), "主机登录账号已更新")


@bp.delete("/<int:host_id>/accounts/<int:account_id>")
@admin_required
def delete_account(host_id: int, account_id: int):
    account = db.session.get(HostAccount, account_id)
    if account is None or account.host_id != host_id:
        return api_error("主机登录账号不存在", 404, code="NOT_FOUND")
    active = SessionRecord.query.filter_by(account_id=account_id, status="active").count()
    if active:
        return api_error("该账号还有在线会话，请先中断", 409, code="SESSION_ACTIVE")
    name = account.name
    Grant.query.filter_by(host_account_id=account_id).delete()
    db.session.delete(account)
    db.session.commit()
    log_event(
        "host",
        "delete_account",
        target_type="host_account",
        target_id=account_id,
        target_name=name,
        message=f"删除主机登录账号 {name}",
    )
    return api_ok(None, "主机登录账号已删除")


def _test_winrm_account(host, account, account_id: int):
    """WinRM 资产的「测试连接」：读 whoami / 计算机名 / PowerShell 版本 / 系统版本。"""
    from flask import current_app

    from ..winrm import WinrmError
    from ..winrm import build_target as build_winrm_target
    from ..winrm import close as close_winrm
    from ..winrm import connect as connect_winrm
    from ..winrm import run_script as run_winrm_script
    from ..winrm.client import PROBE_SCRIPT

    try:
        target = build_winrm_target(
            host,
            account,
            connect_timeout=current_app.config["SSH_CONNECT_TIMEOUT"],
            command_timeout=current_app.config["COMMAND_TIMEOUT"],
        )
        connection = connect_winrm(target)
    except WinrmError as exc:
        log_event(
            "host",
            "test_account",
            result="failure",
            target_type="host_account",
            target_id=account_id,
            target_name=account.name,
            message=f"WinRM 连通性测试失败：{exc}",
        )
        return api_error(str(exc), 400, code="WINRM_CONNECT_FAILED")

    try:
        result = run_winrm_script(connection, PROBE_SCRIPT, timeout=20.0)
    except Exception as exc:  # noqa: BLE001 - 通道异常也要回结构化错误，不能 500
        log_event(
            "host",
            "test_account",
            result="failure",
            target_type="host_account",
            target_id=account_id,
            target_name=account.name,
            message=f"WinRM 连通性测试命令执行失败：{exc}",
        )
        return api_error(f"连通性测试命令执行失败：{exc}", 400, code="WINRM_TEST_COMMAND_FAILED")
    finally:
        close_winrm(connection)

    if result.timed_out:
        return api_error(
            "WinRM 命令超时：目标机没有在 20 秒内返回结果",
            400,
            code="WINRM_TEST_TIMEOUT",
        )
    log_event(
        "host",
        "test_account",
        target_type="host_account",
        target_id=account_id,
        target_name=account.name,
        message="WinRM 连通性测试成功",
    )
    return api_ok(
        {
            "output": (result.stdout or "").strip(),
            "check": "winrm",
            "endpoint": target.endpoint,
            "transport": target.transport,
            "command": "whoami / COMPUTERNAME / PowerShellVersion / OSVersion",
        },
        "连接成功",
    )


@bp.post("/<int:host_id>/accounts/<int:account_id>/test")
@admin_required
def test_account(host_id: int, account_id: int):
    host = db.session.get(Host, host_id)
    account = db.session.get(HostAccount, account_id)
    if host is None or account is None or account.host_id != host_id:
        return api_error("主机或登录账号不存在", 404, code="NOT_FOUND")
    if (host.protocol or "ssh").strip().lower() == "winrm":
        return _test_winrm_account(host, account, account_id)
    from flask import current_app

    try:
        target = build_target(
            host,
            account,
            connect_timeout=current_app.config["SSH_CONNECT_TIMEOUT"],
            banner_timeout=current_app.config["SSH_BANNER_TIMEOUT"],
        )
        info = connect(target)
    except SSHError as exc:
        log_event(
            "host",
            "test_account",
            result="failure",
            target_type="host_account",
            target_id=account_id,
            target_name=account.name,
            message=f"连通性测试失败：{exc}",
        )
        return api_error(str(exc), 400, code="SSH_CONNECT_FAILED")
    try:
        status, out, err = run_single_command(info, "uname -a", timeout=10.0)
    except Exception as exc:  # noqa: BLE001 - 通道/超时异常都要回结构化错误，不能 500
        # 历史缺陷：这里只护住了 connect()，命令执行阶段抛异常（如目标机拒绝 exec 请求时的
        # `Channel closed.`）会一路冒到 Flask 兜底 → 前端拿到 HTTP 500 与内部英文异常，
        # 用户看不懂、审计里也没有记录。现在统一回 400 + 可读原因，并把**已经成功的握手信息**
        # （指纹与两端版本）一并带回去，界面上仍能看到服务器版本与客户端版本。
        log_event(
            "host",
            "test_account",
            result="failure",
            target_type="host_account",
            target_id=account_id,
            target_name=account.name,
            message=f"连通性测试命令执行失败：{exc}",
        )
        return api_error(
            f"连通性测试命令执行失败：{exc}",
            400,
            code="SSH_TEST_COMMAND_FAILED",
            data={
                "fingerprint": info.fingerprint,
                "serverVersion": info.server_version,
                "clientVersion": info.client_version,
                "command": "uname -a",
            },
        )
    finally:
        from ..ssh_client import close

        close(info)
    log_event(
        "host",
        "test_account",
        target_type="host_account",
        target_id=account_id,
        target_name=account.name,
        message="连通性测试成功",
    )
    return api_ok(
        {
            "fingerprint": info.fingerprint,
            "serverVersion": info.server_version,
            "clientVersion": info.client_version,
            "command": "uname -a",
            "exitStatus": status,
            "output": (out or err)[:2000],
        },
        "连通性测试成功",
    )


def _can_read_secret() -> bool:
    from ..security import has_permission, load_actor

    actor = load_actor()
    return bool(actor and has_permission(actor, "host:manage"))


# --------------------------------------------------------------------------
# 主机分组
# --------------------------------------------------------------------------
group_bp = Blueprint("host_groups", __name__, url_prefix="/api/host-groups")


@group_bp.get("")
@permission_required("group:view", "host:view", "grant:view")
def list_groups():
    page, size = page_args(default_size=50)
    query = HostGroup.query
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(HostGroup.name.like(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(HostGroup.id.asc()).offset((page - 1) * size).limit(size).all()
    items = []
    for row in rows:
        data = row.to_dict()
        data["hostCount"] = Host.query.filter_by(group_id=row.id).count()
        items.append(data)
    return api_list(items, total, page, size)


@group_bp.get("/options")
@permission_required("group:view", "host:view", "grant:view")
def group_options():
    rows = HostGroup.query.order_by(HostGroup.id.asc()).all()
    return api_ok([{"label": row.name, "value": row.id} for row in rows])


@group_bp.post("")
@admin_required
def create_group():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return api_error("分组名称不能为空", 400, code="INVALID_ARGUMENT")
    if HostGroup.query.filter_by(name=name).first() is not None:
        return api_error("分组名称已存在", 409, code="DUPLICATED")
    group = HostGroup(name=name, description=(payload.get("description") or "").strip())
    db.session.add(group)
    db.session.commit()
    log_event("host", "create_group", target_type="host_group", target_id=group.id, target_name=group.name)
    return api_ok(group.to_dict(), "分组已创建")


@group_bp.put("/<int:group_id>")
@admin_required
def update_group(group_id: int):
    group = db.session.get(HostGroup, group_id)
    if group is None:
        return api_error("分组不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return api_error("分组名称不能为空", 400, code="INVALID_ARGUMENT")
        duplicated = HostGroup.query.filter(
            HostGroup.name == name, HostGroup.id != group_id
        ).first()
        if duplicated is not None:
            return api_error("分组名称已存在", 409, code="DUPLICATED")
        group.name = name
    if "description" in payload:
        group.description = (payload.get("description") or "").strip()
    db.session.commit()
    log_event("host", "update_group", target_type="host_group", target_id=group.id, target_name=group.name)
    return api_ok(group.to_dict(), "分组已更新")


@group_bp.delete("/<int:group_id>")
@admin_required
def delete_group(group_id: int):
    group = db.session.get(HostGroup, group_id)
    if group is None:
        return api_error("分组不存在", 404, code="NOT_FOUND")
    used = Host.query.filter_by(group_id=group_id).count()
    if used:
        return api_error(f"该分组下还有 {used} 台主机，请先迁移", 409, code="INVALID_OPERATION")
    name = group.name
    db.session.delete(group)
    db.session.commit()
    log_event("host", "delete_group", target_type="host_group", target_id=group_id, target_name=name)
    return api_ok(None, "分组已删除")


_ = parse_bool
