"""模块级权限判定。

权限码形如 ``module:<id>:use``，不由 ``security.ALL_PERMISSIONS`` 静态枚举
（模块可安装/卸载，静态表必然过期）。判定顺序刻意保持为：

1. 超管 / ``*``：放行；
2. 角色权限串精确匹配；
3. 管理员角色兜底。

第 3 条与 ``security.is_admin`` 的既有语义一致；把这段逻辑放在
``app.modules`` 里，避免往 ``security.py`` 塞模块特例，污染其他权限码。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..security import is_admin

# 模块权限码不写进 ALL_PERMISSIONS（模块可装可卸），但角色接口需要能保存
# 形如 ``module:<id>:use`` 的字符串，否则管理员无法把模块授权给普通角色。
MODULE_PERMISSION_RE = re.compile(r"^module:[a-z0-9][a-z0-9-]{0,63}:[a-zA-Z0-9:_-]{1,64}$")


def is_module_permission(code: str | None) -> bool:
    """只校验形状，不要求模块已安装；卸载后角色上的旧授权保留也无害。"""
    return bool(MODULE_PERMISSION_RE.match(str(code or "").strip()))



def module_permission(module_id: str) -> str:
    return f"module:{module_id}:use"


def user_permissions(user: Any) -> set[str]:
    if user is None:
        return set()
    try:
        return set(user.permission_set() or ())
    except Exception:
        # 角色被删/会话异常时按无权限处理，不能把 500 抛到导航接口
        return set()


def has_module_permission(user: Any, permission: str | None) -> bool:
    if user is None or not permission:
        return False
    permissions = user_permissions(user)
    if "*" in permissions:
        return True
    if permission in permissions:
        return True
    return is_admin(user)


def can_access_module(user: Any, module_id: str) -> bool:
    return has_module_permission(user, module_permission(module_id))


def filter_nav_items(items: Iterable[dict], user: Any) -> list[dict]:
    """按 nav 项自带的 ``permission`` 过滤；缺省视为模块默认权限。"""
    allowed: list[dict] = []
    for item in items:
        permission = item.get("permission") or module_permission(item.get("moduleId", ""))
        if has_module_permission(user, permission):
            allowed.append(item)
    return allowed


def module_permission_catalog() -> list[tuple[str, str]]:
    """给角色权限目录动态补上已安装模块声明的权限。"""
    from .registry import registry

    catalog: list[tuple[str, str]] = []
    for record in registry.all():
        name = record.description.get("name") or {}
        label = str(name.get("zh") or name.get("en") or record.id)
        for code in record.description.get("permissions", []):
            if is_module_permission(code):
                catalog.append((code, f"{label}（扩展模块）"))
    return catalog
