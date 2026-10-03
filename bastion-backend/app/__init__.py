"""AutoOps 堡垒机 —— Flask 应用工厂。"""

from __future__ import annotations

import logging
import os
import sys

from flask import Flask, abort, jsonify, redirect, request

from .config import Config, TestConfig
from .extensions import cors, db, jwt, socketio

log = logging.getLogger(__name__)

#: 供 SSH 网关等后台线程使用的应用引用（网关线程不在 Flask 请求上下文里）
_app: Flask | None = None


def get_current_app() -> Flask | None:
    """优先取请求上下文里的应用，否则退回最近一次 create_app 的结果。"""
    try:
        from flask import current_app

        return current_app._get_current_object()
    except RuntimeError:
        return _app


def _configure_logging(app: Flask) -> None:
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _register_error_handlers(app: Flask) -> None:
    from .utils import api_error

    @app.errorhandler(400)
    def _bad_request(err):
        return api_error(getattr(err, "description", "请求参数有误"), 400, code="BAD_REQUEST")

    @app.errorhandler(401)
    def _unauthorized(err):
        return api_error("登录状态无效或已过期，请重新登录", 401, code="UNAUTHORIZED")

    @app.errorhandler(403)
    def _forbidden(err):
        return api_error("没有访问该资源的权限", 403, code="FORBIDDEN")

    @app.errorhandler(404)
    def _not_found(err):
        return api_error("接口不存在", 404, code="NOT_FOUND")

    @app.errorhandler(405)
    def _method_not_allowed(err):
        return api_error("请求方法不被允许", 405, code="METHOD_NOT_ALLOWED")

    @app.errorhandler(500)
    def _server_error(err):  # pragma: no cover
        db.session.rollback()
        log.exception("未处理的服务端异常")
        return api_error("服务端异常，请联系管理员", 500, code="SERVER_ERROR")

    @app.errorhandler(Exception)
    def _unhandled(err):  # pragma: no cover
        from werkzeug.exceptions import HTTPException

        if isinstance(err, HTTPException):
            return err
        db.session.rollback()
        log.exception("未处理的异常")
        return api_error(f"服务端异常：{err}", 500, code="SERVER_ERROR")


def _register_jwt(app: Flask) -> None:
    @jwt.expired_token_loader
    def _expired(_header, _payload):
        return jsonify({"success": False, "code": "TOKEN_EXPIRED", "message": "登录已过期，请重新登录"}), 401

    @jwt.invalid_token_loader
    def _invalid(reason):
        return jsonify({"success": False, "code": "TOKEN_INVALID", "message": f"登录凭据无效：{reason}"}), 401

    @jwt.unauthorized_loader
    def _missing(reason):
        return jsonify({"success": False, "code": "TOKEN_MISSING", "message": f"缺少登录凭据：{reason}"}), 401

    @jwt.revoked_token_loader
    def _revoked(_header, _payload):
        return jsonify({"success": False, "code": "TOKEN_REVOKED", "message": "登录凭据已被吊销"}), 401

    def _in_blocklist(_header, payload) -> bool:
        from .api.auth import is_revoked

        return is_revoked(payload.get("jti", ""))

    loader = getattr(jwt, "token_in_blocklist_loader", None) or getattr(
        jwt, "token_in_blacklist_loader", None
    )
    if loader is not None:
        loader(_in_blocklist)


def _register_blueprints(app: Flask) -> None:
    from .api import register_blueprints

    register_blueprints(app)

    @app.get("/api/health")
    def _health():
        return jsonify(
            {
                "success": True,
                "message": "AutoOps 堡垒机服务运行中",
                "data": {
                    "service": "bastion-backend",
                    "gateway_enabled": bool(app.config.get("GATEWAY_ENABLED")),
                    "gateway_port": app.config.get("GATEWAY_PORT"),
                },
            }
        )


def _register_frontend(app: Flask) -> None:
    """可选：直接托管 ant-design-pro 的构建产物（``dist/``），实现单进程部署。

    - 未配置 ``FRONTEND_DIST`` / 目录不存在 / ``SERVE_FRONTEND=False`` 时，``/`` 回落到
      ``/api/health`` 重定向，纯 API 模式不受影响。
    - ``/api/*`` 与 ``/socket.io/*`` 走各自蓝图，不受通配路由影响；
      其余路径命中真实文件就发文件，否则回落 ``index.html``（前端 history 路由）。
    """
    from flask import send_from_directory

    dist = app.config.get("FRONTEND_DIST")
    serve = bool(app.config.get("SERVE_FRONTEND", True))
    dist_abs = os.path.abspath(os.fspath(dist)) if dist else ""
    index_file = os.path.join(dist_abs, "index.html")

    if not (serve and dist_abs and os.path.isfile(index_file)):
        log.info(
            "未发现前端构建产物（FRONTEND_DIST=%s），仅提供 API；"
            "如需单进程部署请先在 ant-design-pro 目录执行 npm run build",
            dist_abs or "<未配置>",
        )

        @app.get("/")
        def _index_api_only():
            return redirect("/api/health")

        return

    @app.get("/")
    @app.get("/<path:path>")
    def _frontend(path: str = ""):  # noqa: ANN202
        # API 与 Socket.IO 命名空间必须保持 JSON 404 信封，不能被前端 index.html 吞掉
        if path and path.split("/", 1)[0] in {"api", "socket.io"}:
            abort(404)
        target = os.path.join(dist_abs, path) if path else ""
        if path and os.path.isfile(target):
            return send_from_directory(dist_abs, path)
        return send_from_directory(dist_abs, "index.html")

    log.info("已挂载前端构建产物：%s", dist_abs)


