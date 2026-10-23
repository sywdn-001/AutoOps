"""内置（随仓库分发）模块的启动同步。

内置模块走和第三方包一样的目录布局、校验与加载流程，唯一区别是来源是
``MODULES_SRC_DIR`` 而不是上传的 zip。这样做的好处是：内置模块也能被禁用、
卸载、修改设置，运行时没有两套代码路径。

只安装“目标目录里还不存在”的模块：仓库升级带来的新版本不会在用户没有确认的
情况下覆盖已装模块（升级是后续能力，当前先保证幂等与不丢数据）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import spec

logger = logging.getLogger("bastion.modules.builtin")


def sync_builtin(registry) -> list[str]:
    """把内置模块源码目录里尚未安装的模块复制进模块根目录。

    Returns:
        list[str]: 非致命错误；启动流程会把这些错误写进日志，但不会中断后端。
    """
    errors: list[str] = []
    app = registry.app
    raw_root = app.config.get("MODULES_SRC_DIR")
    if not raw_root:
        return errors
    source_root = Path(raw_root)
    if not source_root.is_dir():
        return errors

    app_version = str(app.config.get("APP_VERSION") or "")
    try:
        children = sorted(
            child for child in source_root.iterdir() if child.is_dir()
        )
    except OSError as exc:
        return [f"读取内置模块目录失败：{exc}"]

    for child in children:
        try:
            description = _read_builtin_description(child, app_version)
            if description is None:
                continue
            if registry.store.exists(description["id"]):
                continue
            registry.store.install_directory(child, description, builtin=True)
            logger.info(
                "内置模块已安装：%s %s", description["id"], description["version"]
            )
        except Exception as exc:  # noqa: BLE001 - 单个内置模块坏掉不能拖垮启动
            logger.exception("内置模块同步失败：%s", child.name)
            errors.append(f"{child.name}: {exc}")

    # 发布形态的内置模块是 ``modules-src/<id>/<name>.mod.ops_ext``（契约 §3）。
    # 开发态则是上面的源码目录；两种形态都要能启动，不能只认其中一种。
    for package in _iter_builtin_packages(source_root):
        try:
            archive = spec.parse_archive(package)
            description = spec.validate_parsed(
                archive, app_version=app_version or None
            )
            if registry.store.exists(description["id"]):
                continue
            registry.store.install_archive(archive, description, builtin=True)
            logger.info(
                "内置模块包已安装：%s %s", description["id"], description["version"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("内置模块包同步失败：%s", package)
            errors.append(f"{package.name}: {exc}")
    return errors


def _iter_builtin_packages(source_root: Path) -> list[Path]:
    """收集内置模块包，按路径去重。

    契约只约定了 ``<id>/*.mod.ops_ext``，但仓库根直接放包也很自然；两种都读，
    对同一个文件只安装一次。
    """
    try:
        candidates = list(source_root.glob("*.mod.ops_ext"))
        candidates.extend(source_root.glob("*/*.mod.ops_ext"))
    except OSError as exc:
        logger.warning("扫描内置模块包失败：%s", exc)
        return []
    unique: dict[Path, Path] = {}
    for path in candidates:
        if path.is_file():
            try:
                key = path.resolve()
            except OSError:
                key = path
            unique.setdefault(key, path)
    return [unique[key] for key in sorted(unique, key=lambda item: str(item))]


def _read_builtin_description(source: Path, app_version: str) -> dict | None:
    description_path = source / spec.DESCRIPTION_NAME
    if not description_path.is_file():
        logger.warning("内置模块目录缺少 description.json：%s", source)
        return None
    raw, _ = spec.read_description_file(description_path)
    description = spec.normalize_description(
        raw, app_version=app_version, allow_reserved=True
    )
    modules_dir = source / "modules"
    if modules_dir.exists() and not (modules_dir / "__init__.py").is_file():
        raise spec.ModulePackageError(
            f"内置模块 {description['id']} 的 modules/ 缺少 __init__.py"
        )
    return description
