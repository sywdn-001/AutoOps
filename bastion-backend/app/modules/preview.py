"""安装前预览会话（契约 §7）。

``POST /api/modules/upload-check`` 解析完包之后，把包本身和 ``images/`` 里的
截图留在 ``instance/module-previews/<token>/``，这样：

* 预览窗口可以直接用 ``<img src>`` 显示截图（不用把 base64 塞进 localStorage）；
* 用户在预览窗口里点「安装」时，宿主不用再让他选一次文件。

会话有 TTL，过期目录在下次上传/读取时惰性清理；安装成功或失败都不影响已装模块。
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("bastion.modules.preview")

#: 预览会话存活时间（秒）。
SESSION_TTL_SECONDS = 30 * 60
#: 最多预览多少张截图 / 单张最大体积，避免有人拿 200MB 的 PNG 把内存吃光。
MAX_PREVIEW_IMAGES = 8
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"})

PACKAGE_NAME = "package.mod.ops_ext"
META_NAME = "meta.json"
ASSET_DIR = "asset"

_TOKEN_LENGTH = 32
_HEX_DIGITS = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# 目录 / 元数据
# ---------------------------------------------------------------------------
def _root() -> Path:
    """预览目录默认放在模块目录旁边，和模块数据一起被备份/清理。"""
    from flask import current_app

    configured = current_app.config.get("MODULE_PREVIEW_DIR")
    if configured:
        base = Path(str(configured))
    else:
        modules_dir = current_app.config.get("MODULES_DIR")
        if modules_dir:
            base = Path(str(modules_dir)).parent / "module-previews"
        else:
            base = Path(tempfile.gettempdir()) / "bastion-module-previews"
    base.mkdir(parents=True, exist_ok=True)
    return base


def valid_token(token: str) -> bool:
    text = str(token or "").strip().lower()
    return len(text) == _TOKEN_LENGTH and set(text) <= _HEX_DIGITS


def session_dir(token: str) -> Path:
    return _root() / str(token or "").strip().lower()


def _meta_path(token: str) -> Path:
    return session_dir(token) / META_NAME


def _write_meta(token: str, meta: dict) -> None:
    path = _meta_path(token)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def read_meta(token: str) -> dict | None:
    """读会话元数据；不存在或已过期返回 ``None``（顺手删掉过期目录）。"""
    if not valid_token(token):
        return None
    path = _meta_path(token)
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 元数据坏了就当过期
        logger.warning("预览元数据损坏：%s", token, exc_info=True)
        discard(token)
        return None
    created = int(meta.get("createdAt") or 0)
    if created <= 0 or time.time() - created > SESSION_TTL_SECONDS:
        discard(token)
        return None
    return meta


def discard(token: str) -> None:
    """删掉一个预览会话（安装成功、过期或显式取消时调用）。"""
    if not valid_token(token):
        return
    shutil.rmtree(session_dir(token), ignore_errors=True)


def purge_expired() -> list[str]:
    """清掉所有过期会话，返回被清掉的 token 列表。"""
    removed: list[str] = []
    try:
        root = _root()
    except Exception:  # noqa: BLE001
        return removed
    now = time.time()
    for child in root.iterdir():
        if not child.is_dir() or not valid_token(child.name):
            continue
        meta_path = child / META_NAME
        try:
            created = int(
                (json.loads(meta_path.read_text(encoding="utf-8")) or {}).get(
                    "createdAt"
                )
                or 0
            )
        except Exception:  # noqa: BLE001
            created = 0
        if created <= 0 or now - created > SESSION_TTL_SECONDS:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed


# ---------------------------------------------------------------------------
# 创建会话
# ---------------------------------------------------------------------------
def asset_url(token: str, relative: str) -> str:
    return f"/api/modules/preview/{token}/asset/{quote(relative, safe='/')}"


def create(archive, description: dict, package_path: str) -> dict:
    """为刚解析完的包建一个预览会话，返回 ``{token, images: [...]}``。"""
    purge_expired()
    token = secrets.token_hex(_TOKEN_LENGTH // 2)
    target = session_dir(token)
    asset_dir = target / ASSET_DIR
    asset_dir.mkdir(parents=True, exist_ok=True)

    images: list[dict] = []
    try:
        shutil.copy2(package_path, target / PACKAGE_NAME)
    except Exception:  # noqa: BLE001 - 包存不下来只是不能一键安装，预览还能用
        logger.warning("保存预览包失败：%s", token, exc_info=True)

    wanted = [
        entry
        for entry in archive.entries
        if not entry.is_dir
        and entry.name.startswith("images/")
        and Path(entry.name).suffix.lower() in IMAGE_SUFFIXES
        and 0 < int(entry.size) <= MAX_IMAGE_BYTES
    ][:MAX_PREVIEW_IMAGES]
    if wanted:
        try:
            with archive.open_zip() as handle:
                for entry in wanted:
                    # 保留 images/ 前缀：URL 里的路径和包内相对路径保持一致。
                    destination = asset_dir / entry.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with handle.open(entry.name) as source, open(
                        destination, "wb"
                    ) as sink:
                        shutil.copyfileobj(source, sink, length=256 * 1024)
                    images.append(
                        {
                            "path": entry.name,
                            "size": int(entry.size),
                            "url": asset_url(token, entry.name),
                        }
                    )
        except Exception:  # noqa: BLE001 - 截图只是锦上添花
            logger.warning("提取预览截图失败：%s", token, exc_info=True)

    _write_meta(
        token,
        {
            "token": token,
            "createdAt": int(time.time()),
            "id": description.get("id", ""),
            "version": description.get("version", ""),
            "name": description.get("name", {}),
            "images": [item["path"] for item in images],
        },
    )
    return {"token": token, "images": images}


def asset_file(token: str, relative: str) -> Path | None:
    """预览会话里的截图文件；越界或不存在返回 ``None``。"""
    meta = read_meta(token)
    if meta is None:
        return None
    cleaned = str(relative or "").replace("\\", "/").lstrip("/")
    if not cleaned or any(part in ("", ".", "..") for part in cleaned.split("/")):
        return None
    base = session_dir(token) / ASSET_DIR
    target = (base / cleaned).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def package_path(token: str) -> Path | None:
    meta = read_meta(token)
    if meta is None:
        return None
    path = session_dir(token) / PACKAGE_NAME
    return path if path.is_file() else None
