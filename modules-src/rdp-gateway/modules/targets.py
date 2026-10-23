"""RDP 目标机与会话记录接口。

从旧 `app/api/rdp_gateway.py` 的 `/hosts` 与 `/sessions` 移植。模块运行在宿主进程
内，因此继续复用宿主的资产授权（`app.access`）、会话表与审计；只是把 URL 前缀换成
`ctx.api_prefix`，设置/路径不再依赖宿主蓝图。

与旧实现的唯一数据形状差异：宿主的 `serialize_target()` 账号字段叫 `id`，而网关
二进制读的是 `accountId`，这里显式做一次映射。
"""

from __future__ import annotations

from flask import request

from autoops import ModuleApiError

RDP_PROTOCOL = "rdp"
DEFAULT_RDP_PORT = 3389


def _actor():
    try:
        from app.security import load_actor

        actor = load_actor()
    except Exception:  # noqa: BLE001 - 无请求上下文/令牌失效
        actor = None
    if actor is None:
        raise ModuleApiError("登录状态已失效", status=401, code="UNAUTHORIZED")
    return actor


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _rdp_endpoint_port(host) -> int:
    """取主机的 RDP 端点端口；没有端点配置就退回 3389。

    不能用 ``host.port``：一台主机可能同时开 SSH/WinRM，``Host.port`` 是主协议
    端口，不一定是 RDP。
    """
    try:
        for endpoint in host.protocol_endpoints():
            if (endpoint.protocol or "").lower() == RDP_PROTOCOL and endpoint.port:
                return int(endpoint.port)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_RDP_PORT


