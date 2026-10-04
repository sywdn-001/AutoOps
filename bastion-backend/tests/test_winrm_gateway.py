"""Windows（WinRM）通道测试：客户端、桥接层、资产接口与网页终端分流。

分三层钉死，全部离线跑（不依赖真机 192.168.0.75）：

* ``app/winrm/client.py`` —— 目标参数拼装与口令解密、字节解码、英文异常翻中文、
  ``-EncodedCommand``（UTF-16LE + base64）、超时与 Ctrl-C 中断（用假 WinRS Protocol）。
* ``app/winrm/bridge.py`` —— 逐行命令、尾部回显剥离（退出码 / cwd）、CLIXML 解码、
  策略拒绝、行编辑与历史、``exit`` 收尾、审计事件与 ``stats()``。
* ``app/api/hosts.py`` / ``app/api/sessions.py`` / ``app/session_service.py`` ——
  资产接口的协议 / 默认端口 / 认证方式校验、WinRM「测试连接」、
  网页终端列表与准入校验、按协议选桥。

真机联调证据（win-75 / 192.168.0.75:5985）见 ``README.md`` 与 ``docs/``。
"""

from __future__ import annotations

import base64
import re
import sys
import threading
import time
import types

import pytest

import app.winrm.client as client_module

from app.extensions import db
from app.policy import BUILTIN_POLICIES, FrozenPolicy, FrozenRule
from app.terminal import BridgeConfig
from app.winrm.bridge import (
    WinrmBridge,
    build_script,
    decode_clixml,
    ps_literal,
    split_trailer,
)
from app.winrm.client import (
    POWERSHELL,
    PROBE_SCRIPT,
    WINRM_DEFAULT_PORT,
    WINRM_DEFAULT_SSL_PORT,
    ScriptResult,
    WinrmConnection,
    WinrmError,
    WinrmTarget,
    build_target,
    decode_bytes,
)

MARKER_RE = re.compile(r"@@BASTION-[0-9a-f]{8}@@")


# --------------------------------------------------------------------------- 工具
def frozen(template_name: str) -> FrozenPolicy:
    """从内置策略模板造一份冻结策略（与 test_policy.py 同样的做法）。"""
    for template in BUILTIN_POLICIES:
        if template["name"] == template_name:
            return FrozenPolicy(
                id=1,
                name=template["name"],
                default_action=template["default_action"],
                rules=[
                    FrozenRule(
                        id=index,
                        priority=rule["priority"],
                        action=rule["action"],
                        match_type=rule["match_type"],
                        pattern=rule["pattern"],
                        risk_level=rule["risk_level"],
                        enabled=True,
                        description=rule.get("description", ""),
                    )
                    for index, rule in enumerate(template["rules"], start=1)
                ],
            )
    raise AssertionError(f"内置策略不存在：{template_name}")


class FakeProtocol:
    """假 WinRS Protocol：只记录调用，按 behavior 控制 ``get_command_output``。

    ``block``：一直等到 ``cleanup_command`` 被调用（模拟 WinRS 的 Ctrl-C 中断）。
    ``slow``：睡 ``sleep`` 秒（模拟命令跑得比超时久）。
    ``raise``：抛异常（模拟通道坏了）。
    """

    def __init__(self, *, behavior: str = "ok", stdout: bytes = b"ok\r\n", stderr: bytes = b"", code: int = 0, sleep: float = 0.6):
        self.behavior = behavior
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        self.sleep = sleep
        self.calls: list[tuple] = []
        self.unblock = threading.Event()
        self.shell_seq = 0

    def open_shell(self, codepage=None):
        self.shell_seq += 1
        self.calls.append(("open_shell", codepage))
        return f"shell-{self.shell_seq}"

    def run_command(self, shell_id, command, args):
        self.calls.append(("run_command", shell_id, command, list(args)))
        return f"cmd-{len(self.calls)}"

    def get_command_output(self, shell_id, command_id):
        if self.behavior == "block":
            self.unblock.wait(5.0)
        elif self.behavior == "slow":
            time.sleep(self.sleep)
        elif self.behavior == "raise":
            raise RuntimeError("Connection refused by the target host")
        return (self.stdout, self.stderr, self.code)

    def cleanup_command(self, shell_id, command_id):
        self.calls.append(("cleanup_command", command_id))
        self.unblock.set()

    def close_shell(self, shell_id):
        self.calls.append(("close_shell", shell_id))


def make_connection(protocol: FakeProtocol | None = None, **target_kwargs) -> WinrmConnection:
    proto = protocol or FakeProtocol()
    target = WinrmTarget(host="10.0.0.9", port=WINRM_DEFAULT_PORT, username="Administrator", password="pw", **target_kwargs)
    return WinrmConnection(protocol=proto, target=target, shell_id="shell-1")


def trailer(script: str, code: int, cwd: str = r"C:\Users\Administrator") -> str:
    """按桥自己发出去的标记拼出尾部落款（保证和被测代码用的是同一个标记）。"""
    marker = MARKER_RE.search(script).group(0)
    return f"{marker}{code}|{cwd}{marker}"


