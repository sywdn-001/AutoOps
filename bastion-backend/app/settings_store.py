"""系统设置的读写封装（键值表 SystemSetting）。"""

from __future__ import annotations

from .extensions import db
from .models import SystemSetting

DEFAULTS: dict[str, str] = {
    "site_name": "AutoOps 堡垒机",
    "gateway_host": "0.0.0.0",
    "gateway_port": "2222",
    "gateway_banner": "=== AutoOps 堡垒机 审计网关 ===",
    "session_idle_timeout": "1800",
    "command_timeout": "60",
    "login_max_failures": "5",
    "login_lock_minutes": "15",
    "max_sessions_per_user": "5",
    "default_policy_id": "",
    "record_raw_transcript": "1",
    "notify_on_deny": "1",
}


def get_setting(key: str, default: str | None = None) -> str:
    row = db.session.get(SystemSetting, key)
    if row is not None and row.value is not None:
        return row.value
    if default is not None:
        return default
    return DEFAULTS.get(key, "")


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value) -> None:
    text = "" if value is None else str(value)
    row = db.session.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=text)
        db.session.add(row)
    else:
        row.value = text


def update_settings(mapping: dict) -> list[str]:
    """只接受白名单内的键，返回真正发生变化的键名列表（供审计与提示使用）。"""
    current = all_settings()
    changed: list[str] = []
    for key, value in (mapping or {}).items():
        if key not in DEFAULTS:
            continue
        text = "" if value is None else str(value)
        if current.get(key, "") != text:
            changed.append(key)
        set_setting(key, value)
    db.session.commit()
    return changed


def all_settings() -> dict:
    rows = {row.key: row.value for row in SystemSetting.query.all()}
    merged = dict(DEFAULTS)
    merged.update(rows)
    return merged


def ensure_defaults() -> None:
    changed = False
    for key, value in DEFAULTS.items():
        if db.session.get(SystemSetting, key) is None:
            db.session.add(SystemSetting(key=key, value=value))
            changed = True
    if changed:
        db.session.commit()
