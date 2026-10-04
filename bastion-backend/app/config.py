"""应用配置。

所有敏感密钥首次运行自动生成并落在 ``instance/`` 下：
    - ``secret.key``    Flask / JWT 会话签名密钥
    - ``fernet.key``    资产口令、私钥的对称加密密钥
    - ``gateway_host_rsa.key``  SSH 网关服务端主机密钥
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = Path(os.environ.get("BASTION_INSTANCE_DIR") or (BASE_DIR / "instance"))
TRANSCRIPT_DIR = INSTANCE_DIR / "transcripts"
#: Windows 远程桌面（WebRDP）会话录像落盘目录，与终端录像同源（同在 instance 下）
RDP_RECORDING_DIR = Path(
    os.environ.get("BASTION_RDP_RECORDING_DIR") or (INSTANCE_DIR / "rdp_recordings")
)


def load_dotenv(path: Path | None = None) -> int:
    """极简 ``.env`` 加载器（不引入新依赖）。

    - 支持 ``KEY=VALUE``、``#`` 注释、空行、值两侧的引号；
    - **不覆盖已存在的环境变量**，所以命令行 / 服务管理器传进来的值优先级更高；
    - 返回成功注入的键数量，便于启动时打日志。

    AI 运维的 API Key 就放在这里（``bastion-backend/.env``），密钥不进代码。
    """
    target = path or (BASE_DIR / ".env")
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return 0
    injected = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        injected += 1
    return injected


def _ensure_dirs() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)


def _get_or_create(path: Path, factory) -> str:
    """读取持久化密钥，不存在则生成并落盘（并尽量收紧权限）。"""
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = factory()
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上可能不支持
        pass
    return value


def _new_secret() -> str:
    return secrets.token_urlsafe(48)


def _new_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


_ensure_dirs()

#: 载入 .env（AI 运维的 DEEPSEEK_API_KEY 等；不覆盖已有环境变量）
LOADED_DOTENV_KEYS = load_dotenv()

_DB_PATH = INSTANCE_DIR / "bastion.db"


class Config:
    """默认配置。"""

    BASE_DIR = BASE_DIR
    INSTANCE_DIR = INSTANCE_DIR
    TRANSCRIPT_DIR = TRANSCRIPT_DIR
    RDP_RECORDING_DIR = RDP_RECORDING_DIR

    # --- 会话与签名 -----------------------------------------------------
    SECRET_KEY = os.environ.get("BASTION_SECRET_KEY") or _get_or_create(
        INSTANCE_DIR / "secret.key", _new_secret
    )
    JWT_SECRET_KEY = os.environ.get("BASTION_JWT_SECRET") or SECRET_KEY
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(
        hours=int(os.environ.get("BASTION_TOKEN_HOURS", "12"))
    )
    JWT_TOKEN_LOCATION = ["headers", "query_string"]
    JWT_QUERY_STRING_NAME = "token"

    # --- 资产口令加密 ---------------------------------------------------
    FERNET_KEY = os.environ.get("BASTION_FERNET_KEY") or _get_or_create(
        INSTANCE_DIR / "fernet.key", _new_fernet_key
    )

    # --- 数据库 ---------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "BASTION_DB_URI"
    ) or f"sqlite:///{_DB_PATH.as_posix()}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }

    # --- Web 控制台 -----------------------------------------------------
    JSON_AS_ASCII = False
    CORS_ORIGINS = os.environ.get("BASTION_CORS_ORIGINS", "*")
    #: 会话录像的单文件上限（MB），由上传接口自己校验（超限回 413 信封）
    RDP_RECORDING_MAX_MB = int(os.environ.get("BASTION_RDP_RECORDING_MAX_MB", "512"))
    #: 请求体总上限：默认 8MB 会挡掉录像上传，故按录像上限放宽并多留 8MB 余量。
    #: 业务侧的大小校验在接口里做（见 app/api/rdp.py），这里只是最后一道兜底。
    MAX_CONTENT_LENGTH = (RDP_RECORDING_MAX_MB + 8) * 1024 * 1024
    #: 边录边传：单个分片请求的上限（MB）。前端每 5 秒发一片，实测 1~2MB 量级。
    RDP_RECORDING_CHUNK_MAX_MB = int(os.environ.get("BASTION_RDP_RECORDING_CHUNK_MAX_MB", "32"))
    #: 边录边传：小于这个字节数认为没录到东西（纯黑 / 刚开就断），转正时丢弃
    RDP_RECORDING_MIN_BYTES = int(os.environ.get("BASTION_RDP_RECORDING_MIN_BYTES", "4096"))
    #: 边录边传：静默这么久就认为窗口已经没了，下次有人看录像列表/开新会话时自动收口
    RDP_UPLOAD_STALE_SECONDS = int(os.environ.get("BASTION_RDP_UPLOAD_STALE_SECONDS", "45"))

    # --- 启动参数与内置管理员 -------------------------------------------
    HOST = os.environ.get("BASTION_HOST", "0.0.0.0")
    PORT = int(os.environ.get("BASTION_PORT", "5000"))
    DEFAULT_ADMIN_USERNAME = os.environ.get("BASTION_ADMIN_USERNAME", "admin")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("BASTION_ADMIN_PASSWORD", "admin123")

    # --- 前端静态资源（ant-design-pro 构建产物，可选单进程部署）----------
    #: 默认为同级目录 ant-design-pro/dist；不存在时 Flask 只提供 /api/*。
    FRONTEND_DIST = os.environ.get(
        "BASTION_FRONTEND_DIST",
        os.fspath(BASE_DIR.parent / "ant-design-pro" / "dist"),
    )
    SERVE_FRONTEND = os.environ.get("BASTION_SERVE_FRONTEND", "1") not in ("0", "false", "False")

    # --- 登录安全策略 ---------------------------------------------------
    LOGIN_MAX_FAILURES = int(os.environ.get("BASTION_LOGIN_MAX_FAILURES", "5"))
    LOGIN_LOCK_MINUTES = int(os.environ.get("BASTION_LOGIN_LOCK_MINUTES", "15"))

    # --- SSH 网关 -------------------------------------------------------
    GATEWAY_ENABLED = os.environ.get("BASTION_GATEWAY_ENABLED", "1") not in ("0", "false", "False")
    GATEWAY_HOST = os.environ.get("BASTION_GATEWAY_HOST", "0.0.0.0")
    GATEWAY_PORT = int(os.environ.get("BASTION_GATEWAY_PORT", "2222"))
    GATEWAY_HOST_KEY = str(
        os.environ.get("BASTION_GATEWAY_HOST_KEY") or (INSTANCE_DIR / "gateway_host_rsa.key")
    )
    GATEWAY_SERVER_VERSION = os.environ.get("BASTION_GATEWAY_SERVER_VERSION", "SSH-2.0-BastionGW_1.0")
    GATEWAY_ALLOW_PASSWORD = os.environ.get("BASTION_GATEWAY_ALLOW_PASSWORD", "1") not in ("0", "false")
    GATEWAY_ALLOW_PUBKEY = os.environ.get("BASTION_GATEWAY_ALLOW_PUBKEY", "1") not in ("0", "false")
    GATEWAY_MAX_SESSIONS_PER_USER = int(os.environ.get("BASTION_GATEWAY_MAX_SESSIONS", "5"))
    GATEWAY_AUTH_TIMEOUT = int(os.environ.get("BASTION_GATEWAY_AUTH_TIMEOUT", "120"))
    GATEWAY_BANNER = os.environ.get(
        "BASTION_GATEWAY_BANNER",
        "=== AutoOps 堡垒机 审计网关 ===",
    )

    # --- 会话与命令审计 -------------------------------------------------
    #: 单条命令最长等待输出时间（秒）
    COMMAND_TIMEOUT = int(os.environ.get("BASTION_COMMAND_TIMEOUT", "60"))
    #: 单条命令落库的输出上限（字节）
    MAX_OUTPUT_BYTES = int(os.environ.get("BASTION_MAX_OUTPUT_BYTES", str(256 * 1024)))
    #: 会话空闲超时（秒），0 表示不限制
    SESSION_IDLE_TIMEOUT = int(os.environ.get("BASTION_SESSION_IDLE_TIMEOUT", "1800"))
    #: 终端回放上限（事件数）
    TRANSCRIPT_MAX_EVENTS = int(os.environ.get("BASTION_TRANSCRIPT_MAX_EVENTS", "200000"))

    # --- SSH 客户端行为 -------------------------------------------------
    SSH_CONNECT_TIMEOUT = int(os.environ.get("BASTION_SSH_CONNECT_TIMEOUT", "15"))
    SSH_BANNER_TIMEOUT = int(os.environ.get("BASTION_SSH_BANNER_TIMEOUT", "20"))
    SSH_KEEPALIVE = int(os.environ.get("BASTION_SSH_KEEPALIVE", "30"))

    # --- AI 运维（AIOps / DeepSeek）-------------------------------------
    #: 总开关；关掉后 /api/ai/* 一律 503
    AI_ENABLED = os.environ.get("AI_ENABLED", "1") not in ("0", "false", "False")
    #: DeepSeek API Key（放 .env，不入库、不回显）
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    #: 兼容 OpenAI 协议的地址
    DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    #: 模型名（deepseek-flash / deepseek-v4-pro）
    AI_MODEL = os.environ.get("AI_MODEL", "deepseek-flash")
    #: 单次上游请求超时（秒）
    AI_REQUEST_TIMEOUT = int(os.environ.get("AI_REQUEST_TIMEOUT", "120"))
    #: 单轮生成上限；**推理模型会先消耗 reasoning token**，给小了会空响应
    AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "8192"))
    #: 一个用户回合内最多允许的工具调用轮数
    AI_MAX_TOOL_ROUNDS = int(os.environ.get("AI_MAX_TOOL_ROUNDS", "8"))
    #: 敏感操作确认票据有效期（秒）
    AI_CONFIRM_TTL = int(os.environ.get("AI_CONFIRM_TTL", "300"))
    #: 回放给模型的历史消息条数上限
    AI_HISTORY_LIMIT = int(os.environ.get("AI_HISTORY_LIMIT", "40"))
    #: 工具结果回灌给模型的最大字符数（避免上下文爆炸）
    AI_TOOL_RESULT_LIMIT = int(os.environ.get("AI_TOOL_RESULT_LIMIT", "8000"))


class TestConfig(Config):
    """测试用配置：内存库 + 关闭网关 + 关闭 AI 上游。"""

    TESTING = True
    GATEWAY_ENABLED = False
    AI_ENABLED = False
    DEEPSEEK_API_KEY = ""
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    WTF_CSRF_ENABLED = False