class Collector:
    """把桥的回调收集起来，供断言用（输出按 CRLF 归一成 LF 方便比对）。"""

    def __init__(self) -> None:
        self.output: list[str] = []
        self.events: list[dict] = []
        self.closed: list[str] = []
        self.done = threading.Event()

    def on_output(self, data: bytes) -> None:
        self.output.append(data.decode("utf-8", "replace"))

    def on_command(self, event: dict) -> None:
        self.events.append(event)
        if not event.get("command", "").startswith("@"):
            self.done.set()

    def on_close(self, reason: str) -> None:
        self.closed.append(reason)

    @property
    def text(self) -> str:
        return "".join(self.output).replace("\r\n", "\n")

    def wait(self, count: int = 1, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.events) >= count:
                return True
            time.sleep(0.02)
        return False


def make_bridge(connection: WinrmConnection, policy=None, *, command_timeout: int = 30, **kwargs) -> tuple[WinrmBridge, Collector]:
    collector = Collector()
    config = BridgeConfig(
        sid="test-winrm-sid",
        policy=policy,
        host_label="win-75-winrm",
        channel_kind="web",
        command_timeout=command_timeout,
        on_output=collector.on_output,
        on_command=collector.on_command,
        on_close=collector.on_close,
        **kwargs,
    )
    return WinrmBridge(connection, config, session=None), collector


# =========================================================================== 客户端
def test_build_target_decrypts_password_and_uses_default_port(app, make_host, make_account):
    host_id = make_host(name="win-09", address="10.0.0.9", port=WINRM_DEFAULT_PORT, protocol="winrm", os_type="windows")
    account_id = make_account(host_id, name="Administrator", username="Administrator", password="P@ssw0rd-中文")
    with app.app_context():
        from app.models import Host, HostAccount

        target = build_target(db.session.get(Host, host_id), db.session.get(HostAccount, account_id))
    assert target.host == "10.0.0.9"
    assert target.port == WINRM_DEFAULT_PORT
    assert target.username == "Administrator"
    assert target.password == "P@ssw0rd-中文", "口令必须就地解密"
    assert target.transport == "ntlm"
    assert target.use_ssl is False
    assert target.endpoint == "http://10.0.0.9:5985/wsman"
    assert target.address == "10.0.0.9:5985"
    assert target.host_label == "win-09"


def test_build_target_marks_5986_as_https(app, make_host, make_account):
    host_id = make_host(name="win-09s", address="10.0.0.9", port=WINRM_DEFAULT_SSL_PORT, protocol="winrm", os_type="windows")
    account_id = make_account(host_id, username="Administrator", password="pw")
    with app.app_context():
        from app.models import Host, HostAccount

        target = build_target(db.session.get(Host, host_id), db.session.get(HostAccount, account_id))
    assert target.use_ssl is True
    assert target.endpoint == "https://10.0.0.9:5986/wsman"


def test_build_target_rejects_key_accounts(app, make_host, make_account):
    host_id = make_host(name="win-09k", address="10.0.0.9", protocol="winrm", os_type="windows")
    account_id = make_account(host_id, username="Administrator", password="", auth_type="key")
    with app.app_context():
        from app.models import Host, HostAccount

        with pytest.raises(WinrmError) as excinfo:
            build_target(db.session.get(Host, host_id), db.session.get(HostAccount, account_id))
    assert "不支持私钥登录" in str(excinfo.value)


def test_build_target_rejects_accounts_without_password(app, make_host, make_account):
    host_id = make_host(name="win-09e", address="10.0.0.9", protocol="winrm", os_type="windows")
    account_id = make_account(host_id, username="Administrator", password="")
    with app.app_context():
        from app.models import Host, HostAccount

        with pytest.raises(WinrmError) as excinfo:
            build_target(db.session.get(Host, host_id), db.session.get(HostAccount, account_id))
    assert "没有可用凭据" in str(excinfo.value)


def test_build_target_falls_back_on_unknown_transport(app, make_host, make_account):
    host_id = make_host(
        name="win-09t",
        address="10.0.0.9",
        protocol="winrm",
        os_type="windows",
        winrm_transport="kerberos",
    )
    account_id = make_account(host_id, username="Administrator", password="pw")
    with app.app_context():
        from app.models import Host, HostAccount

        target = build_target(db.session.get(Host, host_id), db.session.get(HostAccount, account_id))
    assert target.transport == "ntlm", "非法/未支持的认证方式一律回落到默认 ntlm，绝不把 kerberos 传给 pywinrm"


def test_decode_bytes_handles_utf8_gbk_and_garbage():
    assert decode_bytes("中文输出".encode("utf-8")) == "中文输出"
    assert decode_bytes("中文输出".encode("gbk")) == "中文输出", "中文 Windows 默认代码页是 GBK"
    assert decode_bytes(b"\xff\xfe\x41") != "", "非法字节要容错解码，不能抛异常"
    assert decode_bytes(None) == ""
    assert decode_bytes(b"") == ""


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Read timed out. (read timeout=30)", "超时"),
        ("[WinError 10061] No connection could be made because the target machine actively refused it", "拒绝了连接"),
        ("the specified 401 Unauthorized response", "认证失败"),
        ("403 Forbidden", "没有远程管理权限"),
        ("SSL certificate verify failed", "证书校验失败"),
        ("something else entirely", "连接目标主机 10.0.0.9:5985 失败"),
    ],
)
def test_friendly_translates_pywinrm_errors(message, expected):
    from app.winrm.client import _friendly

    text = _friendly(RuntimeError(message), WinrmTarget(host="10.0.0.9", port=5985))
    assert expected in text


