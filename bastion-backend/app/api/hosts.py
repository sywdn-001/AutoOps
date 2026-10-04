"""主机资产管理接口。"""

from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy import or_

from ..audit import log_event
from ..extensions import db
from ..models import Grant, Host, HostAccount, HostGroup, SessionRecord
from ..rdp.proxy import RDP_SECURITY_CHOICES, RDP_SECURITY_DEFAULT
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

ALLOWED_PROTOCOLS = {"ssh", "rdp"}
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


def _validate_host_payload(payload: dict, *, host_id: int | None = None):
    name = (payload.get("name") or "").strip()
    address = (payload.get("address") or "").strip()
    port = parse_int(payload.get("port"), 22) or 22
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
    return {
        "name": name,
        "address": address,
        "port": int(port),
        "protocol": (payload.get("protocol") or "ssh").strip().lower(),
        "rdp_security": rdp_security,
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
        return api_error("当前仅支持 ssh 协议主机", 400, code="INVALID_ARGUMENT")
    host = Host(**data)
    db.session.add(host)
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
        "osType": payload.get("osType", host.os_type),
        "groupId": payload.get("groupId", host.group_id),
        "description": payload.get("description", host.description),
        "status": payload.get("status", host.status),
        "tags": payload.get("tags", host.tags or []),
    }
    data, error = _validate_host_payload(merged, host_id=host_id)
    if error is not None:
        return error
    for key, value in data.items():
        setattr(host, key, value)
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


# --------------------------------------------------------------------------
# 主机登录账号
# --------------------------------------------------------------------------
@bp.get("/<int:host_id>/accounts")
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


@bp.post("/<int:host_id>/accounts/<int:account_id>/test")
@admin_required
def test_account(host_id: int, account_id: int):
    host = db.session.get(Host, host_id)
    account = db.session.get(HostAccount, account_id)
    if host is None or account is None or account.host_id != host_id:
        return api_error("主机或登录账号不存在", 404, code="NOT_FOUND")
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
