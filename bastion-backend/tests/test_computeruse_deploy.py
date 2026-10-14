"""ComputerUse 部署助手回归测试。

这里的每一条都对应一次真机踩坑，留着当回归网：

- WinRS 会在命令结束时把它拉起的子进程树一起杀掉 → 长任务改走计划任务（``_run_detached``）；
- ``C:\\Installer`` 在目标机上并不存在 → ``_write_remote_file`` 先建父目录；
- WinRM 的 stderr 是 CLIXML 包装 → ``clean_remote_stderr`` 只留错误正文；
- 计划任务的账号要按目标机的计算机名拼，不能拿堡垒机上的主机名当域。
"""

from __future__ import annotations

import pytest

from app.computeruse import deploy as cu_deploy


def _ctx(**overrides) -> cu_deploy.DeployContext:
    data: dict = {
        "deployment_id": 1,
        "host_id": 5,
        "account_id": 5,
        "account_username": "Administrator",
        "account_password": "pw",
        "python_version": "3.13.16",
        "installer_name": "python-3.13.16-amd64.exe",
        "host_name": "win-75",
        "host_address": "192.168.0.75",
    }
    data.update(overrides)
    return cu_deploy.DeployContext(**data)


class FakePs:
    """顶掉 ``runner._ps``：记下下发的脚本，按预设依次回答。"""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.labels: list[str] = []
        self.replies: list[tuple[int, str, str]] = []

    def __call__(self, script, *, timeout=120, stage=None, label=""):
        self.scripts.append(script)
        self.labels.append(label)
        if self.replies:
            return self.replies.pop(0)
        return 0, "", ""


def _runner(**overrides) -> tuple[cu_deploy.DeploymentRunner, FakePs]:
    runner = cu_deploy.DeploymentRunner(1, _ctx(**overrides))
    fake = FakePs()
    runner._ps = fake  # type: ignore[method-assign]
    return runner, fake


# --------------------------------------------------------------------------- #
# CLIXML 清洗
# --------------------------------------------------------------------------- #
CLIXML = (
    "#< CLIXML\r\n<Objs Version=\"1.1.0.1\" "
    'xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<S S="Error">Exception calling "WriteAllBytes" with "3" argument(s): '
    "&quot;Access to the path is denied.&quot;</S>"
    '<S S="Progress">progress noise</S></Objs>'
)


def test_clean_remote_stderr_keeps_error_text_only():
    text = cu_deploy.clean_remote_stderr(CLIXML)
    assert text == 'Exception calling "WriteAllBytes" with "3" argument(s): "Access to the path is denied."'
    assert "CLIXML" not in text
    assert "progress noise" not in text


def test_clean_remote_stderr_passes_plain_text_through():
    assert cu_deploy.clean_remote_stderr("  Access is denied.  ") == "Access is denied."
    assert cu_deploy.clean_remote_stderr("") == ""
    assert cu_deploy.clean_remote_stderr("   ") == ""


def test_clean_remote_stderr_without_error_block_returns_empty():
    assert cu_deploy.clean_remote_stderr('#< CLIXML\r\n<Objs><S S="Progress">x</S></Objs>') == ""


# --------------------------------------------------------------------------- #
# 远程写文件：先建父目录（真机上 C:\Installer 不存在）
# --------------------------------------------------------------------------- #
def test_write_remote_file_creates_parent_directory():
    runner, fake = _runner()
    payload = b"hello world"
    fake.replies.append((0, str(len(payload)), ""))
    runner._write_remote_file(r"C:\Installer\download-installer.ps1", payload, stage="upload")
    script = fake.scripts[0]
    assert "New-Item -ItemType Directory -Force -Path 'C:\\Installer' | Out-Null;" in script
    assert "[System.IO.File]::WriteAllBytes('C:\\Installer\\download-installer.ps1'" in script
    assert fake.labels[0].startswith("写入 ")


def test_write_remote_file_without_directory_skips_mkdir():
    runner, fake = _runner()
    fake.replies.append((0, "3", ""))
    runner._write_remote_file("bg.ps1", b"abc", stage="upload", label="写个小文件")
    assert "New-Item" not in fake.scripts[0]
    assert fake.labels[0] == "写个小文件"


def test_write_remote_file_rejects_size_mismatch():
    runner, fake = _runner()
    fake.replies.append((0, "2", ""))
    with pytest.raises(cu_deploy.DeployError, match="大小不一致"):
        runner._write_remote_file(r"C:\Installer\x.ps1", b"abcdef", stage="upload")