def test_run_script_sends_encoded_command_as_utf16le():
    protocol = FakeProtocol(stdout=b"hello\r\n")
    connection = make_connection(protocol)
    script = "Write-Output '中文命令' \r\n$LASTEXITCODE"
    result = client_module.run_script(connection, script, timeout=5.0)

    call = next(item for item in protocol.calls if item[0] == "run_command")
    assert call[2] == POWERSHELL
    args = call[3]
    assert args[:4] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    decoded = base64.b64decode(args[4]).decode("utf-16-le")
    assert decoded == script, "命令必须以 UTF-16LE 编码后 base64 传入，中文才不会乱码"
    assert result.stdout == "hello\r\n"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.cancelled is False
    assert any(item[0] == "cleanup_command" for item in protocol.calls), "正常结束也要回收命令"


def test_run_script_honours_timeout_and_requests_interrupt():
    protocol = FakeProtocol(behavior="block")
    connection = make_connection(protocol)

    result = client_module.run_script(connection, "Start-Sleep -Seconds 600", timeout=0.2)

    assert result.timed_out is True
    assert result.cancelled is False
    assert any(item[0] == "cleanup_command" for item in protocol.calls), "超时必须请求中断，不能干等"


def test_run_script_honours_cancel_event():
    protocol = FakeProtocol(behavior="slow", sleep=0.5)
    connection = make_connection(protocol)
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()

    result = client_module.run_script(connection, "Start-Sleep -Seconds 30", timeout=30.0, cancel=cancel)

    assert result.cancelled is True
    assert any(item[0] == "cleanup_command" for item in protocol.calls)


def test_run_script_raises_chinese_error_when_channel_breaks():
    protocol = FakeProtocol(behavior="raise")
    connection = make_connection(protocol)

    with pytest.raises(WinrmError) as excinfo:
        client_module.run_script(connection, "whoami", timeout=5.0)
    assert "拒绝了连接" in str(excinfo.value)
    assert connection.shell_id == "", "通道坏了要丢掉 shell_id，下一条命令重开"


def test_connect_builds_protocol_with_timeouts(monkeypatch):
    captured: dict = {}

    class FakeProtocolClass:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def open_shell(self, codepage=None):
            captured["codepage"] = codepage
            return "shell-9"

    monkeypatch.setitem(sys.modules, "winrm", types.SimpleNamespace(Protocol=FakeProtocolClass))
    from app.winrm.client import connect

    connection = connect(
        WinrmTarget(
            host="10.0.0.9",
            port=5985,
            username="Administrator",
            password="pw",
            transport="ntlm",
            command_timeout=30,
        )
    )
    assert connection.shell_id == "shell-9"
    assert captured["endpoint"] == "http://10.0.0.9:5985/wsman"
    assert captured["transport"] == "ntlm"
    assert captured["username"] == "Administrator"
    assert captured["server_cert_validation"] == "ignore"
    assert captured["operation_timeout_sec"] == 30, "WinRS 的 OperationTimeout 必须小于 pywinrm 默认 60s，否则 Receive 长时间阻塞"
    assert captured["read_timeout_sec"] == 45
    assert captured["codepage"] == 65001


def test_connect_wraps_transport_error_in_chinese(monkeypatch):
    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("Connection refused by the target host")

    monkeypatch.setitem(sys.modules, "winrm", types.SimpleNamespace(Protocol=Boom))
    from app.winrm.client import connect

    with pytest.raises(WinrmError) as excinfo:
        connect(WinrmTarget(host="10.0.0.9", port=5985, username="Administrator", password="pw"))
    assert "拒绝了连接" in str(excinfo.value)


def test_test_connection_returns_probe_text(monkeypatch):
    monkeypatch.setattr(client_module, "build_target", lambda *args, **kwargs: WinrmTarget(host="10.0.0.9", port=5985))
    monkeypatch.setattr(client_module, "connect", lambda target: make_connection())
    monkeypatch.setattr(
        client_module,
        "run_script",
        lambda connection, script, timeout=None, cancel=None: ScriptResult(
            stdout="user=win-930nkgjcoed\\administrator\r\nhost=WIN-930NKGJCOED",
            exit_code=0,
        ),
    )
    text = client_module.test_connection(object(), object())
    assert "host=WIN-930NKGJCOED" in text


