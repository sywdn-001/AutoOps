"""WebRDP 接口：列出可远程桌面的主机、换一次性连接票据、会话录像的存取。

真正的隧道是 `/api/rdp/ws` 的 WebSocket（`app/rdp/proxy.py` 的 RDCleanPath 网关）。
这里做三件事：**用堡垒机的权限体系决定「谁能连哪台机器、用哪个账号」**、
**发一张短命的一次性票据**（WebSocket 只带票据 id，不带 token），
以及**会话录像的上传 / 列表 / 回看 / 删除**（浏览器端 MediaRecorder 录的 webm）。

口令为什么要交给浏览器（安全口径，写清楚）：
远程桌面的 CredSSP/NLA 必须在 RDP 客户端一侧完成 —— 浏览器里的 ironrdp-wasm 就是
那个客户端，它需要口令才能算出 NTLM 应答。所以本接口会把**该账号的资产口令**随票据
一起下发（`credential` 字段），并**专门写一条 `rdp_credential_reveal` 审计**留痕：
「谁在什么时候把哪台机器的哪个账号口令交给了浏览器」。这是「口令由堡垒机集中托管、
操作员不需要知道目标机口令」这个模型的必然代价；如果管理员要求口令绝不出服务器，
就得改成服务端 RDP 客户端（guacd / FreeRDP 那一路，把位图流回浏览器），本项目当前不做。

录像的落盘口径：文件名一律由服务端 uuid 生成，**不用请求里的任何字符串拼路径**；
`Content-Disposition: inline` + HTTP Range 支持是 `<video>` 能拖动进度条的前提。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta

from flask import Blueprint, Response, current_app, request
from flask_jwt_extended import create_access_token, decode_token, verify_jwt_in_request
from sqlalchemy import or_

from ..access import accessible_targets, serialize_target
from ..audit import log_event
from ..crypto import decrypt
from ..extensions import db
from ..models import Host, HostAccount, RdpRecording, User
from ..rdp.proxy import (
    RDP_SECURITY_DEFAULT,
    TICKETS,
    TICKET_TTL_SECONDS,
)
from ..security import (
    admin_required,
    has_any_permission,
    load_actor,
    login_required,
    permission_required,
)
from ..utils import api_error, api_list, api_ok, page_args, parse_datetime, parse_int

log = logging.getLogger("bastion.api.rdp")

bp = Blueprint("rdp", __name__, url_prefix="/api/rdp")

#: 远程桌面主体协议标识（主机 protocol 字段的取值）
RDP_PROTOCOL = "rdp"

#: 允许上传的录像容器；`application/octet-stream` 是浏览器没带类型时的兜底，统一存成 webm
ALLOWED_RECORDING_MIME = ("video/webm", "video/x-matroska")
FALLBACK_RECORDING_MIME = "video/webm"

#: 列表接口允许的可见性权限码：有其一即可看全部录像，否则只能看自己上传的
RECORDING_VIEW_ALL_PERMISSIONS = ("session:view_all", "audit:view")

#: 流式返回录像的分块大小（字节）
STREAM_CHUNK_SIZE = 256 * 1024

#: 回放票据的用途标记（写进 JWT 的 `scope` 声明，防止登录令牌被拿来当回放票）
RECORDING_TICKET_SCOPE = "rdp-recording"

#: 回放票据有效期（秒）：够看完一段录像，又不至于成为长期有效的下载链接
RECORDING_TICKET_TTL_SECONDS = 600


def _client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "")


@bp.get("/targets")
@permission_required("rdp:use")
def rdp_targets():
    """当前账号可远程桌面的主机（只含协议为 rdp 且在授权范围内的机器）。"""
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    targets = accessible_targets(actor, protocols=(RDP_PROTOCOL,))
    return api_ok([serialize_target(item) for item in targets])


@bp.post("/sessions")
@permission_required("rdp:use")
def create_rdp_session():
    """换一次性票据：`{hostId, accountId?}` → `{ticket, wsPath, expiresIn}`。

    准入判定与终端完全同源（`accessible_targets` 的授权、时段、配额都在里面），
    另外要求该授权打开交互式登录开关（`canWebterm`，即「允许在这台机器上开会话」），
    账号也必须在授权范围内 —— 不允许静默换用别的账号。
    """
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    payload = request.get_json(silent=True) or {}
    host_id = parse_int(payload.get("hostId") if "hostId" in payload else payload.get("host_id"))
    if not host_id:
        return api_error("请指定要连接的主机", 400, code="INVALID_ARGUMENT")

    targets = {item["hostId"]: item for item in accessible_targets(actor, protocols=(RDP_PROTOCOL,))}
    target = targets.get(host_id)
    if target is None:
        return api_error(
            "该主机不是可远程桌面的主机（协议须为 rdp），或你没有访问权限",
            403,
            code="FORBIDDEN",
        )
    if not target.get("canWebterm"):
        return api_error("该授权不允许交互式登录，请联系管理员", 403, code="FORBIDDEN")

    accounts = target.get("accounts") or []
    if not accounts:
        return api_error("该主机下没有可用账号，请联系管理员配置资产账号", 400, code="INVALID_ARGUMENT")
    account_id = parse_int(payload.get("accountId") if "accountId" in payload else payload.get("account_id"))
    if account_id:
        account = next((item for item in accounts if item["id"] == account_id), None)
        if account is None:
            return api_error("该账号不在你的授权范围内", 403, code="FORBIDDEN")
    else:
        account = accounts[0]

    host = target["host"]
    record = db.session.get(HostAccount, account["id"])
    if record is None:
        return api_error("账号不存在或已被删除", 400, code="INVALID_ARGUMENT")
    if (record.auth_type or "password") != "password":
        return api_error(
            "该账号使用密钥认证，Windows 远程桌面需要口令账号，请为该主机配置口令账号",
            400,
            code="INVALID_ARGUMENT",
        )
    secret = decrypt(record.secret_enc)
    if not secret:
        return api_error(
            "该账号没有可用口令（可能是密钥认证或口令未保存），请联系管理员配置",
            400,
            code="INVALID_ARGUMENT",
        )

    ticket = TICKETS.issue(
        user_id=actor.id,
        username=actor.username,
        role_code=actor.role_code or "",
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        port=int(host.port or 3389),
        account_id=account["id"],
        account_username=account["username"] or "",
        grant_id=(target.get("grantIds") or [None])[0],
        client_ip=_client_ip(),
        security=(host.rdp_security or RDP_SECURITY_DEFAULT),
    )
    log_event(
        "session",
        "rdp_ticket",
        message=(
            f"{actor.username} 申请 {host.name} 的远程桌面连接票据"
            f"（账号 {account['username']}）"
        ),
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        actor_id=actor.id,
        actor_username=actor.username,
        actor_role=actor.role_code or "",
        ip=_client_ip(),
        detail={
            "protocol": RDP_PROTOCOL,
            "account": account["username"],
            "destination": f"{host.address}:{host.port}",
            "ticketTtl": TICKET_TTL_SECONDS,
            "rdpSecurity": host.rdp_security or RDP_SECURITY_DEFAULT,
        },
    )
    log.info("签发 RDP 票据 user=%s host=%s account=%s", actor.username, host.name, account["username"])
    # 口令交给浏览器是 RDP 客户端的硬约束（CredSSP/NLA 在客户端侧算），单独留一条审计：
    # 审计中心能查到「谁在什么时候把哪台机器的哪个账号口令交给了浏览器」。
    log_event(
        "session",
        "rdp_credential_reveal",
        message=(
            f"{actor.username} 打开 {host.name} 的远程桌面，"
            f"资产账号 {account['username']} 的口令已下发到浏览器（CredSSP 需要）"
        ),
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        actor_id=actor.id,
        actor_username=actor.username,
        actor_role=actor.role_code or "",
        ip=_client_ip(),
        detail={
            "protocol": RDP_PROTOCOL,
            "account": account["username"],
            "authType": record.auth_type,
            "ticket": ticket.id,
        },
    )
    return api_ok(
        {
            "ticket": ticket.id,
            "expiresIn": TICKET_TTL_SECONDS,
            "wsPath": f"/api/rdp/ws?ticket={ticket.id}",
            "protocol": RDP_PROTOCOL,
            "host": serialize_target(target),
            "account": {
                "id": account["id"],
                "name": account["name"],
                "username": account["username"],
            },
            "credential": {
                "username": record.username,
                "password": secret,
                "domain": "",
            },
        }
    )


# --------------------------------------------------------------------------- #
# 会话录像（WebRDP）
# --------------------------------------------------------------------------- #
def _recording_dir() -> str:
    """录像落盘目录；不存在就按需创建（与终端录像同一约定）。"""
    path = os.fspath(current_app.config["RDP_RECORDING_DIR"])
    os.makedirs(path, exist_ok=True)
    return path


def _recording_path(recording: RdpRecording) -> str:
    """录像在磁盘上的绝对路径。

    只拼服务端生成的 uuid 文件名；若该列被写入了路径分隔符（历史数据 / 人为改库），
    退回只取 basename，绝不允许越出录像目录。
    """
    name = os.path.basename(str(recording.filename or ""))
    return os.path.join(_recording_dir(), name)


def _recording_max_bytes() -> int:
    return int(current_app.config.get("RDP_RECORDING_MAX_MB", 512)) * 1024 * 1024


def _visible_all(actor) -> bool:
    """能否看全部人的录像：审计 / 全量会话查看权限，或管理员。"""
    return has_any_permission(actor, RECORDING_VIEW_ALL_PERMISSIONS)


def _recording_of(recording_id: int, actor):
    """按可见性取一条录像：``(recording, None)`` 或 ``(None, 错误响应)``。"""
    recording = db.session.get(RdpRecording, recording_id)
    if recording is None:
        return None, api_error("录像不存在", 404, code="NOT_FOUND")
    if not _visible_all(actor) and (recording.username or "") != (actor.username or ""):
        return None, api_error("无权查看该录像", 403, code="FORBIDDEN")
    return recording, None


def _iter_file(path: str, start: int, length: int, chunk_size: int = STREAM_CHUNK_SIZE):
    """按 ``[start, start+length)`` 分块读文件，读完即关（Response 迭代结束会关闭生成器）。"""
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单段 Range 头，返回 ``(start, 字节数)``。

    返回 ``None`` = 没有 Range 或格式不可解析 → 走 200 全量；
    返回 ``(-1, 0)`` = 起点越界 → 调用方回 416（RFC 9110 要求而不是静默截断）。
    """
    if not header:
        return None
    text = str(header).strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[len("bytes=") :].split(",")[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # `bytes=-N`：最后 N 个字节
            last = int(end_text)
            if last <= 0:
                return None
            start = max(0, size - last)
            return start, size - start
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except (TypeError, ValueError):
        return None
    if start >= size:
        return -1, 0
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end - start + 1


@bp.post("/recordings")
@permission_required("rdp:use")
def upload_recording():
    """上传一次远程桌面会话的录像（multipart/form-data，字段见模块 docstring / 前端契约）。

    准入与「换票据」同源：该 hostId 必须落在当前账号可远程桌面的目标里，
    否则任何人都能往任意主机名下塞录像。文件名由服务端 uuid 生成，与请求内容无关。
    """
    actor = load_actor()
    if actor is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")

    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return api_error("请选择要上传的录像文件", 400, code="INVALID_ARGUMENT")

    host_id = parse_int(request.form.get("hostId"))
    if not host_id:
        return api_error("请指定录像所属的主机", 400, code="INVALID_ARGUMENT")

    targets = {item["hostId"]: item for item in accessible_targets(actor, protocols=(RDP_PROTOCOL,))}
    target = targets.get(host_id)
    if target is None:
        return api_error(
            "该主机不是可远程桌面的主机（协议须为 rdp），或你没有访问权限",
            403,
            code="FORBIDDEN",
        )
    host = target["host"]

    mime = (upload.mimetype or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_RECORDING_MIME and mime != "application/octet-stream":
        return api_error(
            "只接受 webm / mkv 格式的会话录像",
            400,
            code="UNSUPPORTED_MEDIA_TYPE",
        )

    # 先量出文件本身的字节数（不含 multipart 分隔头），超限就直接拒绝、
    # 不让几百 MB 的数据落到磁盘上再回滚。
    size = 0
    try:
        upload.stream.seek(0, os.SEEK_END)
        size = upload.stream.tell()
        upload.stream.seek(0)
    except (AttributeError, OSError):  # pragma: no cover - 非 seekable 流的兜底
        size = request.content_length or 0
    if size <= 0:
        return api_error("录像文件为空", 400, code="INVALID_ARGUMENT")
    max_bytes = _recording_max_bytes()
    if size > max_bytes:
        return api_error(
            f"录像文件超过上限 {current_app.config.get('RDP_RECORDING_MAX_MB', 512)} MB",
            413,
            code="TOO_LARGE",
        )

    session_id = parse_int(request.form.get("sessionId"))
    if session_id is not None:
        from ..models import SessionRecord

        if db.session.get(SessionRecord, session_id) is None:
            session_id = None

    filename = f"{uuid.uuid4().hex}.webm"
    path = os.path.join(_recording_dir(), filename)
    upload.save(path)
    actual = os.path.getsize(path)
    if actual <= 0:
        try:
            os.remove(path)
        except OSError:  # pragma: no cover - 清理失败不影响错误返回
            log.exception("清理空录像文件失败 path=%s", path)
        return api_error("录像文件为空", 400, code="INVALID_ARGUMENT")
    if actual > max_bytes:  # pragma: no cover - Content-Length 缺失时的兜底
        try:
            os.remove(path)
        except OSError:
            log.exception("清理超限录像文件失败 path=%s", path)
        return api_error(
            f"录像文件超过上限 {current_app.config.get('RDP_RECORDING_MAX_MB', 512)} MB",
            413,
            code="TOO_LARGE",
        )

    account_username = (request.form.get("accountUsername") or "").strip()
    if not account_username:
        accounts = target.get("accounts") or []
        account_username = accounts[0]["username"] if accounts else ""
    recording = RdpRecording(
        session_id=session_id,
        host_id=host.id,
        host_name=host.name,
        host_address=host.address,
        username=actor.username,
        account_username=account_username,
        filename=filename,
        size_bytes=actual,
        duration_seconds=max(0, parse_int(request.form.get("durationSeconds"), 0) or 0),
        mime_type=FALLBACK_RECORDING_MIME,
        width=parse_int(request.form.get("width")),
        height=parse_int(request.form.get("height")),
        started_at=parse_datetime(request.form.get("startedAt")),
    )
    db.session.add(recording)
    db.session.commit()

    log_event(
        "session",
        "rdp_recording_saved",
        message=(
            f"{actor.username} 保存了 {host.name} 的远程桌面录像"
            f"（{actual / 1024:.0f} KB，{recording.duration_seconds or 0} 秒）"
        ),
        target_type="host",
        target_id=host.id,
        target_name=host.name,
        actor_id=actor.id,
        actor_username=actor.username,
        actor_role=actor.role_code or "",
        ip=_client_ip(),
        detail={
            "protocol": RDP_PROTOCOL,
            "recordingId": recording.id,
            "sizeBytes": actual,
            "durationSeconds": recording.duration_seconds or 0,
            "mimeType": recording.mime_type,
            "account": account_username,
        },
    )
    log.info("保存 RDP 录像 user=%s host=%s size=%s", actor.username, host.name, actual)
    return api_ok(recording.to_dict(), message="录像已保存", status=201)


@bp.get("/recordings")
@login_required
def list_recordings():
    """录像列表：审计 / 全量查看权限可见全部，其他人只看自己上传的。"""
    actor = load_actor()
    if actor is None:  # pragma: no cover - login_required 已挡掉
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    page, size = page_args()
    query = db.session.query(RdpRecording)
    if not _visible_all(actor):
        query = query.filter(RdpRecording.username == (actor.username or ""))

    host_id = parse_int(request.args.get("hostId"))
    if host_id:
        query = query.filter(RdpRecording.host_id == host_id)
    session_id = parse_int(request.args.get("sessionId"))
    if session_id:
        query = query.filter(RdpRecording.session_id == session_id)
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                RdpRecording.host_name.like(like),
                RdpRecording.host_address.like(like),
                RdpRecording.username.like(like),
                RdpRecording.account_username.like(like),
            )
        )

    total = query.count()
    rows = (
        query.order_by(RdpRecording.created_at.desc(), RdpRecording.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return api_list([item.to_dict() for item in rows], total, page, size)


def _actor_from_ticket_or_jwt(recording_id: int):
    """回放的两种鉴权方式，返回 ``(actor, via_ticket)``。

    为什么要有票据这条路：`<video src>` 发不出 `Authorization` 头（浏览器只在同源 XHR 里带
    自定义头），而录像动辄几十上百 MB、不能整包拉成 blob 再播，所以先在带 token 的请求里
    换一张短命票据，再让 `?ticket=` 走流式回放。

    注意：**票据这条路不能调用 `load_actor()`** —— 它内部走 `get_jwt_identity()`，而
    flask_jwt_extended 的 `get_jwt()` 在没跑过 `jwt_required()` / `verify_jwt_in_request()`
    的请求里会直接抛 `RuntimeError`，所以这里自己解 claims、自己取用户。
    """
    ticket = (request.args.get("ticket") or "").strip()
    if ticket:
        try:
            claims = decode_token(ticket)
        except Exception:  # noqa: BLE001 - 过期 / 伪造 / 结构损坏一律拒绝
            return None, True
        if not isinstance(claims, dict) or claims.get("scope") != RECORDING_TICKET_SCOPE:
            return None, True
        if parse_int(claims.get("recordingId")) != recording_id:
            return None, True
        user_id = parse_int(claims.get("sub"))
        user = db.session.get(User, user_id) if user_id else None
        if user is None or not user.is_active():
            return None, True
        return user, True

    try:
        verify_jwt_in_request(optional=True)
    except Exception:  # noqa: BLE001 - 令牌损坏就当没登录
        return None, False
    return load_actor(), False


@bp.post("/recordings/<int:recording_id>/ticket")
@login_required
def create_recording_ticket(recording_id: int):
    """签一张回放票据：10 分钟有效、绑定「这一条录像 + 这个用户」。"""
    actor = load_actor()
    if actor is None:  # pragma: no cover - login_required 已挡掉
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    recording, failure = _recording_of(recording_id, actor)
    if failure is not None:
        return failure
    if not os.path.isfile(_recording_path(recording)):
        return api_error("录像文件已丢失，请联系管理员", 404, code="FILE_MISSING")

    ticket = create_access_token(
        identity=str(actor.id),
        additional_claims={
            "scope": RECORDING_TICKET_SCOPE,
            "recordingId": recording.id,
            "username": actor.username,
        },
        expires_delta=timedelta(seconds=RECORDING_TICKET_TTL_SECONDS),
    )
    # 回看审计写在这里：拿到票就等于要看了。票在 10 分钟内可反复使用，
    # 播放器那一串 Range 请求就不会各写一条日志了 —— 一次回看不会刷出几十条 rdp_recording_viewed。
    log_event(
        "session",
        "rdp_recording_viewed",
        message=(
            f"{actor.username} 回看 {recording.host_name or recording.host_address} 的远程桌面录像"
        ),
        target_type="host",
        target_id=recording.host_id or "",
        target_name=recording.host_name or "",
        actor_id=actor.id,
        actor_username=actor.username,
        actor_role=actor.role_code or "",
        ip=_client_ip(),
        detail={
            "recordingId": recording.id,
            "ticket": True,
            "ttl": RECORDING_TICKET_TTL_SECONDS,
        },
    )
    return api_ok(
        {
            "ticket": ticket,
            "path": f"/api/rdp/recordings/{recording.id}/file",
            "expiresIn": RECORDING_TICKET_TTL_SECONDS,
        },
        message="回放票据已签发",
    )


@bp.get("/recordings/<int:recording_id>/file")
def get_recording_file(recording_id: int):
    """回放录像本体：支持 HTTP Range（否则 `<video>` 拖不动进度条）。

    鉴权两条路：`Authorization: Bearer <token>`（同源 XHR / 下载工具），
    或 `?ticket=`（`<video>` 只能用这条）。
    """
    actor, via_ticket = _actor_from_ticket_or_jwt(recording_id)
    if actor is None:
        return api_error("登录状态已失效或回放票据无效", 401, code="UNAUTHORIZED")
    recording, failure = _recording_of(recording_id, actor)
    if failure is not None:
        return failure

    path = _recording_path(recording)
    if not os.path.isfile(path):
        return api_error("录像文件已丢失，请联系管理员", 404, code="FILE_MISSING")
    size = os.path.getsize(path)
    parsed = _parse_range(request.headers.get("Range"), size)
    if parsed == (-1, 0):
        response = Response(status=416)
        response.headers["Content-Range"] = f"bytes */{size}"
        return response

    status = 200
    start, length = 0, size
    if parsed is not None:
        start, length = parsed
        status = 206
    response = Response(
        _iter_file(path, start, length),
        status=status,
        mimetype=recording.mime_type or FALLBACK_RECORDING_MIME,
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.headers["Content-Disposition"] = f'inline; filename="{recording.filename}"'
    if status == 206:
        response.headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"

    # 播放器一开就发很多次 Range 请求，只在「不带 Range」或「从 0 开始」时留审计，
    # 免得一次回看刷出几十条 rdp_recording_viewed。走票据的请求不在这里重复记：
    # 签票那一步已经写过一条了（票本身就是「要看」的证据）。
    if not via_ticket and (parsed is None or start == 0):
        log_event(
            "session",
            "rdp_recording_viewed",
            message=f"{actor.username} 回看 {recording.host_name or recording.host_address} 的远程桌面录像",
            target_type="host",
            target_id=recording.host_id or "",
            target_name=recording.host_name or "",
            actor_id=actor.id,
            actor_username=actor.username,
            actor_role=actor.role_code or "",
            ip=_client_ip(),
            detail={
                "recordingId": recording.id,
                "sizeBytes": size,
                "range": request.headers.get("Range") or "",
            },
        )
    return response


@bp.delete("/recordings/<int:recording_id>")
@admin_required
def delete_recording(recording_id: int):
    """删除录像：先落盘文件、再删行，最后写审计。仅管理员。"""
    actor = load_actor()
    if actor is None:  # pragma: no cover - admin_required 已挡掉
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    recording = db.session.get(RdpRecording, recording_id)
    if recording is None:
        return api_error("录像不存在", 404, code="NOT_FOUND")

    path = _recording_path(recording)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        log.exception("删除录像文件失败 path=%s", path)
        return api_error("录像文件删除失败，请稍后重试", 500, code="SERVER_ERROR")

    host_id = recording.host_id
    host_name = recording.host_name or ""
    filename = recording.filename
    db.session.delete(recording)
    db.session.commit()

    log_event(
        "session",
        "rdp_recording_deleted",
        message=f"{actor.username} 删除了 {host_name or host_id} 的远程桌面录像（{filename}）",
        target_type="host",
        target_id=host_id or "",
        target_name=host_name,
        actor_id=actor.id,
        actor_username=actor.username,
        actor_role=actor.role_code or "",
        ip=_client_ip(),
        detail={"recordingId": recording_id, "filename": filename},
    )
    return api_ok({"id": recording_id, "deleted": True}, message="录像已删除")

