"""认证相关接口：登录 / 登出 / 当前用户 / 改密，并兼容 Ant Design Pro 的登录协议。"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, request
from flask_jwt_extended import create_access_token, get_jwt, jwt_required
from sqlalchemy import func

from ..audit import log_event
from ..extensions import db
from ..models import AuditLog, User, to_naive_utc, utcnow
from ..security import hash_password, load_actor, verify_password
from ..utils import api_error, api_ok, get_client_ip, get_user_agent

bp = Blueprint("auth", __name__, url_prefix="/api")

#: 已登出令牌（内存黑名单，进程内有效；重启即失效，对单机部署足够）
_REVOKED: set[str] = set()


def pro_current_user(user: User) -> dict:
    """Ant Design Pro ``CurrentUser`` 结构。

    除 Pro 首页/头像需要的字段外，额外带上 ``permissions`` / ``roleCode`` /
    ``isAdmin`` / ``mustChangePassword``：前端 ``src/access.ts`` 靠 permissions 做
    菜单与按钮级鉴权，网关与网页终端的可用性也依赖这几个开关。
    """
    return {
        "name": user.display_name or user.username,
        "avatar": "",
        "userid": str(user.id),
        "username": user.username,
        "email": user.email or "",
        "signature": user.remark or "让运维更安全一点",
        "title": user.role_name,
        "group": user.role_name,
        "tags": [
            {"key": "role", "label": user.role_name},
            {"key": "status", "label": "正常" if user.is_active() else "停用"},
        ],
        "notifyCount": 0,
        "unreadCount": 0,
        "country": "China",
        "access": user.role_code or "user",
        "roleCode": user.role_code or "user",
        "permissions": sorted(user.permission_set()),
        "isAdmin": bool(user.is_superuser or "*" in user.permission_set()),
        "mustChangePassword": bool(user.must_change_password),
        "gatewayEnabled": bool(user.gateway_enabled),
        "webtermEnabled": bool(user.webterm_enabled),
        "geographic": {"province": "Beijing", "city": "Beijing", "address": ""},
        "phone": user.phone or "",
    }


def _token_for(user: User) -> tuple[str, str]:
    expires = timedelta(hours=12)
    token = create_access_token(
        identity=str(user.id),
        additional_claims={
            "username": user.username,
            "role": user.role_code,
            "permissions": sorted(user.permission_set()),
        },
        expires_delta=expires,
    )
    return token, expires.total_seconds()


def _check_login_allowed(user: User) -> str | None:
    if not user.is_active():
        return "账号已被停用，请联系管理员"
    if user.is_locked():
        remain = (to_naive_utc(user.locked_until) - utcnow()) if user.locked_until else None
        mins = int(remain.total_seconds() // 60) + 1 if remain else 0
        return f"账号已被锁定，请 {mins} 分钟后重试"
    return None


def do_login(username: str, password: str, *, source: str = "console"):
    """统一登录逻辑，控制台与 Pro 兼容接口共用。"""
    from ..config import Config
    from ..settings_store import get_int

    max_failures = get_int("login_max_failures", Config.LOGIN_MAX_FAILURES) or 5
    lock_minutes = get_int("login_lock_minutes", Config.LOGIN_LOCK_MINUTES) or 15

    user = User.query.filter(func.lower(User.username) == (username or "").strip().lower()).first()
    if user is None:
        log_event(
            "auth",
            "login",
            result="failure",
            message=f"用户名不存在：{username}",
            actor_username=username or "",
            ip=get_client_ip(),
            user_agent=get_user_agent(),
        )
        return None, "用户名或密码错误"

    blocked = _check_login_allowed(user)
    if blocked:
        return None, blocked

    if not verify_password(password or "", user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= max_failures:
            user.locked_until = utcnow() + timedelta(minutes=lock_minutes)
            user.failed_attempts = 0
            db.session.commit()
            log_event(
                "auth",
                "login_locked",
                result="failure",
                message=f"连续登录失败，账号 {user.username} 锁定 {lock_minutes} 分钟",
                actor=user,
                ip=get_client_ip(),
            )
            return None, f"连续登录失败次数过多，账号已锁定 {lock_minutes} 分钟"
        db.session.commit()
        remaining = max_failures - user.failed_attempts
        log_event(
            "auth",
            "login",
            result="failure",
            message=f"密码错误（{user.username}），剩余尝试次数 {remaining}",
            actor=user,
            ip=get_client_ip(),
            user_agent=get_user_agent(),
        )
        return None, f"用户名或密码错误，还剩 {remaining} 次尝试机会"

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_login_ip = get_client_ip()
    db.session.commit()
    log_event(
        "auth",
        "login",
        result="success",
        message=f"{user.username} 登录成功（{source}）",
        actor=user,
        ip=get_client_ip(),
        user_agent=get_user_agent(),
        detail={"source": source},
    )
    return user, ""


# --------------------------------------------------------------------------- 自有协议
@bp.post("/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return api_error("请输入用户名和密码", 400, code="INVALID_ARGUMENT")
    user, message = do_login(username, password, source="console")
    if user is None:
        return api_error(message, 401, code="LOGIN_FAILED")
    token, expires_in = _token_for(user)
    return api_ok(
        {
            "token": token,
            "tokenType": "Bearer",
            "expiresIn": int(expires_in),
            "user": user.to_dict(),
            "permissions": sorted(user.permission_set()),
            "isAdmin": bool(user.is_superuser or user.role_code == "admin"),
        },
        "登录成功",
    )


@bp.post("/auth/logout")
@jwt_required(optional=True)
def logout():
    claims = get_jwt() or {}
    jti = claims.get("jti")
    if jti:
        _REVOKED.add(jti)
    user = load_actor()
    log_event(
        "auth",
        "logout",
        actor=user,
        actor_username=user.username if user else claims.get("username", ""),
        ip=get_client_ip(),
    )
    return api_ok(None, "已退出登录")


@bp.get("/auth/me")
@jwt_required()
def me():
    user = load_actor()
    if user is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    recent = (
        AuditLog.query.filter_by(actor_id=user.id)
        .order_by(AuditLog.ts.desc())
        .limit(10)
        .all()
    )
    data = user.to_dict()
    data["permissions"] = sorted(user.permission_set())
    data["isAdmin"] = bool(user.is_superuser or user.role_code == "admin")
    data["recentActivities"] = [row.to_dict() for row in recent]
    return api_ok(data)


@bp.post("/auth/password")
@jwt_required()
def change_password():
    user = load_actor()
    if user is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    payload = request.get_json(silent=True) or {}
    old_password = payload.get("oldPassword") or payload.get("old_password") or ""
    new_password = payload.get("newPassword") or payload.get("new_password") or ""
    if not verify_password(old_password, user.password_hash):
        return api_error("原密码不正确", 400, code="INVALID_PASSWORD")
    error = validate_password_strength(new_password)
    if error:
        return api_error(error, 400, code="WEAK_PASSWORD")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.session.commit()
    log_event("auth", "change_password", actor=user, ip=get_client_ip())
    return api_ok(None, "密码已更新")


def validate_password_strength(password: str, min_length: int = 8) -> str | None:
    if not password or len(password) < min_length:
        return f"新密码长度不得少于 {min_length} 位"
    kinds = 0
    kinds += any(ch.islower() for ch in password)
    kinds += any(ch.isupper() for ch in password)
    kinds += any(ch.isdigit() for ch in password)
    kinds += any(not ch.isalnum() for ch in password)
    if kinds < 2:
        return "新密码需包含大小写字母、数字、符号中的至少两类"
    return None


# --------------------------------------------------------------------------- Pro 兼容协议
@bp.post("/login/account")
def pro_login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    login_type = payload.get("type") or "account"
    if login_type != "account":
        return api_error("本系统仅支持账号密码登录", 400, code="UNSUPPORTED_TYPE")
    if payload.get("autoLogin") is False:
        pass
    user, message = do_login(username, password, source="pro-console")
    if user is None:
        return api_ok(
            {"status": "error", "type": login_type, "currentAuthority": "guest"},
            message,
        )
    token, _ = _token_for(user)
    return api_ok(
        {
            "status": "ok",
            "type": login_type,
            "currentAuthority": user.role_code or "user",
            "token": token,
            "user": user.to_dict(),
        }
    )


@bp.get("/currentUser")
@jwt_required()
def pro_current():
    user = load_actor()
    if user is None:
        return api_error("登录状态已失效", 401, code="UNAUTHORIZED")
    return api_ok(pro_current_user(user))


@bp.post("/login/outLogin")
@jwt_required(optional=True)
def pro_out_login():
    claims = get_jwt() or {}
    if claims.get("jti"):
        _REVOKED.add(claims["jti"])
    return api_ok({}, "已退出登录")


@bp.get("/notices")
@jwt_required(optional=True)
def pro_notices():
    """Pro Header 的通知下拉：用最近审计事件填充。"""
    rows = AuditLog.query.order_by(AuditLog.ts.desc()).limit(6).all()
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "title": f"{row.actor_username or '系统'} · {row.action}",
                "datetime": row.ts.strftime("%Y-%m-%d %H:%M:%S") if row.ts else "",
                "type": "notification" if row.result == "success" else "message",
                "read": True,
                "extra": row.message or "",
            }
        )
    return api_ok({"list": items, "total": len(items)})


def is_revoked(jti: str) -> bool:
    return bool(jti) and jti in _REVOKED