def test_test_connection_reports_timeout_and_empty_output(monkeypatch):
    monkeypatch.setattr(client_module, "build_target", lambda *args, **kwargs: WinrmTarget(host="10.0.0.9", port=5985))
    monkeypatch.setattr(client_module, "connect", lambda target: make_connection())
    monkeypatch.setattr(
        client_module,
        "run_script",
        lambda connection, script, timeout=None, cancel=None: ScriptResult(timed_out=True),
    )
    with pytest.raises(WinrmError) as excinfo:
        client_module.test_connection(object(), object(), timeout=15)
    assert "没有在 15 秒内响应" in str(excinfo.value)

    monkeypatch.setattr(
        client_module,
        "run_script",
        lambda connection, script, timeout=None, cancel=None: ScriptResult(stdout="", stderr="", exit_code=1),
    )
    with pytest.raises(WinrmError) as excinfo:
        client_module.test_connection(object(), object())
    assert "没有返回任何内容" in str(excinfo.value)


# =========================================================================== 桥接层
def test_ps_literal_escapes_single_quotes():
    assert ps_literal(r"C:\Program Files") == r"'C:\Program Files'"
    assert ps_literal("it's") == "'it''s'"


def test_build_script_sets_cwd_and_echoes_code_and_cwd():
    script = build_script("whoami", r"C:\Windows", "@@BASTION-deadbeef@@")
    lines = script.splitlines()
    assert lines[0] == "$ErrorActionPreference = 'Continue'"
    assert any(line.startswith("Set-Location -LiteralPath 'C:\\Windows'") for line in lines)
    assert "whoami" in lines
    tail = lines[-1]
    assert "@@BASTION-deadbeef@@" in tail
    assert "(Get-Location).Path" in tail and "$__bastion_code" in tail

    empty = build_script("", "", "@@BASTION-deadbeef@@")
    assert "Set-Location" not in empty, "cwd 未知时不能设目录"
    assert "whoami" not in empty


def test_split_trailer_takes_the_first_marker_not_the_last():
    """回归：用 rfind 会先命中结尾标记，把整段落款留在正文里（曾导致退出码恒 -1、cwd 恒空）。"""
    marker = "@@BASTION-deadbeef@@"
    text = f"hello\r\nworld\r\n{marker}0|C:\\Windows{marker}"
    body, code, cwd = split_trailer(text, marker)
    assert body == "hello\r\nworld\r\n"
    assert code == 0
    assert cwd == r"C:\Windows"
    assert marker not in body
    assert marker not in cwd


def test_split_trailer_handles_missing_or_broken_trailer():
    marker = "@@BASTION-deadbeef@@"
    assert split_trailer("plain output", marker) == ("plain output", -1, "")
    body, code, cwd = split_trailer(f"out{marker}not-a-number|{marker}", marker)
    assert body == "out" and code == -1 and cwd == ""
    body, code, cwd = split_trailer(f"out{marker}3|C:\\Windows", marker)
    assert body == "out" and code == -1, "只有半个落款时不敢猜，退出码回 -1"


def test_decode_clixml_restores_readable_error():
    raw = (
        "#< CLIXML\r\n<Objs Version=\"1.1.0.1\" xmlns=\"http://schemas.microsoft.com/powershell/2004/04\">"
        "<S S=\"Error\">Get-Item : 找不到路径“C:\\nope”。_x000D__x000A_</S>"
        "<S S=\"Error\">+ CategoryInfo : ObjectNotFound_x000D__x000A_</S></Objs>"
    )
    text = decode_clixml(raw)
    assert "找不到路径" in text
    assert "ObjectNotFound" in text
    assert "_x000D_" not in text and "_x000A_" not in text
    assert "CLIXML" not in text and "<S S=" not in text
    # 非 CLIXML 原样返回；空错误流返回空串
    assert decode_clixml("普通错误") == "普通错误"
    assert decode_clixml("") == ""


def test_bridge_runs_a_command_and_strips_the_trailer(monkeypatch):
    """端到端（假 WinRS）：输入一行 → 目标机输出 + 落款 → 审计事件里是干净正文。"""
    import app.winrm.bridge as bridge_module

    protocol = FakeProtocol(stdout=b"", code=0)

    def fake_run(connection, script, *, timeout=None, cancel=None):
        return ScriptResult(stdout=f"win-930nkgjcoed\\administrator\r\n{trailer(script, 0, r'C:\Users\Administrator')}")

    monkeypatch.setattr(bridge_module, "run_script", fake_run)
    bridge, collector = make_bridge(make_connection(protocol), policy=None)
    bridge.start(arm_timeout=10.0)
    try:
        bridge.feed_input("whoami\r".encode())
        assert collector.wait(1), f"没等到命令事件：{collector.events}"
    finally:
        bridge.stop("测试结束")

    event = collector.events[0]
    assert event["command"] == "whoami"
    assert "win-930nkgjcoed\\administrator" in event["output"]
    assert "@@BASTION" not in event["output"], "落款不能漏进审计输出"
    assert event["action"] == "allow"
    assert event["channel"] == "web"
    assert event["seq"] == 1
    assert "@@BASTION" not in collector.text
    assert "PS C:\\Users\\Administrator>" in collector.text, "提示符要跟上目标机的当前目录"
    assert "[退出码" not in collector.text


