"""扩展模块包格式：``description.json`` 解析、ZIP 结构校验与归一化。

安装接口、内置模块扫描、``tools/pack_module.py --check`` 全部走这里，
这样「安装时能过」与「打包时能过」永远一致，不会出现打包器放行、安装器拒绝
的割裂。**本模块绝不 import 模块代码**，只做静态检查。
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

DESCRIPTION_NAME = "description.json"

#: 模块 id 的权威规则（契约 §1）。2~39 位，小写字母开头。
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
SETTING_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")
NAV_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PERMISSION_RE = re.compile(r"^[A-Za-z0-9_*][A-Za-z0-9_:.*-]{0,127}$")

#: 严格 semver（SemVer 2.0.0 的常用子集，拒绝前导零）。
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

#: 保留 id：避免模块与核心路由 / 管理页面抢命名空间。
RESERVED_IDS = frozenset({"core", "system", "admin", "modules", "_builtin"})

MAX_ENTRIES = 20000
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 256 * 1024

#: Windows 上这些名字（含带扩展名的形式）无法落盘，解压必然失败；
#: 提前拒绝比解压到一半再回滚更可控。
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_ILLEGAL_NAME_CHARS = frozenset('<>:"|?*')
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f", ""})


class ModulePackageError(ValueError):
    """包格式/描述文件错误。``code`` 直接作为 API 错误码返回。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MODULE_INVALID_PACKAGE",
        status: int = 400,
        field: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.field = field


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    size: int
    is_dir: bool


@dataclass
class ParsedArchive:
    """ZIP 的静态快照。保留 ``origin``/``payload`` 供安装时重新打开解压。"""

    entries: tuple[ArchiveEntry, ...]
    description: dict
    description_bytes: bytes
    total_size: int
    origin: Path | None = None
    payload: bytes | None = None

    @property
    def names(self) -> set[str]:
        return {entry.name for entry in self.entries}

    def open_zip(self) -> zipfile.ZipFile:
        if self.origin is not None:
            return zipfile.ZipFile(self.origin)
        if self.payload is None:
            raise ModulePackageError("安装包内容已丢失，请重新上传")
        return zipfile.ZipFile(io.BytesIO(self.payload))


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def check_member_name(name: str) -> str:
    """校验 ZIP 条目/包内相对路径，返回去掉尾部斜杠的规范形式。

    这些拒绝项不是「洁癖」：Zip Slip、NTFS 备用数据流（``a.php:1``）、
    Windows 保留设备名都能让解压写到预期目录之外，或者让后续静态文件
    服务读到不该读的位置，所以必须在落盘前一次性拒绝。
    """
    if not isinstance(name, str) or not name:
        raise ModulePackageError("包内存在空文件名")
    if "\x00" in name:
        raise ModulePackageError("包内文件名包含 NUL 字符")
    if "\\" in name:
        raise ModulePackageError(f"包内文件名不能包含反斜杠：{name}")
    if name.startswith("/"):
        raise ModulePackageError(f"包内文件名不能是绝对路径：{name}")
    if re.match(r"^[A-Za-z]:", name):
        raise ModulePackageError(f"包内文件名不能带盘符：{name}")

    stripped = name[:-1] if name.endswith("/") else name
    parts = stripped.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ModulePackageError(f"包内文件名包含非法路径片段：{name}")
    for part in parts:
        if any(ch in _ILLEGAL_NAME_CHARS or ord(ch) < 32 for ch in part):
            raise ModulePackageError(f"包内文件名包含非法字符：{name}")
        if part.endswith(".") or part.endswith(" "):
            raise ModulePackageError(f"包内文件名不能以点或空格结尾：{name}")
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            raise ModulePackageError(f"包内文件名使用了保留设备名：{name}")
    return "/".join(parts)


