"""SSH 网关：连接后清屏（用户要求「连接到 Linux 客户机之后清一下屏，要不然太乱了」）。

两条硬约束在这里被钉死：

1. 清屏是**纯客户端**行为：写 ``\\x1b[2J\\x1b[H`` + 重打紧凑上下文，再用一个**裸回车**
   让 bash 重画 PS1。**绝不能在目标机上执行 ``clear``**——那等于用户没敲却多出一条命令，
   既过命令策略又落审计。
2. 清屏必须发生在 MOTD 已经回流之后：桥接的 ``at_prompt`` 一置位就说明开机输出已转发，
   此时才擦屏；超过 ~2 秒等不到就静默跳过，绝不卡住会话循环。
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from app.gateway.server import (
    CLEAR_SCREEN,
    _clear_screen_after_prompt,
    _write_session_context,
    strip_ansi,
)
from app.terminal.bridge import BridgeConfig, ShellBridge

ENTRY = {"hostId": 1, "hostName": "db-01", "address": "10.0.0.12"}
ACCOUNT = {"id": 7, "name": "运维", "username": "root"}
SID = "0123456789abcdef"


class FakeWriter:
    """收集网关写给客户端的全部字节（含 ANSI 控制序列）。"""

    def __init__(self):
        self.text = ""

    def write(self, text: str):
        self.text += text

    def write_bytes(self, data: bytes):
        self.text += data.decode("utf-8", "replace")

    def line(self, text: str = ""):
        self.write(text + "\r\n")

    @property
    def plain(self) -> str:
        return strip_ansi(self.text)


class FakeBridge:
    """只保留清屏需要的接口：就绪标志 + 发往目标机的字节记录。"""

    def __init__(self, *, at_prompt: bool = True, segmented: bool = True, closed: bool = False):
        self.at_prompt = at_prompt
        self.segmented = segmented
        self.closed = closed
        self.inputs: list[bytes] = []

    def feed_input(self, data: bytes):
        self.inputs.append(bytes(data))

    @property
    def target_bytes(self) -> bytes:
        return b"".join(self.inputs)


class RecordingRecorder:
    """分离「审计事件」与「I/O 流水」，这样才能证明空行没生成任何命令。"""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.io: list[tuple[str, bytes]] = []

    def record(self, kind: str, payload: dict):
        self.events.append((kind, dict(payload)))

    def record_io(self, direction: str, data: bytes):
        self.io.append((direction, bytes(data)))


class FakeChannel:
    """假目标机通道：只要不真的起读写线程，它就不会收到任何字节。"""

    def __init__(self):
        self.sent = bytearray()

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, timeout):
        pass

    def resize_pty(self, width=80, height=24):
        pass

    def close(self):
        pass


def _queued_for_target(bridge: ShellBridge) -> bytes:
    """把桥接写队列里攒下的字节取出来，即「目标机实际会收到的字节」。"""
    out = bytearray()
    while True:
        try:
            item = bridge._queue.get_nowait()
        except queue.Empty:
            break
        if item:
            out += item
    return bytes(out)


# ---------------------------------------------------------------------------
# 清屏本身
# ---------------------------------------------------------------------------
def test_clear_screen_writes_ansi_then_context_and_sends_only_a_bare_enter():
    writer = FakeWriter()
    bridge = FakeBridge()

    assert _clear_screen_after_prompt(bridge, writer, ENTRY, ACCOUNT, SID) is True

    # 字节级：先擦屏（擦全屏 + 光标归位），紧接着才是重打的上下文
    assert writer.text.startswith(f"{CLEAR_SCREEN}\r\n"), "清屏序列必须在最前面，否则会把上下文一起擦掉"
    assert writer.text.index(CLEAR_SCREEN) < writer.text.index("已连接")

    plain = writer.plain
    assert "已连接 db-01（10.0.0.12）" in plain
    assert "账号 root" in plain and f"会话号 {SID}" in plain
    assert "输入的命令与输出都会被审计记录" in plain
    assert "/ask-ai <问题>" in plain
    assert "\x1b[" in writer.text, "重打的上下文要保留配色"

    # 目标机侧：只有一个裸回车（让 bash 重画 PS1），没有任何 clear 命令
    assert bridge.inputs == [b"\n"], f"只应给目标机送一个空行，实际 {bridge.inputs!r}"
    assert b"clear" not in bridge.target_bytes
    assert bridge.target_bytes == b"\n"


def test_clear_screen_context_is_the_same_wording_as_the_connect_banner():
    """连接横幅与清屏重打共用 ``_write_session_context``，文案不能漂移。"""
    banner = FakeWriter()
    _write_session_context(banner, ENTRY, ACCOUNT, SID)

    writer = FakeWriter()
    _clear_screen_after_prompt(FakeBridge(), writer, ENTRY, ACCOUNT, SID)

    assert writer.text == CLEAR_SCREEN + banner.text


def test_clear_screen_waits_for_the_target_prompt():
    """目标机还没到提示符时要等；到了才擦屏（保证 MOTD 已经回流）。"""
    writer = FakeWriter()
    bridge = FakeBridge(at_prompt=False)
    threading.Timer(0.12, lambda: setattr(bridge, "at_prompt", True)).start()

    assert _clear_screen_after_prompt(bridge, writer, ENTRY, ACCOUNT, SID, timeout=2.0) is True
    assert CLEAR_SCREEN in writer.text
    assert bridge.inputs == [b"\n"]


def test_clear_screen_treats_non_segmented_sessions_as_ready():
    """原始录制模式拿不到提示符标记，``_arm`` 已排空开机输出 → 直接清。"""
    writer = FakeWriter()
    bridge = FakeBridge(at_prompt=False, segmented=False)

    assert _clear_screen_after_prompt(bridge, writer, ENTRY, ACCOUNT, SID) is True
    assert CLEAR_SCREEN in writer.text


def test_clear_screen_gives_up_quickly_without_blocking_the_session():
    """超时（提示符一直没出现）就静默跳过：不写任何字节，也不拖住会话循环。"""
    writer = FakeWriter()
    bridge = FakeBridge(at_prompt=False)

    started = time.monotonic()
    assert _clear_screen_after_prompt(bridge, writer, ENTRY, ACCOUNT, SID, timeout=0.15) is False
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"清屏失败必须立刻放弃，实际等了 {elapsed:.3f}s"
    assert writer.text == ""
    assert bridge.inputs == []


def test_clear_screen_skips_a_session_that_already_closed():
    writer = FakeWriter()
    bridge = FakeBridge(at_prompt=False, closed=True)

    started = time.monotonic()
    assert _clear_screen_after_prompt(bridge, writer, ENTRY, ACCOUNT, SID, timeout=2.0) is False
    assert time.monotonic() - started < 0.5, "会话已关闭就该立刻返回，不能空等 deadline"
    assert writer.text == "" and bridge.inputs == []


# ---------------------------------------------------------------------------
# 裸回车为什么安全：空行不建命令、不进策略、不落库
# ---------------------------------------------------------------------------
def _bare_bridge(recorder: RecordingRecorder) -> ShellBridge:
    bridge = ShellBridge(FakeChannel(), BridgeConfig(sid="clear-sid", recorder=recorder, host_label="db-01"))
    bridge.segmented = True
    bridge.at_prompt = True
    return bridge


def test_empty_enter_only_writes_a_carriage_return_to_the_target():
    recorder = RecordingRecorder()
    bridge = _bare_bridge(recorder)

    bridge.feed_input(b"\n")
    bridge.feed_input(b"\r")

    assert _queued_for_target(bridge) == b"\r\r", "空行只该把 CR 回写给目标机，让 bash 重画 PS1"
    assert bridge._pending is None, "空行不能建命令 pending"
    assert bridge.seq == 0, "空行不能让 seq 前进（seq 前进 = 落库一条命令事件）"
    assert recorder.events == [], f"空行不该产生任何审计事件，实际 {recorder.events!r}"
    assert recorder.io == [("in", b"\n"), ("in", b"\r")], "只应留下 I/O 流水"


def test_empty_enter_never_reaches_the_command_policy(monkeypatch):
    """把命令策略换成「一调用就炸」，空行仍然平安通过 = 证明它没进策略。"""

    def boom(*args, **kwargs):
        raise AssertionError("空行不该进命令策略（否则等于用户没敲却多一条命令）")

    monkeypatch.setattr("app.terminal.bridge.evaluate_policy", boom)

    recorder = RecordingRecorder()
    bridge = _bare_bridge(recorder)
    bridge.feed_input(b"\n")

    assert _queued_for_target(bridge) == b"\r"
    assert recorder.events == []


def test_clear_screen_sequence_is_pure_text_and_ansi():
    """终端输出只能是纯文本 + ANSI，不能夹带交互控件（CSI 查询/鼠标模式等）。"""
    writer = FakeWriter()
    _clear_screen_after_prompt(FakeBridge(), writer, ENTRY, ACCOUNT, SID)

    assert writer.text.startswith("\x1b[2J\x1b[H")
    for forbidden in ("\x1b[?1049", "\x1b[?25", "\x1b[?1000", "\x1b]0;", "\x1b[6n"):
        assert forbidden not in writer.text, f"不该出现交互控制序列 {forbidden!r}"


@pytest.mark.parametrize("account", [None])
def test_clear_screen_handles_a_session_without_an_account(account):
    writer = FakeWriter()
    assert _clear_screen_after_prompt(FakeBridge(), writer, ENTRY, account, SID) is True
    assert "账号 -" in writer.plain