def test_bridge_runs_cd_then_carries_the_directory_forward(monkeypatch):
    """每条命令一个新进程，所以 ``cd`` 的效果必须由桥自己带到下一条命令。"""
    import app.winrm.bridge as bridge_module

    seen: list[str] = []

    def fake_run(connection, script, *, timeout=None, cancel=None):
        seen.append(script)
        if "cd C:\\Windows" in script:
            return ScriptResult(stdout=trailer(script, 0, r"C:\Windows"))
        return ScriptResult(stdout=trailer(script, 0, r"C:\Users\Administrator"))

    monkeypatch.setattr(bridge_module, "run_script", fake_run)
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.start(arm_timeout=10.0)
    try:
        assert bridge.cwd == r"C:\Users\Administrator"
        bridge.feed_input("cd C:\\Windows\r".encode())
        assert collector.wait(1)
        assert bridge.cwd == r"C:\Windows"
        bridge.feed_input("Get-Location\r".encode())
        assert collector.wait(2)
    finally:
        bridge.stop("测试结束")
    assert "Set-Location -LiteralPath 'C:\\Windows'" in seen[-1], "下一条命令要先把 cd 后的目录设回去"


def test_bridge_reports_nonzero_exit_code(monkeypatch):
    import app.winrm.bridge as bridge_module

    monkeypatch.setattr(
        bridge_module,
        "run_script",
        lambda connection, script, *, timeout=None, cancel=None: ScriptResult(
            stdout=f"错误\r\n{trailer(script, 1)}"
        ),
    )
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.start(arm_timeout=10.0)
    try:
        bridge.feed_input("cmd /c exit 1\r".encode())
        assert collector.wait(1)
    finally:
        bridge.stop("测试结束")
    assert "[退出码 1]" in collector.text
    assert collector.events[0]["action"] == "allow"


def test_bridge_decodes_clixml_stderr(monkeypatch):
    import app.winrm.bridge as bridge_module

    clixml = (
        '#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">Access is denied_x000D__x000A_</S></Objs>'
    )
    monkeypatch.setattr(
        bridge_module,
        "run_script",
        lambda connection, script, *, timeout=None, cancel=None: ScriptResult(
            stdout=trailer(script, 1), stderr=clixml
        ),
    )
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.start(arm_timeout=10.0)
    try:
        bridge.feed_input("Stop-Service Foo\r".encode())
        assert collector.wait(1)
    finally:
        bridge.stop("测试结束")
    assert "Access is denied" in collector.text
    assert "CLIXML" not in collector.text
    assert "Access is denied" in collector.events[0]["output"]


def test_bridge_denies_command_by_policy_without_touching_the_target(monkeypatch):
    import app.winrm.bridge as bridge_module

    calls: list[str] = []

    def fake_run(connection, script, *, timeout=None, cancel=None):
        calls.append(script)
        return ScriptResult(stdout=trailer(script, 0))

    monkeypatch.setattr(bridge_module, "run_script", fake_run)
    bridge, collector = make_bridge(make_connection(), policy=frozen("默认策略·高危命令拦截"))
    bridge.start(arm_timeout=10.0)
    try:
        before = len(calls)
        bridge.feed_input("rm -rf /\r".encode())
        assert collector.wait(1)
        time.sleep(0.1)
        after = len(calls)
    finally:
        bridge.stop("测试结束")

    event = collector.events[0]
    assert event["command"] == "rm -rf /"
    assert event["action"] == "deny"
    assert event["reason"]
    assert event["output"] == ""
    assert after == before, "被策略拒绝的命令绝不能发到目标机"
    assert "命令被拒绝" in collector.text
    assert event["seq"] == 1
    assert bridge.seq == 0, "被拒绝的命令不占执行序号（stats().commands 只数真正跑过的命令）"


def test_bridge_records_exit_as_session_control(monkeypatch):
    import app.winrm.bridge as bridge_module

    monkeypatch.setattr(
        bridge_module,
        "run_script",
        lambda connection, script, *, timeout=None, cancel=None: ScriptResult(stdout=trailer(script, 0)),
    )
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.start(arm_timeout=10.0)
    bridge.feed_input(b"exit\r")
    assert collector.wait(1)

    event = collector.events[0]
    assert event["command"] == "exit"
    assert event["action"] == "allow"
    assert "会话控制指令" in event["reason"]
    assert collector.closed and "退出" in collector.closed[0]
    assert bridge.closed is True
    assert bridge.stats()["commands"] == 1


def test_bridge_clears_screen_locally_without_touching_the_target(monkeypatch):
    """`cls` / `clear` 就地清屏：WinRM 会话没有真实控制台，不能发给目标机。"""
    import app.winrm.bridge as bridge_module

    sent: list[str] = []

    def fake_run_script(connection, script, *, timeout=None, cancel=None):
        sent.append(script)
        return ScriptResult(stdout=trailer(script, 0))

    monkeypatch.setattr(bridge_module, "run_script", fake_run_script)
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.start(arm_timeout=10.0)
    before = list(sent)

    bridge.feed_input(b"cls\r")
    assert collector.wait(1)

    assert sent == before, "清屏命令不能发到 Windows 目标机"
    event = collector.events[0]
    assert event["command"] == "cls"
    assert event["action"] == "allow"
    assert "清屏" in event["reason"]
    assert event["output"] == ""
    assert "\x1b[2J\x1b[H" in collector.text, "要往用户终端写清屏序列"
    tail = collector.text[collector.text.rindex("\x1b[2J\x1b[H") :]
    assert "PS C:\\Users\\Administrator>" in tail, "清屏后必须重画提示符，用户才能接着敲"
    assert bridge.stats()["commands"] == 1
    assert collector.closed == [], "清屏不是退出会话"
    bridge.stop("测试结束")