def seed_data(app: Flask) -> None:
    """初始化内置角色 / 管理员 / 默认策略 / 系统设置。"""
    from .extensions import db as _db
    from .file_policy import seed_file_policies
    from .models import HostGroup, Role, User
    from .policy import seed_policies
    from .security import hash_password
    from .settings_store import ensure_defaults

    builtin_roles = [
        (
            "admin",
            "系统管理员",
            "拥有全部权限，可添加机器、账号、授权与策略",
            ["*"],
        ),
        (
            "ops",
            "运维工程师",
            "可登录已授权主机执行命令，查看会话与命令审计",
            [
                "dashboard:view",
                "host:view",
                "account:view",
                "group:view",
                "grant:view",
                "policy:view",
                "session:view",
                "session:view_all",
                "session:replay",
                "session:terminate",
                "command:view",
                "command:view_all",
                "audit:view",
                "terminal:use",
                "file:use",
                "filepolicy:view",
            ],
        ),
        (
            "auditor",
            "安全审计员",
            "只读查看会话录像、命令记录与审计日志，无法登录主机",
            [
                "dashboard:view",
                "host:view",
                "policy:view",
                "session:view",
                "session:view_all",
                "session:replay",
                "command:view",
                "command:view_all",
                "audit:view",
            ],
        ),
        (
            "viewer",
            "只读观察者",
            "只能看资产与会话概览",
            ["dashboard:view", "host:view", "session:view", "command:view"],
        ),
    ]

    for code, name, description, permissions in builtin_roles:
        role = Role.query.filter_by(code=code).first()
        if role is None:
            role = Role(
                code=code,
                name=name,
                description=description,
                permissions=permissions,
                is_builtin=True,
            )
            _db.session.add(role)
        else:
            role.name = name
            role.description = description
            role.permissions = permissions
            role.is_builtin = True
    # 必须无条件提交：老库里角色都已存在，只改不提交等于没改
    # （否则新增/调整内置权限对已有安装永远不生效 —— 例如 file:use 加不进去）
    _db.session.commit()

    admin_role = Role.query.filter_by(code="admin").first()
    admin = User.query.filter_by(username=Config.DEFAULT_ADMIN_USERNAME).first()
    if admin is None:
        admin = User(
            username=Config.DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(Config.DEFAULT_ADMIN_PASSWORD),
            display_name="超级管理员",
            role_id=admin_role.id,
            is_superuser=True,
            status="active",
            must_change_password=True,
            remark="系统内置账号，首次登录请立即修改密码",
        )
        _db.session.add(admin)
        _db.session.commit()
        app.logger.warning(
            "已创建内置管理员账号：%s / %s（请立即修改密码）",
            Config.DEFAULT_ADMIN_USERNAME,
            Config.DEFAULT_ADMIN_PASSWORD,
        )

    if HostGroup.query.count() == 0:
        _db.session.add(HostGroup(name="默认分组", description="系统内置分组"))
        _db.session.commit()

    seed_policies(_db.session)
    seed_file_policies(_db.session)
    ensure_defaults()
    # 种子阶段统一收口：内置命令策略/文件策略同样只 flush 不提交，
    # 老库升级（模板版本变更）必须真的落库，否则规则刷新是假动作。
    _db.session.commit()


def create_app(config_object=None, *, create_tables: bool = True, seed: bool = True) -> Flask:
    global _app

    config_object = config_object or Config
    app = Flask(
        __name__,
        instance_path=os.fspath(Config.INSTANCE_DIR),
        instance_relative_config=True,
    )
    app.config.from_object(config_object)

    _configure_logging(app)
    db.init_app(app)
    jwt.init_app(app)
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}},
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        expose_headers=["Content-Disposition"],
    )
    socketio.init_app(
        app,
        cors_allowed_origins=app.config.get("CORS_ORIGINS", "*"),
        async_mode="threading",
        ping_interval=20,
        ping_timeout=60,
        logger=False,
        engineio_logger=False,
    )

    _register_jwt(app)
    _register_error_handlers(app)
    _register_blueprints(app)
    _register_frontend(app)

    with app.app_context():
        if create_tables:
            from . import models  # noqa: F401 - 确保模型已注册
            from .schema_sync import ensure_schema

            db.create_all()
            # create_all 只建新表；老库缺的列在这里补（只增不减的轻量迁移）
            ensure_schema()
        if seed:
            seed_data(app)

    # 注册 WebSocket 事件（必须在 socketio.init_app 之后：init_app 会重建 server，
    # 事件处理器必须重新挂到当前 server 上，否则同进程内第二个 app 收不到任何事件）
    from .webterm import events as _webterm_events  # noqa: F401

    _webterm_events.register_events(app)

    # 空闲超时清理：参数设置里的 session_idle_timeout 必须真的生效，否则客户端异常离开
    # 的会话会一直挂在在线会话表里（实测会撞满文件管理器会话上限而打不开窗口）。
    if seed:
        from . import idle_sweeper

        idle_sweeper.start(app)

    _app = app
    return app
