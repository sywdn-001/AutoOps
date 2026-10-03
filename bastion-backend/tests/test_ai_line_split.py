"""``app/ai/line_split.py``：按键分流与 ``/ask-ai`` 识别。

重点回归用户报的 bug：**粘贴**输入（bracketed paste，``ESC[200~`` / ``ESC[201~``）时，
旧的 ``escape = 2`` 硬编码只吞两个字节，会把 ``00~`` 当成正文写进影子行，``/ask-ai`` 漏检后
被目标机 bash 执行——用户看到的就是 ``-bash: /ask-ai: 没有那个文件或目录``。

统一口径：**转义序列原样转发、但不进影子行；颜色/控制字节不影响判断；命中时不转发回车。**
"""

from __future__ import annotations

import pytest

from app.ai.line_split import (
    AI_PREFIXES,
    MAX_SHADOW,
    LineShadow,
    extract_question,
    is_ai_command,
)

# ---------------------------------------------------------------------------
# 命令识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/ask-ai 帮我看看磁盘", "帮我看看磁盘"),
        ("/ask-ai 你好", "你好"),
        ("/ask 你好", "你好"),  # 别名
        ("  /ask-ai   缩进也要 trim  ", "缩进也要 trim"),
        ("/ask-ai\t制表符分隔", "制表符分隔"),
        ("/ask-ai", ""),  # 只敲命令本身 → 空串（调用方打印用法）
        ("/ask", ""),
        ("/ask-aiX", None),  # 前缀后面必须跟空格/制表符
        ("/ask-a", None),
        ("echo /ask-ai", None),  # 出现在参数里不算
        ("", None),
        ("ls -l", None),
    ],
)
def test_extract_question(line: str, expected: str | None):
    assert extract_question(line) == expected
    assert is_ai_command(line) is (expected is not None)


def test_prefixes_are_the_shared_source_of_truth():
    """网关与网页终端共用同一套前缀，别再各自写一份。"""
    assert AI_PREFIXES == ("/ask-ai", "/ask")


# ---------------------------------------------------------------------------
# 按键分流
# ---------------------------------------------------------------------------


def test_bare_typing_is_intercepted_and_enter_is_not_forwarded():
    shadow = LineShadow()
    forward, hit = shadow.feed(b"/ask-ai \xe4\xbd\xa0\xe5\xa5\xbd\r")

    assert hit == "/ask-ai 你好"
    assert not forward.endswith(b"\r"), "命中时不能把回车转发给目标机"
    assert forward == "/ask-ai 你好".encode()


def test_type_by_type_then_enter_still_hits():
    """逐字符输入（真实终端会分段到达）也要命中。"""
    shadow = LineShadow()
    hits: list[str | None] = []
    for byte in "/ask-ai hi".encode():
        _, hit = shadow.feed(bytes([byte]))
        hits.append(hit)
    assert hits[:-1] == [None] * (len(hits) - 1)
    _, last = shadow.feed(b"\r")
    assert last == "/ask-ai hi"


def test_bracketed_paste_enter_after_the_end_marker():
    """粘贴：``ESC[200~ … ESC[201~`` 之后再回车（用户报的 bug 场景之一）。"""
    shadow = LineShadow()
    raw = b"\x1b[200~/ask-ai \xe4\xbd\xa0\xe5\xa5\xbd\x1b[201~\r"
    forward, hit = shadow.feed(raw)

    assert hit == "/ask-ai 你好", "粘贴的 /ask-ai 必须被识别（旧实现会漏检）"
    assert b"\r" not in forward
    assert b"\x1b[200~" in forward, "粘贴标记本身照旧转发给目标机"
    assert b"\x1b[201~" in forward


def test_paste_end_marker_in_the_same_chunk_is_still_forwarded():
    """命中之后同一批按键里剩下的字节不能丢。

    粘贴的结束标记 ``ESC[201~`` 通常紧跟在回车后面到达；丢掉它 readline 会一直停在
    括号粘贴状态，用户之后敲的回车会被当成粘贴正文（命令执行不了）——w1/w3 两位队友
    都独立指出过这个边界，这里锁死。
    """
    shadow = LineShadow()
    forward, hit = shadow.feed(b"\x1b[200~/ask-ai hi\r\x1b[201~")

    assert hit == "/ask-ai hi"
    assert b"\r" not in forward
    assert forward == b"\x1b[200~/ask-ai hi\x1b[201~", "收尾标记必须转发出去"


