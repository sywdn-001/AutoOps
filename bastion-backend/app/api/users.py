"""账号管理接口。"""

from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy import or_

from ..audit import log_event
from ..extensions import db
from ..models import Grant, Role, User
from ..security import admin_required, hash_password, load_actor, permission_required
from ..utils import api_error, api_list, api_ok, page_args, parse_bool, parse_int

bp = Blueprint("users", __name__, url_prefix="/api/users")


def _resolve_role(payload: dict) -> tuple[Role | None, str | None]:
    """按 ``roleId`` 或 ``roleCode`` 解析角色。

    ``users.role_id`` 是 NOT NULL：解析不出来时必须在这里返回 400，
    绝不能让 SQLAlchemy 抛 IntegrityError（那会变成 500 且前端拿不到原因）。
    """
    role_id = parse_int(payload.get("roleId"))
    if role_id:
        role = db.session.get(Role, role_id)
        if role is None:
            return None, "指定的角色不存在"
        return role, None
    role_code = (payload.get("roleCode") or "").strip()
    if role_code:
        role = Role.query.filter_by(code=role_code).first()
        if role is None:
            return None, f"角色不存在：{role_code}"
        return role, None
    return None, "必须指定角色（roleId 或 roleCode）"


def _query_from_args():
    query = User.query
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(User.username.like(like), User.display_name.like(like), User.email.like(like))
        )
    role_id = parse_int(request.args.get("roleId"))
    if role_id:
        query = query.filter(User.role_id == role_id)
    status = (request.args.get("status") or "").strip()
    if status:
        query = query.filter(User.status == status)
    return query


@bp.get("")
@permission_required("user:view")
def list_users():
    page, size = page_args()
    query = _query_from_args()
    total = query.count()
    rows = query.order_by(User.id.asc()).offset((page - 1) * size).limit(size).all()
    return api_list([row.to_dict() for row in rows], total, page, size)


@bp.get("/options")
@permission_required("grant:view", "user:view", "session:view")
def user_options():
    rows = User.query.order_by(User.id.asc()).all()
    return api_ok(
        [
            {
                "label": f"{row.display_name or row.username}（{row.username}）",
                "value": row.id,
                "username": row.username,
                "roleCode": row.role_code,
                "status": row.status,
            }
            for row in rows
        ]
    )


@bp.get("/<int:user_id>")
@permission_required("user:view")
def get_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return api_error("账号不存在", 404, code="NOT_FOUND")
    data = user.to_dict()
    grants = Grant.query.filter_by(user_id=user_id).all()
    data["grants"] = [grant.to_dict() for grant in grants]
    return api_ok(data)


@bp.post("")
@admin_required
def create_user():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username:
        return api_error("用户名不能为空", 400, code="INVALID_ARGUMENT")
    if len(password) < 8:
        return api_error("初始密码长度不得少于 8 位", 400, code="WEAK_PASSWORD")
    if User.query.filter_by(username=username).first() is not None:
        return api_error("用户名已存在", 409, code="DUPLICATED")
    role, error = _resolve_role(payload)
    if error:
        return api_error(error, 400, code="INVALID_ARGUMENT")

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=(payload.get("displayName") or username).strip(),
        email=(payload.get("email") or "").strip(),
        phone=(payload.get("phone") or "").strip(),
        remark=(payload.get("remark") or "").strip(),
        role_id=role.id,
        status=(payload.get("status") or "active"),
        is_superuser=parse_bool(payload.get("isSuperuser")),
        gateway_enabled=parse_bool(payload.get("gatewayEnabled"), True),
        webterm_enabled=parse_bool(payload.get("webtermEnabled"), True),
        must_change_password=parse_bool(payload.get("mustChangePassword"), True),
    )
    db.session.add(user)
    db.session.commit()
    log_event(
        "user",
        "create_user",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        message=f"创建账号 {user.username}",
    )
    return api_ok(user.to_dict(), "账号已创建")


@bp.put("/<int:user_id>")
@admin_required
def update_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return api_error("账号不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}

    if "displayName" in payload:
        user.display_name = (payload.get("displayName") or "").strip()
    if "email" in payload:
        user.email = (payload.get("email") or "").strip()
    if "phone" in payload:
        user.phone = (payload.get("phone") or "").strip()
    if "remark" in payload:
        user.remark = (payload.get("remark") or "").strip()
    if "roleId" in payload or "roleCode" in payload:
        role, error = _resolve_role(payload)
        if error:
            return api_error(error, 400, code="INVALID_ARGUMENT")
        user.role_id = role.id
    if "status" in payload:
        user.status = payload.get("status") or "active"
    if "isSuperuser" in payload:
        user.is_superuser = parse_bool(payload.get("isSuperuser"))
    if "gatewayEnabled" in payload:
        user.gateway_enabled = parse_bool(payload.get("gatewayEnabled"), True)
    if "webtermEnabled" in payload:
        user.webterm_enabled = parse_bool(payload.get("webtermEnabled"), True)
    if "mustChangePassword" in payload:
        user.must_change_password = parse_bool(payload.get("mustChangePassword"))
    db.session.commit()
    log_event(
        "user",
        "update_user",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        message=f"更新账号 {user.username}",
        detail={key: payload[key] for key in payload if key != "password"},
    )
    return api_ok(user.to_dict(), "账号已更新")


@bp.delete("/<int:user_id>")
@admin_required
def delete_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return api_error("账号不存在", 404, code="NOT_FOUND")
    actor = load_actor()
    if actor is not None and actor.id == user.id:
        return api_error("不能删除当前登录账号", 400, code="INVALID_OPERATION")
    if user.username == "admin":
        return api_error("内置超级管理员不允许删除", 400, code="INVALID_OPERATION")
    Grant.query.filter_by(user_id=user_id).delete()
    username = user.username
    db.session.delete(user)
    db.session.commit()
    log_event("user", "delete_user", target_type="user", target_id=user_id, target_name=username)
    return api_ok(None, "账号已删除")


@bp.post("/<int:user_id>/password")
@admin_required
def reset_password(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return api_error("账号不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    password = payload.get("password") or ""
    if len(password) < 8:
        return api_error("新密码长度不得少于 8 位", 400, code="WEAK_PASSWORD")
    user.password_hash = hash_password(password)
    user.must_change_password = True
    user.failed_attempts = 0
    user.locked_until = None
    db.session.commit()
    log_event(
        "user",
        "reset_password",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        message=f"重置账号 {user.username} 的密码",
    )
    return api_ok(None, "密码已重置，该账号下次登录需修改密码")


@bp.post("/<int:user_id>/unlock")
@admin_required
def unlock_user(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return api_error("账号不存在", 404, code="NOT_FOUND")
    user.failed_attempts = 0
    user.locked_until = None
    db.session.commit()
    log_event(
        "user",
        "unlock",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        message=f"解锁账号 {user.username}",
    )
    return api_ok(None, "账号已解锁")
