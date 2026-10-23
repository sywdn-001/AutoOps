"""系统设置接口（仅管理员可改）。"""

from __future__ import annotations

import socket

from flask import Blueprint, current_app, request

from ..audit import log_event
from ..extensions import db
from ..models import CommandPolicy, CommandRule, Role, SessionRecord, SystemSetting
from ..security import admin_required, permission_required
from ..settings_store import (
    BOOL_KEYS,
    DEFAULTS,
    all_defaults,
    all_settings,
    default_meta,
    ensure_defaults,
    is_bool_key,
    update_settings,
)
from ..ssh_client import default_client_version
from ..utils import api_error, api_ok, parse_bool, parse_int

bp = Blueprint("settings", __name__, url_prefix="/api/settings")

SETTING_LABELS = {
    "site_name": "堡垒机名称",
    "gateway_host": "SSH 网关监听地址",
    "gateway_port": "SSH 网关端口",
    "gateway_banner": "SSH 登录横幅",
    "session_idle_timeout": "会话空闲超时（秒）",
    "command_timeout": "单条命令超时（秒）",
    "login_max_failures": "登录失败锁定阈值（次）",
    "login_lock_minutes": "锁定时长（分钟）",
    "max_sessions_per_user": "每账号最大并发会话数",
    "default_policy_id": "默认命令策略",
    "record_raw_transcript": "记录原始字节流录像",
    "notify_on_deny": "命令被拦截时提示用户",
    "gateway_enabled": "启用 SSH 网关",
    "ai_max_tool_rounds": "AI 任务最大调用轮数",
}

INT_KEYS = {
    "gateway_port",
    "session_idle_timeout",
    "command_timeout",
    "login_max_failures",
    "login_lock_minutes",
    "max_sessions_per_user",
    "default_policy_id",
    "ai_max_tool_rounds",
}
# 允许留空的整型配置：默认值本身就是空串，留空表示「未指定」（例如没有默认命令策略）
OPTIONAL_INT_KEYS = {"default_policy_id"}
# 整型配置的合法区间：既用来校验（PUT），也下发给前端做输入框的 min/max
INT_RANGES = {
    "gateway_port": (1, 65535),
    "ai_max_tool_rounds": (1, 50),
}


def _label(key: str) -> str:
    meta = default_meta(key)
    if meta:
        label = meta.get("label") or {}
        return label.get("zh") or label.get("en") or meta.get("rawKey") or key
    return SETTING_LABELS.get(key, key)


def _kind(key: str) -> str:
    """配置项类型：int / bool / string。模块设置以 description.json 的声明为准。"""
    meta = default_meta(key)
    if meta:
        return meta.get("type") or "string"
    if key in INT_KEYS:
        return "int"
    if is_bool_key(key):
        return "bool"
    return "string"


def _bounds(key: str):
    """整型配置的合法区间（也用来下发前端 min/max）。"""
    meta = default_meta(key)
    if meta:
        if (meta.get("type") or "") != "int":
            return None, None
        lo, hi = meta.get("min"), meta.get("max")
        return (lo, hi) if lo is not None and hi is not None else (None, None)
    return INT_RANGES.get(key, (None, None))


def _extra(key: str) -> dict:
    """模块设置才有 group/scope，核心设置保持原有字段，避免前端改动过大。"""
    meta = default_meta(key)
    if not meta:
        return {}
    return {
        "scope": "module",
        "moduleId": meta.get("moduleId"),
        "group": meta.get("group") or meta.get("moduleId") or "",
    }


def _payload() -> dict:
    stored = all_settings()
    items = []
    for key, default in all_defaults().items():
        kind = _kind(key)
        item = {
            "key": key,
            "label": _label(key),
            "value": stored.get(key, default),
            "default": default,
            "type": kind,
        }
        lo, hi = _bounds(key)
        if kind == "int" and lo is not None and hi is not None:
            # 下发区间，前端 InputNumber 直接照它设 min/max（后端仍然独立校验一遍）
            item["min"], item["max"] = lo, hi
        item.update(_extra(key))
        items.append(item)
    return {"items": items, "values": {item["key"]: item["value"] for item in items}}


@bp.get("")
@permission_required("setting:view")
def get_settings():
    return api_ok(_payload())


