"""演示目标机的 tty 行规程（ECHO）回归测试。

历史缺陷（用户报「在终端输入还是不会回显」）：``tools/demo_ssh_target.py`` 只在收到
Enter 之后把整行回显出去（``f"{line}\\r\\n{output}"``），而**真实 Linux 的逐字符回显是
tty 驱动（ECHO 标志）做的事，shell 本身不回显**。于是经堡垒机连接这台演示目标机时，
用户敲键期间一个字节都看不到 —— 看起来像「堡垒机把输入吞了」，实际是目标机不产生回显
（用 A/B 探针证实：直连 2200 与经网关 2222 都是同样的空回显，堡垒机是忠实透传）。

修法：把行规程抽成纯函数 ``line_discipline_echo``，shell 每收到一个字节就调用它。
本文件即钉住该行为，避免以后又退回「Enter 才回显」。
"""

from __future__ import annotations

import importlib.util
import os
import time

DEMO_TARGET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "demo_ssh_target.py",
)


def _load_demo_target():
    """按路径加载脚本（顶层无副作用，``main()`` 只在 ``__main__`` 下执行）。"""
    spec = importlib.util.spec_from_file_location("demo_ssh_target_under_test", DEMO_TARGET_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = _load_demo_target()
PROMPT = "root@demo-linux-01:~# "


def _type(text: str, buffer: bytearray | None = None):
    """模拟逐字节敲键盘，返回累计回显与行缓冲。"""
    buf = bytearray() if buffer is None else buffer
    echoed = b"".join(
        demo.line_discipline_echo(byte, buf, PROMPT) for byte in text.encode("utf-8")
    )
    return echoed, buf


def test_every_keystroke_is_echoed_immediately():
    """核心回归：不需要按 Enter，敲一个字符就回显一个字符。"""
    echoed, buf = _type("who")
    assert echoed == b"who", f"没有逐字符回显：{echoed!r}"
    assert bytes(buf) == b"who"


def test_type_command_then_enter_only_adds_newline():
    """Enter 之前命令行已经在屏幕上；Enter 之后 shell 只需补一个换行。"""
    echoed, buf = _type("whoami")
    assert echoed == b"whoami"
    assert bytes(buf) == b"whoami"  # shell_loop 在 Enter 分支里 send(b"\r\n")


def test_backspace_erases_one_character():
    buf = bytearray()
    _type("who", buf)
    echo = demo.line_discipline_echo(0x7F, buf, PROMPT)
    assert echo == b"\b \b", f"退格回显不对：{echo!r}"
    assert bytes(buf) == b"wh"
    # 空行上按退格：不回显、不越界
    empty = bytearray()
    assert demo.line_discipline_echo(0x08, empty, PROMPT) == b""


def test_ctrl_u_erases_the_whole_line():
    buf = bytearray()
    _type("whoami", buf)
    echo = demo.line_discipline_echo(0x15, buf, PROMPT)
    assert echo == b"\b \b" * 6, f"Ctrl-U 擦除量不对：{echo!r}"
    assert bytes(buf) == b""


def test_ctrl_c_rewrites_prompt_and_clears_buffer():
    buf = bytearray()
    _type("wr", buf)
    echo = demo.line_discipline_echo(0x03, buf, PROMPT)
    assert echo == b"^C\r\n" + PROMPT.encode(), f"Ctrl-C 回显不对：{echo!r}"
    assert bytes(buf) == b""


def test_tab_is_buffered_and_echoed():
    buf = bytearray()
    echo = demo.line_discipline_echo(0x09, buf, PROMPT)
    assert echo == b"\t"
    assert bytes(buf) == b"\t"


def test_other_control_bytes_are_silent_and_not_buffered():
    """ESC / Ctrl-A 之类：不回显也不进命令行（演示目标机不处理转义序列）。"""
    buf = bytearray()
    for byte in (0x1B, 0x01, 0x1A):
        assert demo.line_discipline_echo(byte, buf, PROMPT) == b""
    assert bytes(buf) == b""


def test_echo_follows_buffer_state_across_many_keystrokes():
    """连续行为：敲 6 个字符、退 2 个、再敲 3 个 —— 回显与缓冲必须始终一致。"""
    buf = bytearray()
    echoed, _ = _type("whoami", buf)
    echoed += demo.line_discipline_echo(0x7F, buf, PROMPT)
    echoed += demo.line_discipline_echo(0x7F, buf, PROMPT)
    echoed += demo.line_discipline_echo(ord("x"), buf, PROMPT)
    assert echoed == b"whoami" + b"\b \b" * 2 + b"x"
    assert bytes(buf) == b"whoax"


# ---------------------------------------------------------------------------
# 非交互命令执行（exec 请求）
# ---------------------------------------------------------------------------


class _FakeChannel:
    """够用的通道替身：记录服务端写出去的字节、退出状态与关闭动作。"""

    def __init__(self, send_delay: float = 0.0):
        self.sent = bytearray()
        self.exit_status = None
        self.closed = False
        self.send_delay = send_delay

    def send(self, data):  # noqa: ANN001
        if self.send_delay:
            time.sleep(self.send_delay)
        self.sent.extend(data)

    def send_exit_status(self, status):  # noqa: ANN001
        self.exit_status = status

    def close(self):
        self.closed = True


def _wait_for_close(channel, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not channel.closed:
        time.sleep(0.01)
    return channel.closed


def test_exec_request_is_accepted_and_returns_command_output():
    """历史缺陷：本演示目标机没实现 `check_channel_exec_request`，paramiko 的默认实现直接
    拒绝 exec 请求 → 客户端抛 `Channel closed.`。表现：堡垒机「连接测试」（跑 `uname -a`）
    在演示目标机上 HTTP 500，AI 的 `run_command` 这类工具也必然失败，只能走交互式 shell。
    """
    server = demo.DemoTargetServer("root", "s3cret")
    channel = _FakeChannel()

    accepted = server.check_channel_exec_request(channel, b"uname -a")

    assert accepted is True, "exec 请求必须被接受，否则客户端只能拿到 Channel closed."
    assert _wait_for_close(channel), "命令输出写完必须关闭通道，否则客户端一直等 EOF"
    assert channel.sent.decode() == demo.respond("uname -a")
    assert channel.exit_status == 0
    assert server.commands[-1] == "uname -a"


def test_exec_request_accepts_str_command_and_reports_unknown_commands():
    server = demo.DemoTargetServer("root", "s3cret")

    channel = _FakeChannel()
    assert server.check_channel_exec_request(channel, "whoami") is True
    assert _wait_for_close(channel)
    assert channel.sent.decode() == "opsadmin\n"

    other = _FakeChannel()
    assert server.check_channel_exec_request(other, "systemctl status nginx") is True
    assert _wait_for_close(other)
    assert "command not found" in other.sent.decode()


def test_exec_survives_a_client_that_disconnects_midway():
    """客户端提前断开时不能把异常抛回连接线程（否则演示目标机会掉线程/刷栈）。"""

    class _Exploding(_FakeChannel):
        def send(self, data):  # noqa: ANN001, ARG002
            raise OSError("connection reset by peer")

    server = demo.DemoTargetServer("root", "s3cret")
    channel = _Exploding()

    assert server.check_channel_exec_request(channel, b"df -h") is True
    assert _wait_for_close(channel), "异常路径也要走到 close()，否则通道泄漏"
    assert channel.exit_status is None, "发送失败时不该谎报退出码"


def test_escape_filter_swallows_bracketed_paste_markers():
    """粘贴标记是模式信号：真机由 readline 消费，演示目标机也不能把它写进行缓冲。

    回归的用户可见症状：经网关粘贴 ``/ask-ai 你好`` 后，下一条命令被拼成
    ``-bash: [201~echo AFTER-PASTE: command not found``。
    """
    f = demo.TtyEscapeFilter()

    assert f.feed(b"\x1b[200~/ask-ai \xe4\xbd\xa0\xe5\xa5\xbd\x1b[201~") == b"/ask-ai \xe4\xbd\xa0\xe5\xa5\xbd"
    assert f.feed(b"\x1b[201~") == b""  # 迟到的结束标记（AI 会话期间才读到）也不回显
    assert f.feed(b"echo AFTER-PASTE\n") == b"echo AFTER-PASTE\n"


def test_escape_filter_handles_split_sequences_and_arrow_keys():
    """半截序列要跨 recv 保持；方向键同样不能被当普通字符。"""
    f = demo.TtyEscapeFilter()

    assert f.feed(b"\x1b[") == b""
    assert f.feed(b"3~") == b""
    assert f.feed(b"\x1bOA\x1b[D") == b""
    assert f.feed(b"ls") == b"ls"
    # 单独 ESC 之后紧跟 Enter：控制字符要交回调用方，不能吞掉回车
    assert f.feed(b"\x1b\r") == b"\r"


def test_escape_filter_gives_up_on_oversized_or_malformed_sequences():
    """畸形/超长序列不能把后面所有输入都吃掉。"""
    f = demo.TtyEscapeFilter()

    # 超长参数序列：到达上限就放弃，之后的普通字节照常放行（不能永久吞输入）
    assert f.feed(b"\x1b[" + b"1" * 64 + b"ls").endswith(b"ls")
    # OSC（ESC ] …）不在支持范围：只吞 ESC ] 两个字节，其余照常（BEL 由行规程丢弃）
    assert f.feed(b"\x1b]0;title\x07ok") == b"0;title\x07ok"