def safe_relative_path(value: Any, *, field: str, allow_empty: bool = False) -> str:
    """校验 description 里声明的包内相对路径（图片、入口、导航地址）。"""
    if value in (None, ""):
        if allow_empty:
            return ""
        raise ModulePackageError(f"{field} 不能为空", field=field)
    if not isinstance(value, str):
        raise ModulePackageError(f"{field} 必须是字符串", field=field)
    text = value.strip()
    if not text:
        if allow_empty:
            return ""
        raise ModulePackageError(f"{field} 不能为空", field=field)
    return check_member_name(text)


def _normalize_nav_path(value: Any, *, field: str) -> str:
    """把 nav.path 归一成以 ``/`` 开头的相对前端路径；``/`` 表示模块根。"""
    if value is None:
        return "/"
    if not isinstance(value, str):
        raise ModulePackageError(f"{field} 必须是字符串", field=field)
    text = value.strip()
    if not text:
        return "/"
    if "?" in text or "#" in text:
        raise ModulePackageError(f"{field} 不能包含查询串或锚点", field=field)
    if not text.startswith("/"):
        text = "/" + text
    if text == "/":
        return "/"
    rel = check_member_name(text[1:])
    return "/" + rel


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------


def is_semver(value: Any) -> bool:
    return isinstance(value, str) and bool(SEMVER_RE.match(value))


def version_key(version: str) -> tuple:
    """把 semver 变成可比较的 tuple（预发布版小于同号正式版）。"""
    match = SEMVER_RE.match(version)
    if not match:
        raise ModulePackageError(f"版本号不是合法 semver：{version}")
    major, minor, patch, pre, _build = match.groups()
    if pre is None:
        pre_key: tuple = (1, ())
    else:
        identifiers = []
        for part in pre.split("."):
            if part.isdigit():
                identifiers.append((0, int(part), ""))
            else:
                identifiers.append((1, 0, part))
        pre_key = (0, tuple(identifiers))
    return (int(major), int(minor), int(patch), pre_key)


def check_min_bastion_version(value: Any, app_version: str | None) -> str:
    if value in (None, ""):
        return ""
    if not is_semver(value):
        raise ModulePackageError(f"minBastionVersion 不是合法 semver：{value!r}")
    if app_version:
        if not is_semver(app_version):
            # 后端版本号本身不规范时不要误伤模块安装
            return value
        if version_key(value) > version_key(app_version):
            raise ModulePackageError(
                f"模块要求 Bastion >= {value}，当前宿主机版本为 {app_version}"
            )
    return value


# ---------------------------------------------------------------------------
# description.json 归一化
# ---------------------------------------------------------------------------


def _bilingual(
    value: Any,
    *,
    field: str,
    required: bool = False,
    default: str = "",
    limit: int = 200,
) -> dict[str, str]:
    if value is None:
        if required:
            raise ModulePackageError(f"{field} 必填", field=field)
        return {"zh": default, "en": default}
    if isinstance(value, str):
        text = value.strip()
        if not text and required:
            raise ModulePackageError(f"{field} 不能为空", field=field)
        return {"zh": text[:limit], "en": text[:limit]}
    if isinstance(value, dict):
        zh = str(value.get("zh") or "").strip()
        en = str(value.get("en") or "").strip()
        if not zh:
            zh = en
        if not en:
            en = zh
        if not zh and required:
            raise ModulePackageError(f"{field} 至少需要 zh 或 en", field=field)
        return {"zh": zh[:limit], "en": en[:limit]}
    raise ModulePackageError(
        f"{field} 必须是字符串或 zh/en 对象", field=field
    )


