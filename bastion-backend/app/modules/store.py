"""模块磁盘存储：``<MODULES_DIR>/<id>/{package,data,state.json}``。

「文件系统即真相」是这里唯一的持久化模型：没有数据库表、没有安装清单，
所以备份/迁移只要复制目录。安装过程刻意分成「临时目录解压 → 校验 →
``os.replace`` 改名」三步，任一步失败都只删临时目录，不会留下半个模块。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import spec
from .spec import ModulePackageError, ParsedArchive

logger = logging.getLogger("bastion.modules.store")

PACKAGE_DIR = "package"
DATA_DIR = "data"
STATE_FILE = "state.json"

#: 打包/内置安装共同忽略的目录与文件（契约 §8）。``.DS_Store`` 是 macOS 的
#: 目录元数据，混进模块包没有任何意义，还会在 Windows 上显示成怪文件。
_IGNORED_NAMES = frozenset({"__pycache__", ".git", "node_modules", ".DS_Store"})
_IGNORED_SUFFIXES = (".pyc", ".pyo")

_STAGING_PREFIXES = (".installing-", ".updating-")

#: 目录改名的重试参数。Windows 上刚写出的 exe 会被杀软 / 资源管理器的
#: 写入通知短暂占用，``os.replace`` 抛 ``WinError 5``（拒绝访问）或
#: ``WinError 32``（共享冲突），退避重试即可自愈。
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF = 0.2
_REPLACE_MAX_DELAY = 2.0
_REPLACE_WINERRORS = frozenset({5, 32})


def is_ignored_name(name: str) -> bool:
    return name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES)


def utc_now_iso() -> str:
    """状态文件用带 Z 的 ISO 时间，和契约样例一致；DB 内时间仍走 utcnow。"""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _ensure_within(base: Path, target: Path) -> None:
    """确保 ``target`` 在 ``base`` 之内（防 Zip Slip 的最后一道闸）。"""
    base_resolved = os.path.realpath(os.path.abspath(base))
    target_resolved = os.path.realpath(os.path.abspath(target))
    try:
        common = os.path.commonpath([base_resolved, target_resolved])
    except ValueError as exc:  # 不同盘符
        raise ModulePackageError("安装包路径越界（Zip Slip）") from exc
    if os.path.normcase(common) != os.path.normcase(base_resolved):
        raise ModulePackageError("安装包路径越界（Zip Slip）")


def remove_tree(path: Path) -> None:
    """删除目录；Windows 上只读文件会挡住 rmtree，逐个去掉只读位再试。"""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except OSError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink()
        return

    def _onerror(func, failed_path, _exc_info):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            logger.warning("删除模块临时文件失败：%s", failed_path)

    shutil.rmtree(path, onerror=_onerror)


def replace_tree(source: Path, dest: Path) -> None:
    """把临时目录整体改名到最终位置，占用时退避重试。

    模块包里可能带几十 MB 的可执行文件（如内置 ``rdp-gateway`` 的
    14MB 网关），刚写完的那一瞬间常被杀软、索引服务或资源管理器的
    写入通知短暂占用；此时 ``os.replace`` 会失败，稍等即可成功。
    """
    delay = _REPLACE_BACKOFF
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, dest)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            last = attempt == _REPLACE_ATTEMPTS
            if winerror not in _REPLACE_WINERRORS or last:
                raise
            logger.warning(
                "模块目录改名被占用（第 %d 次），%.1fs 后重试：%s",
                attempt,
                delay,
                exc,
            )
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_MAX_DELAY)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


class ModuleStore:
    """一个 ``MODULES_DIR`` 的薄封装，所有路径都从 ``root`` 推导。"""

    def __init__(self, root: str | os.PathLike | None) -> None:
        self.root = Path(root) if root else None

    # -- 基础 ---------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self.root is not None

    def ensure(self) -> Path:
        if self.root is None:
            raise ModulePackageError(
                "未配置 MODULES_DIR，无法管理扩展模块",
                code="MODULE_STORE_UNAVAILABLE",
                status=500,
            )
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def module_dir(self, module_id: str) -> Path:
        root = self.ensure()
        return root / module_id

    def package_dir(self, module_id: str) -> Path:
        return self.module_dir(module_id) / PACKAGE_DIR

    def data_dir(self, module_id: str) -> Path:
        return self.module_dir(module_id) / DATA_DIR

    def state_path(self, module_id: str) -> Path:
        return self.module_dir(module_id) / STATE_FILE

    def exists(self, module_id: str) -> bool:
        if self.root is None:
            return False
        return self.module_dir(module_id).is_dir()

    def installed_ids(self) -> list[str]:
        """已安装模块 id（只认带 ``package/description.json`` 的目录）。"""
        if self.root is None or not self.root.is_dir():
            return []
        result: list[str] = []
        for child in self.root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not spec.ID_RE.match(child.name):
                continue
            if (child / PACKAGE_DIR / spec.DESCRIPTION_NAME).is_file():
                result.append(child.name)
        return sorted(result)

    def cleanup_temp(self) -> None:
        """清理上次进程崩溃留下的半成品安装目录。"""
        if self.root is None or not self.root.is_dir():
            return
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith(_STAGING_PREFIXES):
                logger.info("清理未完成的模块安装目录：%s", child)
                remove_tree(child)

    # -- 状态 ---------------------------------------------------------------

    def load_state(self, module_id: str) -> dict:
        path = self.state_path(module_id)
        raw: dict = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            logger.warning("模块 %s 的 state.json 损坏，按默认状态处理", module_id)
        if not isinstance(raw, dict):
            raw = {}

        installed_at = raw.get("installedAt")
        if not isinstance(installed_at, str) or not installed_at:
            installed_at = self._mtime_iso(self.module_dir(module_id))
        updated_at = raw.get("updatedAt")
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = installed_at
        return {
            "enabled": raw.get("enabled") is not False,
            "installedAt": installed_at,
            "updatedAt": updated_at,
            "version": str(raw.get("version") or ""),
        }

    def save_state(self, module_id: str, state: dict) -> dict:
        payload = {
            "enabled": bool(state.get("enabled", True)),
            "installedAt": state.get("installedAt") or utc_now_iso(),
            "updatedAt": state.get("updatedAt") or utc_now_iso(),
            "version": str(state.get("version") or ""),
        }
        _write_json_atomic(self.state_path(module_id), payload)
        return payload

    def set_enabled(self, module_id: str, enabled: bool) -> dict:
        state = self.load_state(module_id)
        state["enabled"] = bool(enabled)
        state["updatedAt"] = utc_now_iso()
        return self.save_state(module_id, state)

    def mark_updated(self, module_id: str) -> dict:
        state = self.load_state(module_id)
        state["updatedAt"] = utc_now_iso()
        return self.save_state(module_id, state)

    @staticmethod
    def _mtime_iso(path: Path) -> str:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return ""
        return (
            datetime.fromtimestamp(stamp, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    # -- 读取 ---------------------------------------------------------------

    def read_description(self, module_id: str) -> dict:
        path = self.package_dir(module_id) / spec.DESCRIPTION_NAME
        raw, _ = spec.read_description_file(path)
        return raw

    def load_description(
        self, module_id: str, *, app_version: str | None = None
    ) -> dict:
        """校验并归一化已安装模块的描述；失败抛 :class:`ModulePackageError`。"""
        raw = self.read_description(module_id)
        description = spec.normalize_description(raw, app_version=app_version)
        if description["id"] != module_id:
            raise ModulePackageError(
                f"description.json 的 id（{description['id']}）与目录名（{module_id}）不一致"
            )
        modules_dir = self.package_dir(module_id) / "modules"
        if modules_dir.exists() and not (modules_dir / "__init__.py").is_file():
            raise ModulePackageError(
                "modules/ 目录存在时必须包含 modules/__init__.py"
            )
        return description

    def package_size(self, module_id: str) -> int:
        package = self.package_dir(module_id)
        total = 0
        if not package.is_dir():
            return 0
        for current, _dirs, files in os.walk(package):
            for name in files:
                try:
                    total += (Path(current) / name).stat().st_size
                except OSError:
                    continue
        return total

    # -- 安装 ---------------------------------------------------------------

    def install_archive(
        self,
        archive: ParsedArchive,
        description: dict,
    ) -> dict:
        """从已校验的 ZIP 安装。返回写入后的 state。"""
        module_id = description["id"]
        root = self.ensure()
        dest = root / module_id
        if dest.exists():
            raise ModulePackageError(
                f"模块 {module_id} 已安装", code="MODULE_EXISTS", status=409
            )

        staging = Path(
            tempfile.mkdtemp(prefix=f".installing-{module_id}-", dir=root)
        )
        try:
            package = staging / PACKAGE_DIR
            package.mkdir(parents=True, exist_ok=True)
            spec.extract_package(archive, package)
            (staging / DATA_DIR).mkdir(parents=True, exist_ok=True)
            state = {
                "enabled": True,
                "installedAt": utc_now_iso(),
                "updatedAt": utc_now_iso(),
                "version": description["version"],
            }
            _write_json_atomic(staging / STATE_FILE, state)
            _ensure_within(root, staging)
            if dest.exists():
                raise ModulePackageError(
                    f"模块 {module_id} 已安装", code="MODULE_EXISTS", status=409
                )
            replace_tree(staging, dest)
            return state
        except ModulePackageError:
            remove_tree(staging)
            raise
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            remove_tree(staging)
            raise ModulePackageError(f"安装失败：{exc}") from exc
        except Exception:
            remove_tree(staging)
            raise

    def install_directory(
        self,
        source: str | os.PathLike,
        description: dict,
    ) -> dict:
        """从源码目录安装（开发调试用，省掉打包/解压环节）。"""
        module_id = description["id"]
        root = self.ensure()
        dest = root / module_id
        if dest.exists():
            raise ModulePackageError(
                f"模块 {module_id} 已安装", code="MODULE_EXISTS", status=409
            )

        source_path = Path(source)
        staging = Path(
            tempfile.mkdtemp(prefix=f".installing-{module_id}-", dir=root)
        )
        total = 0
        try:
            package = staging / PACKAGE_DIR
            package.mkdir(parents=True, exist_ok=True)
            for rel, full in spec.iter_source_files(source_path):
                rel = spec.check_member_name(rel)
                target = package.joinpath(*rel.split("/"))
                _ensure_within(package, target)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(full, "rb") as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > spec.MAX_TOTAL_BYTES:
                            raise ModulePackageError(
                                "模块目录超过 "
                                f"{spec.MAX_TOTAL_BYTES // (1024 * 1024)}MB 上限"
                            )
                        dst.write(chunk)
            (staging / DATA_DIR).mkdir(parents=True, exist_ok=True)
            state = {
                "enabled": True,
                "installedAt": utc_now_iso(),
                "updatedAt": utc_now_iso(),
                "version": description["version"],
            }
            _write_json_atomic(staging / STATE_FILE, state)
            _ensure_within(root, staging)
            if dest.exists():
                raise ModulePackageError(
                    f"模块 {module_id} 已安装", code="MODULE_EXISTS", status=409
                )
            replace_tree(staging, dest)
            return state
        except ModulePackageError:
            remove_tree(staging)
            raise
        except OSError as exc:
            remove_tree(staging)
            raise ModulePackageError(f"安装模块失败：{exc}") from exc

    def uninstall(self, module_id: str) -> bool:
        """删除模块目录；返回是否真的删了东西。"""
        directory = self.module_dir(module_id)
        if not directory.is_dir():
            return False
        remove_tree(directory)
        return True
