"""在线会话注册表：网关 / 网页终端把活跃会话登记在此，供审计与强制中断使用。"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def register(sid: str, **info) -> None:
    with _lock:
        _sessions[sid] = {"sid": sid, "started_at": time.time(), "last_active": time.time(), **info}


def touch(sid: str) -> None:
    with _lock:
        entry = _sessions.get(sid)
        if entry is not None:
            entry["last_active"] = time.time()


def unregister(sid: str) -> None:
    with _lock:
        _sessions.pop(sid, None)


def get(sid: str) -> dict | None:
    with _lock:
        entry = _sessions.get(sid)
        return dict(entry) if entry else None


def list_all() -> list[dict]:
    with _lock:
        return [dict(item) for item in _sessions.values()]


def count_for_user(user_id: int) -> int:
    with _lock:
        return sum(1 for item in _sessions.values() if item.get("user_id") == user_id)


def count_for_grant(grant_id: int) -> int:
    with _lock:
        return sum(1 for item in _sessions.values() if item.get("grant_id") == grant_id)


def find_by_sid_prefix(prefix: str) -> list[dict]:
    with _lock:
        return [dict(item) for item in _sessions.values() if item["sid"].startswith(prefix)]


def close(sid: str, reason: str = "管理员强制断开") -> bool:
    """请求关闭某条在线会话；返回是否找到。

    `bridge`（SSH 网关 / 网页终端 / 文件管理器）与 `stop`（RDP 中继）二选一即可：
    两者都是「调一下就能让转发循环退出」的对象，怎么退由注册方自己实现。
    """
    entry = get(sid)
    if entry is None:
        return False
    bridge = entry.get("bridge")
    if bridge is not None:
        try:
            bridge.stop(reason)
        except Exception:  # noqa: BLE001
            pass
    stop = entry.get("stop")
    if callable(stop):
        try:
            stop(reason)
        except Exception:  # noqa: BLE001
            pass
    unregister(sid)
    return True


class _Registry:
    """模块函数门面，方便以 ``registry.xxx()`` 的方式调用。"""

    register = staticmethod(register)
    touch = staticmethod(touch)
    unregister = staticmethod(unregister)
    get = staticmethod(get)
    list_all = staticmethod(list_all)
    count_for_user = staticmethod(count_for_user)
    count_for_grant = staticmethod(count_for_grant)
    find_by_sid_prefix = staticmethod(find_by_sid_prefix)
    close = staticmethod(close)


registry = _Registry()

__all__ = [
    "registry",
    "register",
    "touch",
    "unregister",
    "get",
    "list_all",
    "count_for_user",
    "count_for_grant",
    "find_by_sid_prefix",
    "close",
]
