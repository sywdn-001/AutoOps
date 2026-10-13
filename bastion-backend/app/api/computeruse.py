"""ComputerUse（Windows-MCP）部署与调用 API。

分成三块：

1. **准备（部署向导第 1 步）**：`/versions` 列出华为云镜像里可下载的 Windows amd64
   Python 安装包，`POST /installers` 把它缓存到堡垒机，`/installers/<file>` 既能给
   管理员下载、也能给**目标机**用带签名的地址拉走（目标机没有 JWT，所以用
   HMAC + 过期时间，见 `app/computeruse/deploy.py` 的 `signed_download_url`）。
2. **部署（第 2、3 步）**：`POST /deployments` 建一行并起后台线程，
   `GET /deployments/<id>/logs` 是 SSE 实时日志（按 `since` 续传，刷新页面不丢），
   还能取消、重试、删除记录。
3. **使用（侧边栏）**：`/status` 看这台机器有没有就绪的 ComputerUse，
   `/mcp/tools` 拿目标机上的工具清单，`POST /mcp/call` 直接调一个 MCP 工具
   （`Screenshot` 会返回图片给侧边栏内联渲染），`POST /mcp/probe` 重新探活。

每次真正调用 MCP 都会落一条审计（`computeruse` / `mcp_call`），带上主机、工具名、
参数摘要、是否只读、耗时与结果 —— 这样「AI 用鼠标键盘操作了哪台机器」事后查得到。
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, current_app, request, send_file, stream_with_context

from ..audit import log_event
from ..computeruse import deploy as deploy_mod
from ..computeruse import mcp as mcp_mod
from ..computeruse import mirror
from ..crypto import decrypt
from ..extensions import db
from ..models import ComputerUseDeployment, Host, utcnow
from ..security import has_permission, is_admin, load_actor, permission_required
from ..utils import api_error, api_ok, get_client_ip, get_user_agent, parse_bool, parse_int

logger = logging.getLogger(__name__)

bp = Blueprint("computeruse", __name__, url_prefix="/api/computer-use")

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

CU_PERMISSION_LABELS = {
    "computeruse:view": "查看 ComputerUse 状态",
    "computeruse:deploy": "部署 ComputerUse",
    "computeruse:use": "用 ComputerUse 操作主机",
}


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _sse(payload: dict) -> str:
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _row_or_404(deployment_id: int) -> ComputerUseDeployment:
    row = db.session.get(ComputerUseDeployment, int(deployment_id))
    if row is None:
        raise LookupError("部署记录不存在")
    return row


def _ready_row(host_id: int) -> ComputerUseDeployment | None:
    """这台主机上「已经部署好、可以直接用」的那一条记录。"""
    return (
        ComputerUseDeployment.query.filter_by(host_id=int(host_id), status="success")
        .order_by(ComputerUseDeployment.id.desc())
        .first()
    )


def _client_for(row: ComputerUseDeployment, *, timeout: float = 60.0) -> mcp_mod.McpClient:
    if not row.mcp_url:
        raise LookupError(f"{row.host_name} 还没有可用的 MCP 地址，请先完成部署")
    auth_key = decrypt(row.mcp_auth_key_enc) if row.mcp_auth_key_enc else ""
    return mcp_mod.McpClient(row.mcp_url, auth_key=auth_key, timeout=timeout)


def _host_or_404(host_id: int) -> Host:
    host = db.session.get(Host, int(host_id))
    if host is None:
        raise LookupError("主机不存在")
    return host


def _payload() -> dict:
    return request.get_json(silent=True) or {}


def _resolve_host_id(payload: dict) -> int | None:
    """``hostId`` 或 ``deploymentId`` 二选一定位主机。"""
    host_id = parse_int(payload.get("hostId"))
    if host_id:
        return host_id
    deployment_id = parse_int(payload.get("deploymentId"))
    if deployment_id:
        row = db.session.get(ComputerUseDeployment, deployment_id)
        if row is not None:
            return row.host_id
    return None


def _bastion_url(payload: dict, host_address: str = "") -> str:
    """目标机要拉安装包时用的堡垒机地址：优先用请求里手工指定的。

    如果操作员是在堡垒机本机用 ``127.0.0.1`` 打开的控制台，请求里的 Host 目标机根本
    回连不到，这时换成「本机默认路由用的那个网卡地址」，协议与端口照抄请求。
    """
    explicit = (payload.get("bastionUrl") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    parts = urlsplit(request.host_url)
    if (parts.hostname or "") in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        lan_ip = _detect_bastion_ip(host_address)
        if lan_ip:
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{lan_ip}{port}"
    return request.host_url.rstrip("/")


def _detect_bastion_ip(host_address: str) -> str:
    """猜一个「目标机能回连到的堡垒机 IP」：取本机默认路由用的那个地址。"""
    import socket

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(1.5)
        probe.connect((host_address or "192.168.0.1", 9))
        ip = probe.getsockname()[0]
        probe.close()
        return "" if ip.startswith("127.") else ip
    except Exception:  # noqa: BLE001 - 猜不到就让上层退化成镜像站直下
        return ""


def _snapshot(row: ComputerUseDeployment) -> dict:
    return deploy_mod.deployment_snapshot(row, deploy_mod.get_runner(row.id))


# --------------------------------------------------------------------------- #
# 安装包：镜像清单 / 缓存 / 下载
# --------------------------------------------------------------------------- #
@bp.get("/versions")
@permission_required("computeruse:deploy")
def list_versions():
    """华为云镜像里「真的能下到 Windows amd64 安装包」的版本清单。"""
    refresh = parse_bool(request.args.get("refresh"))
    probe = parse_int(request.args.get("probe")) or mirror.PROBE_VERSIONS
    try:
        data = mirror.list_versions(refresh=refresh, probe=probe)
    except mirror.MirrorError as exc:
        return api_error(str(exc), 502, code="MIRROR_FAILED")
    data["cached"] = _cached_installers()
    return api_ok(data)


@bp.get("/installers")
@permission_required("computeruse:deploy")
def list_installers():
    return api_ok({"items": _cached_installers(), "dir": str(mirror.INSTALLER_DIR)})


def _cached_installers() -> list[dict]:
    items = []
    for path in sorted(mirror.INSTALLER_DIR.glob("python-*-amd64.exe")):
        state = mirror.cached_state(path.name) or {}
        meta = {}
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        if meta_path.exists():
            import json

            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 元数据坏了不影响下载
                meta = {}
        items.append(
            {
                "filename": path.name,
                "version": meta.get("version", ""),
                "size": state.get("size") or path.stat().st_size,
                "sha256": state.get("sha256", ""),
                "cachedAt": state.get("cachedAt", ""),
                "path": str(path),
            }
        )
    return items


@bp.post("/installers")
@permission_required("computeruse:deploy")
def cache_installer_api():
    """把选中的安装包缓存到堡垒机（下载 + 记 SHA256）。"""
    payload = _payload()
    version = (payload.get("version") or "").strip()
    filename = (payload.get("filename") or "").strip()
    if not version or not filename:
        return api_error("请提供 version 与 filename", 400, code="BAD_REQUEST")
    try:
        result = mirror.cache_installer(version, filename)
    except mirror.MirrorError as exc:
        return api_error(str(exc), 502, code="CACHE_FAILED")
    log_event(
        "computeruse",
        "cache_installer",
        message=f"缓存 Python 安装包 {filename}（{result.get('size', 0) / 1024 / 1024:.1f} MB）到堡垒机",
        target_type="installer",
        target_id=filename,
        target_name=filename,
        detail={"version": version, "skipped": result.get("skipped"), "sha256": result.get("sha256")},
    )
    return api_ok(result, message="已缓存" if not result.get("skipped") else "本地已有，跳过下载")


@bp.get("/installers/<path:filename>")
def download_installer(filename: str):
    """给目标机（带 HMAC 签名）或管理员（带 JWT）下载安装包。"""
    name = Path(filename).name
    if name != filename or not name.startswith("python-"):
        return api_error("文件名不合法", 400, code="BAD_FILENAME")
    path = mirror.local_installer_path(name)
    if not path.exists():
        return api_error(f"堡垒机上没有 {name}", 404, code="NOT_FOUND")

    signed = deploy_mod.verify_download_signature(
        name,
        request.args.get("e", ""),
        request.args.get("s", ""),
        str(current_app.config.get("SECRET_KEY") or ""),
    )
    if not signed:
        actor = None
        try:
            # 目标机走签名；管理员可以直接用 JWT 下（没带令牌就按匿名处理，下面会 403）
            from flask_jwt_extended import verify_jwt_in_request

            verify_jwt_in_request(optional=True)
            actor = load_actor()
        except Exception:  # noqa: BLE001 - 令牌无效/缺失都当匿名
            actor = None
        allowed = actor is not None and (
            is_admin(actor) or has_permission(actor, "computeruse:deploy")
        )
        if not allowed:
            return api_error("链接已过期或签名不对（管理员请先登录）", 403, code="FORBIDDEN")
        log_event(
            "computeruse",
            "download_installer",
            message=f"{getattr(actor, 'username', '')} 下载安装包 {name}",
            target_type="installer",
            target_id=name,
            target_name=name,
        )
    return send_file(path, as_attachment=True, download_name=name, conditional=True)


@bp.delete("/installers/<path:filename>")
@permission_required("computeruse:deploy")
def delete_installer(filename: str):
    name = Path(filename).name
    if name != filename:
        return api_error("文件名不合法", 400, code="BAD_FILENAME")
    path = mirror.local_installer_path(name)
    if not path.exists():
        return api_error(f"堡垒机上没有 {name}", 404, code="NOT_FOUND")
    size = path.stat().st_size
    path.unlink()
    for suffix in (".sha256", ".meta.json"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()
    log_event(
        "computeruse",
        "delete_installer",
        message=f"删除堡垒机上的 Python 安装包 {name}",
        target_type="installer",
        target_id=name,
        target_name=name,
        detail={"size": size},
    )
    return api_ok(None, message="已删除")


# --------------------------------------------------------------------------- #
# 主机与部署记录
# --------------------------------------------------------------------------- #
@bp.get("/hosts")
@permission_required("computeruse:deploy")
def list_hosts():
    """能作为 ComputerUse 目标的主机（必须已经有 WinRM 端点）。"""
    items = []
    for host in Host.query.order_by(Host.id).all():
        endpoint = host.endpoint_for("winrm")
        latest = (
            ComputerUseDeployment.query.filter_by(host_id=host.id)
            .order_by(ComputerUseDeployment.id.desc())
            .first()
        )
        accounts = [
            {
                "id": account.id,
                "name": account.name,
                "username": account.username,
                "authType": account.auth_type or "password",
            }
            for account in host.accounts
            if (account.auth_type or "password") == "password"
        ]
        items.append(
            {
                "id": host.id,
                "name": host.name,
                "address": host.address,
                "osType": host.os_type,
                "hasWinrm": endpoint is not None,
                "winrmPort": endpoint.port if endpoint else None,
                "winrmTransport": endpoint.winrm_transport if endpoint else "",
                "accounts": accounts,
                "deployment": latest.to_dict() if latest else None,
                "ready": bool(latest and latest.status == "success" and latest.mcp_url),
            }
        )
    return api_ok({"items": items, "minPython": ".".join(str(part) for part in mirror.MCP_MIN_VERSION)})


@bp.get("/deployments")
@permission_required("computeruse:view")
def list_deployments():
    host_id = parse_int(request.args.get("hostId"))
    query = ComputerUseDeployment.query
    if host_id:
        query = query.filter_by(host_id=host_id)
    rows = query.order_by(ComputerUseDeployment.id.desc()).limit(50).all()
    return api_ok({"items": [_snapshot(row) for row in rows]})


@bp.post("/deployments")
@permission_required("computeruse:deploy")
def create_deployment():
    """建一条部署记录并立刻起后台线程（前端拿 id 去订阅日志流）。"""
    payload = _payload()
    host_id = parse_int(payload.get("hostId"))
    if not host_id:
        return api_error("请选择要部署的目标主机", 400, code="BAD_REQUEST")
    host = db.session.get(Host, host_id)
    if host is None:
        return api_error("主机不存在", 404, code="NOT_FOUND")
    endpoint = host.endpoint_for("winrm")
    if endpoint is None:
        return api_error(
            f"{host.name} 没有 WinRM 端点：请先在「资产管理 → 编辑主机」里勾上 Windows 命令行（WinRM）",
            400,
            code="NO_WINRM",
        )

    running = ComputerUseDeployment.query.filter_by(host_id=host_id, status="running").first()
    if running is not None:
        if deploy_mod.get_runner(running.id) is not None:
            return api_error(
                f"{host.name} 已有一个部署在进行中（#{running.id}），请等它结束或先取消",
                409,
                code="ALREADY_RUNNING",
            )
        # 没有活着的运行器 = 后端重启或线程异常退出留下的僵尸记录，别让它永远堵着这台主机。
        running.status = "failed"
        running.last_error = "部署线程已不在（后端重启或异常退出），本次部署被判定为中断"
        running.finished_at = utcnow()
        db.session.commit()
        log_event(
            "computeruse",
            "deploy_stale",
            result="failure",
            message=f"清理僵尸部署 #{running.id}（{host.name}）：运行线程已不存在",
            target_type="host",
            target_id=host.id,
            target_name=host.name,
            detail={"deploymentId": running.id},
        )

    version = (payload.get("pythonVersion") or "").strip()
    filename = (payload.get("installerName") or "").strip()
    if not version or not filename:
        return api_error("请先选择 Python 版本（第 1 步）", 400, code="NO_INSTALLER")
    cached = mirror.local_installer_path(filename)
    if not cached.exists():
        return api_error(f"堡垒机上还没有 {filename}，请先在第 1 步缓存", 400, code="NOT_CACHED")

    account_id = parse_int(payload.get("accountId"))
    account = None
    if account_id:
        account = next((item for item in host.accounts if item.id == account_id), None)
        if account is None:
            return api_error("选中的资产账号不属于这台主机", 400, code="BAD_ACCOUNT")
    else:
        account = next(
            (item for item in host.accounts if (item.auth_type or "password") == "password"), None
        )
    if account is None:
        return api_error(
            f"{host.name} 没有可用的口令账号：WinRM 不接受私钥登录，请先加一个「用户名 + 口令」账号",
            400,
            code="NO_ACCOUNT",
        )

    actor = load_actor()
    options = payload.get("options") or {}
    mcp_port = parse_int(options.get("mcpPort")) or 8000
    if not 1 <= mcp_port <= 65535:
        return api_error("MCP 端口必须在 1-65535 之间", 400, code="BAD_PORT")
    options = {
        "mcpPort": mcp_port,
        "pipIndex": (options.get("pipIndex") or "").strip(),
        "bindAll": parse_bool(options.get("bindAll")),
        "portproxy": not parse_bool(options.get("bindAll")),
        "authKey": (options.get("authKey") or "").strip(),
    }

    row = ComputerUseDeployment(
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        account_id=account.id,
        account_username=account.username,
        status="pending",
        stage="queued",
        progress=0,
        python_version=version,
        installer_name=filename,
        installer_size=cached.stat().st_size,
        installer_sha256=mirror.read_sha256(filename),
        mcp_port=mcp_port,
        options=options,
        bastion_url=_bastion_url(payload, host.address),
        deployed_by=getattr(actor, "username", "") or "",
        deployed_by_id=getattr(actor, "id", None),
    )
    db.session.add(row)
    db.session.commit()

    bastion_url = row.bastion_url
    bastion_ip = _detect_bastion_ip(host.address)
    if not bastion_ip:
        # 退一步：把请求头里的 Host 当堡垒机地址（同机部署时通常是对的）
        host_header = (request.host or "").split(":")[0]
        bastion_ip = "" if host_header in ("", "localhost", "127.0.0.1") else host_header
    ctx = deploy_mod.DeployContext(
        deployment_id=row.id,
        host_id=host.id,
        account_id=account.id,
        python_version=version,
        installer_name=filename,
        options=options,
        bastion_url=bastion_url,
        bastion_ip=bastion_ip,
        host_name=host.name,
        host_address=host.address,
        deployed_by=row.deployed_by,
        auth_key=options["authKey"],
        installer_size=row.installer_size or 0,
        installer_sha256=row.installer_sha256 or "",
    )
    deploy_mod.start_deployment(current_app._get_current_object(), row.id, ctx)
    return api_ok(_snapshot(row), message=f"已开始部署（#{row.id}）")


@bp.get("/deployments/<int:deployment_id>")
@permission_required("computeruse:view")
def get_deployment(deployment_id: int):
    try:
        row = _row_or_404(deployment_id)
    except LookupError as exc:
        return api_error(str(exc), 404, code="NOT_FOUND")
    return api_ok(_snapshot(row))


@bp.get("/deployments/<int:deployment_id>/logs")
@permission_required("computeruse:view")
def deployment_logs(deployment_id: int):
    """SSE 实时日志：``?since=<seq>`` 续传，刷新页面不会丢。"""
    try:
        row = _row_or_404(deployment_id)
    except LookupError as exc:
        return api_error(str(exc), 404, code="NOT_FOUND")
    since = parse_int(request.args.get("since"), 0) or 0
    runner = deploy_mod.get_runner(deployment_id)
    initial = _snapshot(row)
    stored_tail = row.log_tail or ""

    if runner is None:
        # 进程重启过（或这是很久以前的记录）：只回放库里存的摘要
        def replay():
            yield _sse({"type": "hello", "deployment": initial})
            for line in stored_tail.splitlines():
                yield _sse({"type": "log", "event": {"seq": 0, "level": "info", "message": line}})
            yield _sse({"type": "end", "status": initial.get("status")})

        return Response(stream_with_context(replay()), mimetype="text/event-stream", headers=SSE_HEADERS)

    def stream():
        yield _sse({"type": "hello", "deployment": initial})
        cursor = since
        while True:
            events = runner.wait_for_events(cursor, timeout=20.0)
            for event in events:
                cursor = event.seq
                yield _sse({"type": "log", "event": event.to_dict()})
            if not events:
                yield ": ping\n\n"
            snapshot = runner.snapshot()
            yield _sse({"type": "status", **snapshot})
            if snapshot["status"] in ("success", "failed", "cancelled"):
                yield _sse({"type": "end", "status": snapshot["status"]})
                return

    return Response(stream_with_context(stream()), mimetype="text/event-stream", headers=SSE_HEADERS)


@bp.post("/deployments/<int:deployment_id>/cancel")
@permission_required("computeruse:deploy")
def cancel_deployment(deployment_id: int):
    try:
        row = _row_or_404(deployment_id)
    except LookupError as exc:
        return api_error(str(exc), 404, code="NOT_FOUND")
    runner = deploy_mod.get_runner(deployment_id)
    if runner is None or not runner.cancel():
        return api_error("这次部署已经结束，无法取消", 409, code="NOT_RUNNING")
    log_event(
        "computeruse",
        "deploy_cancel_request",
        message=f"请求取消 {row.host_name} 的 ComputerUse 部署（#{row.id}）",
        target_type="host",
        target_id=row.host_id,
        target_name=row.host_name,
    )
    return api_ok(_snapshot(row), message="已请求取消")


@bp.delete("/deployments/<int:deployment_id>")
@permission_required("computeruse:deploy")
def delete_deployment(deployment_id: int):
    try:
        row = _row_or_404(deployment_id)
    except LookupError as exc:
        return api_error(str(exc), 404, code="NOT_FOUND")
    runner = deploy_mod.get_runner(deployment_id)
    if runner is not None and runner.status == "running":
        return api_error("部署还在跑，先取消再删除", 409, code="STILL_RUNNING")
    host_name = row.host_name
    db.session.delete(row)
    db.session.commit()
    log_event(
        "computeruse",
        "deployment_delete",
        message=f"删除 {host_name} 的 ComputerUse 部署记录（#{deployment_id}，目标机上的 MCP 不受影响）",
        target_type="host",
        target_name=host_name,
    )
    return api_ok(None, message="记录已删除（目标机上的 MCP 仍在运行）")


# --------------------------------------------------------------------------- #
# 侧边栏：状态 / 工具 / 调用
# --------------------------------------------------------------------------- #
@bp.get("/status")
@permission_required("computeruse:view")
def computeruse_status():
    host_id = parse_int(request.args.get("hostId"))
    if not host_id:
        return api_error("缺少 hostId", 400, code="BAD_REQUEST")
    try:
        host = _host_or_404(host_id)
    except LookupError as exc:
        return api_error(str(exc), 404, code="NOT_FOUND")
    row = _ready_row(host_id)
    latest = (
        ComputerUseDeployment.query.filter_by(host_id=host_id)
        .order_by(ComputerUseDeployment.id.desc())
        .first()
    )
    data = {
        "hostId": host.id,
        "hostName": host.name,
        "hostAddress": host.address,
        "ready": bool(row and row.mcp_url),
        "deployment": _snapshot(row or latest) if (row or latest) else None,
    }
    if row is not None:
        data.update(
            {
                "deploymentId": row.id,
                "mcpUrl": row.mcp_url,
                "mcpHost": row.mcp_host,
                "mcpPort": row.mcp_port,
                "mcpVersion": row.mcp_version,
                "pythonPath": row.python_path,
                "toolCount": row.mcp_tool_count or 0,
                "tools": row.mcp_tools or [],
                "hasAuthKey": bool(row.mcp_auth_key_enc),
                "portproxy": bool(row.portproxy),
                "startupPath": row.startup_path,
            }
        )
    return api_ok(data)


@bp.get("/mcp/tools")
@permission_required("computeruse:view")
def mcp_tools():
    host_id = parse_int(request.args.get("hostId"))
    live = parse_bool(request.args.get("live"))
    if not host_id:
        return api_error("缺少 hostId", 400, code="BAD_REQUEST")
    row = _ready_row(host_id)
    if row is None:
        return api_error("这台主机还没有部署好 ComputerUse", 400, code="NOT_DEPLOYED")
    if not live:
        return api_ok({"items": row.mcp_tools or [], "live": False, "mcpUrl": row.mcp_url})
    try:
        client = _client_for(row)
        tools = [
            {
                **tool,
                "observeOnly": tool["name"] in mcp_mod.OBSERVE_TOOLS,
                "mutating": tool["name"] in mcp_mod.MUTATING_TOOLS,
            }
            for tool in client.list_tools()
        ]
    except mcp_mod.McpError as exc:
        return api_error(str(exc), 502, code="MCP_FAILED")
    return api_ok({"items": tools, "live": True, "mcpUrl": row.mcp_url})


@bp.post("/mcp/probe")
@permission_required("computeruse:view")
def mcp_probe():
    payload = _payload()
    host_id = _resolve_host_id(payload)
    if not host_id:
        return api_error("缺少 hostId", 400, code="BAD_REQUEST")
    row = (
        _row_or_404(parse_int(payload.get("deploymentId")))
        if parse_int(payload.get("deploymentId"))
        else _ready_row(host_id)
    )
    if row is None:
        return api_error("这台主机还没有部署好 ComputerUse", 400, code="NOT_DEPLOYED")
    try:
        client = _client_for(row)
        probe = client.probe()
    except (mcp_mod.McpError, LookupError) as exc:
        log_event(
            "computeruse",
            "mcp_probe",
            result="failure",
            message=f"{row.host_name} 的 MCP 探活失败：{exc}",
            target_type="host",
            target_id=row.host_id,
            target_name=row.host_name,
        )
        return api_error(f"MCP 探活失败：{exc}", 502, code="MCP_FAILED")
    if probe.get("ok"):
        # 只有探活成功才回写：失败时把已有记录擦成 0 个工具，侧边栏会以为这台机器没工具可调
        row.mcp_tool_count = probe.get("toolCount") or 0
        row.mcp_tools = probe.get("tools") or []
        row.mcp_url = probe.get("endpoint") or row.mcp_url
        db.session.commit()
    log_event(
        "computeruse",
        "mcp_probe",
        result="success" if probe.get("ok") else "failure",
        message=f"{row.host_name} 的 MCP 探活{'成功' if probe.get('ok') else '失败'}："
        f"{probe.get('toolCount')} 个工具",
        target_type="host",
        target_id=row.host_id,
        target_name=row.host_name,
        detail={"url": row.mcp_url, "latencyMs": probe.get("latencyMs"), "error": probe.get("error")},
    )
    return api_ok(probe)


@bp.post("/mcp/restart")
@permission_required("computeruse:use")
def mcp_restart():
    """重启目标机上的 MCP 服务（卡死自愈）。

    真机实测：``Snapshot(use_ui_tree=True)`` 会让 windows-mcp 死等 UIA，之后**整台机器的
    MCP 都不再回应**（连 ``Screenshot`` 都挂死），只能杀掉那个 python 进程重新起。
    这条路由走 WinRM + 计划任务，**不经过 MCP 本身**（它已经不能用了），失败也回 200 +
    可读原因，避免「修复失败」再给前端一个 5xx。
    """
    payload = _payload()
    host_id = _resolve_host_id(payload)
    if not host_id:
        return api_error("缺少 hostId", 400, code="BAD_REQUEST")
    row = _ready_row(host_id)
    if row is None:
        return api_error("这台主机还没有部署好 ComputerUse", 400, code="NOT_DEPLOYED")
    port = (urlsplit(row.mcp_url).port if row.mcp_url else None) or deploy_mod.DEFAULT_MCP_PORT
    actor = load_actor()
    username = getattr(actor, "username", "") or ""
    started = time.time()
    result = deploy_mod.restart_target_mcp(
        current_app._get_current_object(),
        host_id=row.host_id,
        account_id=row.account_id,
        port=port,
    )
    probe: dict = {}
    if result.get("ok"):
        # 重启完立刻探一次活：这才能证明「MCP 真的又能用了」，而不是只是端口起来了
        try:
            probe = _client_for(row, timeout=20.0).probe()
        except (mcp_mod.McpError, LookupError) as exc:
            probe = {"ok": False, "error": str(exc)}
        if probe.get("ok"):
            row.mcp_tool_count = probe.get("toolCount") or 0
            row.mcp_tools = probe.get("tools") or []
            row.mcp_url = probe.get("endpoint") or row.mcp_url
            db.session.commit()
    result["probe"] = probe
    log_event(
        "computeruse",
        "mcp_restart",
        result="success" if result.get("ok") and probe.get("ok", True) else "failure",
        message=(
            f"{username} 重启 {row.host_name} 的 MCP 服务"
            f"{'成功' if result.get('ok') else '失败'}"
            + (f"：{result.get('error')}" if result.get("error") else "")
        ),
        target_type="host",
        target_id=row.host_id,
        target_name=row.host_name,
        actor_username=username,
        ip=get_client_ip(),
        user_agent=get_user_agent(),
        detail={
            "port": port,
            "killedPid": result.get("killedPid"),
            "ownerPid": result.get("ownerPid"),
            "listen": result.get("listen"),
            "logon": result.get("logon"),
            "desktopActive": result.get("desktopActive"),
            "error": result.get("error"),
            "probeOk": probe.get("ok"),
            "toolCount": probe.get("toolCount"),
            "durationMs": int((time.time() - started) * 1000),
        },
    )
    if not result.get("ok"):
        message = f"重启 MCP 失败：{result.get('error') or '原因未知'}"
    elif probe.get("ok"):
        message = f"已重启目标机上的 MCP（探活通过，{probe.get('toolCount')} 个工具）"
    else:
        message = "已重启目标机上的 MCP，但探活没通过"
    if result.get("ok") and not result.get("desktopActive"):
        # 会话 0 里没有桌面，截图必然是空的 —— 与其让用户猜，不如直接说清楚
        message += "；目标机当前没人登录桌面，MCP 跑在会话 0，截图会是空的（在目标机上登录桌面后重启一次即可）"
    return api_ok(result, message=message)


def _normalize_observe_args(
    tool_name: str, arguments: dict, *, allow_ui_tree: bool = False
) -> tuple[dict, list[str]]:
    """修正「看一眼」类工具的参数（真机踩过的坑，见 ``computeruse/mcp.py`` 顶部注释）。

    * ``Snapshot(use_ui_tree=True)`` 会让 windows-mcp 死等 UIA，之后**整台机器的 MCP
      都不再回应**（连 Screenshot 都挂死），只能重启 MCP 才恢复 —— 所以默认把元素树关掉，
      只回截图；确实要看元素树得由调用方显式传 ``allowUiTree``。
    * ``use_dom`` 依赖元素树；``use_vision`` 不显式打开时服务端不回图片。
    * 真机的 ``Wait`` 参数是 ``duration``（秒），模型习惯写 ``seconds``。
    """
    fixed = dict(arguments or {})
    notes: list[str] = []
    if tool_name == "Snapshot":
        if fixed.get("use_dom") and not fixed.get("use_ui_tree"):
            fixed.pop("use_dom", None)
            notes.append("use_dom 需要 use_ui_tree，已忽略")
        if fixed.get("use_ui_tree"):
            if allow_ui_tree:
                notes.append("按调用方要求取 UI 元素树（真机上可能把 MCP 卡住）")
            else:
                fixed["use_ui_tree"] = False
                notes.append("已关掉 use_ui_tree：真机上取 UI 元素树会把 MCP 卡死，改成只回截图")
        fixed.setdefault("use_vision", True)
    if tool_name == "Wait" and "duration" not in fixed and "seconds" in fixed:
        fixed["duration"] = fixed.pop("seconds")
        notes.append("seconds 已按真机参数名改成 duration")
    return fixed, notes


@bp.post("/mcp/call")
@permission_required("computeruse:use")
def mcp_call():
    """调一个 MCP 工具。观察类（Screenshot / Snapshot …）会返回图片给侧边栏渲染。"""
    payload = _payload()
    host_id = _resolve_host_id(payload)
    tool_name = (payload.get("tool") or "").strip()
    arguments = payload.get("arguments") or {}
    if not host_id or not tool_name:
        return api_error("请提供 hostId 与 tool", 400, code="BAD_REQUEST")
    row = _ready_row(host_id)
    if row is None:
        return api_error("这台主机还没有部署好 ComputerUse", 400, code="NOT_DEPLOYED")
    if not isinstance(arguments, dict) or not arguments:
        # AI 工具把 dict 参数展开进请求体（见 ai/tools.py:build_request），
        # 所以这里同时接受「arguments 嵌套」与「参数平铺在顶层」两种形状。
        reserved = {"hostId", "deploymentId", "tool", "timeout", "reason", "arguments", "allowUiTree"}
        flat = {key: value for key, value in payload.items() if key not in reserved}
        if flat:
            arguments = flat
        elif not isinstance(arguments, dict):
            return api_error("arguments 必须是对象", 400, code="BAD_ARGUMENTS")
        else:
            arguments = {}
    if tool_name not in mcp_mod.OBSERVE_TOOLS and tool_name not in mcp_mod.MUTATING_TOOLS:
        return api_error(f"不认识的 MCP 工具：{tool_name}", 400, code="UNKNOWN_TOOL")
    arguments, notes = _normalize_observe_args(
        tool_name, arguments, allow_ui_tree=parse_bool(payload.get("allowUiTree"))
    )

    actor = load_actor()
    username = getattr(actor, "username", "") or ""
    timeout = parse_int(payload.get("timeout")) or 60
    started = time.time()
    try:
        client = _client_for(row)
        result = client.call_tool(tool_name, arguments, timeout=float(timeout))
    except (mcp_mod.McpError, LookupError) as exc:
        log_event(
            "computeruse",
            "mcp_call",
            result="failure",
            message=f"{username} 调用 {row.host_name} 的 MCP 工具 {tool_name} 失败：{exc}",
            target_type="host",
            target_id=row.host_id,
            target_name=row.host_name,
            actor_username=username,
            detail={"tool": tool_name, "arguments": _trim_args(arguments), "error": str(exc)},
        )
        return api_error(f"调用 MCP 工具 {tool_name} 失败：{exc}", 502, code="MCP_CALL_FAILED")

    duration_ms = int((time.time() - started) * 1000)
    images = [
        {"mime": mime, "dataUrl": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
        for mime, data in mcp_mod.extract_images(result)
    ]
    text = mcp_mod.extract_text(result)
    observe_only = tool_name in mcp_mod.OBSERVE_TOOLS
    log_event(
        "computeruse",
        "mcp_call",
        message=(
            f"{username} 调用 {row.host_name} 的 MCP 工具 {tool_name}"
            f"（{'只读观察' if observe_only else '会操作桌面'}，{duration_ms} ms）"
        ),
        target_type="host",
        target_id=row.host_id,
        target_name=row.host_name,
        actor_username=username,
        ip=get_client_ip(),
        user_agent=get_user_agent(),
        detail={
            "tool": tool_name,
            "arguments": _trim_args(arguments),
            "observeOnly": observe_only,
            "images": len(images),
            "chars": len(text),
            "durationMs": duration_ms,
            "mcpUrl": row.mcp_url,
            "notes": notes,
        },
    )
    return api_ok(
        {
            "tool": tool_name,
            "observeOnly": observe_only,
            "mutating": tool_name in mcp_mod.MUTATING_TOOLS,
            "text": text,
            "images": images,
            "durationMs": duration_ms,
            "notes": notes,
            "raw": result,
        }
    )


def _trim_args(arguments: dict, limit: int = 400) -> dict:
    """审计里记参数，但别把超长的 base64 之类塞进去。"""
    import json

    trimmed = {}
    for key, value in list(arguments.items())[:20]:
        text = value if isinstance(value, (int, float, bool)) or value is None else str(value)
        if isinstance(text, str) and len(text) > limit:
            text = text[:limit] + "…"
        trimmed[key] = text
    if len(arguments) > 20:
        trimmed["_more"] = f"还有 {len(arguments) - 20} 个参数未记录"
    return json.loads(json.dumps(trimmed, ensure_ascii=False, default=str))
