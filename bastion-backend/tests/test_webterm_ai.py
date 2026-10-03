"""/ask-ai 拦截 + 连接后清屏（网页终端）回归测试。

覆盖两组证据：
1. 单元级：用假 bridge / 假 pump 直接驱动 ``events._handle_session_input``，
   断言转发字节、Ctrl-U、AI worker 线程、队列喂行、清屏字节；
2. 真链路：SocketIOTestClient + FakeTargetServer，证明 /ask-ai 到不了目标机、
   清屏字节确实推给了浏览器、并且清屏空行不产生命令审计。

**绝不真连 DeepSeek**：真实 ``ai_shell.run_ask_ai`` 被猴子补丁替换。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.extensions import socketio as socketio_server
from app.gateway import ai_shell as ai_shell_module
from app.models import CommandLog
from app.webterm import events
from tests.conftest import token_of
from tests.test_integration_ssh import (  # noqa: F401  导入即注册 fixture
    target,
    transcript_dir,
)


# ---------------------------------------------------------------------------
# 假对象：不碰真 socket / 真 SSH / 真 AI
# ---------------------------------------------------------------------------
class FakeBridge:
    """替身 Bridget：只记录喂给目标机的字节。"""

    def __init__(self) -> None:
        self.fed: list[bytes] = []
        self.closed = False

    def feed_input(self, data: bytes) -> None:
        if data:
            self.fed.append(bytes(data))

    @property
    def joined(self) -> bytes:
        return b"".join(self.fed)


class FakePump:
    """替身 OutputPump：只记录推给浏览器的字节。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.paused = False
        self.stopped = False

    def push(self, data) -> None:
        if not data:
            return
        if isinstance(data, (bytes, bytearray)):
            self.chunks.append(bytes(data).decode("utf-8", "replace"))
        else:
            self.chunks.append(str(data))

    def pause(self) -> None:
        self.paused = True

    def release(self) -> None:
        self.paused = False

    def stop(self) -> None:
        self.stopped = True

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class FakeOpened:
    """替身 OpenedSession：字段与 app/session_service.OpenedSession 对齐。"""

    def __init__(self, sid: str = "webterm-test-1") -> None:
        self.sid = sid
        self.record_id = 1
        self.bridge = FakeBridge()
        self.segmented = False
        self.host_name = "web-01"
        self.account_username = "root"
        self.policy_name = "默认策略"
        self.closed = False


#: 事件里 ctx["app"] 为 None 时 AI worker 会直接返回，单元测试给个占位对象即可
#: （假 run_ask_ai 不碰 app；真链路测试用的是真 Flask app）
_DUMMY_APP = object()


def _make_ctx(app=_DUMMY_APP, opened=None) -> dict:
    """构造 on_connect/on_open 之后应有的 ctx（键与 events.py 保持一致）。"""
    opened = opened or FakeOpened()
    pump = FakePump()
    ctx = {
        "user_id": 1,
        "username": "alice",
        "opened": opened,
        "pump": pump,
        "early_input": [],
        "shadow": events._line_split().LineShadow(),
        "ai": events.AiTurnState(),
        "writer": None,
        "app": app,
        "cols": 100,
        "ai_entry": {"hostId": 1, "hostName": "web-01", "address": "127.0.0.1:22"},
        "ai_account": {"username": "root"},
    }
    ctx["writer"] = events.WebTermWriter(pump)
    return ctx


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ---------------------------------------------------------------------------
# 夹具：把真实 run_ask_ai 换成假的，并记录它拿到的上下文
# ---------------------------------------------------------------------------
@pytest.fixture()
def ai(monkeypatch):
    rec: dict = {
        "calls": [],
        "answer": "磁盘还剩 42G",
        "need_input": 0,
        "input_event": threading.Event(),
        "done": threading.Event(),
        "thread_name": "",
        "lines": [],
    }

    def fake_run_ask_ai(
        app,
        writer,
        *,
        user_id,
        entry,
        account,
        sid,
        question,
        width,
        read_line,
        conversation_id=None,
    ):
        rec["thread_name"] = threading.current_thread().name
        rec["calls"].append(
            {
                "user_id": user_id,
                "entry": entry,
                "account": account,
                "sid": sid,
                "question": question,
                "width": width,
                "conversation_id": conversation_id,
            }
        )
        try:
            writer.line(f"[堡垒机] 提问：{question}")
            for _ in range(int(rec["need_input"])):
                line, aborted = read_line("管理员密码：", mask=True)
                rec["lines"].append((line, aborted))
                rec["input_event"].set()
                if aborted:
                    return conversation_id
            writer.line(f"[堡垒机] AI 回答：{rec['answer']}")
            return 42
        finally:
            rec["done"].set()

    monkeypatch.setattr(ai_shell_module, "run_ask_ai", fake_run_ask_ai, raising=True)
    return rec


