"""文件管理器（SFTP）接口：会话、浏览、上传下载、增删改与打包。"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from flask import Blueprint, Response, current_app, g, request, send_file, stream_with_context

from ..files import service as files
from ..files.service import FileError
from ..security import permission_required
from ..utils import api_error, api_ok, get_client_ip, parse_bool, parse_int

logger = logging.getLogger(__name__)

bp = Blueprint("files", __name__, url_prefix="/api")


def _json() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _fail(exc: FileError):
    return api_error(exc.message, exc.status, code=exc.code, **exc.extra)


def _load(sid: str):
    """取会话并校验归属：文件会话只能由打开它的那个人用。"""
    session = files.require_file_session(sid)
    actor = getattr(g, "actor", None)
    if actor is not None and session.user_id != actor.id:
        raise FileError("这不是你的文件管理器会话", code="FORBIDDEN", status=403)
    return session


def _mode_arg(raw, default: int | None = None) -> int | None:
    """权限值既接受八进制字符串（"644"）也接受整数（0o644 / 420）。"""
    if raw in (None, ""):
        return default
    if isinstance(raw, str):
        text = raw.strip()
        try:
            return int(text, 8)
        except ValueError:
            return None
    value = parse_int(raw, None)
    if value is None:
        return None
    return value


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@bp.post("/files/sessions")
@permission_required("file:use")
def open_session():
    payload = _json()
    host_id = parse_int(payload.get("hostId"), 0)
    if not host_id:
        return api_error("请指定主机", 400, code="INVALID_ARGUMENT")
    account_id = parse_int(payload.get("accountId"), None)
    try:
        session = files.open_file_session(
            current_app._get_current_object(),
            user=g.actor,
            host_id=host_id,
            account_id=account_id,
            client_ip=get_client_ip(),
        )
    except FileError as exc:
        return _fail(exc)
    except Exception as exc:  # pragma: no cover - 兜底，避免 500 裸栈
        logger.exception("打开文件管理器失败 host=%s", host_id)
        return api_error(f"打开文件管理器失败：{exc}", 502, code="FILE_SESSION_FAILED")
    return api_ok(files.capabilities(session), message="文件管理器已就绪")


@bp.get("/files/sessions")
@permission_required("file:use")
def list_sessions():
    owned = []
    for item in files.list_live_sessions():
        try:
            session = files.get_file_session(item["sid"])
        except Exception:  # pragma: no cover
            session = None
        if session is not None and session.user_id == g.actor.id:
            owned.append(session.to_dict())
    return api_ok(owned)


@bp.get("/files/sessions/<sid>")
@permission_required("file:use")
def session_info(sid: str):
    try:
        session = _load(sid)
    except FileError as exc:
        return _fail(exc)
    return api_ok(files.capabilities(session))


@bp.delete("/files/sessions/<sid>")
@permission_required("file:use")
def close_session(sid: str):
    try:
        session = _load(sid)
    except FileError as exc:
        return _fail(exc)
    files.close_file_session(sid, "用户关闭文件管理器")
    logger.debug("关闭文件会话 sid=%s host=%s", sid, session.host_name)
    return api_ok({"sid": sid}, message="文件管理器已关闭")


@bp.post("/files/sessions/<sid>/check")
@permission_required("file:use")
def check_path(sid: str):
    payload = _json()
    operation = str(payload.get("operation") or "").strip().lower()
    if not operation:
        return api_error("请指定操作", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
    except FileError as exc:
        return _fail(exc)
    decision = files.check_operation(
        session,
        operation,
        str(payload.get("path") or session.home_dir),
        target_path=str(payload.get("targetPath") or ""),
    )
    return api_ok(decision)


# ---------------------------------------------------------------------------
# 浏览与读取
# ---------------------------------------------------------------------------


@bp.get("/files/sessions/<sid>/list")
@permission_required("file:use")
def list_files(sid: str):
    try:
        session = _load(sid)
        data = files.list_dir(
            session,
            request.args.get("path") or session.home_dir,
            show_hidden=parse_bool(request.args.get("showHidden"), True),
        )
    except FileError as exc:
        return _fail(exc)
    return api_ok(data)


@bp.get("/files/sessions/<sid>/stat")
@permission_required("file:use")
def stat_file(sid: str):
    try:
        session = _load(sid)
        data = files.stat_path(session, request.args.get("path") or session.home_dir)
    except FileError as exc:
        return _fail(exc)
    return api_ok(data)


@bp.get("/files/sessions/<sid>/read")
@permission_required("file:use")
def read_file(sid: str):
    try:
        session = _load(sid)
        data = files.read_text(session, request.args.get("path") or "")
    except FileError as exc:
        return _fail(exc)
    return api_ok(data)


@bp.post("/files/sessions/<sid>/write")
@permission_required("file:use")
def write_file(sid: str):
    payload = _json()
    path = str(payload.get("path") or "")
    if not path:
        return api_error("请指定文件路径", 400, code="INVALID_ARGUMENT")
    content = payload.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        return api_error("content 必须是字符串", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        data = files.write_text(
            session,
            path,
            content,
            expected_mtime=parse_int(payload.get("expectedMtime"), None),
        )
    except FileError as exc:
        return _fail(exc)
    return api_ok(data, message="已保存")


# ---------------------------------------------------------------------------
# 增删改
# ---------------------------------------------------------------------------


@bp.post("/files/sessions/<sid>/mkdir")
@permission_required("file:use")
def make_directory(sid: str):
    payload = _json()
    path = str(payload.get("path") or "")
    if not path:
        return api_error("请指定目录路径", 400, code="INVALID_ARGUMENT")
    mode = _mode_arg(payload.get("mode"), 0o755)
    if mode is None:
        return api_error("权限值不合法", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        data = files.mkdir(session, path, mode=mode, parents=parse_bool(payload.get("parents"), False))
    except FileError as exc:
        return _fail(exc)
    return api_ok(data, message="目录已创建")


@bp.post("/files/sessions/<sid>/rename")
@permission_required("file:use")
def rename_entry(sid: str):
    payload = _json()
    path = str(payload.get("path") or "")
    if not path:
        return api_error("请指定要移动的路径", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        data = files.rename_path(
            session,
            path,
            str(payload.get("newName") or ""),
            target_path=(str(payload.get("targetPath")) if payload.get("targetPath") else None),
        )
    except FileError as exc:
        return _fail(exc)
    return api_ok(data, message="已重命名" if data.get("operation") == "rename" else "已移动")


@bp.post("/files/sessions/<sid>/copy")
@permission_required("file:use")
def copy_entry(sid: str):
    payload = _json()
    path = str(payload.get("path") or "")
    target = str(payload.get("targetPath") or "")
    if not path or not target:
        return api_error("请指定源路径与目标路径", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        data = files.copy_path(session, path, target, recursive=parse_bool(payload.get("recursive"), True))
    except FileError as exc:
        return _fail(exc)
    return api_ok(data, message="已复制")


@bp.post("/files/sessions/<sid>/delete")
@permission_required("file:use")
def delete_entries(sid: str):
    payload = _json()
    raw = payload.get("paths")
    if isinstance(raw, str):
        paths = [raw]
    elif isinstance(raw, (list, tuple)):
        paths = [str(item) for item in raw]
    else:
        paths = []
    if not paths:
        return api_error("请选择要删除的文件", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        data = files.delete_paths(session, paths, recursive=parse_bool(payload.get("recursive"), True))
    except FileError as exc:
        return _fail(exc)
    message = f"已删除 {len(data['deleted'])} 项"
    if data["failed"]:
        message += f"，{len(data['failed'])} 项失败"
    return api_ok(data, message=message)


@bp.post("/files/sessions/<sid>/chmod")
@permission_required("file:use")
def chmod_entry(sid: str):
    payload = _json()
    path = str(payload.get("path") or "")
    if not path:
        return api_error("请指定路径", 400, code="INVALID_ARGUMENT")
    mode = _mode_arg(payload.get("mode"), None)
    if mode is None:
        return api_error("请提供八进制权限值，例如 644", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        data = files.chmod_path(session, path, mode)
    except FileError as exc:
        return _fail(exc)
    return api_ok(data, message=f"权限已改为 {data['after']}")


# ---------------------------------------------------------------------------
# 上传 / 下载 / 打包
# ---------------------------------------------------------------------------


@bp.post("/files/sessions/<sid>/upload")
@permission_required("file:use")
def upload(sid: str):
    uploads = [item for item in request.files.getlist("file") if item and item.filename]
    if not uploads:
        uploads = [item for item in request.files.getlist("files") if item and item.filename]
    if not uploads:
        return api_error("没有收到文件", 400, code="INVALID_ARGUMENT")
    directory = (request.form.get("path") or "").strip()
    explicit = (request.form.get("targetPath") or "").strip()
    overwrite = parse_bool(request.form.get("overwrite"), True)
    try:
        session = _load(sid)
    except FileError as exc:
        return _fail(exc)
    results: list[dict] = []
    errors: list[dict] = []
    for item in uploads:
        safe_name = os.path.basename(str(item.filename).replace("\\", "/")).strip()
        if not safe_name or safe_name in (".", ".."):
            errors.append({"name": str(item.filename), "message": "文件名不合法"})
            continue
        target = explicit or (f"{directory.rstrip('/')}/{safe_name}" if directory else f"/{safe_name}")
        try:
            data = files.upload_file(
                session,
                target,
                item.stream,
                overwrite=overwrite,
                declared_size=item.content_length,
            )
            results.append({**data, "name": safe_name})
        except FileError as exc:
            errors.append({"name": safe_name, "message": exc.message, "code": exc.code})
    if not results and errors:
        first = errors[0]
        status = 403 if first.get("code") in ("FILE_DENIED", "FORBIDDEN") else 400
        return api_error(first["message"], status, code=first.get("code") or "UPLOAD_FAILED", failed=errors)
    message = f"已上传 {len(results)} 个文件"
    if errors:
        message += f"，{len(errors)} 个失败"
    return api_ok({"uploaded": results, "failed": errors}, message=message)


@bp.get("/files/sessions/<sid>/download")
@permission_required("file:use")
def download(sid: str):
    try:
        session = _load(sid)
        payload = files.download_file(session, request.args.get("path") or "")
    except FileError as exc:
        return _fail(exc)
    filename = os.path.basename(payload["path"]) or "download"
    response = Response(
        stream_with_context(files.stream_download(payload)),
        mimetype="application/octet-stream",
    )
    response.headers["Content-Disposition"] = _content_disposition(filename)
    response.headers["Content-Length"] = str(payload["size"])
    response.headers["X-File-Path"] = quote(payload["path"])
    return response


@bp.post("/files/sessions/<sid>/archive")
@permission_required("file:use")
def archive(sid: str):
    payload = _json()
    raw = payload.get("paths")
    if isinstance(raw, (list, tuple)):
        paths = [str(item) for item in raw]
    elif raw:
        paths = [str(raw)]
    else:
        paths = []
    if not paths:
        return api_error("请选择要打包的文件", 400, code="INVALID_ARGUMENT")
    try:
        session = _load(sid)
        info = files.archive_paths(session, paths)
    except FileError as exc:
        return _fail(exc)
    temp_path = info["tempPath"]
    response = send_file(
        temp_path,
        as_attachment=True,
        download_name=info["filename"],
        mimetype="application/zip",
    )
    response.headers["X-Archive-Entries"] = str(info["entries"])
    response.call_on_close(lambda: files._cleanup_temp(temp_path))
    return response