def test_write_remote_file_raises_on_nonzero_exit():
    runner, fake = _runner()
    fake.replies.append((1, "", "boom"))
    with pytest.raises(cu_deploy.DeployError, match="exit=1"):
        runner._write_remote_file(r"C:\Installer\x.ps1", b"abc", stage="upload")


# --------------------------------------------------------------------------- #
# 计划任务启动：活过 WinRM 命令
# --------------------------------------------------------------------------- #
def test_run_detached_registers_task_with_computer_name():
    runner, fake = _runner(account_username="Administrator")
    fake.replies.append((0, "TASK=Running LOGON=Interactive ACTIVE=True RESULT=267009", ""))
    summary = runner._run_detached(
        "AutoOps-ComputerUse-MCP",
        "wscript.exe",
        r'"C:\Installer\AutoOps-ComputerUse.vbs"',
        stage="vbs",
        label="立刻启动一次",
        interactive=True,
    )
    assert summary == "TASK=Running LOGON=Interactive ACTIVE=True RESULT=267009"
    script = fake.scripts[0]
    # 账号没写域 → 让目标机自己拼计算机名（堡垒机上的主机名当域会认不出来）
    assert "-UserId ($env:COMPUTERNAME + '\\Administrator')" in script
    assert "$logon = if ($active) { 'Interactive' } else { 'S4U' };" in script
    assert "Register-ScheduledTask -TaskName 'AutoOps-ComputerUse-MCP' -Action $action" in script
    assert "Start-ScheduledTask -TaskName 'AutoOps-ComputerUse-MCP';" in script
    assert "-RunLevel Highest" in script


def test_run_detached_background_uses_s4u():
    runner, fake = _runner()
    fake.replies.append((0, "TASK=Running LOGON=S4U ACTIVE=False RESULT=0", ""))
    runner._run_detached("AutoOps-ComputerUse-Download", "powershell.exe", "-File x.ps1", stage="upload", label="下载")
    script = fake.scripts[0]
    # 非交互用途永远不用 Interactive：没人在桌面登录时那种任务会被挂住
    assert "$logon = if ($active) { 'S4U' } else { 'S4U' };" in script
    assert "-Execute 'powershell.exe'" in script
    assert "-Argument '-File x.ps1'" in script


def test_run_detached_keeps_domain_account_as_is():
    runner, fake = _runner(account_username="CORP\\svc-deploy")
    fake.replies.append((0, "TASK=Ready LOGON=S4U ACTIVE=False RESULT=0", ""))
    runner._run_detached("AutoOps-ComputerUse-Download", "powershell.exe", "-File x.ps1", stage="upload", label="下载")
    assert "-UserId 'CORP\\svc-deploy'" in fake.scripts[0]


def test_run_detached_needs_an_account():
    runner, fake = _runner(account_username="")
    with pytest.raises(cu_deploy.DeployError, match="账号"):
        runner._run_detached("AutoOps-ComputerUse-Download", "powershell.exe", "-File x.ps1", stage="upload", label="下载")
    assert fake.scripts == []


def test_run_detached_raises_when_task_never_registered():
    runner, fake = _runner()
    fake.replies.append((1, "", "#< CLIXML"))
    with pytest.raises(cu_deploy.DeployError, match="计划任务"):
        runner._run_detached("AutoOps-ComputerUse-Download", "powershell.exe", "-File x.ps1", stage="upload", label="下载")


def test_delete_task_ignores_failures():
    runner, fake = _runner()

    def boom(script, *, timeout=120, stage=None, label=""):
        raise cu_deploy.DeployError("目标机掉线了")

    # 删不掉任务不该让整个部署失败
    runner._ps = boom  # type: ignore[method-assign]
    runner._delete_task("AutoOps-ComputerUse-Download", stage="upload")


def test_listen_wait_script_without_owner_accepts_first_listener():
    script = cu_deploy.mcp_listen_wait_script(8000)
    assert "Get-NetTCPConnection -LocalPort 8000 -State Listen" in script
    # 没人占着端口时，第一次连上就算成功
    assert "$new = $true;" in script
    assert "LISTEN=$ok WHO=$who" in script


def test_listen_wait_script_requires_a_new_owner():
    script = cu_deploy.mcp_listen_wait_script(8123, "6620")
    # 真机踩过：接管脚本杀掉老 MCP 中间有几秒空档，只看「端口有人听」会把老进程误判成新的
    assert "$new = ($owner -ne '6620')" in script
    assert "-LocalPort 8123" in script


