"""系统设置的读写封装（键值表 SystemSetting）。

存储约定（**改这里之前先读完**）：

- 所有设置值一律以**字符串**落库，布尔项只能是 ``"1"`` / ``"0"``。
- 历史上 ``PUT /api/settings`` 会把 bool 键先 ``parse_bool`` 成 Python ``True/False``，
  再被 ``set_setting()`` 用 ``str()`` 写成 ``"True"/"False"``；前端只认 ``"1"/"true"``，
  于是**保存一次之后所有开关都显示成关闭**（真实线上 Bug）。
  现在 ``normalize_value()`` 在写入前统一归一化布尔值，``get_setting()/all_settings()``
  在读取时再归一化一遍，``ensure_defaults()`` 顺带把老库里的 ``"True"/"False"`` 洗掉。
- 模块（``.mod.ops_ext``）也可以注册设置项：键名统一为 ``mod.<moduleId>.<key>``，
  默认值来自模块的 ``description.json``（见 ``register_module_defaults()``）。
  模块默认值放在内存里（安装/启用时注册），**不写进** ``DEFAULTS`` 常量。
"""

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
    # AI 任务最大调用轮数：一个用户回合里最多几次「模型要工具 → 执行 → 回灌」。
    # 与 config.AI_MAX_TOOL_ROUNDS（环境变量）同义，但这里是**运行时可改**的那份，
    # 见 app/ai/service.py 的 max_tool_rounds()。
    "ai_max_tool_rounds": "8",
}

#: 布尔型设置键的**唯一**权威定义（api/settings.py 从这里导入，不要再各写一份）。
#: 注意 `gateway_enabled` 不写进 DEFAULTS（它跟随启动配置），但仍然是布尔键。
BOOL_KEYS: frozenset[str] = frozenset(
    {"record_raw_transcript", "notify_on_deny", "gateway_enabled"}
)

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})

#: 模块注册的设置项（内存态，随模块加载/卸载变化）：key -> 默认值
MODULE_DEFAULTS: dict[str, str] = {}
#: 模块设置项的元信息：key -> {label, type, min, max, group, moduleId, rawKey, description}
MODULE_META: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 类型归一化
# ---------------------------------------------------------------------------
def is_bool_key(key: str) -> bool:
    if key in BOOL_KEYS:
        return True
    meta = MODULE_META.get(key)
    return bool(meta and meta.get("type") == "bool")


def normalize_bool(value) -> str:
    """任何真值/假值 → ``"1"`` / ``"0"``。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "0"
    return "1" if str(value).strip().lower() in _TRUTHY else "0"


def normalize_value(key: str, value) -> str:
    """按配置键的类型把值归一化成入库字符串。"""
    if is_bool_key(key):
        return normalize_bool(value)
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# 模块设置项注册
# ---------------------------------------------------------------------------
def module_setting_key(module_id: str, raw_key: str) -> str:
    return f"mod.{module_id}.{raw_key}"


def register_module_defaults(module_id: str, items: list[dict]) -> list[str]:
    """把模块声明的设置项注册进运行态，返回注册的完整键名列表。

    ``items`` 的元素形如 ``{"key": "enabled", "label": {"zh": "启用"}, "type": "bool",
    "default": "0", "min": 0, "max": 65535, "description": {...}}``。
    """
    registered: list[str] = []
    for item in items or []:
        raw = str(item.get("key") or "").strip()
        if not raw:
            continue
        full = module_setting_key(module_id, raw)
        default = item.get("default", "")
        MODULE_DEFAULTS[full] = normalize_value(full, default)
        MODULE_META[full] = {
            "moduleId": module_id,
            "rawKey": raw,
            "label": item.get("label") or {"zh": raw, "en": raw},
            "type": item.get("type") or "string",
            "min": item.get("min"),
            "max": item.get("max"),
            "group": item.get("group") or "",
            "description": item.get("description") or {"zh": "", "en": ""},
        }
        registered.append(full)
    return registered


def unregister_module_defaults(module_id: str) -> None:
    prefix = f"mod.{module_id}."
    for key in [k for k in MODULE_DEFAULTS if k.startswith(prefix)]:
        MODULE_DEFAULTS.pop(key, None)
    for key in [k for k in MODULE_META if k.startswith(prefix)]:
        MODULE_META.pop(key, None)


def all_defaults() -> dict[str, str]:
    merged = dict(DEFAULTS)
    merged.update(MODULE_DEFAULTS)
    return merged


def default_meta(key: str) -> dict | None:
    return MODULE_META.get(key)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def get_setting(key: str, default: str | None = None) -> str:
    row = db.session.get(SystemSetting, key)
    if row is not None and row.value is not None:
        return normalize_value(key, row.value)
    if default is not None:
        return normalize_value(key, default)
    return normalize_value(key, all_defaults().get(key, ""))


def get_bool(key: str, default: bool = False) -> bool:
    stored = db.session.get(SystemSetting, key)
    if stored is None:
        return default
    return normalize_bool(stored.value) == "1"


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value) -> None:
    text = normalize_value(key, value)
    row = db.session.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=text)
        db.session.add(row)
    else:
        row.value = text


def update_settings(mapping: dict) -> list[str]:
    """只接受白名单（核心 + 已注册模块）内的键，返回真正发生变化的键名列表。"""
    known = all_defaults()
    current = all_settings()
    changed: list[str] = []
    for key, value in (mapping or {}).items():
        if key not in known:
            continue
        text = normalize_value(key, value)
        if current.get(key, "") != text:
            changed.append(key)
        set_setting(key, value)
    db.session.commit()
    return changed


def all_settings() -> dict:
    rows = {row.key: row.value for row in SystemSetting.query.all()}
    merged = all_defaults()
    merged.update({key: "" if value is None else value for key, value in rows.items()})
    # 老库里可能是 "True"/"False"，读出来统一成 "1"/"0"，避免前端/网关各自兼容
    for key, value in list(merged.items()):
        if is_bool_key(key):
            merged[key] = normalize_bool(value)
    return merged


def ensure_defaults() -> None:
    changed = False
    for key, value in all_defaults().items():
        if db.session.get(SystemSetting, key) is None:
            db.session.add(SystemSetting(key=key, value=normalize_value(key, value)))
            changed = True
    # 一次性洗掉历史脏数据：布尔项里只要不是 "0"/"1" 就归一化
    bool_keys = [k for k in all_defaults() if is_bool_key(k)] + list(BOOL_KEYS)
    if bool_keys:
        dirty = (
            SystemSetting.query.filter(
                SystemSetting.key.in_(bool_keys),
                SystemSetting.value.notin_(["0", "1"]),
            ).all()
        )
        for row in dirty:
            row.value = normalize_bool(row.value)
            changed = True
    if changed:
        db.session.commit()
