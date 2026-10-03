"""终端输出流的两条硬约束（真机联调暴露的缺陷的防回归护栏）。

缺陷背景（用户报障：「ssh 进去之后提示文字错位」「没有从头开始」）::

    1. 时间顺序：目标机的登录欢迎语（MOTD）抢在堡垒机的
       「[堡垒机] 已连接 <主机>（<地址>），账号 <账号>，会话号 <sid>」横幅之前
       出现 —— 因为横幅是在 ``open_session()`` 返回之后才写的，而 ``bridge.start()``
       已经在它内部启动了读数线程，目标机输出从那一刻起就回流了。
       修法：``open_session(on_ready=...)`` 在 ``bridge.start()`` **之前**回调。

    2. 行结束符：真实 Linux 的 PTY 默认 ``ONLCR``，shell 的 ``\\n`` 会被终端驱动
       转成 ``\\r\\n``；而示例目标机 / 网络设备 / 部分容器 shell 不做这次转换。
       终端里 **LF 只下移一行不回列**，于是输出一行比一行右移，表现为提示符
       「没有从头开始」、回显错位。
       修法：``ShellBridge._emit_output()`` 统一走 ``LineEndingNormalizer``
       （等价于替目标机补上 ONLCR），只在客户端可见的输出路径上归一化，
       录像仍记录原始字节。

这两条都是「pytest 全绿但用户一眼就能看出不对」的问题，所以护栏必须落在
**真实 SSH 协议栈 + 真实字节流**上，而不是渲染函数返回值上。
"""

from __future__ import annotations

import re
import time

import paramiko
import pytest

from app.gateway.server import BANNER_SHIELD, GatewayServer
from app.session_service import open_session, teardown_session
from app.terminal.bridge import LineEndingNormalizer
from tests.test_integration_ssh import FakeTargetServer, _free_port, _wait, transcript_dir  # noqa: F401