def test_bridge_still_denies_cls_when_policy_forbids_it(monkeypatch):
    """本地清屏也必须过命令策略：白名单策略下 `cls` 照样被拒，且不清屏。"""
    import app.winrm.bridge as bridge_module

    sent: list[str] = []

    def fake_run_script(connection, script, *, timeout=None, cancel=None):
        sent.append(script)
        return ScriptResult(stdout=trailer(script, 0))

    monkeypatch.setattr(bridge_module, "run_script", fake_run_script)
    bridge, collector = make_bridge(make_connection(), policy=frozen("只读审计策略"))
    bridge.start(arm_timeout=10.0)
    before = len(sent)

    bridge.feed_input(b"cls\r")
    assert collector.wait(1)

    event = collector.events[0]
    assert event["action"] == "deny"
    assert len(sent) == before
    assert "\x1b[2J\x1b[H" not in collector.text
    assert bridge.seq == 0, "被拒绝的命令不占执行序号"
    bridge.stop("测试结束")


def test_bridge_times_out_and_notices(monkeypatch):
    import app.winrm.bridge as bridge_module

    monkeypatch.setattr(
        bridge_module,
        "run_script",
        lambda connection, script, *, timeout=None, cancel=None: ScriptResult(
            timed_out=True, exit_code=1, stderr="命令未在超时前结束"
        ),
    )
    bridge, collector = make_bridge(make_connection(), policy=None, command_timeout=7)
    bridge.start(arm_timeout=10.0)
    try:
        bridge.feed_input("Start-Sleep -Seconds 600\r".encode())
        assert collector.wait(1)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and "超时" not in collector.text:
            time.sleep(0.05)
    finally:
        bridge.stop("测试结束")
    event = collector.events[0]
    assert event["action"] == "timeout"
    assert "超过 7 秒" in event["reason"]
    assert "超时" in collector.text


def test_bridge_keeps_session_alive_when_one_command_fails(monkeypatch):
    """单条命令的连接错误只影响这条命令，会话本身不能被判定结束。"""
    import app.winrm.bridge as bridge_module

    def fake_run(connection, script, *, timeout=None, cancel=None):
        if "whoami" in script:
            raise WinrmError("目标主机 10.0.0.9:5985 拒绝了连接")
        return ScriptResult(stdout=trailer(script, 0))

    monkeypatch.setattr(bridge_module, "run_script", fake_run)
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.start(arm_timeout=10.0)
    try:
        bridge.feed_input("whoami\r".encode())
        assert collector.wait(1)
    finally:
        bridge.stop("测试结束")
    event = collector.events[0]
    assert event["action"] == "error"
    assert "拒绝了连接" in event["reason"]
    assert "拒绝了连接" in collector.text
    assert collector.closed == [], "单条命令失败不能触发 on_close（会话结束回调）"


def test_bridge_line_editing_and_history():
    bridge, _ = make_bridge(make_connection(), policy=None)
    bridge.feed_input("who".encode())
    bridge.feed_input(b"\x7f")  # backspace
    assert bytes(bridge._buf) == b"wh"
    bridge.feed_input("at is".encode())
    bridge.feed_input(b"\x1b[D\x1b[D\x1b[D")  # 左移 3 格
    assert bridge._cursor == len(bridge._buf) - 3
    bridge.feed_input(b"\x15")  # Ctrl-U 清行
    assert bytes(bridge._buf) == b""

    bridge.history = ["disable-firewall", "ipconfig"]
    bridge.feed_input(b"\x1b[A")
    assert bytes(bridge._buf) == b"ipconfig", "上键取最近一条历史"
    bridge.feed_input(b"\x1b[A")
    assert bytes(bridge._buf) == b"disable-firewall"
    bridge.feed_input(b"\x1b[B")
    assert bytes(bridge._buf) == b"ipconfig"
    bridge.feed_input(b"\x1b[B")
    assert bytes(bridge._buf) == b"", "再往下回到空草稿"

    bridge.feed_input("你好，堡垒机".encode())
    assert bytes(bridge._buf).decode("utf-8") == "你好，堡垒机", "中文输入不能被字节编辑弄坏"
    bridge.feed_input(b"\x17")  # Ctrl-W 删一个词
    assert bytes(bridge._buf) == b""


def test_bridge_ctrl_c_interrupts_pending_command(monkeypatch):
    import app.winrm.bridge as bridge_module

    monkeypatch.setattr(
        bridge_module,
        "run_script",
        lambda connection, script, *, timeout=None, cancel=None: ScriptResult(stdout=trailer(script, 0)),
    )
    bridge, _ = make_bridge(make_connection(), policy=None)
    bridge._pending = {"seq": 1, "started_at": time.monotonic(), "command": "ping"}
    bridge.feed_input(b"\x03")
    assert bridge._interrupt.is_set()
    assert "Ctrl-C" in bridge._pending_notice

    bridge._pending = None
    bridge._interrupt.clear()
    bridge.feed_input(b"\x03")
    assert bytes(bridge._buf) == b"" and bridge.at_prompt is True