def _account_rows(accounts: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[int] = set()
    for account in accounts or []:
        try:
            account_id = int(account.get("id") or account.get("accountId") or 0)
        except (TypeError, ValueError):
            continue
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        rows.append(
            {
                "accountId": account_id,
                "name": account.get("name") or account.get("username") or "",
                "username": account.get("username") or "",
                "authType": account.get("authType") or "",
            }
        )
    return rows


def _target_host_id(entry: dict) -> int | None:
    try:
        return int((entry.get("host") or entry.get("hostId") or {}).id)
    except (AttributeError, TypeError, ValueError):
        try:
            return int(entry.get("hostId"))
        except (TypeError, ValueError):
            return None


def register(ctx) -> None:
    @ctx.route("/hosts", methods=("GET",), permission="rdp:use")
    def list_hosts():
        from app.access import accessible_targets, serialize_target

        actor = _actor()
        try:
            targets = accessible_targets(actor, protocols=(RDP_PROTOCOL,)) or []
        except Exception as exc:  # noqa: BLE001
            raise ModuleApiError(
                f"读取可访问资产失败：{exc}", status=500, code="ACCESS_FAILED"
            ) from exc

        rows: dict[int, dict] = {}
        for entry in targets:
            try:
                target = serialize_target(entry)
            except Exception:  # noqa: BLE001 - 单条资产异常不能拖垮整个列表
                ctx.logger.warning("序列化 RDP 资产失败", exc_info=True)
                continue
            if str(target.get("protocol") or "").lower() != RDP_PROTOCOL:
                continue
            try:
                host_id = int(target["hostId"])
            except (KeyError, TypeError, ValueError):
                continue
            row = rows.get(host_id)
            if row is None:
                try:
                    port = int(target.get("port") or 0) or DEFAULT_RDP_PORT
                except (TypeError, ValueError):
                    port = DEFAULT_RDP_PORT
                row = {
                    "hostId": host_id,
                    "hostName": target.get("hostName") or "",
                    "address": target.get("address") or "",
                    "port": port,
                    "osType": target.get("osType") or "",
                    "groupName": target.get("groupName") or "",
                    "accounts": [],
                }
                rows[host_id] = row
            existing = {item["accountId"] for item in row["accounts"]}
            for account in _account_rows(target.get("accounts") or []):
                if account["accountId"] not in existing:
                    row["accounts"].append(account)
                    existing.add(account["accountId"])

        result = sorted(
            rows.values(), key=lambda item: ((item["hostName"] or "").lower(), item["hostId"])
        )
        return {"hosts": result, "total": len(result)}

    @ctx.route("/sessions", methods=("POST",), permission="rdp:use")
    def create_session():
        from app.access import accessible_targets, serialize_target
        from app.audit import log_event, new_session
        from app.crypto import decrypt
        from app.extensions import db
        from app.models import Host, HostAccount
        from app.utils import parse_int

        actor = _actor()
        payload = request.get_json(silent=True) or {}
        host_id = parse_int(payload.get("hostId"))
        account_id = parse_int(payload.get("accountId"))
        if not host_id:
            raise ModuleApiError("请指定要连接的主机", status=400, code="INVALID_ARGUMENT")

        targets = accessible_targets(actor, protocols=(RDP_PROTOCOL,)) or []
        matched = []
        for entry in targets:
            target = serialize_target(entry)
            if str(target.get("protocol") or "").lower() != RDP_PROTOCOL:
                continue
            try:
                if int(target["hostId"]) == host_id:
                    matched.append((entry, target))
            except (KeyError, TypeError, ValueError):
                continue
        if not matched:
            raise ModuleApiError(
                "该主机不是可远程桌面的主机，或你没有访问权限",
                status=403,
                code="FORBIDDEN",
            )

        accounts: list[dict] = []
        seen: set[int] = set()
        for _entry, target in matched:
            for account in _account_rows(target.get("accounts") or []):
                if account["accountId"] not in seen:
                    accounts.append(account)
                    seen.add(account["accountId"])
        if not accounts:
            raise ModuleApiError(
                "没有可用的登录账号（请检查「访问授权」里绑定的账号）",
                status=400,
                code="NO_ACCOUNT",
            )
        selected = None
        if account_id:
            selected = next(
                (item for item in accounts if item["accountId"] == account_id), None
            )
        if selected is None:
            selected = accounts[0]

        host_name = matched[0][1].get("hostName") or ""
        host_address = matched[0][1].get("address") or ""
        client_ip = _client_ip()

        host_row = db.session.get(Host, host_id)
        account_row = db.session.get(HostAccount, selected["accountId"])
        if host_row is None:
            raise ModuleApiError("主机不存在或已删除", status=400, code="HOST_MISSING")
        if account_row is None:
            raise ModuleApiError("登录账号不存在或已删除", status=400, code="ACCOUNT_MISSING")

        port = _rdp_endpoint_port(host_row)
        host_name = host_row.name or host_name
        host_address = host_row.address or host_address
        username = account_row.username or selected["username"]

        grant_id = None
        try:
            grant_ids = matched[0][0].get("grantIds") or []
            if grant_ids:
                grant_id = int(grant_ids[0])
        except (TypeError, ValueError, IndexError):
            grant_id = None

        try:
            record = new_session(
                user_id=actor.id,
                username=actor.username,
                role_code=getattr(getattr(actor, "role", None), "code", "") or "",
                host_id=host_id,
                host_name=host_name,
                host_address=f"{host_address}:{port or DEFAULT_RDP_PORT}",
                account_id=selected["accountId"],
                account_username=username,
                grant_id=grant_id,
                source="gateway",
                protocol=RDP_PROTOCOL,
                client_ip=client_ip,
                status="active",
            )
        except Exception as exc:  # noqa: BLE001
            raise ModuleApiError(
                f"创建会话记录失败：{exc}", status=500, code="SESSION_CREATE_FAILED"
            ) from exc

        try:
            log_event(
                "session",
                "rdp_gateway_open",
                message=f"{actor.username} 通过 RDP 网关打开 {host_name}（账号 {username}）",
                target_type="host",
                target_id=host_id,
                target_name=host_name,
                actor=actor,
                detail={
                    "protocol": RDP_PROTOCOL,
                    "source": "gateway",
                    "account": username,
                    "sid": record.sid,
                },
                ip=client_ip,
            )
        except Exception:  # noqa: BLE001
            ctx.logger.warning("写 RDP 网关会话审计失败", exc_info=True)

        password = ""
        try:
            if account_row.secret_enc:
                password = decrypt(account_row.secret_enc)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("会话 %s 解密账号口令失败：%s", record.sid, exc)

        try:
            log_event(
                "session",
                "rdp_credential_reveal",
                message=(
                    f"{actor.username} 的 RDP 网关会话 {record.sid} 取用了 "
                    f"{host_name} 的账号口令（{username}）"
                ),
                target_type="host",
                target_id=host_id,
                target_name=host_name,
                actor=actor,
                detail={
                    "protocol": RDP_PROTOCOL,
                    "source": "gateway-client",
                    "sid": record.sid,
                },
                ip=client_ip,
            )
        except Exception:  # noqa: BLE001
            ctx.logger.warning("写 RDP 网关凭据审计失败", exc_info=True)

        return {
            "sid": record.sid,
            "hostId": host_id,
            "hostName": host_name,
            "accountId": selected["accountId"],
            "accountUsername": username,
            "credential": {
                "address": host_address,
                "port": port,
                "username": username,
                "password": password,
            },
        }
