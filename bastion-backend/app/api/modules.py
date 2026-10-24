"""扩展模块 REST 接口（契约 §6）。

安全边界：
* 安装/启停/卸载走 admin；列表/详情/日志走 ``setting:view``。
* 模块自身 HTTP 路由统一由 ``/<id>/api/<path:rel>`` 分发器承接。Flask 3.1
  在首个请求后不允许再注册路由，所以模块的 ``ctx.route`` 只能先在注册表里
  排队，由这里按 werkzeug 规则匹配后执行。
* 静态资源只暴露 ``web/``、``images/`` 与 ``README.md``，且不要求登录（模块
  的 iframe 页面需要先拿到资源）；动态接口仍然要求登录 + 模块权限。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, g, request, send_from_directory
from werkzeug.exceptions import HTTPException, MethodNotAllowed, NotFound

from .. import audit as audit_service
from ..security import admin_required, login_required, permission_required
from ..utils import api_error, api_ok
from ..modules import loader, preview, spec
from ..modules.context import ModuleApiError
from ..modules.permissions import has_module_permission
from ..modules.registry import registry

modules_bp = Blueprint("modules", __name__, url_prefix="/api/modules")
logger = logging.getLogger("bastion.modules.api")

#: 契约里的实验性功能确认语，逐字匹配（仅去首尾空白）。
EXPERIMENTAL_ACK = "我知道这是实验性功能，使用不当的话也许会造成系统损毁"
_PACKAGE_SUFFIX = ".mod.ops_ext"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _app_version() -> str:
    return str(current_app.config.get("APP_VERSION") or "")


def _module_payload(record) -> dict:
    return record.to_dict(_app_version())


def _package_error(exc: spec.ModulePackageError):
    return api_error(str(exc), exc.status, exc.code)


def _not_found(message: str = "模块不存在"):
    return api_error(message, 404, "MODULE_NOT_FOUND")


def _installed_record(module_id: str):
    record = registry.get(module_id)
    if record is None:
        return None
    return record


def _audit(action: str, *, module_id: str, message: str, detail: dict | None = None) -> None:
    actor = getattr(g, "actor", None)
    audit_service.log_event(
        "module",
        action,
        message=message,
        target_type="module",
        target_id=module_id,
        detail=detail or {},
        actor=actor,
    )


def _save_upload() -> tuple[str, str]:
    """把上传文件落到系统临时目录，返回 ``(临时目录, 文件路径)``。"""
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        raise spec.ModulePackageError("请上传 .mod.ops_ext 模块包")
    filename = str(upload.filename).strip()
    if not filename.lower().endswith(_PACKAGE_SUFFIX):
        raise spec.ModulePackageError(
            f"模块包扩展名必须是 {_PACKAGE_SUFFIX}"
        )
    workdir = tempfile.mkdtemp(prefix="bastion-module-upload-")
    path = os.path.join(workdir, "package" + _PACKAGE_SUFFIX)
    try:
        upload.save(path)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return workdir, path


def _ack_text() -> str:
    return str(request.form.get("ack") or "").strip()


def _ensure_ack(description: dict) -> None:
    if not description.get("experimental"):
        return
    if _ack_text() != EXPERIMENTAL_ACK:
        raise spec.ModulePackageError(
            "实验性模块需要逐字确认风险提示后才能安装",
            code="EXPERIMENTAL_NOT_ACKED",
            status=400,
        )


# ---------------------------------------------------------------------------
# 列表 / 详情 / 导航
# ---------------------------------------------------------------------------
@modules_bp.get("")
@permission_required("setting:view")
def list_modules():
    records = sorted(registry.all(), key=lambda item: item.id)
    return api_ok([_module_payload(record) for record in records])


@modules_bp.get("/nav")
@login_required
def module_nav():
    """扁平 nav 项数组；只返回当前用户有权访问且已启用的模块。"""
    actor = getattr(g, "actor", None)
    items: list[dict] = []
    for record in sorted(registry.all(), key=lambda item: item.id):
        if not record.enabled:
            continue
        for nav in record.description.get("nav", []):
            permission = nav.get("permission") or f"module:{record.id}:use"
            if not has_module_permission(actor, permission):
                continue
            items.append(
                {
                    "moduleId": record.id,
                    "key": nav["key"],
                    "title": dict(nav.get("title") or {}),
                    "path": nav.get("path", f"/m/{record.id}/"),
                    "icon": nav.get("icon", ""),
                    "type": nav.get("type", "iframe"),
                    "entry": nav.get("entry", ""),
                    "permission": permission,
                    "loaded": record.available,
                    "moduleName": dict(record.description.get("name") or {}),
                }
            )
    return api_ok(items)


@modules_bp.get("/<module_id>")
@permission_required("setting:view")
def get_module(module_id: str):
    record = _installed_record(module_id)
    if record is None:
        return _not_found()
    return api_ok(_module_payload(record))


@modules_bp.get("/<module_id>/logs")
@permission_required("setting:view")
def get_module_logs(module_id: str):
    if _installed_record(module_id) is None:
        return _not_found()
    return api_ok(registry.logs(module_id))


# ---------------------------------------------------------------------------
# 安装 / 预览
# ---------------------------------------------------------------------------
@modules_bp.post("/install")
@admin_required
def install_module():
    workdir = None
    try:
        workdir, path = _save_upload()
        archive = spec.parse_archive(path)
        description = spec.validate_parsed(archive, app_version=_app_version())
        _ensure_ack(description)
        if registry.get(description["id"]) is not None or registry.store.exists(
            description["id"]
        ):
            raise spec.ModulePackageError(
                f"模块 {description['id']} 已安装",
                code="MODULE_EXISTS",
                status=409,
            )
        # 安装期只做文件操作与 description 校验，绝不 import 模块代码。
        state = registry.store.install_archive(archive, description)
        try:
            record = registry.add_installed(description, state)
        except Exception:
            # 目录已经原子改名成功；登记失败时不能把它留在磁盘上。
            registry.rollback_install(description["id"])
            raise
        _audit(
            "install",
            module_id=description["id"],
            message=f"安装扩展模块 {description['id']} {description['version']}",
            detail={
                "version": description["version"],
                "experimental": description["experimental"],
            },
        )
        return api_ok(_module_payload(record), "模块安装成功")
    except spec.ModulePackageError as exc:
        return _package_error(exc)
    except Exception:  # noqa: BLE001
        logger.exception("安装扩展模块失败")
        return api_error("模块安装失败", 500, "MODULE_INSTALL_FAILED")
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


@modules_bp.post("/preview/<token>/install")
@admin_required
def install_from_preview(token: str):
    """在预览窗口里直接安装：包已经随 upload-check 留在预览目录里了。"""
    meta = preview.read_meta(token)
    path = preview.package_path(token)
    if meta is None or path is None:
        return api_error(
            "安装预览已过期，请重新选择模块包", 410, "MODULE_PREVIEW_EXPIRED"
        )
    body = request.get_json(silent=True) or {}
    ack = str(body.get("ack") or "").strip()
    module_id = ""
    try:
        archive = spec.parse_archive(str(path))
        description = spec.validate_parsed(archive, app_version=_app_version())
        if description["experimental"] and ack != EXPERIMENTAL_ACK:
            raise spec.ModulePackageError(
                "实验性模块需要逐字确认风险提示后才能安装",
                code="EXPERIMENTAL_NOT_ACKED",
                status=400,
            )
        module_id = description["id"]
        if registry.get(module_id) is not None or registry.store.exists(module_id):
            raise spec.ModulePackageError(
                f"模块 {module_id} 已安装", code="MODULE_EXISTS", status=409
            )
        state = registry.store.install_archive(archive, description)
        try:
            record = registry.add_installed(description, state)
        except Exception:
            registry.rollback_install(module_id)
            raise
    except spec.ModulePackageError as exc:
        return _package_error(exc)
    except Exception:  # noqa: BLE001
        logger.exception("预览窗口安装扩展模块失败")
        return api_error("模块安装失败", 500, "MODULE_INSTALL_FAILED")
    preview.discard(token)
    _audit(
        "install",
        module_id=module_id,
        message=f"安装扩展模块 {module_id} {description['version']}",
        detail={
            "version": description["version"],
            "experimental": description["experimental"],
            "via": "preview",
        },
    )
    return api_ok(_module_payload(record), "模块安装成功")


@modules_bp.post("/upload-check")
@admin_required
def upload_check():
    """只解析/校验，不落模块目录、不执行代码。"""
    workdir = None
    extras: dict = {}
    session: dict = {"token": "", "images": []}
    try:
        workdir, path = _save_upload()
        archive = spec.parse_archive(path)
        description = spec.validate_parsed(archive, app_version=_app_version())
        # 临时目录在 finally 里就删掉了，README 必须趁现在读。
        extras = _preview_package_info(archive)
        # 预览窗口要显示截图、并且支持在窗口里直接安装，所以先把包留一份。
        session = preview.create(archive, description, path)
    except spec.ModulePackageError as exc:
        return _package_error(exc)
    except Exception:  # noqa: BLE001
        logger.exception("解析模块包失败")
        return api_error("模块包解析失败", 400, "MODULE_INVALID_PACKAGE")
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    # installable 只由「真正装不进去」的原因决定；实验性模块是可以装的，
    # 只是安装前必须逐字确认风险提示，所以它进 warnings 而不是 issues。
    # 中文句子给旧前端兜底，*Items 带 key/params 让前端按界面语言翻译
    # （key 是中文模板，参数用 {name} 占位）。
    issues: list[str] = []
    warnings: list[str] = []
    issue_items: list[dict] = []
    warning_items: list[dict] = []
    module_id = description["id"]
    if registry.get(module_id) is not None or registry.store.exists(module_id):
        issues.append(f"模块 {module_id} 已安装")
        issue_items.append(
            {"key": "模块 {id} 已安装", "params": {"id": module_id}}
        )
    if description["experimental"]:
        warnings.append("这是实验性模块，安装时需要逐字确认风险提示")
        warning_items.append(
            {
                "key": "这是实验性模块，安装时需要逐字确认风险提示",
                "params": {},
            }
        )
    payload = _module_payload_preview(description)
    payload.update(extras)
    # 包内截图给出可访问的 URL，预览窗口才能真的把图渲染出来。
    found = {item["path"]: item for item in session["images"]}
    merged: list[dict] = []
    for item in description.get("images", []):
        path = item["path"]
        merged.append({**item, **found.get(path, {})})
    for path, item in found.items():
        if all(entry["path"] != path for entry in merged):
            merged.append(dict(item))
    payload["images"] = merged
    payload["previewToken"] = session["token"]
    payload["installable"] = not issues
    payload["issues"] = issues + warnings
    payload["warnings"] = warnings
    payload["issueItems"] = issue_items
    payload["warningItems"] = warning_items
    return api_ok(payload, "模块包校验通过")


_README_NAMES = ("readme.md", "readme.markdown", "readme.txt")
_README_EN_NAMES = ("readme.en.md", "readme-en.md", "readme.en_us.md")
_README_LIMIT = 20000


def _preview_package_info(archive: spec.ParsedArchive) -> dict:
    """预览窗口要展示的包内信息：README 正文（中/英）、体积、文件概况。

    只从 ZIP 中央目录与 README 文本里取，**不执行**模块代码。
    ``README.md`` 与 ``README.en.md`` 可以同时存在，前端按界面语言挑一份。
    """
    files = [entry for entry in archive.entries if not entry.is_dir]
    folders = sorted(
        {
            entry.name.split("/", 1)[0] + "/"
            for entry in files
            if "/" in entry.name
        }
    )
    readme = ""
    readme_name = ""
    readme_en = ""
    readme_en_name = ""
    try:
        with archive.open_zip() as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                base = name.rsplit("/", 1)[-1].lower()
                if base in _README_EN_NAMES:
                    if not readme_en:
                        with zf.open(name) as handle:
                            readme_en = handle.read(_README_LIMIT).decode(
                                "utf-8", "replace"
                            )
                        readme_en_name = name
                    continue
                if base in _README_NAMES:
                    if not readme:
                        with zf.open(name) as handle:
                            readme = handle.read(_README_LIMIT).decode(
                                "utf-8", "replace"
                            )
                        readme_name = name
    except Exception:  # noqa: BLE001 - README 读不出来不影响预览
        logger.warning("读取模块包 README 失败", exc_info=True)
    return {
        "readme": readme,
        "readmeName": readme_name,
        "readmeEn": readme_en,
        "readmeEnName": readme_en_name,
        "size": archive.total_size,
        "fileCount": len(files),
        "folders": folders,
    }


def _module_payload_preview(description: dict) -> dict:
    """归一化描述里去掉只有已安装模块才有的运行时字段。"""
    payload = {
        "id": description["id"],
        "name": dict(description["name"]),
        "version": description["version"],
        "author": description["author"],
        "official": bool(description["official"]),
        "description": dict(description["description"]),
        "tags": list(description["tags"]),
        "experimental": bool(description["experimental"]),
        "icon": description.get("icon", ""),
        "license": description.get("license", ""),
        "homepage": description.get("homepage", ""),
        "minBastionVersion": description.get("minBastionVersion", ""),
        "images": [{"path": item["path"]} for item in description.get("images", [])],
        "entry": dict(description.get("entry", {"web": "web/index.html"})),
        "nav": [dict(item) for item in description.get("nav", [])],
        "settings": [dict(item) for item in description.get("settings", [])],
        "permissions": list(description.get("permissions", [])),
        "requires": list(description.get("requires", [])),
        "reserved": bool(description.get("reserved")),
    }
    return payload


# ---------------------------------------------------------------------------
# 启停 / 卸载
# ---------------------------------------------------------------------------
@modules_bp.post("/<module_id>/enable")
@admin_required
def enable_module(module_id: str):
    try:
        record = registry.enable(module_id)
    except KeyError:
        return _not_found()
    except loader.ModuleLoadError as exc:
        logger.warning("模块加载失败：%s", module_id, exc_info=True)
        return api_error(str(exc), 400, "MODULE_LOAD_FAILED")
    except spec.ModulePackageError as exc:
        return _package_error(exc)
    _audit("enable", module_id=module_id, message=f"启用扩展模块 {module_id}")
    return api_ok(_module_payload(record), "模块已启用")


@modules_bp.post("/<module_id>/disable")
@admin_required
def disable_module(module_id: str):
    try:
        record = registry.disable(module_id)
    except KeyError:
        return _not_found()
    except Exception:  # noqa: BLE001
        logger.exception("禁用模块失败：%s", module_id)
        return api_error("禁用模块失败", 500, "MODULE_DISABLE_FAILED")
    _audit("disable", module_id=module_id, message=f"禁用扩展模块 {module_id}")
    return api_ok(_module_payload(record), "模块已禁用")


@modules_bp.delete("/<module_id>")
@admin_required
def uninstall_module(module_id: str):
    try:
        record = registry.uninstall(module_id)
    except KeyError:
        return _not_found()
    except OSError:
        logger.exception("删除模块目录失败：%s", module_id)
        return api_error("删除模块目录失败", 500, "MODULE_UNINSTALL_FAILED")
    _audit(
        "uninstall",
        module_id=module_id,
        message=f"卸载扩展模块 {module_id}",
        detail={"version": record.description.get("version", "")},
    )
    return api_ok(_module_payload(record), "模块已卸载")


# ---------------------------------------------------------------------------
# 模块静态资源 / 动态路由
# ---------------------------------------------------------------------------
@modules_bp.get("/<module_id>/asset/<path:rel>")
def module_asset(module_id: str, rel: str):
    """仅暴露 web/、images/ 与 README.md；其他路径一律 404。"""
    record = _installed_record(module_id)
    if record is None:
        abort(404)
    relative = str(rel or "").replace("\\", "/").lstrip("/")
    if not relative or any(part in ("", ".", "..") for part in relative.split("/")):
        abort(404)
    allowed = (
        relative in ("README.md", "README.en.md")
        or relative.startswith("web/")
        or relative.startswith("images/")
    )
    if not allowed:
        abort(404)
    package_dir = registry.store.package_dir(module_id)
    target = (package_dir / relative).resolve()
    try:
        target.relative_to(package_dir.resolve())
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    response = send_from_directory(str(package_dir), relative, conditional=True)
    # 模块升级会换文件，不能让浏览器缓存旧页面。
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@modules_bp.get("/preview/<token>/asset/<path:rel>")
def module_preview_asset(token: str, rel: str):
    """预览窗口里的截图；token 短期有效，所以不要求登录。"""
    target = preview.asset_file(token, rel)
    if target is None:
        abort(404)
    response = send_from_directory(str(target.parent), target.name, conditional=True)
    response.headers["Cache-Control"] = "private, max-age=600"
    return response


@modules_bp.route(
    "/<module_id>/api/<path:rel>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@modules_bp.route(
    "/<module_id>/api/",
    defaults={"rel": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@login_required
def module_api(module_id: str, rel: str = ""):
    record = _installed_record(module_id)
    if record is None:
        return _not_found()
    if not record.enabled or record.loaded is None:
        return api_error("模块未启用或加载失败", 404, "MODULE_NOT_FOUND")
    try:
        route, values = registry.match_route(module_id, rel, request.method)
    except NotFound:
        return api_error("模块路由不存在", 404, "NOT_FOUND")
    except MethodNotAllowed:
        return api_error("模块路由不支持该请求方法", 405, "METHOD_NOT_ALLOWED")

    actor = getattr(g, "actor", None)
    permission = route.permission or f"module:{module_id}:use"
    if not has_module_permission(actor, permission):
        return api_error("没有访问该模块的权限", 403, "FORBIDDEN")

    exc_info = None
    response = None
    try:
        # ctx.emit / sdk 的模块归属靠 ContextVar；必须在调用线程里设置。
        from ..modules import sdk

        with sdk.module_scope(module_id):
            early = registry.run_before_request(module_id)
            if early is not None:
                response = _as_response(early)
            else:
                result = route.view(**values)
                response = _as_response(result)
            response = registry.run_after_request(module_id, response)
    except spec.ModulePackageError as exc:
        response = _package_error(exc)
    except ModuleApiError as exc:
        response = api_error(str(exc), exc.status, exc.code)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        exc_info = exc
        logger.exception("模块接口执行失败：%s /%s", module_id, rel)
        response = api_error(
            f"模块接口执行失败：{type(exc).__name__}",
            500,
            "MODULE_INTERNAL_ERROR",
        )
    finally:
        try:
            registry.run_teardown_request(module_id, exc_info)
        except Exception:  # noqa: BLE001
            logger.warning("模块 teardown 失败：%s", module_id, exc_info=True)
    return response


def _as_response(value):
    """把模块返回值塞进统一信封；已经构造好的 Response 原样放行。"""
    if isinstance(value, Response):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], int):
        payload, status = value
        return api_ok(payload, status=status)
    return api_ok(value)