def test_listen_wait_script_ignores_the_portproxy_listener():
    """8000 端口上的 portproxy（IP Helper 的 svchost）不算「MCP 起来了」。

    真机现场：自愈报 listen=true / ok=true，但 MCP 的 python 根本没起来，
    堡垒机连上去还是 10061 —— 因为看到的是 portproxy 在听局域网地址。
    """
    script = cu_deploy.mcp_listen_wait_script(8000)
    assert "$_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '::1'" in script
    assert "@('python','pythonw','windows-mcp') -contains $name" in script
    assert "NAME=$name" in script


def test_proxy_heal_script_rebuilds_and_reads_back_the_portproxy():
    script = cu_deploy.mcp_proxy_heal_script(8000)
    # 局域网地址上没人听才重建（bindAll 部署时 MCP 自己就在监听 LAN，不该插一条 portproxy）
    assert "$lanListen -eq 0" in script
    assert "Set-Service iphlpsvc -StartupType Automatic" in script
    assert "Start-Service iphlpsvc" in script
    assert "netsh interface portproxy add v4tov4 listenaddress=$lan listenport=$port" in script
    assert "$port = 8000" in script
    assert "connectaddress=127.0.0.1" in script
    # 读回核对：PROXY 才是「堡垒机这条路通了」的判据
    assert '"PROXY=" + [bool]($now -gt 0)' in script
    assert '"IPHLPSVC=" + (Get-Service iphlpsvc' in script


def test_restart_verdict_requires_both_the_process_and_the_proxy():
    # MCP 没起来
    ok, why = cu_deploy.restart_verdict(listen=False, proxy=True)
    assert ok is False and "MCP" in why
    # MCP 起来了但 portproxy 没转发（真机那次：10061）
    ok, why = cu_deploy.restart_verdict(listen=True, proxy=False)
    assert ok is False and "portproxy" in why
    # 两个都过
    ok, why = cu_deploy.restart_verdict(listen=True, proxy=True)
    assert ok is True and why == ""
    # 前面已经报错就保留原原因，别被后面的判据覆盖
    ok, why = cu_deploy.restart_verdict(listen=False, proxy=False, error="连接目标机失败：拒绝连接")
    assert ok is False and why == "连接目标机失败：拒绝连接"


# --------------------------------------------------------------------------- #
# 放行防火墙：读回核对 + 从堡垒机侧真连一次
# （用户实测「堡垒机连不上 MCP」的落点：以前只发一条 netsh 就当成功）
# --------------------------------------------------------------------------- #
RULE_OK = (
    "FW_PRESENT=1\nFW_ENABLED=True\nFW_DIRECTION=Inbound\nFW_ACTION=Allow\n"
    "FW_PROTO=TCP\nFW_PORT=8000\nFW_REMOTE=192.168.0.106"
)


def test_kv_lines_tolerates_equals_and_skips_noise():
    info = cu_deploy._kv_lines("noise\nLAN=10.0.0.5\nLISTEN=0.0.0.0,::\nNOTE=a=b=c\n_=x\n")
    assert info["LAN"] == "10.0.0.5"
    assert info["LISTEN"] == "0.0.0.0,::"
    # 值里再出现 = 也不能把后半截吃掉（stderr 里常带）
    assert info["NOTE"] == "a=b=c"
    # `_` 去掉下划线后不是字母数字 → 不是我们要的键
    assert "_" not in info
    assert "noise" not in info


def test_stage_expose_requires_iphlpsvc_running():
    runner, fake = _runner()
    fake.replies.append((0, "LAN=192.168.0.75\nIPHLPSVC=Stopped\nLISTEN=", ""))
    with pytest.raises(cu_deploy.DeployError, match="IP Helper"):
        runner._stage_expose(app=None)
    # 地址先记下来，报错文案里能指明是哪台机的哪个口
    assert runner.ctx.lan_ip == "192.168.0.75"
    assert runner.ctx.mcp_url == "http://192.168.0.75:8000/mcp"


def test_stage_expose_picks_lan_by_default_route_and_reports_listener():
    runner, fake = _runner()
    fake.replies.append((0, "LAN=192.168.0.75\nIPHLPSVC=Running\nPP_ENTRY=True\nLISTEN=0.0.0.0", ""))
    runner._stage_expose(app=None)
    script = fake.scripts[0]
    # 多网卡机器上按默认路由挑地址（老实现只看第一个地址，常挑到堡垒机到不了的网卡）
    assert "Get-NetRoute -DestinationPrefix '0.0.0.0/0'" in script
    assert "netsh interface portproxy add v4tov4" in script
    assert "connectaddress=127.0.0.1" in script
    assert "Get-NetTCPConnection -State Listen -LocalPort $port" in script


