"""认证、口令哈希与权限装饰器。"""

from __future__ import annotations

import logging
import re
from functools import wraps

from flask import g
from flask_jwt_extended import get_jwt_identity, jwt_required
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models import User
from .utils import api_error

logger = logging.getLogger(__name__)

#: werkzeug 生成的哈希串前缀（scrypt:32768:8:1$... / pbkdf2:sha256:... / argon2...）
_HASH_PREFIX_RE = re.compile(r"^(scrypt|pbkdf2|argon2|sha256|sha1|md5)[:$]")

#: 控制台权限码全集（前端也用它渲染菜单）
ALL_PERMISSIONS = [
    "dashboard:view",
    "host:view",
    "host:manage",
    "account:view",
    "account:manage",
    "group:view",
    "group:manage",
    "user:view",
    "user:manage",
    "role:view",
    "role:manage",
    "grant:view",
    "grant:manage",
    "policy:view",
    "policy:manage",
    "session:view",
    "session:view_all",
    "session:replay",
    "session:terminate",
    "command:view",
    "command:view_all",
    "audit:view",
    "setting:view",
    "setting:manage",
    "terminal:use",
    "rdp:use",
    "file:use",
    "filepolicy:view",
    "filepolicy:manage",
    "ai:view",
    "ai:use",
    "ai:view_all",
    "ai:tool",
    "ai:tool_write",
    "ai:tool_exec",
    "ai:manage",
]


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令：``password`` 为用户输入的明文，``password_hash`` 为库中存储的哈希串。

    注意参数顺序与 werkzeug 的 ``check_password_hash(hash, password)`` **相反**——
    调用点统一读作 ``verify_password(用户输入, 数据库字段)``。
    传入的哈希串不像哈希时记 warning：历史上这里被传反过参数，
    静默返回 False 会让全站登录静默失效，必须留下可观测痕迹。
    """
    if not password_hash:
        return False
    if not _HASH_PREFIX_RE.match(str(password_hash)):
        logger.warning(
            "口令哈希格式异常，疑似 verify_password 参数顺序传反：%r", str(password_hash)[:24]
        )
        return False
    try:
        return check_password_hash(str(password_hash), password)
    except (ValueError, TypeError):
        return False


def has_permission(user: User | None, code: str) -> bool:
    if user is None:
        return False
    perms = user.permission_set()
    return "*" in perms or code in perms


def has_any_permission(user: User | None, *codes) -> bool:
    """``has_any_permission(u, "a", "b")`` 与 ``has_any_permission(u, ("a", "b"))`` 都支持。

    两种写法混用曾导致 ``TypeError: takes 2 positional arguments but 3 were given``，
    让 /api/dashboard/overview、/api/sessions 等接口 500 —— 这里统一展开，不再挑调用方。
    """
    flat: list[str] = []
    for item in codes:
        if isinstance(item, str):
            flat.append(item)
        elif item:
            flat.extend(item)
    return any(has_permission(user, code) for code in flat)


def is_admin(user: User | None) -> bool:
    if user is None:
        return False
    return bool(user.is_superuser) or user.role_code == "admin" or "*" in user.permission_set()


def load_actor() -> User | None:
    """把 JWT 里的用户装载到 ``g.actor``；账号被禁用则视为会话失效。"""
    identity = get_jwt_identity()
    if identity in (None, ""):
        return None
    try:
        user_id = int(identity)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, user_id)
    if user is None or not user.is_active():
        return None
    g.actor = user
    return user


def login_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user = load_actor()
        if user is None:
            return api_error("账号不存在、已被停用或登录状态已失效", 401, code="UNAUTHORIZED")
        return fn(*args, **kwargs)

    return wrapper


def permission_required(*codes: str):
    """要求当前账号至少具备其中一个权限码。"""

    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if not has_any_permission(g.actor, codes):
                return api_error(
                    "权限不足，需要：" + " / ".join(codes), 403, code="FORBIDDEN"
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(fn):
    """仅管理员。用于「只有管理员可以添加机器 / 管理账号」这类硬约束。"""

    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not is_admin(g.actor):
            return api_error("仅管理员可执行该操作", 403, code="ADMIN_ONLY")
        return fn(*args, **kwargs)

    return wrapper
