"""按文件路径加载模块的 ``package/modules`` 包。

两条铁律：
1. **安装期绝不 import**：这里只在安装成功、模块被启用时调用；
2. 模块之间不能串味：每次加载前清理 ``autoops_mod_<id>*``，卸载后清理
   ``autoops`` 门面，别让上一个模块的同名子模块或旧代码残留。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import sdk
from .context import ModuleApiError, ModuleContext, ModuleError, call_hook

AUTOOP_PACKAGE = "autoops"
MODULE_PACKAGE_PREFIX = "autoops_mod_"

_HOOK_FUNCTIONS = {
    "teardown": "teardown",
    "on_install": "install",
    "on_uninstall": "uninstall",
    "on_enable": "enable",
    "on_disable": "disable",
}


class ModuleLoadError(Exception):
    """模块导入 / setup / 钩子执行失败。消息可以直接回给管理员。"""


@dataclass
class LoadedModule:
    module_id: str
    module: types.ModuleType
    ctx: ModuleContext
    hooks: dict[str, list[Callable]] = field(default_factory=dict)
    teardown: Callable | None = None

    @property
    def routes(self):
        return self.ctx._routes

    @property
    def before_request(self):
        return self.ctx._before_request

    @property
    def after_request(self):
        return self.ctx._after_request

    @property
    def teardown_request(self):
        return self.ctx._teardown_request


def _ensure_shim() -> types.ModuleType:
    """注入 ``autoops`` 门面。

    模块源码里写的是 ``from autoops import bastion``。宿主自己的包叫 ``app``，
    不能让第三方模块依赖 ``app.*``；这里用一个只有 ``__path__ = []`` 的轻量
    包，把 ``bastion`` 指到 SDK 单例上。
    """
    existing = sys.modules.get(AUTOOP_PACKAGE)
    if isinstance(existing, types.ModuleType) and getattr(
        existing, "__autoops_shim__", False
    ):
        return existing

    shim = types.ModuleType(AUTOOP_PACKAGE)
    shim.__path__ = []  # 明确是包，但不从磁盘找子模块
    shim.__package__ = AUTOOP_PACKAGE
    shim.__doc__ = "AutoOps 扩展模块门面；所有能力都在 autoops.bastion 下。"
    shim.__autoops_shim__ = True
    shim.bastion = sdk
    shim.ModuleApiError = ModuleApiError
    shim.ModuleError = ModuleError
    sys.modules[AUTOOP_PACKAGE] = shim
    sys.modules[f"{AUTOOP_PACKAGE}.bastion"] = sdk
    return shim


def _clear_shim() -> None:
    for name in (f"{AUTOOP_PACKAGE}.bastion", AUTOOP_PACKAGE):
        if name in sys.modules:
            sys.modules.pop(name, None)


def _module_package_name(module_id: str) -> str:
    return f"{MODULE_PACKAGE_PREFIX}{module_id}"


def clear_module_imports(module_id: str) -> None:
    """清掉该模块自己的包和所有子模块；不会动共享门面。"""
    package = _module_package_name(module_id)
    for name in [
        name
        for name in sys.modules
        if name == package or name.startswith(package + ".")
    ]:
        sys.modules.pop(name, None)


def clear_import_state(module_id: str, *, keep_shim: bool = False) -> None:
    """卸载后的彻底清理。``keep_shim`` 用于还有其他模块在运行时保留门面。"""
    clear_module_imports(module_id)
    sdk.unregister_loaded_module(module_id)
    if keep_shim:
        _ensure_shim()
    else:
        _clear_shim()


def load_module(
    *,
    app,
    module_id: str,
    description: dict,
    package_dir: Path,
    data_dir: Path,
    logger,
) -> LoadedModule:
    modules_dir = Path(package_dir) / "modules"
    init_path = modules_dir / "__init__.py"
    if not init_path.is_file():
        # 契约允许纯前端模块（没有 modules/）；只有调用方明确要加载 Python
        # 扩展时才走到这里，所以缺文件是错误而不是「跳过」。
        raise ModuleLoadError(f"模块 {module_id} 没有 modules/__init__.py")

    _ensure_shim()
    clear_module_imports(module_id)

    package_name = _module_package_name(module_id)
    try:
        spec = importlib.util.spec_from_file_location(
            package_name,
            str(init_path),
            submodule_search_locations=[str(modules_dir)],
        )
        if spec is None or spec.loader is None:
            raise ModuleLoadError(f"无法为模块 {module_id} 创建 import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)
    except ModuleLoadError:
        clear_module_imports(module_id)
        sdk.unregister_loaded_module(module_id)
        raise
    except Exception as exc:  # noqa: BLE001 - 任何导入错误都只影响本模块
        clear_module_imports(module_id)
        sdk.unregister_loaded_module(module_id)
        logger.exception("导入模块代码失败：%s", module_id)
        raise ModuleLoadError(
            f"导入模块 {module_id} 失败：{type(exc).__name__}: {exc}"
        ) from exc

    setup = getattr(module, "setup", None)
    if not callable(setup):
        clear_module_imports(module_id)
        sdk.unregister_loaded_module(module_id)
        raise ModuleLoadError(f"模块 {module_id} 的 modules/__init__.py 缺少 setup(ctx)")

    ctx = ModuleContext(
        module_id=module_id,
        description=description,
        app=app,
        package_dir=package_dir,
        data_dir=data_dir,
        logger_=logger,
    )

    try:
        with sdk.module_scope(module_id):
            sdk.register_loaded_module(module_id)
            call_hook(setup, ctx)
            ctx._finalize_blueprints()
    except Exception as exc:  # noqa: BLE001
        clear_module_imports(module_id)
        sdk.unregister_loaded_module(module_id)
        logger.exception("模块 setup 失败：%s", module_id)
        if isinstance(exc, ModuleApiError):
            raise ModuleLoadError(f"模块 {module_id} setup 失败：{exc.message}") from exc
        raise ModuleLoadError(
            f"模块 {module_id} setup 失败：{type(exc).__name__}: {exc}"
        ) from exc

    hooks = {name: list(handlers) for name, handlers in ctx._hooks.items()}
    teardown = getattr(module, "teardown", None)
    if teardown is not None and not callable(teardown):
        teardown = None
    for function_name, event in _HOOK_FUNCTIONS.items():
        if function_name == "teardown":
            continue
        handler = getattr(module, function_name, None)
        if callable(handler) and not any(
            handler is existing for existing in hooks[event]
        ):
            hooks[event].append(handler)

    loaded = LoadedModule(
        module_id=module_id,
        module=module,
        ctx=ctx,
        hooks=hooks,
        teardown=teardown if callable(teardown) else None,
    )
    logger.info(
        "模块 %s v%s 已加载（%d 条路由）", module_id, description.get("version"), len(ctx._routes)
    )
    return loaded


def run_hooks(loaded: LoadedModule, event: str, logger) -> list[str]:
    """依次执行某类生命周期钩子，单个失败只记录不打断后续钩子。"""
    errors: list[str] = []
    for handler in list(loaded.hooks.get(event, [])):
        try:
            with sdk.module_scope(loaded.module_id):
                call_hook(handler, loaded.ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("模块 %s 的 %s 钩子失败", loaded.module_id, event)
            errors.append(f"{event} 钩子失败：{type(exc).__name__}: {exc}")
    return errors


def teardown_module(loaded: LoadedModule, logger) -> list[str]:
    errors: list[str] = []
    if loaded.teardown is not None:
        try:
            with sdk.module_scope(loaded.module_id):
                call_hook(loaded.teardown, loaded.ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("模块 %s 的 teardown 失败", loaded.module_id)
            errors.append(f"teardown 失败：{type(exc).__name__}: {exc}")
    return errors


def dispose_module(loaded: LoadedModule, *, keep_shim: bool) -> None:
    clear_import_state(loaded.module_id, keep_shim=keep_shim)


def module_attribute(loaded: LoadedModule, name: str, default: Any = None) -> Any:
    return getattr(loaded.module, name, default)
