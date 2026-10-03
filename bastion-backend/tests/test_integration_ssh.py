"""端到端集成验证：真实 SSH 协议栈，不假手单体。

这里**不 mock paramiko**：文件内起一个真的 SSH 服务端（paramiko ``Transport`` +
自绘 shell）当作「被管理的 Linux 主机」，然后：

1. ``session_service.open_session()`` 真连它 —— 验证需求②「后台调第三方库实现
   网页端连接 Linux 机器及 shell」，以及命令/输出确实落到 ``CommandLog``；
2. ``GatewayServer`` 真监听一个端口，测试自己当客户端 ``paramiko.SSHClient``
   连进去 —— 验证需求④「ssh 堡垒机IP → 用户名密码 → 审计 shell → 选主机执行命令
   → 每条命令和输出都被记录」；
3. 只读策略下被拒的命令**必须没有到达目标机** —— 这是「不能执行的命令」的
   硬证据（需求①），而不是只看接口回了个 deny。

目标机 shell 的应答是确定性的，所以断言可以写死。
"""

from __future__ import annotations

import re
import socket
import threading
import time

import paramiko
import pytest

from app import config as config_module
from app.gateway.server import BANNER_SHIELD, GatewayServer
from app.models import CommandLog, SessionRecord
from app.session_service import open_session, teardown_session
from tests.conftest import token_of

HOST_KEY = None
HOST_KEY_LOCK = threading.Lock()


def _host_key():
    """RSA 生成较慢，整个测试会话共用一把。"""
    global HOST_KEY
    with HOST_KEY_LOCK:
        if HOST_KEY is None:
            HOST_KEY = paramiko.RSAKey.generate(2048)
        return HOST_KEY


# ---------------------------------------------------------------------------
# 假目标机：一个确定性应答的 SSH shell
# ---------------------------------------------------------------------------
class _FakeShell(paramiko.ServerInterface):
    def __init__(self, username="root", password="s3cret", bare_lf=False):
        self.username = username
        self.password = password
        self.shell_ready = threading.Event()
        self.commands: list[str] = []
        self.sessions = 0
        # 真实 Linux 的 PTY 会把 \n 转成 \r\n；这里可以模拟「不做 ONLCR」的目标
        # （演示目标机、网络设备、部分容器 shell 就是这样），用于回归
        # 「裸 LF 让提示符一行比一行右移」的缺陷。
        self.nl = "\n" if bare_lf else "\r\n"

    def check_auth_password(self, username, password):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_ready.set()
        return True

    def check_channel_window_change_request(self, channel, width, height, pw, ph):
        return True


_RESPONSES = {
    "whoami": "audituser\r\n",
    "hostname": "fake-linux-01\r\n",
    "uname -a": "Linux fake-linux-01 6.1.0-fake #1 SMP x86_64 GNU/Linux\r\n",
    "ls -l /tmp": "total 0\r\n",
    "id": "uid=1000(audituser) gid=1000(audituser)\r\n",
    "cat /etc/shadow": "root:*:19000:0:99999:7:::\r\n",
}


def _fake_shell_loop(channel, server: _FakeShell):
    """极简行式 shell：回车执行，确定性命中 _RESPONSES。"""
    state = {"marker": ""}
    buf = bytearray()

    def prompt() -> str:
        return f"{state['marker']}$ "

    while True:
        try:
            data = channel.recv(4096)
        except Exception:  # noqa: BLE001
            return
        if not data:
            return
        for byte in data:
            if byte in (0x0A, 0x0D):
                line = buf.decode("utf-8", "replace")
                buf.clear()
                _fake_execute(channel, server, line, state, prompt)
            elif byte in (0x08, 0x7F):
                if buf:
                    buf.pop()
            elif byte == 0x03:  # Ctrl-C：清行缓冲，并像真实 shell 一样重画提示符
                buf.clear()
                channel.send(f"^C{server.nl}{prompt()}".encode())
            else:
                buf.append(byte)


