"""模块注册表：把磁盘上的模块、运行时句柄、路由和设置串起来。

单例 ``registry`` 是进程内唯一入口。应用工厂可以被多次调用（测试里就是），
所以 ``reset`` 必须幂等：清内存注册、卸旧日志 handler，但不碰数据库里的
模块设置值——那是用户数据，重装/重启不应丢。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import Map, Rule

from .. import settings_store
from ..extensions import db
from ..models import SystemSetting
from . import loader, sdk
from .store import ModuleStore

MODULE_LOG_LINES = 200


class RingBufferHandler(logging.Handler):
    """每模块 200 行内存日志；``GET /api/modules/<id>/logs`` 直接读它。"""

    def __init__(self, capacity: int = MODULE_LOG_LINES) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if record.exc_info:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
        except Exception:  # pragma: no cover - 日志自身不能抛
            return
        with self._lock:
            self._lines.append(message)

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


class ModuleRecord:
    def __init__(
        self,
        *,
        description: dict,
        state: dict,
        size: int = 0,
        error: str = "",
    ) -> None:
        self.description = description
        self.state = state
        self.size = size
        self.loaded: loader.LoadedModule | None = None
        #: 是否存在 modules/__init__.py。纯前端模块没有 Python 入口，
        #: 对外仍应显示为「可用」，否则宿主页会把 js-demo 误判成加载失败。
        self.python_entry: bool = True
        self.errors: list[str] = [error] if error else []

    @property
    def id(self) -> str:
        return self.description["id"]

    @property
    def enabled(self) -> bool:
        return bool(self.state.get("enabled", True))

    @property
    def available(self) -> bool:
        """模块对前端是否可用：Python 入口已加载，或它本来就是纯前端模块。"""
        if self.loaded is not None:
            return True
        return self.enabled and not self.python_entry

    @property
    def builtin(self) -> bool:
        return bool(self.state.get("builtin"))

    def to_dict(self, app_version: str = "") -> dict:
        description = self.description
        module_id = self.id
        return {
            "id": module_id,
            "name": description.get("name", {"zh": module_id, "en": module_id}),
            "version": description.get("version", ""),
            "author": description.get("author", ""),
            "official": bool(description.get("official")),
            "description": description.get("description", {"zh": "", "en": ""}),
            "tags": list(description.get("tags", [])),
            "experimental": bool(description.get("experimental")),
            "icon": description.get("icon", ""),
            "license": description.get("license", ""),
            "homepage": description.get("homepage", ""),
            "minBastionVersion": description.get("minBastionVersion", ""),
            "images": [
                {
                    "path": image["path"],
                    "url": f"/api/modules/{module_id}/asset/{image['path']}",
                }
                for image in description.get("images", [])
            ],
            "entry": dict(description.get("entry", {"web": "web/index.html"})),
            "nav": [dict(item) for item in description.get("nav", [])],
            "settings": [dict(item) for item in description.get("settings", [])],
            "permissions": list(description.get("permissions", [])),
            "requires": list(description.get("requires", [])),
            "reserved": bool(description.get("reserved")),
            "enabled": self.enabled,
            "loaded": self.available,
            "installedAt": self.state.get("installedAt", ""),
            "updatedAt": self.state.get("updatedAt", ""),
            "size": int(self.size),
            "errors": list(self.errors),
            "builtin": self.builtin,
        }


class ModuleRegistry:
    def __init__(self) -> None:
        self.app = None
        self.store: ModuleStore | None = None
        self._records: dict[str, ModuleRecord] = {}
        self._log_buffers: dict[str, RingBufferHandler] = {}
        self._loggers: dict[str, logging.Logger] = {}
        self._route_maps: dict[str, Map] = {}
        self._lock = threading.RLock()

    # -- 初始化 / 扫描 ------------------------------------------------------

    def reset(self, app) -> None:
        with self._lock:
            for module_id, logger_ in list(self._loggers.items()):
                for handler in list(logger_.handlers):
                    if getattr(handler, "_autoops_module_log", False):
                        logger_.removeHandler(handler)
            old_ids = list(self._records)
            self.app = app
            self._records = {}
            self._log_buffers = {}
            self._loggers = {}
            self._route_maps = {}
            self.store = ModuleStore(app.config.get("MODULES_DIR"))
            sdk.reset_loaded_modules()
            sdk.bind_app(app)
            for module_id in old_ids:
                # 只清内存默认值；DB 行保留，重启不能把用户设置洗掉。
                try:
                    settings_store.unregister_module_defaults(module_id)
                except Exception:
                    logging.getLogger("bastion.modules").warning(
                        "注销模块设置默认值失败：%s", module_id, exc_info=True
                    )
                try:
                    # 上一个 Flask app 的模块包还在 sys.modules 里；不清掉的话
                    # 重载会拿到旧代码对象（测试里连续 create_app 尤其明显）。
                    loader.clear_import_state(module_id, keep_shim=False)
                except Exception:
                    logging.getLogger("bastion.modules").warning(
                        "清理旧模块导入失败：%s", module_id, exc_info=True
                    )
            if self.store.configured:
                try:
                    self.store.ensure()
                    self.store.cleanup_temp()
                except Exception:
                    logging.getLogger("bastion.modules").warning(
                        "初始化模块目录失败", exc_info=True
                    )

    def scan(self) -> None:
        """扫描磁盘并注册/加载模块；单个模块的问题只进它自己的 errors。"""
        if self.store is None or not self.store.configured:
            return
        app_version = str((self.app.config.get("APP_VERSION") if self.app else "") or "")
        for module_id in self.store.installed_ids():
            if module_id in self._records:
                continue
            try:
                description = self.store.load_description(
                    module_id, app_version=app_version or None
                )
                state = self.store.load_state(module_id)
                record = ModuleRecord(
                    description=description,
                    state=state,
                    size=self.store.package_size(module_id),
                )
                record.python_entry = self._has_python_entry(module_id)
                self._register_settings(record)
                self._records[module_id] = record
                if record.enabled:
                    self._load_record(record, strict=False)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("bastion.modules").warning(
                    "注册模块 %s 失败", module_id, exc_info=True
                )
                fallback = self._fallback_description(module_id)
                try:
                    state = self.store.load_state(module_id)
                except Exception:  # noqa: BLE001 - 坏 state.json 不能拖垮整个 scan
                    state = {}
                record = ModuleRecord(
                    description=fallback,
                    state=state,
                    size=self.store.package_size(module_id),
                    error=f"模块描述无效：{type(exc).__name__}: {exc}",
                )
                record.python_entry = self._has_python_entry(module_id)
                self._records[module_id] = record

    def get(self, module_id: str) -> ModuleRecord | None:
        return self._records.get(module_id)

    def all(self) -> list[ModuleRecord]:
        return [self._records[key] for key in sorted(self._records)]

    def loaded_all(self) -> list[loader.LoadedModule]:
        return [
            record.loaded for record in self._records.values() if record.loaded is not None
        ]

    # -- 设置 / 日志 --------------------------------------------------------

    def _register_settings(self, record: ModuleRecord) -> None:
        try:
            settings_store.unregister_module_defaults(record.id)
            items = [
                {**item, "key": item["rawKey"]}
                for item in record.description.get("settings", [])
            ]
            if items:
                settings_store.register_module_defaults(record.id, items)
        except Exception:
            record.errors.append("模块设置项注册失败")
            logging.getLogger("bastion.modules").warning(
                "注册模块设置失败：%s", record.id, exc_info=True
            )

    def _drop_db_settings(self, module_id: str) -> None:
        """卸载时删除该模块的设置行；这是唯一删 DB 的路径。"""
        try:
            SystemSetting.query.filter(
                SystemSetting.key.like(f"mod.{module_id}.%")
            ).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.getLogger("bastion.modules").warning(
                "删除模块设置失败：%s", module_id, exc_info=True
            )

    def logger_for(self, module_id: str) -> logging.Logger:
        logger_ = self._loggers.get(module_id)
        if logger_ is not None:
            return logger_
        logger_ = logging.getLogger(f"module.{module_id}")
        logger_.setLevel(logging.INFO)
        logger_.propagate = True
        for handler in list(logger_.handlers):
            if getattr(handler, "_autoops_module_log", False):
                logger_.removeHandler(handler)
        buffer = RingBufferHandler()
        buffer._autoops_module_log = True  # type: ignore[attr-defined]
        logger_.addHandler(buffer)
        self._loggers[module_id] = logger_
        self._log_buffers[module_id] = buffer
        return logger_

    def logs(self, module_id: str) -> list[str]:
        buffer = self._log_buffers.get(module_id)
        return buffer.lines() if buffer is not None else []

    # -- 加载 / 卸载 --------------------------------------------------------

    def _has_python_entry(self, module_id: str) -> bool:
        try:
            return (
                self.store.package_dir(module_id) / "modules" / "__init__.py"
            ).is_file()
        except Exception:  # noqa: BLE001 - 目录异常交给 scan/加载阶段报错
            return True

    def _load_record(self, record: ModuleRecord, *, strict: bool) -> bool:
        if record.loaded is not None:
            return True
        if not record.enabled:
            return False
        package_dir = self.store.package_dir(record.id)
        if not (package_dir / "modules" / "__init__.py").is_file():
            # 纯前端模块：没有 Python 入口，保持 loaded=false，但不算错误。
            return False
        logger_ = self.logger_for(record.id)
        try:
            loaded = loader.load_module(
                app=self.app,
                module_id=record.id,
                description=record.description,
                package_dir=package_dir,
                data_dir=self.store.data_dir(record.id),
                logger=logger_,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or f"{type(exc).__name__}"
            record.errors = [message]
            logger_.error("模块加载失败：%s", message)
            if strict:
                raise
            return False
        record.loaded = loaded
        record.errors = []
        self._route_maps[record.id] = self._build_route_map(loaded)
        return True

    @staticmethod
    def _build_route_map(loaded: loader.LoadedModule) -> Map:
        rules: list[Rule] = []
        for index, route in enumerate(loaded.routes):
            methods = set(route.methods)
            if "GET" in methods:
                methods.add("HEAD")
            rules.append(Rule(route.rule, methods=sorted(methods), endpoint=str(index)))
        return Map(rules)

    def enable(self, module_id: str) -> ModuleRecord:
        record = self._require(module_id)
        if record.loaded is not None and record.enabled:
            # 幂等：重复启用不能重复执行 setup/注册路由。
            return record
        was_enabled = record.enabled
        if not record.enabled:
            record.state = self.store.set_enabled(module_id, True)
            record.state["enabled"] = True
        record.errors = []
        try:
            loaded_ok = self._load_record(record, strict=True)
        except Exception:
            # 启用失败不能留下「enabled=true 但永远加载不了」的状态：
            # 否则重启后宿主每次都会重试一个坏模块，管理员也无法从列表看出真相。
            if not was_enabled:
                record.state = self.store.set_enabled(module_id, False)
                record.state["enabled"] = False
            raise
        if loaded_ok and record.loaded is not None:
            for message in loader.run_hooks(record.loaded, "enable", self.logger_for(module_id)):
                record.errors.append(message)
        return record

    def _unload_record(self, record: ModuleRecord, event: str) -> list[str]:
        loaded = record.loaded
        if loaded is None:
            return []
        logger_ = self.logger_for(record.id)
        errors = loader.run_hooks(loaded, event, logger_)
        errors.extend(loader.teardown_module(loaded, logger_))
        record.loaded = None
        self._route_maps.pop(record.id, None)
        keep_shim = any(
            other.loaded is not None for other in self._records.values() if other is not record
        )
        loader.dispose_module(loaded, keep_shim=keep_shim)
        return errors

    def disable(self, module_id: str) -> ModuleRecord:
        record = self._require(module_id)
        errors = self._unload_record(record, "disable")
        record.state = self.store.set_enabled(module_id, False)
        record.state["enabled"] = False
        for message in errors:
            if message not in record.errors:
                record.errors.append(message)
        return record

    def uninstall(self, module_id: str) -> ModuleRecord:
        record = self._require(module_id)
        errors: list[str] = []
        if record.loaded is not None:
            errors.extend(self._unload_record(record, "uninstall"))
        settings_store.unregister_module_defaults(module_id)
        self._drop_db_settings(module_id)
        self.store.uninstall(module_id)
        self._records.pop(module_id, None)
        self._route_maps.pop(module_id, None)
        logger_ = self._loggers.pop(module_id, None)
        buffer = self._log_buffers.pop(module_id, None)
        if logger_ is not None:
            for handler in list(logger_.handlers):
                if handler is buffer or getattr(handler, "_autoops_module_log", False):
                    logger_.removeHandler(handler)
        if errors:
            record.errors = errors
        return record

    def rollback_install(self, module_id: str) -> None:
        """安装后登记失败时清掉半成品。

        文件已经原子改名成功，但注册设置/加载对象这一步仍可能抛异常；
        如果只在 API 层返回 500，磁盘上会留下一个「已安装却不可管理」的目录。
        这里尽量把内存、设置和目录一起回滚，回滚自身的异常只记日志。
        """
        record = self._records.pop(module_id, None)
        if record is not None and record.loaded is not None:
            try:
                self._unload_record(record, "uninstall")
            except Exception:  # noqa: BLE001
                logging.getLogger("bastion.modules").warning(
                    "回滚模块 %s 时卸载失败", module_id, exc_info=True
                )
        self._route_maps.pop(module_id, None)
        settings_store.unregister_module_defaults(module_id)
        try:
            self._drop_db_settings(module_id)
        except Exception:  # noqa: BLE001
            logging.getLogger("bastion.modules").warning(
                "回滚模块 %s 时删除设置失败", module_id, exc_info=True
            )
        try:
            self.store.uninstall(module_id)
        except Exception:  # noqa: BLE001
            logging.getLogger("bastion.modules").warning(
                "回滚模块 %s 时删除目录失败", module_id, exc_info=True
            )
        logger_ = self._loggers.pop(module_id, None)
        buffer = self._log_buffers.pop(module_id, None)
        if logger_ is not None:
            for handler in list(logger_.handlers):
                if handler is buffer or getattr(handler, "_autoops_module_log", False):
                    logger_.removeHandler(handler)

    def _require(self, module_id: str) -> ModuleRecord:
        record = self._records.get(module_id)
        if record is None:
            raise KeyError(module_id)
        return record

    # -- 安装 ---------------------------------------------------------------

    def add_installed(
        self,
        description: dict,
        state: dict,
        *,
        module_id: str | None = None,
    ) -> ModuleRecord:
        module_id = module_id or description["id"]
        record = ModuleRecord(
            description=description,
            state=state,
            size=self.store.package_size(module_id),
        )
        record.python_entry = self._has_python_entry(module_id)
        self._register_settings(record)
        self._records[module_id] = record
        if record.enabled:
            try:
                self._load_record(record, strict=False)
                if record.loaded is not None:
                    for message in loader.run_hooks(
                        record.loaded, "install", self.logger_for(module_id)
                    ):
                        record.errors.append(message)
            except Exception as exc:  # noqa: BLE001
                record.errors.append(str(exc))
        return record

    # -- 路由 ---------------------------------------------------------------

    def match_route(self, module_id: str, path: str, method: str):
        """返回 ``(RouteEntry, values)``；未加载/找不到时抛 werkzeug HTTPException。"""
        record = self._records.get(module_id)
        loaded = record.loaded if record is not None else None
        if loaded is None:
            raise NotFound()
        route_map = self._route_maps.get(module_id)
        if route_map is None:
            route_map = self._build_route_map(loaded)
            self._route_maps[module_id] = route_map
        adapter = route_map.bind("")
        normalized = path if path.startswith("/") else f"/{path}"
        endpoint, values = adapter.match(normalized, method=method)
        index = int(endpoint)
        return loaded.routes[index], values

    def run_before_request(self, module_id: str):
        loaded = self._loaded(module_id)
        if loaded is None:
            return None
        for func in loaded.before_request:
            result = func()
            if result is not None:
                return result
        return None

    def run_after_request(self, module_id: str, response):
        loaded = self._loaded(module_id)
        if loaded is None:
            return response
        for func in reversed(loaded.after_request):
            updated = func(response)
            if updated is not None:
                response = updated
        return response

    def run_teardown_request(self, module_id: str, exc) -> None:
        loaded = self._loaded(module_id)
        if loaded is None:
            return
        for func in loaded.teardown_request:
            try:
                func(exc)
            except Exception:
                logging.getLogger("bastion.modules").warning(
                    "模块 %s 的 teardown_request 失败", module_id, exc_info=True
                )

    def _loaded(self, module_id: str) -> loader.LoadedModule | None:
        record = self._records.get(module_id)
        return record.loaded if record is not None else None

    # -- 描述 ---------------------------------------------------------------

    @staticmethod
    def _fallback_description(module_id: str) -> dict:
        name = {"zh": module_id, "en": module_id}
        return {
            "id": module_id,
            "name": name,
            "version": "",
            "author": "",
            "official": False,
            "description": {"zh": "", "en": ""},
            "tags": [],
            "experimental": False,
            "icon": "",
            "license": "",
            "homepage": "",
            "minBastionVersion": "",
            "images": [],
            "entry": {"web": "web/index.html"},
            "nav": [],
            "settings": [],
            "permissions": [f"module:{module_id}:use"],
            "requires": [],
            "reserved": False,
        }


#: 进程级单例；``app.modules.registry`` 对外就是它，不是模块对象。
registry = ModuleRegistry()