# ---------------------------------------------------------------------------
# 交付 1：/ask-ai 拦截
# ---------------------------------------------------------------------------
def test_ask_ai_in_one_chunk_is_intercepted_without_enter(ai):
    """一次 chunk 敲完 /ask-ai 你好\\r：回车不能到目标机，AI 在独立线程回答。"""
    ctx = _make_ctx()
    events._handle_session_input(ctx, "/ask-ai 你好\r")

    fed = ctx["opened"].bridge.fed
    joined = ctx["opened"].bridge.joined
    assert fed == [b"/ask-ai \xe4\xbd\xa0\xe5\xa5\xbd", b"\x15"], fed
    assert b"\r" not in joined and b"\n" not in joined, "命中 /ask-ai 时绝不能把回车转发给目标机"
    assert fed[-1] == b"\x15", "命中后必须补发 Ctrl-U 抹掉目标机行缓冲"

    assert ai["done"].wait(5), "AI worker 没有在 5 秒内跑完"
    assert ai["calls"][0]["question"] == "你好"
    assert ai["thread_name"].startswith("webterm-ai-"), "AI 问答必须跑在独立 worker 线程"
    assert _wait_until(lambda: ctx["ai"].active is False), "worker 结束后 active 应复位"
    assert "AI 回答" in ctx["pump"].text, "AI 输出没有推到该网页终端"


def test_bracketed_paste_ask_ai_is_intercepted(ai):
    """终端 bracketed paste 粘贴 /ask-ai hi：命中、回车不转发、补 Ctrl-U。"""
    ctx = _make_ctx()
    events._handle_session_input(ctx, "\x1b[200~/ask-ai hi\r\x1b[201~")

    joined = ctx["opened"].bridge.joined
    assert b"\x1b[200~" in joined, "粘贴起始标记必须原样转发"
    assert b"\r" not in joined, joined
    assert ctx["opened"].bridge.fed[-1] == b"\x15"
    assert ai["done"].wait(5)
    assert ai["calls"][0]["question"] == "hi"
    # Lead 已修 `line_split.LineShadow.feed` 的尾部丢弃（命中后不再提前 return）：
    # 同一个 chunk 里 Enter 之后的收尾标记 `ESC[201~` 现在会照常转发，
    # 远端 readline 的 bracketed-paste 状态才能正确收尾。
    assert b"\x1b[201~" in joined, (
        "LineShadow.feed 命中后仍丢弃了同一 chunk 的尾部字节"
    )


def test_normal_input_is_forwarded_verbatim(ai):
    """普通命令 ls\\r 必须一个字节都不变地转发，且不触发 AI。"""
    ctx = _make_ctx()
    events._handle_session_input(ctx, "l")
    events._handle_session_input(ctx, "s")
    events._handle_session_input(ctx, "\r")

    assert ctx["opened"].bridge.fed == [b"l", b"s", b"\r"]
    assert ctx["opened"].bridge.joined == b"ls\r"
    assert ai["calls"] == [], "普通输入不能触发 AI"
    assert ctx["ai"].active is False


def test_ai_prompt_input_goes_to_worker_queue_not_bridge(ai):
    """AI 正在等输入（如管理员密码）时，按键进队列、不回显明文、不漏给目标机。"""
    ai["need_input"] = 1
    ctx = _make_ctx()
    events._handle_session_input(ctx, "/ask-ai 重启一下 nginx\r")
    assert _wait_until(lambda: ctx["ai"].awaiting), "worker 没有进入 read_line"

    before = ctx["opened"].bridge.fed[:]
    events._handle_session_input(ctx, "admin\r")

    assert ai["input_event"].wait(5), "worker 线程没有拿到这一行"
    assert ai["lines"] == [("admin", False)]
    assert ctx["opened"].bridge.fed == before, "AI 等输入时按键漏给了目标机"
    assert "*****" in ctx["pump"].text, "密码必须以掩码回显"
    assert "admin" not in ctx["pump"].text, "密码明文不能出现在终端输出里"
    assert ai["done"].wait(5)


