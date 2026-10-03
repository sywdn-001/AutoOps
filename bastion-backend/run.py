#!/usr/bin/env python
"""堡垒机统一启动入口：Web 后台（Flask + Socket.IO）与 SSH 网关（同进程线程）。

用法::

    python run.py                        # 同时启动 Web(5000) 与 SSH 网关(2222)
    python run.py --no-gateway           # 只启动 Web 后台
    python run.py --port 8080 --gateway-port 2222
"""

from __future__ import annotations

import argparse
import logging
import sys

from app import create_app
from app.config import Config
from app.extensions import socketio

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Python Flask 堡垒机")
    parser.add_argument("--host", default=Config.HOST, help="Web 监听地址")
    parser.add_argument("--port", type=int, default=Config.PORT, help="Web 监听端口")
    parser.add_argument("--gateway-host", default=None, help="SSH 网关监听地址")
    parser.add_argument("--gateway-port", type=int, default=None, help="SSH 网关监听端口")
    parser.add_argument("--no-gateway", action="store_true", help="不启动 SSH 网关")
    parser.add_argument("--debug", action="store_true", help="调试模式（不要在生产开启）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--reset-admin", action="store_true", help="把管理员 admin 的密码重置为初始密码")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format=LOG_FORMAT)
    log = logging.getLogger("bastion")

    if args.gateway_host:
        Config.GATEWAY_HOST = args.gateway_host
    if args.gateway_port:
        Config.GATEWAY_PORT = args.gateway_port

    app = create_app()

    with app.app_context():
        from app.session_service import reconcile_stale_sessions

        fixed = reconcile_stale_sessions(app)
        if fixed:
            log.warning("发现 %d 个上次进程残留的在线会话，已标记为中断", fixed)

    if args.reset_admin:
        with app.app_context():
            _reset_admin()
            log.warning("管理员 admin 的密码已重置为初始密码")
        return 0

    gateway = None
    if Config.GATEWAY_ENABLED and not args.no_gateway:
        from app.gateway import start_gateway

        gateway = start_gateway(app)
        if gateway is not None and getattr(gateway, "last_error", ""):
            log.error("SSH 网关启动失败：%s", gateway.last_error)
        elif gateway is not None:
            log.info("SSH 网关已监听 %s:%s", gateway.host, gateway.port)
    else:
        log.info("SSH 网关未启动（--no-gateway 或配置 GATEWAY_ENABLED=False）")

    print("=" * 72)
    print("  Python Flask 堡垒机")
    print(f"  后台地址   : http://{_display_host(args.host)}:{args.port}")
    print(f"  API 健康检查: http://{_display_host(args.host)}:{args.port}/api/health")
    if gateway is not None:
        print(f"  SSH 网关   : ssh -p {gateway.port} <堡垒机用户名>@{_display_host(gateway.host)}")
        print(f"  网关主机指纹: {getattr(gateway, 'fingerprint', '')}")
    else:
        print("  SSH 网关   : 未启动")
    print(f"  数据目录   : {Config.INSTANCE_DIR}")
    print(f"  默认管理员 : admin / {Config.DEFAULT_ADMIN_PASSWORD}（首次登录请立即修改）")
    print("=" * 72)

    try:
        socketio.run(
            app,
            host=args.host,
            port=args.port,
            debug=args.debug,
            use_reloader=False,
            allow_unsafe_werkzeug=True,
        )
    except KeyboardInterrupt:
        log.info("收到中断信号，正在关闭……")
    finally:
        if gateway is not None:
            gateway.stop()
            log.info("SSH 网关已停止")
    return 0


def _display_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _reset_admin() -> None:
    from app.extensions import db
    from app.models import User
    from app.security import hash_password

    user = User.query.filter_by(username=Config.DEFAULT_ADMIN_USERNAME).first()
    if user is None:
        print("未找到 admin 账号，请先正常运行一次以初始化数据库")
        return
    user.password_hash = hash_password(Config.DEFAULT_ADMIN_PASSWORD)
    user.must_change_password = True
    user.failed_attempts = 0
    user.locked_until = None
    db.session.commit()
    print(f"admin 密码已重置为 {Config.DEFAULT_ADMIN_PASSWORD}")


if __name__ == "__main__":
    sys.exit(main())