BARE_LF = re.compile(rb"(?<!\r)\n")
BARE_CR = re.compile(rb"\r(?!\n)")
ANSI_SGR = re.compile(rb"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# 1) 归一化器本身
# ---------------------------------------------------------------------------
def test_normalizer_converts_bare_lf_to_crlf():
    normalizer = LineEndingNormalizer()
    assert normalizer.feed(b"a\nb") == b"a\r\nb"
    assert normalizer.feed(b"one\ntwo\nthree") == b"one\r\ntwo\r\nthree"


def test_normalizer_keeps_existing_crlf_intact():
    normalizer = LineEndingNormalizer()
    assert normalizer.feed(b"a\r\nb\r\n") == b"a\r\nb\r\n"
    assert not BARE_LF.search(normalizer.feed(b"x\r\ny\r\n"))


def test_normalizer_does_not_double_cr_across_chunks():
    """``\\r`` 和 ``\\n`` 被拆到两个网络包时，不能插出 ``\\r\\r\\n``。"""
    normalizer = LineEndingNormalizer()
    first = normalizer.feed(b"prompt> ")
    second = normalizer.feed(b"\r")
    third = normalizer.feed(b"\nnext line")
    blob = first + second + third
    assert blob == b"prompt> \r\nnext line"
    assert b"\r\r" not in blob


def test_normalizer_fast_paths_and_edge_cases():
    normalizer = LineEndingNormalizer()
    assert normalizer.feed(b"") == b""
    # 没有 LF 的包原样返回（不做逐字节拷贝）
    data = b"\x1b[31mred\x1b[0m"
    assert normalizer.feed(data) is data
    # 单独的 CR（进度条 / 覆盖重绘）不被改写
    assert normalizer.feed(b"50%\r") == b"50%\r"
    # 混合：裸 LF 补 CR，已有 CRLF 不重复
    assert normalizer.feed(b"a\r\nb\nc") == b"a\r\nb\r\nc"


def test_normalizer_is_stateful_per_instance():
    one, two = LineEndingNormalizer(), LineEndingNormalizer()
    assert one.feed(b"x\r") == b"x\r"
    assert one.feed(b"\n") == b"\n"  # 接着上一个 CR，不补
    assert two.feed(b"\n") == b"\r\n"  # 另一个实例不受影响


# ---------------------------------------------------------------------------
# 2) 网页终端路径：on_ready 先于任何输出，且客户端看不到裸 LF
# ---------------------------------------------------------------------------
def test_webterm_ready_callback_runs_before_target_output(
    app, make_user, make_host, make_account, make_grant, transcript_dir
):
    target = FakeTargetServer(bare_lf=True)
    try:
        user_id = make_user("bareuser")
        host_id = make_host("bare-web-01", address=target.host, port=target.port)
        account_id = make_account(host_id, name="root", username="root", password="s3cret")
        make_grant(user_id, host_id, account_id=account_id)

        order: list[tuple[str, object]] = []
        chunks: list[bytes] = []

        def on_output(data: bytes) -> None:
            order.append(("output", len(data)))
            chunks.append(data)

        def on_ready(sid: str, info: dict) -> None:
            order.append(("ready", (sid, info)))

        opened = open_session(
            app,
            user_id=user_id,
            host_id=host_id,
            account_id=account_id,
            source="web",
            send_output=on_output,
            on_ready=on_ready,
        )
        try:
            assert order, "on_ready 没有被调用"
            assert order[0][0] == "ready", f"回调顺序不对：{[e[0] for e in order]}"
            ready_sid, info = order[0][1]  # type: ignore[misc]
            assert ready_sid == opened.sid
            assert info["hostName"] == "bare-web-01"
            assert info["accountUsername"] == "root"
            assert info["policyName"], "policyName 不能为空"

            opened.bridge.feed_input(b"whoami\r")
            assert _wait(lambda: b"audituser" in b"".join(chunks) or None), "没收到目标机输出"

            blob = b"".join(chunks)
            assert not BARE_LF.search(blob), f"网页终端收到裸 LF：{blob!r}"
            assert b"\r\n" in blob, "输出被整体吞掉了？"
        finally:
            teardown_session(opened, reason="测试结束")
    finally:
        target.stop()


# ---------------------------------------------------------------------------
# 3) 网关路径：横幅先于目标机输出，且整条客户端字节流没有裸 LF
# ---------------------------------------------------------------------------
def _collect_bytes(channel, seconds: float = 2.0, stop_at: bytes | None = None) -> bytes:
    """按**字节**收集通道输出（裸 LF 检查必须在字节层做）。"""
    buf = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if channel.recv_ready():
            buf += channel.recv(65536)
            if stop_at is not None and stop_at in buf:
                break
        else:
            time.sleep(0.05)
    return bytes(buf)


def test_gateway_banner_precedes_target_output_and_stream_has_no_bare_lf(
    app, make_user, make_host, make_account, make_grant, transcript_dir
):
    target = FakeTargetServer(bare_lf=True)
    username, password = "baregw", "User1234"
    client = None
    gateway = None
    try:
        user_id = make_user(username, password=password)
        host_id = make_host("bare-gw-01", address=target.host, port=target.port)
        account_id = make_account(host_id, name="root", username="root", password="s3cret")
        make_grant(user_id, host_id, account_id=account_id)

        app.config["GATEWAY_ENABLED"] = True
        app.config["GATEWAY_HOST"] = "127.0.0.1"
        app.config["GATEWAY_PORT"] = _free_port()
        gateway = GatewayServer(app)
        assert gateway.start(), f"网关没起来：{gateway.last_error}"

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=gateway.port,
            username=username,
            password=password,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
        )
        channel = client.invoke_shell()
        menu = _collect_bytes(channel, 8.0, stop_at="选择主机".encode())
        assert "选择主机" in menu.decode("utf-8", "replace"), f"没等到菜单：{menu[-300:]!r}"
        # 进站字符画也是多行输出：它同样必须走 CRLF、同样必须出现在菜单之前
        art = BANNER_SHIELD[1].encode()
        assert art in menu, f"没有打出进站字符画：{menu[:300:]!r}"
        assert menu.index(art) < menu.index("你可访问的主机".encode())

        channel.send("1\n")
        stream = menu + _collect_bytes(channel, 6.0, stop_at=b"$ ")
        # 横幅里 32m/0m 这类 SGR 颜色码夹在「[堡垒机]」与「已连接」之间，
        # 断言前先剥掉颜色码（产品自己带色是刻意的）。
        plain = ANSI_SGR.sub(b"", stream)
        assert "[堡垒机] 已连接 bare-gw-01".encode() in plain, f"横幅没出现：{plain[-400:]!r}"

        # 顺序：横幅必须排在目标机输出（假目标机注入 PS1 后回的 "$ " 提示符）之前
        assert plain.index("[堡垒机] ".encode()) < plain.index(b"$ "), (
            "目标机输出抢在堡垒机横幅之前：" + repr(plain)
        )
        # 目标机整条裸 LF 的流被归一化：客户端一个裸 LF 都不该看到
        assert not BARE_LF.search(stream), f"客户端收到裸 LF：{stream!r}"
        assert b"\r\n" in stream
        assert not BARE_CR.search(stream), f"客户端收到孤立 CR：{stream!r}"

        # 需要真发一条命令，证明会话确实在跑（不是只有横幅）
        channel.send("hostname\n")
        tail = _collect_bytes(channel, 6.0, stop_at=b"fake-linux-01")
        assert b"fake-linux-01" in tail, f"命令没有执行：{tail!r}"
        assert not BARE_LF.search(tail)
        assert target.commands, "命令没有真的发到目标机"
    finally:
        if client is not None:
            client.close()
        if gateway is not None:
            gateway.stop()
        target.stop()