# ---------------------------------------------------------------------------
# 交付 2：连接后清屏
# ---------------------------------------------------------------------------
def test_clear_screen_after_connect_paints_context_and_keeps_bridge_clean():
    """清屏：先写 \\x1b[2J\\x1b[H，再重画上下文行；只补一个空行且不能是 clear。"""
    ctx = _make_ctx()
    opened = ctx["opened"]
    ready = {
        "sid": "sess-9",
        "hostName": "db-01",
        "address": "127.0.0.1:22",
        "accountUsername": "root",
    }
    events._clear_screen_after_connect(ctx, opened, ready, 100)

    text = ctx["pump"].text
    assert text.startswith(events.CLEAR_SCREEN), text[:40]
    assert "\x1b[2J\x1b[H" in text
    assert "已连接 db-01（127.0.0.1:22），账号 root，会话号 sess-9" in text
    assert "/ask-ai" in text
    assert opened.bridge.fed == [b"\n"], "清屏只能补一个空行让 bash 重画 PS1"
    assert b"clear" not in opened.bridge.joined, "不能执行真实命令清屏"


# ---------------------------------------------------------------------------
# 真链路：Socket.IO + 假目标机（不含任何真实 AI 调用）
# ---------------------------------------------------------------------------
def test_webterm_end_to_end_ask_ai_and_clear_screen(
    app, client, make_user, make_host, make_account, make_grant, target, transcript_dir, ai
):
    from flask_socketio.test_client import SocketIOTestClient

    username, password = "webterm-ai", "User1234"
    user_id = make_user(username, role_code="ops", password=password)
    host_id = make_host("ai-01", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id)
    token = token_of(client, username, password)

    sio = SocketIOTestClient(
        app, socketio_server, auth={"token": token}, flask_test_client=client
    )
    assert sio.is_connected(), f"Socket.IO 没连上：{sio.get_received()}"

    packets: list[dict] = []

    def drain_until(predicate, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            packets.extend(sio.get_received())
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def outputs_text() -> str:
        return "".join(
            (item.get("args") or [{}])[0].get("data", "")
            for item in packets
            if item.get("name") == "terminal:output"
        )

    try:
        assert drain_until(lambda: any(p["name"] == "terminal:ready" for p in packets))
        sio.emit(
            "terminal:open",
            {"hostId": host_id, "accountId": account_id, "cols": 100, "rows": 30},
        )
        assert drain_until(
            lambda: any(p["name"] == "terminal:opened" for p in packets)
        ), f"没有 terminal:opened：{[p['name'] for p in packets]}"

        # 交付 2：清屏与上下文行真的推给了浏览器
        assert drain_until(lambda: "\x1b[2J\x1b[H" in outputs_text()), outputs_text()[:400]
        text = outputs_text()
        assert "已连接 ai-01" in text
        assert "/ask-ai" in text
        assert not any("clear" in cmd for cmd in target.commands), target.commands
        with app.app_context():
            assert CommandLog.query.filter_by(user_id=user_id).count() == 0, (
                "清屏空行不能进命令库"
            )

        # 交付 1：/ask-ai 被拦截，目标机永远收不到这一行
        sio.emit("terminal:input", {"data": "/ask-ai 你好\r"})
        assert ai["done"].wait(10), "AI worker 没有跑起来"
        assert ai["calls"][0]["question"] == "你好"
        assert ai["thread_name"].startswith("webterm-ai-")
        assert "/ask-ai" not in " ".join(target.commands), target.commands
        assert drain_until(lambda: "AI 回答" in outputs_text()), outputs_text()[-400:]
        with app.app_context():
            assert CommandLog.query.filter_by(user_id=user_id).count() == 0, (
                "被拦截的 /ask-ai 不能进命令库"
            )
    finally:
        try:
            sio.emit("terminal:close")
        finally:
            sio.disconnect()
