"""网页终端模块（浏览器 WebSocket ↔ 远端 SSH shell）。"""

from .events import register_events

__all__ = ["register_events"]
