"""空闲超时清理：把超过 ``session_idle_timeout`` 的在线会话断开。

背景（实测缺陷）
----------------
参数设置里的「会话空闲超时（秒）」原来只是个摆设：全仓库没有任何地方读它去关闭会话。
网页终端 / SSH 网关 / 文件管理器的 ``last_active`` 一直在更新（``registry.touch``、
``FileSession.touch``），但**没有人检查**它。后果：客户端异常离开（关掉标签页、断网、
进程被杀）后，会话永远挂在 ``session_registry`` 里，直到管理员手动中断或后端重启 ——
文件管理器会因此撞满 ``MAX_SESSIONS_PER_USER`` 而再也打不开（真机实测泄漏 8 条后
``POST /api/files/sessions`` 直接被 ``QUOTA_EXCEEDED`` 挡住）。

现在的行为
----------
``create_app()`` 启动一个守护线程，每 ``interval`` 秒读一次参数设置里的
``session_idle_timeout``，把 ``now - last_active >= timeout`` 的会话按
「空闲超时自动断开」收口：``session_registry.close()`` 会走各自的收口路径
（终端 → 关闭 SSH 通道并落库会话记录；文件管理器 → ``FileSession.stop()``）。
活动时间由两端喂入：终端桥接的输入 / 输出（``ShellBridge.feed_input`` /
``ShellBridge._emit_output``）与文件管理器的每次操作（``FileSession.touch``）。
"""

from __future__ import annotations

import logging
import threading
import time

from . import session_registry
from .config import Config

log = logging.getLogger(__name__)

IDLE_REASON = "空闲超时自动断开"
DEFAULT_INTERVAL = 30.0


def timeout_seconds(app) -> float:
    """读取参数设置里的会话空闲超时；读不到就退回配置文件默认值。<=0 表示不清理。"""
    raw: object = Config.SESSION_IDLE_TIMEOUT
    try:
        from .settings_store import get_int

        with app.app_context():
            raw = get_int("session_idle_timeout", Config.SESSION_IDLE_TIMEOUT)
    except Exception:  # noqa: BLE001 - 参数表读不到时不能拖垮清理线程
        log.debug("读取 session_idle_timeout 失败，用配置默认值", exc_info=True)
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = float(Config.SESSION_IDLE_TIMEOUT)
    return seconds if seconds > 0 else 0.0


def sweep_once(timeout: float, *, now: float | None = None) -> list[dict]:
    """断开会话空闲时间已达 ``timeout`` 秒的会话，返回被断开的会话信息。"""
    if timeout <= 0:
        return []
    moment = time.time() if now is None else now
    closed: list[dict] = []
    for entry in session_registry.list_all():
        sid = str(entry.get("sid") or "")
        if not sid:
            continue
        idle = moment - float(entry.get("last_active") or moment)
        if idle < timeout:
            continue
        if not session_registry.close(sid, reason=IDLE_REASON):
            continue
        closed.append(
            {
                "sid": sid,
                "kind": entry.get("kind") or "",
                "username": entry.get("username") or "",
                "hostName": entry.get("host_name") or "",
                "idleSeconds": round(idle, 1),
            }
        )
        log.info(
            "空闲超时断开会话 sid=%s kind=%s user=%s idle=%.1fs",
            sid,
            entry.get("kind"),
            entry.get("username"),
            idle,
        )
    return closed


def start(app, *, interval: float = DEFAULT_INTERVAL) -> threading.Thread | None:
    """启动后台清扫线程；``TESTING`` 或 ``IDLE_SWEEPER_DISABLED`` 时不启动。"""
    if app.config.get("TESTING") or app.config.get("IDLE_SWEEPER_DISABLED"):
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                limit = timeout_seconds(app)
                if limit > 0:
                    sweep_once(limit)
            except Exception:  # noqa: BLE001 - 守护线程绝不能因异常退出
                log.exception("空闲会话清理失败")

    thread = threading.Thread(target=_loop, name="bastion-idle-sweeper", daemon=True)
    thread.start()
    log.info("空闲会话清理线程已启动，检查间隔 %.0f 秒", interval)
    return thread


__all__ = ["IDLE_REASON", "DEFAULT_INTERVAL", "sweep_once", "timeout_seconds", "start"]