def test_bridge_prompt_prints_pending_notice_in_red():
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge._pending_notice = "命令正在执行，暂不支持交互输入"
    bridge._prompt()
    assert "命令正在执行" in collector.text
    assert "\x1b[31m" in collector.text or "\x1b[91m" in collector.text


def test_bridge_stats_and_resize():
    bridge, collector = make_bridge(make_connection(), policy=None)
    bridge.resize(200, 50)
    assert bridge._cols == 200 and bridge._rows == 50, "resize 只用于审计还原"
    assert bridge.stats() == {
        "sid": "test-winrm-sid",
        "bytes_in": 0,
        "bytes_out": 0,
        "commands": 0,
        "segmented": True,
        "duration": 0,
    }


def test_bridge_banner_declares_the_transport(tmp_path):
    from app.terminal import TranscriptRecorder

    recorder = TranscriptRecorder("sid-banner", path=str(tmp_path / "t.log"))
    bridge, collector = make_bridge(make_connection(host_label="win-09"), policy=None, recorder=recorder)
    bridge._banner()
    assert "已连接到 Windows 目标机" in collector.text
    assert "账号：Administrator" in collector.text
    assert "WinRM NTLM" in collector.text
    assert "输入 exit 结束会话" in collector.text
    assert collector.text.count("\n") >= 4
    recorder.close("测试收尾")


# =========================================================================== 会话分发
def test_open_session_dispatches_winrm_host_to_winrm_bridge(app, monkeypatch, make_host, make_account, make_grant, make_user):
    import app.session_service as service

    user_id = make_user("win-ops")
    host_id = make_host(name="win-09", address="10.0.0.9", port=5985, protocol="winrm", os_type="windows")
    account_id = make_account(host_id, name="Administrator", username="Administrator", password="pw")
    make_grant(user_id, host_id, account_id)

    monkeypatch.setattr(
        service,
        "build_winrm_target",
        lambda *args, **kwargs: WinrmTarget(host="10.0.0.9", port=5985, username="Administrator", password="pw", host_label="win-09"),
    )
    monkeypatch.setattr(service, "connect_winrm", lambda target: make_connection())
    started: list[str] = []
    monkeypatch.setattr(service.WinrmBridge, "start", lambda self, arm_timeout=15.0: started.append(self.cfg.sid) or True)

    opened = service.open_session(app, user_id=user_id, host_id=host_id, account_id=account_id, source="web")
    try:
        assert isinstance(opened.bridge, WinrmBridge)
        assert opened.meta["protocol"] == "winrm"
        assert started == [opened.sid]
        with app.app_context():
            from app.models import SessionRecord

            record = SessionRecord.query.filter_by(sid=opened.sid).one()
            assert record.protocol == "winrm"
            assert record.source == "web"
            assert record.host_name == "win-09"
    finally:
        service.teardown_session(opened, "测试收尾")


def test_open_session_rejects_rdp_hosts(app, monkeypatch, make_host, make_account, make_grant, make_user):
    import app.session_service as service

    user_id = make_user("rdp-ops")
    host_id = make_host(name="win-09rdp", address="10.0.0.9", port=3389, protocol="rdp", os_type="windows")
    account_id = make_account(host_id, name="Administrator", username="Administrator", password="pw")
    make_grant(user_id, host_id, account_id)

    with pytest.raises(service.SessionError) as excinfo:
        service.open_session(app, user_id=user_id, host_id=host_id, account_id=account_id, source="web")
    assert "只有 ssh / winrm 能进网页终端" in str(excinfo.value)


# =========================================================================== 资产接口
def test_hosts_api_accepts_winrm_protocol(app, client, admin_headers):
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": "win-11", "address": "10.0.0.11", "protocol": "winrm", "osType": "windows"},
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["protocol"] == "winrm"
    assert data["port"] == 5985, "winrm 不填端口时默认 5985"
    assert data["winrmTransport"] == "ntlm"

    updated = client.put(
        f"/api/hosts/{data['id']}",
        headers=admin_headers,
        json={"winrmTransport": "basic", "description": "明文认证测试机"},
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["data"]["winrmTransport"] == "basic"

    kept = client.put(f"/api/hosts/{data['id']}", headers=admin_headers, json={"name": "win-11-renamed"})
    assert kept.status_code == 200
    assert kept.get_json()["data"]["port"] == 5985, "未传 port 时不能把端口改掉"


def test_hosts_api_rejects_unknown_winrm_transport(client, admin_headers):
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": "win-12", "address": "10.0.0.12", "protocol": "winrm", "winrmTransport": "kerberos"},
    )
    assert resp.status_code == 400
    assert "WinRM 认证方式只能是 ntlm / basic" in resp.get_json()["message"]


def test_hosts_api_rejects_unknown_protocol(client, admin_headers):
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": "odd", "address": "10.0.0.13", "protocol": "telnet"},
    )
    assert resp.status_code == 400
    assert "主机协议只能是" in resp.get_json()["message"]


