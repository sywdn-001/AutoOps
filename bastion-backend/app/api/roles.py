"""角色与权限码接口。"""

from __future__ import annotations

from flask import Blueprint, request

from ..audit import log_event
from ..extensions import db
from ..models import Role, User
from ..security import ALL_PERMISSIONS, admin_required, permission_required
from ..utils import api_error, api_list, api_ok, page_args, parse_bool

bp = Blueprint("roles", __name__, url_prefix="/api/roles")

PERMISSION_GROUPS = [
    {
        "group": "概览",
        "items": [("dashboard:view", "查看控制台概览")],
    },
    {
        "group": "资产",
        "items": [
            ("host:view", "查看主机列表与详情"),
            ("host:manage", "新增/修改/删除主机"),
            ("account:view", "查看主机登录账号"),
            ("account:manage", "新增/修改/删除主机登录账号"),
            ("group:view", "查看主机分组"),
            ("group:manage", "维护主机分组"),
        ],
    },
    {
        "group": "权限",
        "items": [
            ("user:view", "查看堡垒机账号"),
            ("user:manage", "维护堡垒机账号"),
            ("role:view", "查看角色"),
            ("role:manage", "维护角色与权限"),
            ("grant:view", "查看主机授权"),
            ("grant:manage", "维护主机授权（谁能碰哪台机器）"),
        ],
    },
    {
        "group": "审计",
        "items": [
            ("policy:view", "查看命令策略"),
            ("policy:manage", "维护命令策略（能执行什么命令）"),
            ("filepolicy:view", "查看文件策略"),
            ("filepolicy:manage", "维护文件策略（能操作哪些路径）"),
            ("session:view", "查看自己的会话"),
            ("session:view_all", "查看全部会话"),
            ("session:replay", "回放会话录像"),
            ("session:terminate", "强制中断在线会话"),
            ("command:view", "查看自己的命令记录"),
            ("command:view_all", "查看全部命令记录"),
            ("audit:view", "查看操作审计日志"),
        ],
    },
    {
        "group": "系统",
        "items": [
            ("setting:view", "查看系统设置"),
            ("setting:manage", "修改系统设置"),
            ("terminal:use", "使用网页终端 / SSH 网关"),
            ("file:use", "使用文件管理器（SFTP 浏览与上传下载）"),
        ],
    },
    {
        "group": "AI 运维",
        "items": [
            ("ai:view", "查看 AI 对话审计"),
            ("ai:use", "使用 AI 助手对话"),
            ("ai:view_all", "查看所有人的 AI 对话"),
            ("ai:tool", "AI 只读工具（查询资产/审计/策略等）"),
            ("ai:tool_write", "AI 写入工具（增删改主机/用户/授权/策略，需管理员密码）"),
            ("ai:tool_exec", "AI 远程执行工具（在目标机执行命令，需管理员密码）"),
            ("ai:manage", "管理 AI 对话（删除他人对话、查看 AI 配置）"),
        ],
    },
]


@bp.get("")
@permission_required("role:view", "user:view")
def list_roles():
    page, size = page_args(default_size=50)
    query = Role.query
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(Role.name.like(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(Role.id.asc()).offset((page - 1) * size).limit(size).all()
    items = []
    for row in rows:
        data = row.to_dict()
        data["userCount"] = User.query.filter_by(role_id=row.id).count()
        items.append(data)
    return api_list(items, total, page, size)


@bp.get("/options")
@permission_required("user:view", "role:view")
def role_options():
    rows = Role.query.order_by(Role.id.asc()).all()
    return api_ok(
        [
            {"label": row.name, "value": row.id, "code": row.code, "permissions": row.permissions or []}
            for row in rows
        ]
    )


@bp.get("/permissions")
@permission_required("role:view", "user:view")
def permission_catalog():
    return api_ok(
        [
            {
                "group": group["group"],
                "items": [{"code": code, "label": label} for code, label in group["items"]],
            }
            for group in PERMISSION_GROUPS
        ]
    )


@bp.get("/<int:role_id>")
@permission_required("role:view")
def get_role(role_id: int):
    role = db.session.get(Role, role_id)
    if role is None:
        return api_error("角色不存在", 404, code="NOT_FOUND")
    return api_ok(role.to_dict(with_users=True))


@bp.post("")
@admin_required
def create_role():
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code or not name:
        return api_error("角色标识和名称都不能为空", 400, code="INVALID_ARGUMENT")
    if Role.query.filter_by(code=code).first() is not None:
        return api_error("角色标识已存在", 409, code="DUPLICATED")
    permissions = _sanitize_permissions(payload.get("permissions"))
    role = Role(
        code=code,
        name=name,
        description=(payload.get("description") or "").strip(),
        permissions=permissions,
        is_builtin=False,
    )
    db.session.add(role)
    db.session.commit()
    log_event("role", "create_role", target_type="role", target_id=role.id, target_name=role.name)
    return api_ok(role.to_dict(), "角色已创建")


@bp.put("/<int:role_id>")
@admin_required
def update_role(role_id: int):
    role = db.session.get(Role, role_id)
    if role is None:
        return api_error("角色不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        role.name = (payload.get("name") or role.name).strip()
    if "description" in payload:
        role.description = (payload.get("description") or "").strip()
    if "permissions" in payload:
        role.permissions = _sanitize_permissions(payload.get("permissions"))
    db.session.commit()
    log_event(
        "role",
        "update_role",
        target_type="role",
        target_id=role.id,
        target_name=role.name,
        detail={"permissions": role.permissions},
    )
    return api_ok(role.to_dict(), "角色已更新")


@bp.delete("/<int:role_id>")
@admin_required
def delete_role(role_id: int):
    role = db.session.get(Role, role_id)
    if role is None:
        return api_error("角色不存在", 404, code="NOT_FOUND")
    if role.is_builtin:
        return api_error("内置角色不允许删除", 400, code="INVALID_OPERATION")
    in_use = User.query.filter_by(role_id=role_id).count()
    if in_use:
        return api_error(f"还有 {in_use} 个账号使用该角色，无法删除", 400, code="INVALID_OPERATION")
    name = role.name
    db.session.delete(role)
    db.session.commit()
    log_event("role", "delete_role", target_type="role", target_id=role_id, target_name=name)
    return api_ok(None, "角色已删除")


def _sanitize_permissions(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [item for item in value.replace(",", " ").split() if item]
    result = []
    for item in value or []:
        code = str(item).strip()
        if code == "*" or code in ALL_PERMISSIONS:
            if code not in result:
                result.append(code)
    return result


_ = parse_bool  # 保留：角色接口后续可能扩展布尔开关
