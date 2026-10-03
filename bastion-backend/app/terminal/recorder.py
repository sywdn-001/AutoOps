"""会话录制：原始字节流 + 结构化事件，写入 JSONL 便于回放与审计。"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from ..config import Config


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class TranscriptRecorder:
    """线程安全的 JSONL 会话录制器。

    每行一个事件：{"ts": "...", "t": "<type>", ...}
    type: session_start / input / output / command / deny / notice / resize / session_end
    """

    def __init__(self, sid: str, path: str | None = None, max_events: int | None = None):
        self.sid = sid
        self.path = path or os.path.join(Config.TRANSCRIPT_DIR, f"{sid}.log")
        self.max_events = max_events or Config.TRANSCRIPT_MAX_EVENTS
        self.events = 0
        self.truncated = False
        self._lock = threading.Lock()
        self._fh = None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None

    # ------------------------------------------------------------------ 写
    def record(self, etype: str, payload: dict | None = None, *, flush: bool = False) -> None:
        if self._fh is None:
            return
        with self._lock:
            if self.events >= self.max_events:
                if not self.truncated:
                    self.truncated = True
                    self._write({"ts": _now(), "t": "notice", "message": "录制事件数超限，后续事件被丢弃"})
                return
            entry = {"ts": _now(), "t": etype}
            if payload:
                entry.update(payload)
            self._write(entry)
            self.events += 1
            if flush:
                try:
                    self._fh.flush()
                except OSError:
                    pass

    def _write(self, entry: dict) -> None:
        try:
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def record_io(self, direction: str, data) -> None:
        """direction: 'in' (用户输入) / 'out' (远端输出)。"""
        if isinstance(data, bytes):
            text = data.decode("utf-8", "replace")
        else:
            text = str(data)
        self.record("input" if direction == "in" else "output", {"data": text})

    def close(self, reason: str = "", **extra) -> None:
        self.record("session_end", {"reason": reason, **extra}, flush=True)
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    # ------------------------------------------------------------------ 读
    @staticmethod
    def exists(path: str) -> bool:
        return bool(path) and os.path.isfile(path)

    @staticmethod
    def size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    @staticmethod
    def iter_events(path: str, offset: int = 0, limit: int = 500):
        """按行读取事件，返回 ``(events, next_offset)``，用于录像回放分页。

        ``offset`` / ``limit`` 都按**行号**计；``next_offset`` 是下次应传的 offset
        （命中 limit 时指向尚未读取的那一行，保证分页不丢事件）。

        契约提醒：本方法返回**二元组**。历史缺陷是只返回 list，而
        `app/api/sessions.py::session_transcript` 解包两个值 →
        `ValueError: too many values to unpack` → 会话录像回放接口 500。
        """
        out = []
        if not path or not os.path.isfile(path):
            return out, offset
        next_offset = offset
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index < offset:
                    continue
                if len(out) >= limit:
                    break
                next_offset = index + 1
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out, next_offset


def tail_stats(path: str, max_lines: int = 20000) -> dict:
    """统计录制文件中的命令数 / 拒绝数 / 时长，用于会话详情。"""
    stats = {"commands": 0, "denied": 0, "bytes": 0, "first_ts": "", "last_ts": ""}
    if not path or not os.path.isfile(path):
        return stats
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for index, line in enumerate(fh):
            if index >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            ts = evt.get("ts") or ""
            if ts:
                if not stats["first_ts"]:
                    stats["first_ts"] = ts
                stats["last_ts"] = ts
            if evt.get("t") == "command":
                stats["commands"] += 1
            elif evt.get("t") == "deny":
                stats["denied"] += 1
            elif evt.get("t") in ("input", "output"):
                stats["bytes"] += len(evt.get("data") or "")
    return stats


def wait_for_file(path: str, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.isfile(path):
            return True
        time.sleep(0.05)
    return os.path.isfile(path)