def test_winrm_test_account_endpoint(client, monkeypatch, admin_headers, make_host, make_account):
    import app.winrm as winrm_package

    host_id = make_host(name="win-14", address="10.0.0.14", port=5985, protocol="winrm", os_type="windows")
    account_id = make_account(host_id, name="Administrator", username="Administrator", password="pw")

    monkeypatch.setattr(winrm_package, "build_target", lambda *args, **kwargs: WinrmTarget(host="10.0.0.14", port=5985, username="Administrator", password="pw"))
    monkeypatch.setattr(winrm_package, "connect", lambda target: make_connection())
    monkeypatch.setattr(winrm_package, "close", lambda connection: None)

    def fake_run_script(connection, script, *, timeout=None, cancel=None):
        assert script == PROBE_SCRIPT
        assert timeout == 20.0
        return ScriptResult(stdout="user=WIN-14\\administrator\r\nhost=WIN-14", exit_code=0)

    monkeypatch.setattr(winrm_package, "run_script", fake_run_script)
    resp = client.post(f"/api/hosts/{host_id}/accounts/{account_id}/test", headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["message"] == "连接成功"
    assert body["data"]["check"] == "winrm"
    assert body["data"]["transport"] == "ntlm"
    assert body["data"]["endpoint"] == "http://10.0.0.14:5985/wsman"
    assert "host=WIN-14" in body["data"]["output"]


def test_winrm_test_account_reports_connect_failure(client, monkeypatch, admin_headers, make_host, make_account):
    import app.winrm as winrm_package

    host_id = make_host(name="win-15", address="10.0.0.15", port=5985, protocol="winrm", os_type="windows")
    account_id = make_account(host_id, name="Administrator", username="Administrator", password="pw")

    monkeypatch.setattr(winrm_package, "build_target", lambda *args, **kwargs: WinrmTarget(host="10.0.0.15", port=5985))
    monkeypatch.setattr(
        winrm_package,
        "connect",
        lambda target: (_ for _ in ()).throw(WinrmError("目标主机 10.0.0.15:5985 拒绝了连接")),
    )
    resp = client.post(f"/api/hosts/{host_id}/accounts/{account_id}/test", headers=admin_headers)
    assert resp.status_code == 400
    assert "拒绝了连接" in resp.get_json()["message"]


# =========================================================================== 网页终端入口
def test_terminal_targets_include_winrm_hosts(client, admin_headers, make_host, make_account):
    winrm_id = make_host(name="win-16", address="10.0.0.16", port=5985, protocol="winrm", os_type="windows")
    make_account(winrm_id, name="Administrator", username="Administrator")

    body = client.get("/api/terminal/targets", headers=admin_headers).get_json()
    entries = {item["hostId"]: item for item in body["data"]}
    assert winrm_id in entries, "WinRM 主机必须出现在网页终端启动页列表里"
    entry = entries[winrm_id]
    assert entry["protocol"] == "winrm"
    assert entry["osType"] == "windows"
    assert entry["port"] == 5985

    checked = client.post(f"/api/terminal/targets/{winrm_id}/check", headers=admin_headers)
    assert checked.status_code == 200
    assert checked.get_json()["data"]["allowed"] is True


def test_terminal_targets_show_winrm_hosts_to_terminal_use_holders(app, client, make_host, make_account, make_grant):
    """winrm 与 ssh 共用 `terminal:use`：只有该权限的角色也能看到 Windows 命令行资产。"""
    ssh_id = make_host(name="linux-17", address="10.0.0.17", port=22, protocol="ssh")
    winrm_id = make_host(name="win-17", address="10.0.0.17", port=5985, protocol="winrm", os_type="windows")
    rdp_id = make_host(name="win-17-rdp", address="10.0.0.17", port=3389, protocol="rdp", os_type="windows")

    with app.app_context():
        from app.models import Role, User
        from app.security import hash_password

        code = "winrm-test-terminal-only"
        if Role.query.filter_by(code=code).first() is None:
            db.session.add(Role(code=code, name=code, description="只给 terminal:use", permissions=["dashboard:view", "terminal:use"]))
            db.session.commit()
        role = Role.query.filter_by(code=code).one()
        user = User(
            username=code,
            password_hash=hash_password("User1234"),
            display_name=code,
            role_id=role.id,
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    for host_id in (ssh_id, winrm_id, rdp_id):
        make_grant(user_id, host_id)

    login = client.post(
        "/api/login/account",
        json={"username": code, "password": "User1234", "type": "account", "autoLogin": True},
    )
    token = (login.get_json() or {}).get("data", {}).get("token")
    assert token, login.get_json()
    headers = {"Authorization": f"Bearer {token}"}
    body = client.get("/api/terminal/targets", headers=headers).get_json()
    ids = [item["hostId"] for item in body["data"]]
    assert ssh_id in ids
    assert winrm_id in ids, "WinRM 是字符终端，应随 terminal:use 一起可见"
    assert rdp_id not in ids, "远程桌面走 rdp:use，不该出现在字符终端入口"