@bp.put("")
@admin_required
def put_settings():
    payload = request.get_json(silent=True) or {}
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    known = all_defaults()
    cleaned: dict = {}
    for key, value in (values or {}).items():
        if key not in known:
            continue
        kind = _kind(key)
        label = _label(key)
        if kind == "int":
            if key in OPTIONAL_INT_KEYS and str(value if value is not None else "").strip() == "":
                cleaned[key] = ""
                continue
            number = parse_int(value)
            if number is None:
                return api_error(f"「{label}」必须是整数", 400, code="INVALID_ARGUMENT")
            lo, hi = _bounds(key)
            if lo is not None and hi is not None and not (lo <= number <= hi):
                return api_error(
                    f"「{label}」必须在 {lo}-{hi} 之间",
                    400,
                    code="INVALID_ARGUMENT",
                )
            if key == "default_policy_id":
                if number and db.session.get(CommandPolicy, number) is None:
                    return api_error("指定的默认策略不存在", 400, code="INVALID_ARGUMENT")
            cleaned[key] = number
        elif kind == "bool":
            # 归一化成 True/False，真正落库的 "1"/"0" 由 settings_store 统一负责
            cleaned[key] = parse_bool(value)
        else:
            cleaned[key] = str(value or "").strip()
    changed = update_settings(cleaned)
    if changed:
        log_event(
            "setting",
            "update_settings",
            target_type="setting",
            message=f"修改系统设置：{', '.join(changed)}",
            detail=cleaned,
        )
    return api_ok(_payload(), f"已更新 {len(changed)} 项设置" if changed else "设置无变化")


@bp.post("/reset")
@admin_required
def reset_settings():
    payload = request.get_json(silent=True) or {}
    known = all_defaults()
    keys = payload.get("keys") or list(known.keys())
    values = {key: known[key] for key in keys if key in known}
    changed = update_settings(values)
    log_event("setting", "reset_settings", message=f"恢复默认设置：{', '.join(changed)}")
    return api_ok(_payload(), "已恢复默认值")


@bp.get("/gateway")
@permission_required("setting:view")
def gateway_info():
    """网关运行态信息，便于部署后在界面上直接看到 ssh 命令怎么敲。"""
    host = current_app.config.get("GATEWAY_HOST", "0.0.0.0")
    port = int(current_app.config.get("GATEWAY_PORT", 2222))
    public = request.host.split(":")[0] or _guess_lan_ip()
    enabled = bool(current_app.config.get("GATEWAY_ENABLED"))
    running = False
    try:
        from ..gateway.server import is_running

        running = bool(is_running())
    except Exception:  # noqa: BLE001 - 网关模块可能未就绪
        running = False
    return api_ok(
        {
            "enabled": enabled,
            "running": running,
            "listenHost": host,
            "listenPort": port,
            "suggestedHost": public,
            "connectCommand": f"ssh -p {port} <account>@{public}",
            "allowPassword": bool(current_app.config.get("GATEWAY_ALLOW_PASSWORD", True)),
            "allowPublicKey": bool(current_app.config.get("GATEWAY_ALLOW_PUBKEY", True)),
            "hostKeyFingerprint": _host_key_fingerprint(),
            "onlineSessions": SessionRecord.query.filter_by(status="active").count(),
            "gatewaySessions": SessionRecord.query.filter_by(status="active", source="gateway").count(),
            "banner": current_app.config.get("GATEWAY_BANNER", ""),
            "serverVersion": current_app.config.get("GATEWAY_SERVER_VERSION", ""),
            "clientVersion": default_client_version(),
            "hostname": socket.gethostname(),
        }
    )


def _guess_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _host_key_fingerprint() -> str:
    """网关主机密钥指纹（OpenSSH 风格 SHA256）。

    与 `ssh-keyscan -p <port> <host> | ssh-keygen -lf -` 的输出一致；
    复用 gateway 模块里的实现，避免 API 与启动横幅显示成两种格式。
    """
    from ..gateway.server import host_key_fingerprint

    path = current_app.config.get("GATEWAY_HOST_KEY") or ""
    return host_key_fingerprint(path)


@bp.post("/maintenance/reconcile")
@admin_required
def reconcile():
    """把因重启而残留的 active 会话标记为中断。"""
    from ..session_service import reconcile_stale_sessions

    count = reconcile_stale_sessions(current_app)
    log_event("setting", "reconcile_sessions", message=f"清理残留在线会话 {count} 个")
    return api_ok({"reconciled": count}, f"已清理 {count} 个残留会话")


@bp.post("/maintenance/seed")
@admin_required
def reseed():
    from ..policy import seed_policies

    ensure_defaults()
    policies = seed_policies(db.session)
    log_event("setting", "reseed", message="重建内置角色/策略/设置")
    # 返回扁平计数，便于前端直接渲染（避免嵌套对象显示成 [object Object]）
    return api_ok(
        {
            "roles": Role.query.count(),
            "policies": CommandPolicy.query.count(),
            "policyRules": CommandRule.query.count(),
            "settings": SystemSetting.query.count(),
            "seeded": len(policies),
        },
        "内置数据已就绪",
    )


_ = (admin_required,)
