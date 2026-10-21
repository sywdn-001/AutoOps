"""RDP 网关（`mstsc` 连 4489）的后端支撑接口 —— 实验分支。

真正的 RDP 协议实现不在 Python 这侧（见 `docs/rdp-gateway-experiment.md`）：
`bastion-rdp-gateway/`（Rust + ironrdp-server）负责收 mstsc 的连接、画选单、转发画面。
这里给它提供两件事：

1. `GET /api/rdp-gateway/hosts` —— 当前账号**有权远程桌面**的主机与账号（分组好直接给选单画）；
2. `POST /api/rdp-gateway/sessions` —— 选中一台机器时**开一条会话记录**并写审计（后续录像挂在这条 sid 上）。

鉴权与准入完全复用网页 WebRDP 那一套（`accessible_targets` 里已经含授权、时段、配额与
「允许交互式登录」的判定），所以网关上「能看到什么」与网页端「能连什么」永远一致。
"""

from __future__ import annotations

import logging

from flask import Blueprint, request

from ..access import accessible_targets, serialize_target
from ..audit import log_event, new_session
from ..security import load_actor, permission_required
from ..utils import api_error, api_ok, parse_int

log = logging.getLogger("bastion.api.rdp_gateway")

bp = Blueprint("rdp_gateway", __name__, url_prefix="/api/rdp-gateway")

RDP_PROTOCOL = "rdp"


@bp.get("/hosts")
@permission_required("rdp:use")
def gateway_hosts():
    """网关选单的数据源：按主机聚合的「主机 + 可登录账号」。"""
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    targets = accessible_targets(actor, protocols=(RDP_PROTOCOL,))
    serialized = [serialize_target(item) for item in targets]
    hosts: dict[int, dict] = {}
    for entry in serialized:
        if entry.get("protocol") != RDP_PROTOCOL:
            continue
        host_id = int(entry.get("hostId") or 0)
        row = hosts.setdefault(
            host_id,
            {
                "hostId": host_id,
                "hostName": entry.get("hostName") or "",
                "address": entry.get("address") or "",
                "port": int(entry.get("port") or 3389),
                "osType": entry.get("osType") or "",
                "accounts": [],
            },
        )
        for account in entry.get("accounts") or []:
            row["accounts"].append(
                {
                    "accountId": int(account.get("id") or 0),
                    "username": account.get("username") or "",
                }
            )
    rows = sorted(hosts.values(), key=lambda item: (item["hostName"].lower(), item["hostId"]))
    return api_ok({"hosts": rows, "total": len(rows)})


@bp.post("/sessions")
@permission_required("rdp:use")
def gateway_open_session():
    """选中主机后开会话记录：写 `sessions` 表 + 一条 `rdp_gateway_open` 审计。"""
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    payload = request.get_json(silent=True) or {}
    host_id = parse_int(payload.get("hostId"))
    account_id = parse_int(payload.get("accountId"))
    if not host_id:
        return api_error("请指定要连接的主机", 400, code="INVALID_ARGUMENT")

    target = None
    for entry in accessible_targets(actor, protocols=(RDP_PROTOCOL,)):
        if int(entry.get("hostId") or 0) == host_id:
            target = entry
            break
    if target is None:
        return api_error("该主机不是可远程桌面的主机，或你没有访问权限", 403, code="FORBIDDEN")

    accounts = target.get("accounts") or []
    if account_id:
        accounts = [item for item in accounts if int(item.get("id") or 0) == account_id]
    if not accounts:
        return api_error("没有可用的登录账号（请检查「访问授权」里绑定的账号）", 400, code="NO_ACCOUNT")
    account = accounts[0]

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "") or ""
    record = new_session(
        user_id=actor.id,
        username=actor.username,
        role_code=getattr(actor.role, "code", "") if getattr(actor, "role", None) else "",
        host_id=host_id,
        host_name=target.get("hostName") or "",
        host_address=f"{target.get('address')}:{target.get('port') or 3389}",
        account_id=int(account.get("id") or 0),
        account_username=account.get("username") or "",
        grant_id=int(target.get("grantId") or 0) if target.get("grantId") else None,
        source="gateway",
        protocol="rdp",
        client_ip=client_ip.split(",")[0].strip(),
        status="active",
    )
    log_event(
        "session",
        "rdp_gateway_open",
        message=f"{actor.username} 通过 RDP 网关打开 {target.get('hostName')}（账号 {account.get('username')}）",
        target_type="host",
        target_id=host_id,
        target_name=target.get("hostName") or "",
        actor=actor,
        detail={
            "protocol": "rdp",
            "source": "gateway",
            "account": account.get("username"),
            "sid": record.sid,
        },
        ip=client_ip.split(",")[0].strip(),
    )
    return api_ok(
        {
            "sid": record.sid,
            "hostId": host_id,
            "hostName": target.get("hostName") or "",
            "accountId": int(account.get("id") or 0),
            "accountUsername": account.get("username") or "",
        }
    )