def test_stage_firewall_passes_when_rule_reads_back_and_port_is_reachable():
    runner, fake = _runner(bastion_ip="192.168.0.106")
    fake.replies.append((0, RULE_OK, ""))
    runner._tcp_reachable = lambda host, port, **kw: (True, "")  # type: ignore[method-assign]
    runner._stage_firewall(app=None)
    assert runner.ctx.firewall_ok is True
    assert runner.ctx.lan_reachable is True
    assert "192.168.0.106" in runner.ctx.firewall_rule
    script = fake.scripts[0]
    assert "netsh advfirewall firewall add rule name='AutoOps-ComputerUse'" in script
    assert "remoteip=$remote" in script
    assert "-RemoteAddress $remote" in script  # netsh 写不上时退到 cmdlet
    assert "Get-NetFirewallRule -DisplayName 'AutoOps-ComputerUse'" in script


def test_stage_firewall_rejects_a_rule_that_did_not_stick():
    runner, fake = _runner(bastion_ip="192.168.0.106")
    fake.replies.append((0, "FW_PRESENT=0", "Access is denied."))
    with pytest.raises(cu_deploy.DeployError, match="放行 8000 端口失败"):
        runner._stage_firewall(app=None)
    assert runner.ctx.firewall_ok is False


def test_stage_firewall_rejects_wrong_port_or_scope():
    runner, fake = _runner(bastion_ip="192.168.0.106")
    fake.replies.append(
        (
            0,
            "FW_PRESENT=1\nFW_ENABLED=True\nFW_DIRECTION=Inbound\nFW_ACTION=Allow\n"
            "FW_PROTO=TCP\nFW_PORT=3389\nFW_REMOTE=Any",
            "",
        )
    )
    with pytest.raises(cu_deploy.DeployError, match="端口里没有 8000"):
        runner._stage_firewall(app=None)


def test_stage_firewall_rejects_a_rule_that_never_got_enabled():
    runner, fake = _runner(bastion_ip="192.168.0.106")
    fake.replies.append(
        (0, RULE_OK.replace("FW_ENABLED=True", "FW_ENABLED=False"), "")
    )
    with pytest.raises(cu_deploy.DeployError, match="没启用"):
        runner._stage_firewall(app=None)


def test_stage_firewall_fails_when_the_bastion_still_cannot_connect():
    runner, fake = _runner(bastion_ip="192.168.0.106")
    fake.replies.append((0, RULE_OK, ""))
    # 复核监听地址那次（连不上时才会问）
    fake.replies.append((0, "LISTEN=", ""))
    runner._tcp_reachable = lambda host, port, **kw: (  # type: ignore[method-assign]
        False,
        "ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝",
    )
    with pytest.raises(cu_deploy.DeployError, match="从堡垒机连不上"):
        runner._stage_firewall(app=None)
    assert runner.ctx.lan_reachable is False


def test_stage_firewall_falls_back_to_local_subnet_without_a_bastion_ip():
    runner, fake = _runner(bastion_ip="")
    fake.replies.append((0, RULE_OK.replace("192.168.0.106", "LocalSubnet"), ""))
    runner._tcp_reachable = lambda host, port, **kw: (True, "")  # type: ignore[method-assign]
    runner._stage_firewall(app=None)
    assert "$remote = 'LocalSubnet';" in fake.scripts[0]
    assert runner.ctx.firewall_remote == "LocalSubnet"


def test_tcp_reachable_retries_then_reports_the_reason():
    runner, _ = _runner()
    calls: list[tuple] = []

    def fake_connect(target, timeout=None):
        calls.append(target)
        raise ConnectionRefusedError("nope")

    original = cu_deploy.socket.create_connection
    cu_deploy.socket.create_connection = fake_connect  # type: ignore[assignment]
    try:
        ok, why = runner._tcp_reachable("192.168.0.75", 8000, attempts=2, pause=0)
    finally:
        cu_deploy.socket.create_connection = original
    assert ok is False
    assert "ConnectionRefusedError" in why
    assert calls == [("192.168.0.75", 8000), ("192.168.0.75", 8000)]

