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
import os
import socket
import subprocess
from pathlib import Path

from flask import Blueprint, Response, current_app, request

from ..access import accessible_targets, serialize_target
from ..audit import log_event, new_session
from ..crypto import decrypt
from ..extensions import db
from ..models import Host, HostAccount, SessionRecord
from ..rdp import window_bridge
from ..security import admin_required, load_actor, permission_required
from ..settings_store import get_int, get_setting
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

# --------------------------------------------------------------------------- #
# 网关进程管理：状态 / 启动 / 停止（管理端在「系统设置 → RDP 网关」里用）
# --------------------------------------------------------------------------- #
#: 仓库根（app/api/rdp_gateway.py → api → app → bastion-backend → 仓库根）
REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_DIR = REPO_ROOT / "bastion-rdp-gateway"
GATEWAY_BIN = GATEWAY_DIR / "target" / "release" / ("bastion-rdp-gateway.exe" if os.name == "nt" else "bastion-rdp-gateway")


def _pid_path() -> Path:
    return Path(current_app.instance_path) / "rdp-gateway.pid"


def _read_pid() -> int | None:
    try:
        text = _pid_path().read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except Exception:  # noqa: BLE001
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            return "bastion-rdp-gateway" in out.lower()
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _port() -> int:
    value = get_int("rdp_gateway_port", 4489)
    return value or 4489


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _adopt_pid(port: int) -> int | None:
    """端口在听但没有 pid 文件（比如网关是手动 `cargo run` 起的）时，按端口把 pid 找回来，
    这样页面上的「停止」也能用。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=8
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in line.upper():
            continue
        local = parts[1]
        if local.endswith(f":{port}") and parts[4].isdigit():
            pid = int(parts[4])
            try:
                _pid_path().write_text(str(pid), encoding="utf-8")
            except OSError:
                pass
            return pid
    return None


@bp.get("/status")
@permission_required("rdp:use")
def gateway_status():
    """网关是否在跑：以「端口是否在听」为准，pid 只用来停。"""
    port = _port()
    listening = _listening(port)
    pid = _read_pid()
    if listening and not _pid_alive(pid):
        pid = _adopt_pid(port)
    return api_ok(
        {
            "enabled": str(get_setting("rdp_gateway_enabled", "0")) in ("1", "true", "True"),
            "port": port,
            "listening": listening,
            "pid": pid if _pid_alive(pid) else None,
            "binary": str(GATEWAY_BIN),
            "binaryExists": GATEWAY_BIN.exists(),
            "logFile": str(Path(current_app.instance_path) / "rdp-gateway.log"),
        }
    )


@bp.post("/start")
@admin_required
def gateway_start():
    """按设置里的端口拉起 Rust 网关进程（已在跑就直接返回）。"""
    port = _port()
    if _listening(port):
        return api_ok({"port": port, "started": False, "message": "网关已经在运行"})
    if not GATEWAY_BIN.exists():
        return api_error(
            f"找不到网关程序：{GATEWAY_BIN}（先在 bastion-rdp-gateway 目录执行 cargo build --release）",
            400,
            code="BINARY_MISSING",
        )
    log_path = Path(current_app.instance_path) / "rdp-gateway.log"
    env = dict(os.environ)
    env["RDP_GATEWAY_PORT"] = str(port)
    env["BASTION_INSTANCE_DIR"] = current_app.instance_path
    env.setdefault("RUST_LOG", "info")
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603 - 路径来自本仓库固定位置
            [str(GATEWAY_BIN)],
            cwd=str(GATEWAY_DIR),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    _pid_path().write_text(str(proc.pid), encoding="utf-8")
    log_event(
        "session",
        "rdp_gateway_start",
        message=f"{getattr(load_actor(), 'username', 'admin')} 启动了 RDP 网关（端口 {port}）",
        actor=load_actor(),
        ip=(request.remote_addr or ""),
    )
    return api_ok({"port": port, "pid": proc.pid, "started": True})


@bp.post("/stop")
@admin_required
def gateway_stop():
    pid = _read_pid()
    if not _pid_alive(pid):
        _pid_path().unlink(missing_ok=True)
        return api_ok({"stopped": False, "message": "网关没有在运行"})
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)
    except Exception as exc:  # noqa: BLE001
        return api_error(f"停止失败：{exc}", 500, code="STOP_FAILED")
    _pid_path().unlink(missing_ok=True)
    log_event(
        "session",
        "rdp_gateway_stop",
        message=f"{getattr(load_actor(), 'username', 'admin')} 停止了 RDP 网关（pid {pid}）",
        actor=load_actor(),
        ip=(request.remote_addr or ""),
    )
    return api_ok({"stopped": True, "pid": pid})

# --------------------------------------------------------------------------- #
# 目标机桥：网关选中主机后，这里用 mstsc 连目标机，把画面块 + 输入在两边来回搬
# （过渡实现，见 app/rdp/window_bridge.py 里的限制说明）
# --------------------------------------------------------------------------- #
def _session_or_error(sid: str):
    record = SessionRecord.query.filter_by(sid=sid).first() if sid else None
    if record is None:
        return None, api_error("会话不存在或已结束", 404, code="SESSION_NOT_FOUND")
    return record, None


def _rdp_endpoint_port(host) -> int:
    """RDP 端点端口：优先主机上登记的 rdp 端点，没有就用 3389（不能用 Host.port，
    那可能是这台机器的 WinRM/SSH 端口）。"""
    try:
        for endpoint in host.protocol_endpoints():
            if (getattr(endpoint, "protocol", "") or "").lower() == "rdp" and endpoint.port:
                return int(endpoint.port)
    except Exception:  # noqa: BLE001
        pass
    return 3389


@bp.post("/connect")
@permission_required("rdp:use")
def gateway_connect_target():
    """`{sid}` → 用该会话登记的主机 + 账号口令，拉起桥接用的 mstsc。"""
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    payload = request.get_json(silent=True) or {}
    sid = str(payload.get("sid") or "")
    record, error = _session_or_error(sid)
    if error is not None:
        return error

    host = db.session.get(Host, record.host_id) if record.host_id else None
    if host is None:
        return api_error("会话对应的主机已不存在", 400, code="HOST_MISSING")
    account = db.session.get(HostAccount, record.account_id) if record.account_id else None
    if account is None:
        return api_error("会话对应的账号已不存在", 400, code="ACCOUNT_MISSING")

    try:
        password = decrypt(account.secret_enc) if account.secret_enc else ""
    except Exception as exc:  # noqa: BLE001
        return api_error(f"口令解密失败：{exc}", 500, code="DECRYPT_FAILED")

    log_event(
        "session",
        "rdp_credential_reveal",
        message=f"{actor.username} 的 RDP 网关会话 {sid} 取用了 {host.name} 的账号口令（{account.username}）",
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        actor=actor,
        detail={"protocol": "rdp", "source": "gateway-bridge", "account": account.username, "sid": sid},
        ip=(request.remote_addr or ""),
    )

    port = _rdp_endpoint_port(host)
    try:
        window_bridge.open_bridge(sid, host.address, port, account.username, password)
    except window_bridge.BridgeError as exc:
        return api_error(str(exc), 502, code="BRIDGE_FAILED")
    except Exception as exc:  # noqa: BLE001
        return api_error(f"桥接失败：{exc}", 500, code="BRIDGE_ERROR")
    return api_ok({"sid": sid, "host": f"{host.address}:{port}", "width": 1280, "height": 800})


@bp.get("/frame")
@permission_required("rdp:use")
def gateway_frame():
    """目标机画面的**变化块**（二进制）：`[u32 n][n × (u16 x,y,w,h + RGBA)]`；没变化回 204。"""
    sid = str(request.args.get("sid") or "")
    bridge = window_bridge.bridge_for(sid)
    if bridge is None:
        return Response(b"", status=204)
    try:
        payload = bridge.frame(full=bool(request.args.get("full")))
    except Exception as exc:  # noqa: BLE001
        log.warning("抓帧失败 sid=%s: %s", sid, exc)
        return Response(b"", status=204)
    if not payload:
        return Response(b"", status=204)
    return Response(payload, status=200, mimetype="application/octet-stream")


@bp.post("/input")
@permission_required("rdp:use")
def gateway_input():
    """把网关收到的键盘 / 鼠标事件转给目标机。"""
    payload = request.get_json(silent=True) or {}
    sid = str(payload.get("sid") or "")
    events = payload.get("events") or []
    bridge = window_bridge.bridge_for(sid)
    if bridge is None:
        return api_error("没有进行中的目标机会话", 404, code="NO_BRIDGE")
    if not isinstance(events, list):
        return api_error("events 必须是数组", 400, code="INVALID_ARGUMENT")
    try:
        bridge.send_input(events)
    except Exception as exc:  # noqa: BLE001
        log.warning("转发输入失败 sid=%s: %s", sid, exc)
        return api_error(f"转发输入失败：{exc}", 500, code="INPUT_FAILED")
    return api_ok({"forwarded": len(events)})


@bp.post("/disconnect")
@permission_required("rdp:use")
def gateway_disconnect_target():
    payload = request.get_json(silent=True) or {}
    sid = str(payload.get("sid") or "")
    closed = window_bridge.close_bridge(sid)
    if closed:
        log_event(
            "session",
            "rdp_gateway_close",
            message=f"关闭 RDP 网关会话 {sid} 的目标机连接",
            actor=load_actor(),
            ip=(request.remote_addr or ""),
        )
    return api_ok({"closed": closed})
