"""空闲超时清理（``session_idle_timeout`` 必须真的生效）的回归用例。

真机缺陷背景：参数设置里的「会话空闲超时（秒）」原来没有任何地方读取 —— 客户端异常
离开（关标签页 / 断网）后会话一直挂在 ``session_registry`` 里，直到管理员手动中断或
后端重启；文件管理器实测因此泄漏 8 条、撞满 ``MAX_SESSIONS_PER_USER`` 打不开窗口。
"""

from __future__ import annotations

import time

import pytest

from app import session_registry
from app.idle_sweeper import IDLE_REASON, sweep_once, timeout_seconds


class _StubBridge:
    def __init__(self):
        self.stopped: list[str] = []

    def stop(self, reason: str = "") -> None:
        self.stopped.append(reason)


def test_idle_session_is_closed_and_active_one_survives():
    idle_bridge = _StubBridge()
    active_bridge = _StubBridge()
    session_registry.register("idle-1", kind="web", username="ops", bridge=idle_bridge)
    session_registry.register("active-1", kind="web", username="ops", bridge=active_bridge)
    # 手工把空闲那条的 last_active 拨到 20 分钟前
    with session_registry._lock:
        session_registry._sessions["idle-1"]["last_active"] = time.time() - 1200

    closed = sweep_once(600)

    assert [row["sid"] for row in closed] == ["idle-1"]
    assert closed[0]["idleSeconds"] >= 600
    assert idle_bridge.stopped == [IDLE_REASON]
    assert session_registry.get("idle-1") is None
    assert session_registry.get("active-1") is not None
    assert active_bridge.stopped == []
    session_registry.unregister("active-1")


def test_timeout_zero_disables_sweeping():
    bridge = _StubBridge()
    session_registry.register("idle-2", kind="web", bridge=bridge)
    with session_registry._lock:
        session_registry._sessions["idle-2"]["last_active"] = 0.0

    assert sweep_once(0) == []
    assert session_registry.get("idle-2") is not None
    session_registry.close("idle-2", reason="测试收尾")


def test_sweeper_reads_the_configured_timeout(app):
    from app.settings_store import update_settings

    with app.app_context():
        update_settings({"session_idle_timeout": "120"})
        assert timeout_seconds(app) == 120.0
        update_settings({"session_idle_timeout": "1800"})


@pytest.fixture()
def _clean_registry():
    yield
    for entry in session_registry.list_all():
        session_registry.unregister(entry["sid"])
