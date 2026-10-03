"""通用工具：统一响应体、分页、时间解析。"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Iterable

from flask import jsonify, request


def api_ok(data: Any = None, message: str = "ok", status: int = 200, **extra):
    body = {"success": True, "message": message, "data": data}
    body.update(extra)
    return jsonify(body), status


def api_list(items: Iterable[Any], total: int, page: int = 1, page_size: int = 20, **extra):
    body = {
        "success": True,
        "data": list(items),
        "total": total,
        "page": page,
        "pageSize": page_size,
    }
    body.update(extra)
    return jsonify(body)


def api_error(message: str, status: int = 400, code: str | None = None, **extra):
    body = {"success": False, "message": message, "code": code or "ERROR", "data": None}
    body.update(extra)
    return jsonify(body), status


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def get_user_agent() -> str:
    return (request.headers.get("User-Agent") or "")[:255]


def parse_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def parse_datetime(value) -> datetime | None:
    """接受 ISO8601 字符串或时间戳，统一返回 naive-UTC datetime（数据库列无时区）。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _coerce_aware(value).astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _coerce_aware(parsed).astimezone(timezone.utc).replace(tzinfo=None)


def parse_time(value) -> time | None:
    """接受 ``HH:MM`` / ``HH:MM:SS`` 字符串，返回 ``time``。"""
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def page_args(default_size: int = 20, max_size: int = 200) -> tuple[int, int]:
    page = max(1, parse_int(request.args.get("page", request.args.get("current")), 1) or 1)
    size = parse_int(request.args.get("pageSize"), default_size) or default_size
    size = max(1, min(size, max_size))
    return page, size


def extract_ids(payload: dict, key: str = "ids") -> list[int] | None:
    """解析「按选择清除」的 id 列表。

    返回 ``None`` 表示这次请求**没有**按 id 选择（调用方应据此走 all/筛选分支）；
    参数存在但格式非法时抛 ``ValueError``，由调用方回 400，而不是静默当成全量清除。
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get(key)
    if raw is None or raw == "" or raw == []:
        return None
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError(f"{key} 必须是数组")
    ids: list[int] = []
    for item in raw:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 含非法 id：{item!r}") from None
    return ids or None


def purge_before(payload: dict):
    """清除接口的 ``before`` 参数（ISO8601 / 时间戳）→ naive-UTC datetime。

    传了但解析不出来时抛 ``ValueError``（拒绝「以为按时间清、实际清了个寂寞」）。
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("before")
    if raw in (None, ""):
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        raise ValueError(f"before 不是合法时间：{raw!r}")
    return parsed


TIME_RANGE_CHOICES = ("today", "7d", "30d", "90d")
