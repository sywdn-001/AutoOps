"""模块运行时上下文（``ModuleContext``）：模块唯一允许触碰宿主的入口。

为什么用上下文对象而不是全局函数：模块需要按实例隔离（设置、数据目录、
日志、路由、生命周期钩子）。把 ``app`` 直接交给模块会让第三方很容易绕过
权限/审计，所以只暴露 SDK 与这里的薄封装。
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from flask import Blueprint
from flask.blueprints import BlueprintSetupState

from ..extensions import db, socketio
from ..settings_store import (
    get_setting as _store_get_setting,
    module_setting_key,
    set_setting as _store_set_setting,
)

logger = logging.getLogger("bastion.modules.context")


class ModuleApiError(Exception):
    """模块视图主动抛出的业务错误，由分发器转成统一错误信封。

    ``autoops.ModuleApiError`` 与这里是同一个类；模块只有通过 ``autoops``
    才能拿到它，避免第三方直接依赖 ``app.modules.context``。
    """

    def __init__(self, message: str, status: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.message = str(message)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 400
        self.status = status if 400 <= status <= 599 else 400
        self.code = code


class ModuleError(Exception):
    """SDK 操作失败（SSH 策略拒绝、连接失败等）的通用异常。"""


class RouteEntry:
    __slots__ = ("rule", "methods", "view", "permission")

    def __init__(
        self, rule: str, methods: tuple[str, ...], view: Callable, permission: str
    ) -> None:
        self.rule = rule
        self.methods = methods
        self.view = view
        self.permission = permission


class _CaptureApp:
    """给 ``BlueprintSetupState`` 用的假 app：只收集 deferred 路由，不真注册。

    Flask 3.1 在第一个请求之后禁止 ``add_url_rule``，而模块是运行时安装的；
    所以把 Blueprint 的 deferred 函数按它本来会走的路径「预演」一遍，落到
    registry 的分发器里。这样模块作者拿到的仍是**真的 Blueprint**。
    """

    def __init__(self, ctx: "ModuleContext") -> None:
        self.ctx = ctx
        # Blueprint 的 url_prefix 会被 BlueprintSetupState 拼进 rule；分发器
        # 收到的 path 已经去掉了这段前缀，收集时要还原成模块内的相对路径。
        self.prefix = ctx.api_prefix
        self.before_request_funcs: dict = {}
        self.after_request_funcs: dict = {}
        self.teardown_request_funcs: dict = {}
        self.url_value_preprocessors: dict = {}
        self.url_default_functions: dict = {}
        self.error_handler_spec: dict = {}
        self.template_context_processors: dict = {}

    def add_url_rule(self, rule, endpoint=None, view_func=None, **_options) -> None:
        rule = str(rule or "")
        if self.prefix and (rule == self.prefix or rule.startswith(self.prefix + "/")):
            rule = rule[len(self.prefix):] or "/"
        self.ctx._register_route(
            rule,
            _options.pop("methods", None),
            view_func,
            _options.pop("permission", None),
        )


class ModuleContext:
    def __init__(
        self,
        *,
        module_id: str,
        description: dict,
        app,
        package_dir: Path,
        data_dir: Path,
        logger_: logging.Logger,
    ) -> None:
        self.id = module_id
        self.version = description["version"]
        self.author = description["author"]
        self.official = bool(description["official"])
        self.app = app
        self.package_dir = Path(package_dir)
        self.data_dir = Path(data_dir)
        self.logger = logger_
        self.description = description

        self._routes: list[RouteEntry] = []
        self._hooks: dict[str, list[Callable]] = {
            "install": [],
            "uninstall": [],
            "enable": [],
            "disable": [],
        }
        self._before_request: list[Callable] = []
        self._after_request: list[Callable] = []
        self._teardown_request: list[Callable] = []
        self._blueprints: list[Blueprint] = []
        self._setting_items = {
            item["rawKey"]: item for item in description.get("settings", [])
        }
        self.settings: dict[str, str] = {
            raw: self.get_setting(raw) for raw in self._setting_items
        }

    # -- 设置 ---------------------------------------------------------------

    def _full_key(self, key: str) -> str:
        text = str(key or "").strip()
        if not text:
            raise ModuleApiError("设置项 key 不能为空", 400)
        if text.startswith(f"mod.{self.id}."):
            return text
        return module_setting_key(self.id, text)

    def get_setting(self, key: str, default: str | None = None) -> str:
        full = self._full_key(key)
        try:
            return _store_get_setting(full, default)
        except Exception:
            # 数据库暂不可用（如 create_tables=False 的维护脚本）时退回声明默认值，
            # 模块加载不能因为读设置失败就整体挂掉。
            self.logger.warning("读取模块设置失败：%s", full, exc_info=True)
            item = self._setting_items.get(str(key).strip())
            if default is not None:
                return str(default)
            return str(item.get("default", "")) if item else ""

    def set_setting(self, key: str, value) -> str:
        raw = str(key or "").strip()
        if raw not in self._setting_items:
            raise KeyError(f"未声明的模块设置项：{raw}")
        full = self._full_key(raw)
        _store_set_setting(full, value)
        db.session.commit()
        current = _store_get_setting(full)
        self.settings[raw] = current
        return current

    # -- 路由 ---------------------------------------------------------------

    def _default_permission(self) -> str:
        nav = self.description.get("nav") or []
        if nav and nav[0].get("permission"):
            return nav[0]["permission"]
        return f"module:{self.id}:use"

    def _register_route(
        self,
        path: Any,
        methods: Iterable[str] | str | None,
        view: Callable | None,
        permission: str | None,
    ) -> None:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ModuleApiError(f"模块路由必须以 / 开头：{path!r}", 400)
        if not callable(view):
            raise ModuleApiError(f"模块路由 {path} 缺少视图函数", 400)
        if "\\" in path or "\x00" in path or "?" in path or "#" in path:
            raise ModuleApiError(f"模块路由路径非法：{path}", 400)
        if methods is None:
            methods = ("GET",)
        elif isinstance(methods, str):
            methods = (methods,)
        normalized_methods: list[str] = []
        for method in methods:
            text = str(method).strip().upper()
            if text not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
            }:
                raise ModuleApiError(f"模块路由 {path} 使用了不支持的 HTTP 方法：{method}", 400)
            if text not in normalized_methods:
                normalized_methods.append(text)
        if not normalized_methods:
            normalized_methods = ["GET"]
        self._routes.append(
            RouteEntry(
                rule=path,
                methods=tuple(normalized_methods),
                view=view,
                permission=str(permission or self._default_permission()),
            )
        )

    def route(
        self,
        path: str,
        methods: Iterable[str] | str = ("GET",),
        permission: str | None = None,
    ):
        """装饰器：``@ctx.route("/stats")``。"""

        def decorator(view: Callable) -> Callable:
            self._register_route(path, methods, view, permission)
            return view

        return decorator

    @property
    def api_prefix(self) -> str:
        """模块 HTTP 路由在宿主上的挂载前缀（Blueprint 与分发器共用）。"""
        return f"/api/modules/{self.id}/api"

    def blueprint(self, name: str = "api") -> Blueprint:
        """返回真 Blueprint；路由在 setup 结束后由 loader 统一收集。"""
        safe_name = "".join(
            ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(name or "api")
        )
        bp = Blueprint(
            f"module_{self.id}_{safe_name}",
            __name__,
            url_prefix=self.api_prefix,
        )
        self._blueprints.append(bp)
        return bp

    def _finalize_blueprints(self) -> None:
        for bp in self._blueprints:
            capture_app = _CaptureApp(self)
            try:
                state = BlueprintSetupState(bp, capture_app, {}, False)
                for deferred in bp.deferred_functions:
                    try:
                        deferred(state)
                    except Exception:
                        # 单个 deferred（比如 app_template_filter）失败不应拖垮整组路由
                        self.logger.warning(
                            "收集模块 %s 的 Blueprint 路由失败", self.id, exc_info=True
                        )
            except Exception:
                self.logger.warning("模块 %s 的 Blueprint 无法收集", self.id, exc_info=True)
            self._before_request.extend(capture_app.before_request_funcs.get(None, []))
            self._after_request.extend(capture_app.after_request_funcs.get(None, []))
            self._teardown_request.extend(
                capture_app.teardown_request_funcs.get(None, [])
            )

    # -- 生命周期 -----------------------------------------------------------

    def on(self, event: str, handler: Callable) -> None:
        name = str(event or "").strip().lower()
        if name not in self._hooks:
            raise ModuleApiError(
                f"不支持的生命周期事件：{event}（仅 install/uninstall/enable/disable）",
                400,
            )
        if not callable(handler):
            raise ModuleApiError("生命周期钩子必须是可调用对象", 400)
        self._hooks[name].append(handler)

    # -- 事件 ---------------------------------------------------------------

    def emit(self, topic: str, payload: Any = None) -> None:
        name = str(topic or "").strip()
        if not name or ":" in name or len(name) > 64:
            raise ModuleApiError("事件 topic 不能为空、不能含冒号且最长 64 字符", 400)
        try:
            socketio.emit(f"module:{self.id}:{name}", payload)
        except Exception:
            # 通知失败不能让模块业务失败（例如 Socket.IO 尚未初始化）
            self.logger.warning("发送模块事件失败：%s", name, exc_info=True)


def call_hook(handler: Callable, ctx: ModuleContext) -> None:
    """兼容 ``def hook()`` 与 ``def hook(ctx)`` 两种写法。

    示例模块用的是 ``teardown(ctx)``，契约里也有不带参数的写法；不要用
    「先无参调用、TypeError 再带参」的试探——那会把钩子内部的 TypeError
    误判成签名不匹配，导致钩子被执行两次。
    """
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        handler(ctx)
        return
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if not positional:
        handler()
    else:
        handler(ctx)