def _fake_execute(channel, server, line, state, prompt):
    text = line.strip()
    nl = server.nl
    # 桥接层会注入 export PS1='<marker>...'，这里只认标记
    found = re.search(r"__BASTION_[0-9a-f]{16}__", line)
    if found and "PS1=" in line:
        state["marker"] = found.group(0)
        channel.send(prompt() + nl)
        return
    if not text:
        channel.send(prompt())
        return
    server.commands.append(text)
    if text in ("exit", "logout"):
        channel.send(prompt() + nl + "logout" + nl)
        time.sleep(0.05)
        channel.close()
        return
    body = _RESPONSES.get(text, f"-bash: {text}: command not found" + nl)
    # 真实终端会回显命令行，这里也回显，捕获到的输出才贴近线上形态
    channel.send(f"{text}{nl}{body}{prompt()}")


class FakeTargetServer:
    """监听 127.0.0.1 的假 Linux 目标机。"""

    def __init__(self, bare_lf=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.host, self.port = self.sock.getsockname()
        self.server_interface = _FakeShell(bare_lf=bare_lf)
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-ssh", daemon=True)
        self._thread.start()

    @property
    def commands(self) -> list[str]:
        return list(self.server_interface.commands)

    def _serve(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                client, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        transport = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(_host_key())
            transport.start_server(server=self.server_interface)
            channel = transport.accept(10)
            if channel is None:
                return
            self.server_interface.shell_ready.wait(10)
            _fake_shell_loop(channel, self.server_interface)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def target():
    server = FakeTargetServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def transcript_dir(tmp_path, monkeypatch):
    """录像落到临时目录，别污染真实 instance/。"""
    monkeypatch.setattr(config_module.Config, "TRANSCRIPT_DIR", tmp_path, raising=False)
    return tmp_path


def _wait(predicate, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _commands_of(app, session_id: int) -> list[CommandLog]:
    with app.app_context():
        return (
            CommandLog.query.filter_by(session_id=session_id)
            .order_by(CommandLog.seq.asc(), CommandLog.id.asc())
            .all()
        )


# ---------------------------------------------------------------------------
# 需求②：网页端真连 Linux + 命令与输出全部落库
# ---------------------------------------------------------------------------
def test_web_session_records_command_and_output(
    app, make_user, make_host, make_account, make_grant, target, transcript_dir
):
    user_id = make_user("webuser")
    host_id = make_host("web-01", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id)

    chunks: list[bytes] = []
    opened = open_session(
        app,
        user_id=user_id,
        host_id=host_id,
        account_id=account_id,
        source="web",
        send_output=chunks.append,
    )
    try:
        assert opened.segmented is True, "提示符标记注入失败，说明 shell 交互没走通"
        opened.bridge.feed_input(b"whoami\r")

        rows = _wait(lambda: _commands_of(app, opened.record_id) or None)
        assert rows, "命令没有被审计落库"
        row = rows[0]
        assert row.command == "whoami"
        assert row.action == "allow"
        assert "audituser" in (row.output or ""), f"输出没抓到：{row.output!r}"
        assert row.duration_ms >= 0
        assert "whoami" in target.commands, "命令没有真的发到目标机"

        # 网页端拿到的字节流里必须有远端输出（前端 xterm 靠这个渲染）
        assert _wait(lambda: b"audituser" in b"".join(chunks) or None)
    finally:
        teardown_session(opened, reason="测试结束")

    with app.app_context():
        record = SessionRecord.query.filter_by(sid=opened.sid).one()
        assert record.status in ("closed", "terminated")
        assert record.ended_at is not None
        assert record.command_count >= 1
        assert record.bytes_in > 0 and record.bytes_out > 0
        assert record.transcript_path
        assert record.transcript_path.endswith(f"{opened.sid}.log")

    transcript = (transcript_dir / f"{opened.sid}.log").read_text(encoding="utf-8")
    assert "session_start" in transcript
    assert "whoami" in transcript
    assert "audituser" in transcript
    assert "session_end" in transcript or "closed" in transcript


def test_denied_command_never_reaches_target(
    app, make_user, make_host, make_account, make_grant, target, transcript_dir
):
    """需求①：不能执行的命令 —— 策略拒绝，且命令根本没到目标机。"""
    from app.models import CommandPolicy

    with app.app_context():
        readonly_id = CommandPolicy.query.filter_by(name="只读审计策略").one().id

    user_id = make_user("denyuser")
    host_id = make_host("web-02", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id, policy_id=readonly_id)

    notices: list[str] = []
    chunks: list[bytes] = []
    opened = open_session(
        app,
        user_id=user_id,
        host_id=host_id,
        account_id=account_id,
        source="web",
        send_output=chunks.append,
        notify=notices.append,
    )
    try:
        opened.bridge.feed_input(b"cat /etc/shadow\r")
        rows = _wait(lambda: _commands_of(app, opened.record_id) or None)
        assert rows, "被拒绝的命令也必须留痕（否则审计是假的）"
        row = rows[0]
        assert row.action == "deny"
        assert row.command == "cat /etc/shadow"
        assert row.matched_rule_id is not None
        assert f"#{row.matched_rule_id}" in row.reason, (
            "拒绝原因里必须带命中规则编号，否则管理员按说明找规则极易删错"
        )
        # 拒绝提示只在终端输出流里出现一次：以前 notify 通道还会再推一条同义提示，
        # 用户会看到「[堡垒机] 命令被拒绝 —— …」和「[策略通知] 命令被拦截：…」两行。
        marker = "命令被拒绝".encode()
        assert _wait(lambda: marker in b"".join(chunks) or None), "用户没有被提示命令被拒绝"
        assert b"".join(chunks).count(marker) == 1, "同一次拒绝只应提示一次"
        assert notices == [], f"拒绝提示不该再走 notify 通道：{notices}"

        # 白名单内命令仍然能用
        opened.bridge.feed_input(b"whoami\r")
        assert _wait(lambda: len(_commands_of(app, opened.record_id)) >= 2 or None)
        rows = _commands_of(app, opened.record_id)
        assert [r.action for r in rows] == ["deny", "allow"]
    finally:
        teardown_session(opened, reason="测试结束")

    assert "cat /etc/shadow" not in target.commands, "被拒绝的命令竟然发到了目标机！"
    assert "whoami" in target.commands


# ---------------------------------------------------------------------------
# 需求④：SSH 网关端到端（真实客户端 → 网关 → 目标机 → 审计）
# ---------------------------------------------------------------------------
def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _read_until(channel, needle: str, timeout: float = 8.0) -> str:
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if channel.recv_ready():
            buf += channel.recv(65536).decode("utf-8", "replace")
            if needle in buf:
                return buf
        else:
            time.sleep(0.05)
    return buf


def test_gateway_end_to_end_menu_session_audit(
    app, make_user, make_host, make_account, make_grant, target, transcript_dir
):
    username, password = "gwuser", "User1234"
    user_id = make_user(username, password=password)
    host_id = make_host("gw-target-01", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id)

    port = _free_port()
    app.config["GATEWAY_ENABLED"] = True
    app.config["GATEWAY_HOST"] = "127.0.0.1"
    app.config["GATEWAY_PORT"] = port
    gateway = GatewayServer(app)
    assert gateway.start(), f"网关没起来：{gateway.last_error}"
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            "127.0.0.1",
            port=port,
            username=username,
            password=password,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
        )
        channel = client.invoke_shell()
        menu = _read_until(channel, "选择主机")
        assert "选择主机" in menu, f"没等到主机菜单：{menu[-500:]!r}"
        assert "gw-target-01" in menu, "菜单里没有授权主机"
        assert target.host in menu
        # 进站字符画（盾牌 + AutoOps 字标）必须排在菜单之前，且带颜色
        assert BANNER_SHIELD[1] in menu, f"SSH 进站没有打出字符画 logo：{menu[:300:]!r}"
        assert "\x1b[" in menu, "SSH 菜单没有上色"
        assert menu.index(BANNER_SHIELD[1]) < menu.index("你可访问的主机"), "字符画没有排在菜单之前"

        channel.send("1\n")
        opened = _read_until(channel, "会话号")
        assert "已连接 gw-target-01" in opened, f"没进会话：{opened[-500:]!r}"

        channel.send("hostname\n")
        assert _wait(lambda: "fake-linux-01" in _read_until(channel, "fake-linux-01", 6.0))

        # 会话与命令都必须落库，source=gateway
        def find_session():
            with app.app_context():
                return (
                    SessionRecord.query.filter_by(user_id=user_id, source="gateway")
                    .order_by(SessionRecord.id.desc())
                    .first()
                )

        record = _wait(find_session)
        assert record is not None, "网关会话没有落库"
        rows = _wait(lambda: _commands_of(app, record.id) or None)
        assert rows, "网关会话的命令没有审计"
        assert any(r.command == "hostname" and r.action == "allow" for r in rows)
        assert "hostname" in target.commands

        channel.send("exit\n")
        time.sleep(0.5)
        channel.send("q\n")
        back = _read_until(channel, "再见", 6.0)
        assert "再见" in back or "选择主机" in back
    finally:
        if client is not None:
            client.close()
        gateway.stop()

    # 网关登录本身也要有审计
    from app.models import AuditLog

    with app.app_context():
        actions = {row.action for row in AuditLog.query.filter_by(category="auth").all()}
    assert "gateway_login" in actions, f"网关登录没有审计：{actions}"


def test_gateway_rejects_bad_password(app, make_user):
    username = "gwbad"
    make_user(username, password="User1234")
    app.config["GATEWAY_ENABLED"] = True
    app.config["GATEWAY_HOST"] = "127.0.0.1"
    app.config["GATEWAY_PORT"] = _free_port()
    gateway = GatewayServer(app)
    assert gateway.start(), gateway.last_error
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        with pytest.raises(paramiko.AuthenticationException):
            client.connect(
                "127.0.0.1",
                port=gateway.port,
                username=username,
                password="wrong-password",
                allow_agent=False,
                look_for_keys=False,
                timeout=10,
            )
    finally:
        client.close()
        gateway.stop()

    from app.models import AuditLog, User

    with app.app_context():
        user = User.query.filter_by(username=username).one()
        assert user.failed_attempts >= 1, "失败次数没累计"
        failed = AuditLog.query.filter_by(result="failure", actor_username=username).count()
        assert failed >= 1, "网关认证失败没有审计"


# ---------------------------------------------------------------------------
# 以下 3 个用例是「真机端到端联调」暴露出的缺陷的防回归护栏。
# 它们过去的共同点：pytest 全绿但功能在生产 100% 不可用——
# 因为既有 232 个用例没有一个真的覆盖到这条链路。
# ---------------------------------------------------------------------------


def test_transcript_endpoint_replays_recorded_events(
    client,
    admin_headers,
    app,
    make_user,
    make_host,
    make_account,
    make_grant,
    target,
    transcript_dir,
):
    """P0 回归：录像回放接口必 500。

    历史缺陷：``TranscriptRecorder.iter_events`` 只返回事件列表，而
    ``app/api/sessions.py::session_transcript`` 按 ``events, next_offset`` 解包 →
    ``ValueError: too many values to unpack (expected 2)`` →
    ``GET /api/sessions/<id>/transcript`` 直接 500，前端「录像回放」整条不可用。
    """
    user_id = make_user("transcript-user")
    host_id = make_host("tr-01", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id)

    opened = open_session(
        app,
        user_id=user_id,
        host_id=host_id,
        account_id=account_id,
        source="web",
        send_output=lambda _chunk: None,
    )
    try:
        assert opened.segmented is True
        opened.bridge.feed_input(b"whoami\r")
        assert _wait(lambda: _commands_of(app, opened.record_id) or None), "命令没落库"
    finally:
        teardown_session(opened, reason="测试结束")

    url = f"/api/sessions/{opened.record_id}/transcript"
    resp = client.get(f"{url}?offset=0&limit=100", headers=admin_headers)
    assert resp.status_code == 200, f"录像接口挂了：HTTP {resp.status_code} {resp.get_data(as_text=True)[:300]}"
    body = resp.get_json()
    assert body["success"] is True, body
    data = body["data"]
    events = data["events"]
    assert events, "录像事件一条都读不出来"
    types = [item.get("t") for item in events]
    assert "session_start" in types, f"缺少 session_start：{types}"
    assert any(item.get("t") == "command" and item.get("command") == "whoami" for item in events), (
        f"命令事件没进录像：{events}"
    )
    assert data["nextOffset"] >= len(events), data
    assert "stats" in data and data["size"] > 0

    # 分页：limit=1 每次只回一条，翻页不丢事件（next_offset 指向未读行）
    first = client.get(f"{url}?offset=0&limit=1", headers=admin_headers).get_json()["data"]
    assert len(first["events"]) == 1
    assert first["nextOffset"] == 1, first
    second = client.get(f"{url}?offset=1&limit=1", headers=admin_headers).get_json()["data"]
    assert len(second["events"]) == 1
    assert second["events"][0]["t"] == events[1]["t"], "翻页后的事件与全量读取不一致"

    # commandOnly 过滤也要能用
    only = client.get(f"{url}?offset=0&limit=100&commandOnly=1", headers=admin_headers).get_json()["data"]
    allowed = {"command", "deny", "interactive_input", "session_start", "session_end"}
    assert all(item.get("t") in allowed for item in only["events"]), only["events"]


def test_webterm_socket_pushes_command_events(
    app,
    client,
    make_user,
    make_host,
    make_account,
    make_grant,
    target,
    transcript_dir,
):
    """P0 回归：网页终端的实时命令审计表恒空。

    历史缺陷：``app/webterm/events.py`` 定义了 ``on_command`` 却从没传给
    ``open_session(...)``，服务端因此永远不 emit ``terminal:command``。
    本用例走真实的 Socket.IO 层（不是直接调 open_session），所以能钉住这条链路。
    """
    from flask_socketio.test_client import SocketIOTestClient

    from app.extensions import socketio as socketio_server

    username, password = "termuser", "User1234"
    user_id = make_user(username, role_code="ops", password=password)
    host_id = make_host("term-01", address=target.host, port=target.port)
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

        sio.emit("terminal:open", {"hostId": host_id, "accountId": account_id, "cols": 100, "rows": 30})
        opening = drain({"terminal:opened", "terminal:error"})
        assert opening, "terminal:open 没有任何回执"
        assert opening[-1]["name"] == "terminal:opened", f"会话没打开：{opening[-1]}"

        sio.emit("terminal:input", {"data": "whoami\n"})
        commands = drain({"terminal:command"})
        assert commands, "terminal:command 没有推送（前端实时审计表会恒空）"
        payload = commands[0]["args"][0]
        assert payload["command"] == "whoami", payload
        assert payload["action"] == "allow", payload

        outputs = drain({"terminal:output"})
        assert any("audituser" in item["args"][0].get("data", "") for item in outputs), "终端输出没推给前端"
    finally:
        sio.emit("terminal:close")
        sio.disconnect()


def test_gateway_forced_password_change_reaches_menu(
    app, make_user, make_host, make_account, make_grant
):
    """P0 回归：must_change_password 的账号过去根本进不了网关。

    历史缺陷：``app/gateway/server.py`` 写 ``from ..security import validate_password_strength``，
    而该函数定义在 ``app/api/auth.py`` → 强制改密分支抛 ImportError →
    连接只看到 banner 就 ``OSError: Socket is closed``，主机菜单永远不出现。
    经 API 新建的账号必然带 must_change_password，等于**所有新用户都用不了网关**。
    """
    from app.security import verify_password

    username, old_password, new_password = "gwfresh", "OldPass123", "NewPass456"
    user_id = make_user(
        username, password=old_password, role_code="ops", must_change_password=True
    )
    host_id = make_host("gw-fresh-01", address="127.0.0.1", port=22)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id)

    app.config["GATEWAY_ENABLED"] = True
    app.config["GATEWAY_HOST"] = "127.0.0.1"
    app.config["GATEWAY_PORT"] = _free_port()
    gateway = GatewayServer(app)
    assert gateway.start(), gateway.last_error

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    channel = None
    buffer = ""
    try:
        ssh.connect(
            "127.0.0.1",
            port=gateway.port,
            username=username,
            password=old_password,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
        )
        channel = ssh.invoke_shell()
        buffer = _read_until(channel, "必须先设置新密码", timeout=15)
        assert "必须先设置新密码" in buffer, f"没有进入强制改密流程：{buffer!r}"

        channel.send(new_password + "\n")
        buffer += _read_until(channel, "再次输入新密码", timeout=15)
        channel.send(new_password + "\n")
        buffer += _read_until(channel, "gw-fresh-01", timeout=20)
        assert "密码已更新" in buffer, f"改密没有成功：{buffer!r}"
        assert "gw-fresh-01" in buffer, f"改密后没有出现主机菜单：{buffer!r}"
    finally:
        if channel is not None:
            channel.close()
        ssh.close()
        gateway.stop()

    with app.app_context():
        from app.models import User

        user = User.query.filter_by(username=username).one()
        assert user.must_change_password is False, "强制改密标记没清掉"
        assert verify_password(new_password, user.password_hash), "新密码没写进库"
        assert not verify_password(old_password, user.password_hash), "旧密码还能用"


# ---------------------------------------------------------------------------
# 会话控制指令：白名单策略下也必须能退出会话
# ---------------------------------------------------------------------------
def test_session_exit_is_allowed_under_readonly_policy(
    app, make_user, make_host, make_account, make_grant, target, transcript_dir
):
    """回归：只读（白名单）策略把 ``exit`` 也判成「白名单外一律拒绝」→ 用户被困在会话里。

    历史缺陷：网关横幅写着「输入 exit 返回主机菜单」，但只读策略 default_action=deny，
    ``exit`` 命中 priority 200 的兜底 deny 规则 → 命令被拒绝、目标机收不到 ``exit``、
    远端 shell 不结束，用户只能关掉整个 SSH 连接，且审计里留下一条无意义的 deny。
    修法：``app/policy.py::is_session_exit`` + ``bridge._handle_enter`` 在策略评估前放行
    会话控制指令（仍完整留痕，action=allow，reason 标注「不受命令策略限制」）。
    """
    from app.models import CommandPolicy

    with app.app_context():
        readonly_id = CommandPolicy.query.filter_by(name="只读审计策略").one().id

    user_id = make_user("exituser")
    host_id = make_host("exit-01", address=target.host, port=target.port)
    account_id = make_account(host_id, name="root", username="root", password="s3cret")
    make_grant(user_id, host_id, account_id=account_id, policy_id=readonly_id)

    opened = open_session(
        app,
        user_id=user_id,
        host_id=host_id,
        account_id=account_id,
        source="web",
    )
    try:
        opened.bridge.feed_input(b"exit\r")
        rows = _wait(lambda: _commands_of(app, opened.record_id) or None)
        assert rows, "exit 也必须留痕"
        row = rows[0]
        assert row.command == "exit"
        assert row.action == "allow", f"白名单策略又把 exit 拒了：{row.action} {row.reason!r}"
        assert "会话控制" in (row.reason or ""), f"reason 没标注会话控制指令：{row.reason!r}"
        assert "exit" in target.commands, "exit 没有被真的发到目标机"
        # 目标机收到 exit 后关闭 shell → 桥接层必须感知到会话结束（用户不被困住）
        assert _wait(lambda: opened.bridge.closed or None), "exit 之后会话没有关闭"
    finally:
        teardown_session(opened, reason="测试结束")