# ---------------------------------------------------------------------------
# 3) 网页终端：会话就绪前到达的按键不能丢
# ---------------------------------------------------------------------------
def test_webterm_keeps_input_sent_before_session_is_ready(
    app,
    client,
    make_user,
    make_host,
    make_account,
    make_grant,
    transcript_dir,  # noqa: F811 - 让录像写进临时目录
):
    """P0 回归：会话就绪前到达的按键被静默丢弃。

    历史缺陷（真机 E2E 37/40 的三条 FAIL 全出自它）：``terminal:opened`` 事件一度在
    ``open_session()`` **内部**发出，而 ``ctx["opened"] = opened`` 在它**返回之后**
    才执行。客户端收到 opened 立刻发 ``terminal:input`` 时，处理线程里 ctx 还没有会话，
    ``on_input`` 直接 ``return``，于是：

    * 第一条命令凭空消失（网页终端里 ``whoami`` 不回显）；
    * ``terminal:command`` 的 allow 事件永远收不到；
    * 命令日志里只剩 15 秒后发的第二条（``cat /etc/shadow`` deny）。

    本用例在真实 Socket.IO 层**故意先发按键、后开会话**，钉住「按键不丢」这条契约。
    """
    from flask_socketio.test_client import SocketIOTestClient

    from app.extensions import socketio as socketio_server
    from tests.conftest import token_of

    username, password = "earlyuser", "User1234"
    user_id = make_user(username, role_code="ops", password=password)
    target = FakeTargetServer()
    host_id = make_host("early-01", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id)
    token = token_of(client, username, password)

    sio = SocketIOTestClient(
        app, socketio_server, auth={"token": token}, flask_test_client=client
    )
    assert sio.is_connected(), f"Socket.IO 没连上：{sio.get_received()}"
    packets: list[dict] = []

    def drain(names: set[str], timeout: float = 20.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while True:
            packets.extend(sio.get_received())
            hits = [item for item in packets if item["name"] in names]
            if hits or time.monotonic() >= deadline:
                return hits
            time.sleep(0.05)

    try:
        assert drain({"terminal:ready"}), "没有收到 terminal:ready"

        # 会话还没建立就先敲键盘（握手期间用户已经在输入）
        sio.emit("terminal:input", {"data": "whoami\n"})
        sio.emit(
            "terminal:open",
            {"hostId": host_id, "accountId": account_id, "cols": 100, "rows": 30},
        )
        opening = drain({"terminal:opened", "terminal:error"})
        assert opening, "terminal:open 没有任何回执"
        assert opening[-1]["name"] == "terminal:opened", f"会话没打开：{opening[-1]}"

        commands = drain({"terminal:command"})
        assert commands, "就绪前的按键被丢了：terminal:command 一直没推送"
        payload = commands[0]["args"][0]
        assert payload["command"] == "whoami", payload
        assert payload["action"] == "allow", payload

        outputs = drain({"terminal:output"})
        assert any(
            "audituser" in item["args"][0].get("data", "") for item in outputs
        ), "就绪前的按键没有真的执行到目标机"
    finally:
        sio.emit("terminal:close")
        sio.disconnect()
        target.stop()