def _as_bool(value: Any, *, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
    raise ModulePackageError(f"{field} 必须是布尔值", field=field)


def _as_string(value: Any, *, field: str, default: str = "", limit: int = 255) -> str:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise ModulePackageError(f"{field} 必须是字符串", field=field)
    return value.strip()[:limit]


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModulePackageError("tags 必须是字符串数组", field="tags")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ModulePackageError("tags 必须是字符串数组", field="tags")
        text = item.strip()
        if not text:
            continue
        if len(text) > 32:
            raise ModulePackageError("tag 最长 32 个字符", field="tags")
        if text not in tags:
            tags.append(text)
    return tags[:20]


def _normalize_images(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModulePackageError("images 必须是字符串数组", field="images")
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, dict):
            item = item.get("path")
        rel = safe_relative_path(item, field=f"images[{index}]")
        if rel not in seen:
            seen.add(rel)
            images.append({"path": rel})
    return images


def _normalize_entry(value: Any) -> dict[str, str]:
    default_web = "web/index.html"
    if value in (None, ""):
        return {"web": default_web}
    if isinstance(value, str):
        return {"web": safe_relative_path(value, field="entry.web")}
    if not isinstance(value, dict):
        raise ModulePackageError("entry 必须是对象", field="entry")
    web = value.get("web", default_web)
    return {"web": safe_relative_path(web, field="entry.web")}


def _normalize_nav(
    value: Any,
    *,
    module_id: str,
    module_name: dict[str, str],
    default_permission: str,
    default_entry: str,
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModulePackageError("nav 必须是数组", field="nav")

    items: list[dict] = []
    seen_keys: set[str] = set()
    for index, raw in enumerate(value):
        field_name = f"nav[{index}]"
        if not isinstance(raw, dict):
            raise ModulePackageError(f"{field_name} 必须是对象", field=field_name)
        key = str(raw.get("key") or "").strip()
        if not key:
            raise ModulePackageError(f"{field_name}.key 必填", field=field_name)
        if not NAV_KEY_RE.match(key):
            raise ModulePackageError(
                f"{field_name}.key 只允许字母、数字、下划线、中划线", field=field_name
            )
        if key in seen_keys:
            raise ModulePackageError(f"nav.key 重复：{key}", field=field_name)
        seen_keys.add(key)

        title = _bilingual(
            raw.get("title"), field=f"{field_name}.title", required=True, limit=64
        )
        path = _normalize_nav_path(raw.get("path", "/"), field=f"{field_name}.path")
        entry = raw.get("entry") or default_entry
        entry = safe_relative_path(entry, field=f"{field_name}.entry")
        nav_type = str(raw.get("type") or "iframe").strip().lower()
        if nav_type not in ("iframe", "js"):
            raise ModulePackageError(
                f"{field_name}.type 只支持 iframe 或 js", field=field_name
            )
        icon = _as_string(raw.get("icon"), field=f"{field_name}.icon", limit=64)
        permission = str(raw.get("permission") or default_permission).strip()
        if not PERMISSION_RE.match(permission):
            raise ModulePackageError(f"{field_name}.permission 非法", field=field_name)

        items.append(
            {
                "key": key,
                "title": title,
                "path": f"/m/{module_id}{path}",
                "icon": icon,
                "type": nav_type,
                # entry 是契约 §2 样例漏掉的字段，但 §7 宿主页 iframe 需要它
                "entry": entry,
                "permission": permission,
            }
        )
    return items


def _normalize_settings(
    value: Any, *, module_name: dict[str, str], module_id: str
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModulePackageError("settings 必须是数组", field="settings")
    group = module_name.get("zh") or module_name.get("en") or module_id
    items: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        field_name = f"settings[{index}]"
        if not isinstance(raw, dict):
            raise ModulePackageError(f"{field_name} 必须是对象", field=field_name)
        key = str(raw.get("key") or "").strip()
        if not key:
            raise ModulePackageError(f"{field_name}.key 必填", field=field_name)
        if not SETTING_KEY_RE.match(key):
            raise ModulePackageError(
                f"{field_name}.key 只允许字母开头、字母/数字/下划线，最长 40 字符",
                field=field_name,
            )
        if key in seen:
            raise ModulePackageError(f"settings.key 重复：{key}", field=field_name)
        seen.add(key)

        setting_type = str(raw.get("type") or "string").strip().lower()
        if setting_type not in ("bool", "int", "string"):
            raise ModulePackageError(
                f"{field_name}.type 只支持 bool/int/string", field=field_name
            )

        min_value = raw.get("min")
        max_value = raw.get("max")
        for name, val in (("min", min_value), ("max", max_value)):
            if val is not None:
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ModulePackageError(
                        f"{field_name}.{name} 必须是整数", field=field_name
                    )
        if min_value is not None and max_value is not None and min_value > max_value:
            raise ModulePackageError(
                f"{field_name}.min 不能大于 max", field=field_name
            )

        default = raw.get("default")
        if setting_type == "bool":
            if default is None:
                default = "0"
            default = "1" if _as_bool(default, field=f"{field_name}.default") else "0"
        elif setting_type == "int":
            if default is None:
                default = min_value if min_value is not None and min_value > 0 else 0
            if isinstance(default, bool):
                raise ModulePackageError(
                    f"{field_name}.default 必须是整数", field=field_name
                )
            try:
                default = int(default)
            except (TypeError, ValueError):
                raise ModulePackageError(
                    f"{field_name}.default 必须是整数", field=field_name
                ) from None
            if min_value is not None and default < min_value:
                raise ModulePackageError(
                    f"{field_name}.default 小于 min", field=field_name
                )
            if max_value is not None and default > max_value:
                raise ModulePackageError(
                    f"{field_name}.default 大于 max", field=field_name
                )
            default = str(default)
        else:
            default = "" if default is None else str(default)
            if len(default) > 1024:
                raise ModulePackageError(
                    f"{field_name}.default 过长（最多 1024 字符）", field=field_name
                )

        label = _bilingual(
            raw.get("label"), field=f"{field_name}.label", default=key, limit=64
        )
        description = _bilingual(
            raw.get("description"), field=f"{field_name}.description", limit=256
        )
        items.append(
            {
                "key": f"mod.{module_id}.{key}",
                "rawKey": key,
                "label": label,
                "type": setting_type,
                "default": default,
                "min": min_value,
                "max": max_value,
                "group": group,
                "description": description,
            }
        )
    return items


def _normalize_permissions(
    value: Any, module_id: str, nav_items: list[dict]
) -> list[str]:
    default_permission = f"module:{module_id}:use"
    permissions: list[str] = []
    if value is not None:
        if not isinstance(value, list):
            raise ModulePackageError("permissions 必须是字符串数组", field="permissions")
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ModulePackageError(
                    f"permissions[{index}] 必须是字符串", field="permissions"
                )
            code = item.strip()
            if not PERMISSION_RE.match(code):
                raise ModulePackageError(
                    f"permissions[{index}] 非法：{code}", field="permissions"
                )
            if code not in permissions:
                permissions.append(code)
    if default_permission not in permissions:
        permissions.insert(0, default_permission)
    for nav in nav_items:
        if nav["permission"] not in permissions:
            permissions.append(nav["permission"])
    return permissions


def _normalize_requires(value: Any, module_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModulePackageError("requires 必须是模块 id 数组", field="requires")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not ID_RE.match(item.strip()):
            raise ModulePackageError(
                f"requires[{index}] 不是合法模块 id", field="requires"
            )
        dep = item.strip()
        if dep == module_id:
            raise ModulePackageError("requires 不能引用自己", field="requires")
        if dep not in result:
            result.append(dep)
    return result


def normalize_description(
    raw: Any, *, app_version: str | None = None, allow_reserved: bool = False
) -> dict:
    """把 description.json 的原始 dict 校验并归一化。

    只做「声明层」校验，不要求 images/nav.entry 指向的文件真实存在 ——
    模块可能只提供 Python 路由（没有 web 资源），安装期强制存在性检查会误杀。
    """
    if not isinstance(raw, dict):
        raise ModulePackageError("description.json 必须是 JSON 对象")

    module_id = raw.get("id")
    if not isinstance(module_id, str) or not module_id.strip():
        raise ModulePackageError("description.json 缺少 id", field="id")
    module_id = module_id.strip()
    if not ID_RE.match(module_id):
        raise ModulePackageError(
            "id 必须为 2~40 位小写字母开头的 [a-z0-9-] 字符串", field="id"
        )
    if not allow_reserved and module_id in RESERVED_IDS:
        raise ModulePackageError(
            f"模块 id {module_id!r} 是保留字，请换一个", code="MODULE_RESERVED_ID"
        )

    name = _bilingual(raw.get("name"), field="name", required=True, limit=64)

    version = raw.get("version")
    if not is_semver(version):
        raise ModulePackageError(
            "version 必须是严格 semver（如 1.2.3）", field="version"
        )

    author = raw.get("author")
    if not isinstance(author, str) or not author.strip():
        raise ModulePackageError("author 必填", field="author")
    author = author.strip()[:64]
    official = author.lower() == "autoops"

    description = _bilingual(raw.get("description"), field="description", limit=1000)
    tags = _normalize_tags(raw.get("tags"))
    experimental = _as_bool(
        raw.get("experimental"), field="experimental", default=False
    ) or any(tag.lower() == "experimental" for tag in tags)
    if experimental and not any(tag.lower() == "experimental" for tag in tags):
        # 归一化后 tags 也要反映 experimental，前端只用 tags 也能判断
        tags.append("experimental")

    images = _normalize_images(raw.get("images"))
    entry = _normalize_entry(raw.get("entry"))
    default_permission = f"module:{module_id}:use"
    nav = _normalize_nav(
        raw.get("nav"),
        module_id=module_id,
        module_name=name,
        default_permission=default_permission,
        default_entry=entry["web"],
    )
    settings = _normalize_settings(
        raw.get("settings"), module_name=name, module_id=module_id
    )
    permissions = _normalize_permissions(raw.get("permissions"), module_id, nav)
    requires = _normalize_requires(raw.get("requires"), module_id)

    min_version = check_min_bastion_version(
        raw.get("minBastionVersion"), app_version
    )

    return {
        "id": module_id,
        "name": name,
        "version": version,
        "author": author,
        "official": official,
        "description": description,
        "tags": tags,
        "experimental": experimental,
        "icon": _as_string(raw.get("icon"), field="icon", limit=64),
        "license": _as_string(raw.get("license"), field="license", limit=64),
        "homepage": _as_string(raw.get("homepage"), field="homepage", limit=255),
        "minBastionVersion": min_version,
        "images": images,
        "entry": entry,
        "nav": nav,
        "settings": settings,
        "permissions": permissions,
        "requires": requires,
        "reserved": module_id in RESERVED_IDS,
    }


# ---------------------------------------------------------------------------
# ZIP 解析与结构校验
# ---------------------------------------------------------------------------


def _describe_zip_issue(exc: BaseException) -> str:
    if isinstance(exc, zipfile.BadZipFile):
        return "不是有效的 .mod.ops_ext 压缩包（ZIP 损坏或格式错误）"
    return f"读取安装包失败：{exc}"


def _read_description(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[dict, bytes]:
    if info.file_size > MAX_DESCRIPTION_BYTES:
        raise ModulePackageError(
            f"description.json 超过 {MAX_DESCRIPTION_BYTES // 1024}KB 上限"
        )
    try:
        with zf.open(info) as handle:
            data = handle.read(MAX_DESCRIPTION_BYTES + 1)
    except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise ModulePackageError(f"无法读取 description.json：{exc}") from None
    if len(data) > MAX_DESCRIPTION_BYTES:
        raise ModulePackageError(
            f"description.json 超过 {MAX_DESCRIPTION_BYTES // 1024}KB 上限"
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ModulePackageError("description.json 必须是 UTF-8 编码") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModulePackageError(
            f"description.json 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}"
        ) from None
    if not isinstance(raw, dict):
        raise ModulePackageError("description.json 必须是 JSON 对象")
    return raw, data


def parse_archive(
    source: str | os.PathLike | bytes | bytearray | BinaryIO,
    *,
    filename: str = "",
) -> ParsedArchive:
    """读取 ZIP 的中央目录并做**落盘前**的安全校验。

    这里只检查条目元数据与 description.json，不解压其余文件、不 import 代码。
    """
    origin: Path | None = None
    payload: bytes | None = None
    try:
        if isinstance(source, (bytes, bytearray)):
            payload = bytes(source)
            zf = zipfile.ZipFile(io.BytesIO(payload))
        elif hasattr(source, "read"):
            data = source.read()  # type: ignore[union-attr]
            if not isinstance(data, (bytes, bytearray)):
                raise ModulePackageError("上传的安装包不是二进制内容")
            payload = bytes(data)
            zf = zipfile.ZipFile(io.BytesIO(payload))
        else:
            origin = Path(source)
            if not origin.is_file():
                raise ModulePackageError("安装包文件不存在")
            zf = zipfile.ZipFile(origin)
    except ModulePackageError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ModulePackageError(_describe_zip_issue(exc)) from None

    entries: list[ArchiveEntry] = []
    seen_names: set[str] = set()
    description_info: zipfile.ZipInfo | None = None
    total_size = 0
    try:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ModulePackageError(
                f"安装包条目数 {len(infos)} 超过上限 {MAX_ENTRIES}"
            )
        for info in infos:
            normalized = check_member_name(info.filename)
            is_dir = info.is_dir() or info.filename.endswith("/")
            key_name = normalized.casefold()
            if key_name in seen_names:
                raise ModulePackageError(f"安装包内存在重复文件名：{normalized}")
            seen_names.add(key_name)

            if info.flag_bits & 0x1:
                raise ModulePackageError("不允许安装加密的 ZIP 条目")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode:
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ModulePackageError(
                        f"安装包内不允许符号链接/设备文件：{normalized}"
                    )
                if file_type == stat.S_IFDIR:
                    is_dir = True
            if is_dir and info.file_size:
                raise ModulePackageError(f"目录条目不应包含数据：{normalized}")
            if not is_dir:
                total_size += max(0, int(info.file_size))
                if total_size > MAX_TOTAL_BYTES:
                    raise ModulePackageError(
                        f"安装包解压后总大小超过 {MAX_TOTAL_BYTES // (1024 * 1024)}MB 上限"
                    )
            entries.append(
                ArchiveEntry(name=normalized, size=int(info.file_size), is_dir=is_dir)
            )
            if normalized == DESCRIPTION_NAME and not is_dir:
                description_info = info

        if description_info is None:
            raise ModulePackageError(
                "安装包根目录缺少 description.json（.mod.ops_ext 必须是 ZIP）"
            )
        raw, description_bytes = _read_description(zf, description_info)
    finally:
        zf.close()

    archive = ParsedArchive(
        entries=tuple(entries),
        description=raw,
        description_bytes=description_bytes,
        total_size=total_size,
        origin=origin,
        payload=payload,
    )
    return archive


def validate_structure(archive: ParsedArchive, description: dict) -> None:
    """校验「只要声明了就要求存在」的包结构（不要求 web/images 文件存在）。"""
    files = {entry.name for entry in archive.entries if not entry.is_dir}
    dirs = {entry.name for entry in archive.entries if entry.is_dir}

    if "modules" in files:
        raise ModulePackageError("modules 必须是目录")
    has_modules_tree = any(name.startswith("modules/") for name in files | dirs)
    if has_modules_tree:
        if "modules/__init__.py" not in files:
            raise ModulePackageError("modules/ 目录存在时必须包含 modules/__init__.py")
    _ = description


def validate_parsed(
    archive: ParsedArchive, *, app_version: str | None = None
) -> dict:
    description = normalize_description(archive.description, app_version=app_version)
    validate_structure(archive, description)
    return description


def validate(
    source: str | os.PathLike | bytes | bytearray | BinaryIO | ParsedArchive,
    *,
    app_version: str | None = None,
) -> dict:
    """统一入口：接受 ZIP 路径/字节/文件对象或已解析快照，返回归一化描述。"""
    archive = source if isinstance(source, ParsedArchive) else parse_archive(source)
    return validate_parsed(archive, app_version=app_version)


def extract_package(
    archive: ParsedArchive,
    package_dir: Path,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> int:
    """把已校验的 ZIP 解压到 ``package_dir``，返回实际写出的字节数。

    解压时**重新**校验每个条目（防止解析后被替换的包）并统计真实字节数，
    不能只信中央目录里声明的 file_size。
    """
    from .store import _ensure_within  # 延迟 import，避免循环依赖

    package_dir = Path(package_dir)
    package_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    entries_by_name = {entry.name: entry for entry in archive.entries}
    try:
        with archive.open_zip() as zf:
            for info in zf.infolist():
                name = check_member_name(info.filename)
                entry = entries_by_name.get(name)
                if entry is None or entry.is_dir:
                    continue
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_IFMT(mode) not in (
                    0,
                    stat.S_IFREG,
                    stat.S_IFDIR,
                ):
                    raise ModulePackageError(
                        f"安装包内不允许符号链接/设备文件：{name}"
                    )
                target = package_dir.joinpath(*name.split("/"))
                _ensure_within(package_dir, target)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_total_bytes:
                            raise ModulePackageError(
                                "解压后总大小超过 "
                                f"{max_total_bytes // (1024 * 1024)}MB 上限"
                            )
                        dst.write(chunk)
    except ModulePackageError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ModulePackageError(_describe_zip_issue(exc)) from None
    return written


def read_description_file(path: str | os.PathLike) -> tuple[dict, bytes]:
    """从解包后的 ``description.json`` 读取原始 dict（内置模块扫描用）。"""
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise ModulePackageError(f"无法读取 description.json：{exc}") from None
    if len(data) > MAX_DESCRIPTION_BYTES:
        raise ModulePackageError(
            f"description.json 超过 {MAX_DESCRIPTION_BYTES // 1024}KB 上限"
        )
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModulePackageError(f"description.json 解析失败：{exc}") from None
    if not isinstance(raw, dict):
        raise ModulePackageError("description.json 必须是 JSON 对象")
    return raw, data


def iter_source_files(source_dir: str | os.PathLike):
    """遍历源码目录，跳过打包黑名单；返回相对 POSIX 路径列表。

    内置模块安装与 pack 工具共用同一份排除规则，避免 ``__pycache__``
    被装进模块目录后随代码更新产生「幽灵文件」。
    """
    from .store import is_ignored_name

    root = Path(source_dir)
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if not is_ignored_name(name)
        )
        current_path = Path(current)
        for name in sorted(filenames):
            if is_ignored_name(name):
                continue
            full = current_path / name
            if full.is_symlink():
                raise ModulePackageError(f"源码目录不允许符号链接：{full}")
            yield full.relative_to(root).as_posix(), full


#: 供其他模块做类型标注
__all__ = [
    "ArchiveEntry",
    "DESCRIPTION_NAME",
    "ID_RE",
    "MAX_DESCRIPTION_BYTES",
    "MAX_ENTRIES",
    "MAX_TOTAL_BYTES",
    "ModulePackageError",
    "NAV_KEY_RE",
    "ParsedArchive",
    "RESERVED_IDS",
    "SEMVER_RE",
    "SETTING_KEY_RE",
    "check_member_name",
    "check_min_bastion_version",
    "extract_package",
    "is_semver",
    "iter_source_files",
    "normalize_description",
    "parse_archive",
    "read_description_file",
    "safe_relative_path",
    "validate",
    "validate_parsed",
    "validate_structure",
    "version_key",
]
