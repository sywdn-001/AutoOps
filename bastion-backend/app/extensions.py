"""Flask 扩展单例。

集中实例化，避免循环导入。
"""

from __future__ import annotations

from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
jwt = JWTManager()
cors = CORS()

#: async_mode=threading —— 依赖 simple-websocket，可跑在 werkzeug 上，
#: 不要求 eventlet / gevent，Windows 与 Linux 行为一致。
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
    ping_interval=20,
    ping_timeout=60,
)
