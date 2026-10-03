"""录制文件读取（TranscriptRecorder.iter_events）的分页契约测试。

背景（真实缺陷，由对**运行中服务**的端到端联调暴露）：
`iter_events()` 只返回事件列表，而 `app/api/sessions.py::session_transcript`
按 `events, next_offset = TranscriptRecorder.iter_events(...)` 解包 →
`ValueError: too many values to unpack (expected 2)` →
`GET /api/sessions/<id>/transcript` 直接 500，**会话录像回放功能整条不可用**
（pytest 里没有任何用例打过这个接口，所以一直绿）。

现在契约定死：`iter_events(path, offset, limit) -> (events, next_offset)`，
offset/limit 按**行**计，next_offset 指向下一次应该传的 offset。
"""

from __future__ import annotations

import json

from app.terminal.recorder import TranscriptRecorder, tail_stats


def _write(path, count: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for index in range(count):
            fh.write(json.dumps({"t": "event", "seq": index}, ensure_ascii=False) + "\n")


def test_iter_events_returns_events_and_next_offset(tmp_path):
    path = tmp_path / "s.log"
    _write(path, 3)
    events, next_offset = TranscriptRecorder.iter_events(str(path), offset=0, limit=10)
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert next_offset == 3


def test_iter_events_paginates_without_losing_events(tmp_path):
    path = tmp_path / "s.log"
    _write(path, 5)
    seen: list[int] = []
    offset = 0
    for _ in range(10):
        events, offset = TranscriptRecorder.iter_events(str(path), offset=offset, limit=2)
        if not events:
            break
        seen.extend(e["seq"] for e in events)
    assert seen == [0, 1, 2, 3, 4], f"分页丢事件：{seen}"


def test_iter_events_next_offset_points_at_unread_line(tmp_path):
    """命中 limit 时 next_offset 必须指向尚未读取的那一行，否则会漏事件。"""
    path = tmp_path / "s.log"
    _write(path, 4)
    events, next_offset = TranscriptRecorder.iter_events(str(path), offset=0, limit=2)
    assert [e["seq"] for e in events] == [0, 1]
    assert next_offset == 2
    events2, next_offset2 = TranscriptRecorder.iter_events(str(path), offset=next_offset, limit=2)
    assert [e["seq"] for e in events2] == [2, 3]
    assert next_offset2 == 4


def test_iter_events_skips_blank_and_broken_lines_without_breaking_offset(tmp_path):
    path = tmp_path / "s.log"
    path.write_text(
        "\n".join(
            [
                json.dumps({"t": "session_start"}),
                "",
                "{ not json }",
                json.dumps({"t": "command", "command": "whoami"}),
                "",
            ]
        ),
        encoding="utf-8",
    )
    events, next_offset = TranscriptRecorder.iter_events(str(path), offset=0, limit=10)
    assert [e["t"] for e in events] == ["session_start", "command"]
    # 行号口径 = 文件迭代语义：末尾的 "\n" 只是结束第 4 行，不产生第 5 行；
    # 空行与坏 JSON 行被跳过但行号照常推进（否则翻页会重复/卡死）。
    assert next_offset == 4


def test_iter_events_handles_missing_file(tmp_path):
    events, next_offset = TranscriptRecorder.iter_events(str(tmp_path / "nope.log"), offset=7, limit=5)
    assert events == []
    assert next_offset == 7
    assert TranscriptRecorder.iter_events("", offset=0, limit=5) == ([], 0)


def test_tail_stats_counts_commands_and_denials(tmp_path):
    path = tmp_path / "s.log"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "command", "command": "whoami"}) + "\n")
        fh.write(json.dumps({"t": "deny", "command": "cat /etc/shadow"}) + "\n")
    stats = tail_stats(str(path))
    assert stats["commands"] == 1
    assert stats["denied"] == 1