def test_later_bytes_in_the_same_chunk_are_still_forwarded():
    """命中之后同一批里再来的回车照常转发（多行粘贴不能静默吞掉用户输入）。"""
    shadow = LineShadow()
    forward, hit = shadow.feed(b"/ask-ai hi\rls\r")

    assert hit == "/ask-ai hi"
    assert forward.endswith(b"ls\r")


def test_bracketed_paste_with_newline_inside():
    """粘贴内容自带换行：``ESC[200~text\\nESC[201~``（编辑器粘贴整行的常见形态）。"""
    shadow = LineShadow()
    forward, hit = shadow.feed(b"\x1b[200~/ask-ai disk full?\n\x1b[201~")

    assert hit == "/ask-ai disk full?"
    assert b"\n" not in forward
    # 结束标记在命中后的下一次调用里照常转发，不会丢掉
    forward2, hit2 = shadow.feed(b"")
    assert hit2 is None
    assert forward2 == b""

    shadow2 = LineShadow()
    raw = b"\x1b[200~/ask-ai disk full?\n\x1b[201~"
    forwarded = bytearray()
    question = None
    for index in range(len(raw)):
        out, line = shadow2.feed(raw[index : index + 1])
        forwarded += out
        question = question or line
    assert question == "/ask-ai disk full?"
    assert forwarded == b"\x1b[200~/ask-ai disk full?\x1b[201~"


def test_csi_sequence_split_across_chunks():
    """转义序列被 TCP 分段也要正确吞掉（状态必须跨 feed 保持）。"""
    shadow = LineShadow()
    out1, hit1 = shadow.feed(b"\x1b[20")
    out2, hit2 = shadow.feed(b"0~/ask-ai hi\r")

    assert hit1 is None and hit2 == "/ask-ai hi"
    assert bytes(out1) + out2 == b"\x1b[200~/ask-ai hi"


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1b[A",  # ↑
        b"\x1b[B",  # ↓
        b"\x1b[1;5C",  # Ctrl+→
        b"\x1b[3~",  # Delete
        b"\x1bOP",  # F1
        b"\x1b[200~",  # bracketed paste 开始
        b"\x1b[201~",  # bracketed paste 结束
        b"\x1b[?2004h",  # 括号粘贴模式开关
        b"\x1b]0;title\x07",  # OSC
    ],
)
def test_escape_sequences_never_pollute_the_shadow(sequence: bytes):
    shadow = LineShadow()
    shadow.feed(sequence)
    shadow.feed(b"/ask-ai hi")
    assert shadow.pending == "/ask-ai hi", f"{sequence!r} 不该进影子行"

    _, hit = shadow.feed(b"\r")
    assert hit == "/ask-ai hi"


def test_normal_line_is_forwarded_byte_for_byte():
    shadow = LineShadow()
    raw = b"ls -l /var/log\r"
    forward, hit = shadow.feed(raw)

    assert hit is None
    assert forward == raw, "普通输入必须原样转发（本地回显/补全交给目标机）"
    assert shadow.pending == ""


def test_backspace_edits_the_shadow():
    shadow = LineShadow()
    shadow.feed(b"/ask-ai hi")
    shadow.feed(b"\x7f")  # 退格删掉 i
    _, hit = shadow.feed(b"\r")
    assert hit == "/ask-ai h"


def test_ctrl_c_and_ctrl_u_forget_the_line():
    for control in (b"\x03", b"\x15", b"\x04"):
        shadow = LineShadow()
        shadow.feed(b"/ask-ai partial")
        shadow.feed(control)
        shadow.feed(b"ls")
        _, hit = shadow.feed(b"\r")
        assert hit is None, f"{control!r} 之后行状态要清空"
        assert shadow.pending == ""


def test_shadow_does_not_grow_without_bound():
    shadow = LineShadow()
    shadow.feed(b"x" * (MAX_SHADOW * 3))
    assert len(shadow.pending) <= MAX_SHADOW, "影子行必须有上限，避免异常输入撑爆内存"


def test_pending_and_clear():
    shadow = LineShadow()
    shadow.feed(b"/ask")
    assert shadow.pending == "/ask"
    shadow.clear()
    assert shadow.pending == ""
    _, hit = shadow.feed(b"/ask-ai hi\r")
    assert hit == "/ask-ai hi"
