"""WinRM（Windows 远程管理）客户端：给网页终端提供 Windows shell。

为什么不是「常驻进程 + stdin 交互」
----------------------------------
pywinrm 也支持 ``open_shell()`` + ``run_command(shell_id, "cmd.exe", ["/Q", "/K"])``
+ ``send_command_input()`` 的交互式用法，但 WinRS 的 Receive 要等到命令结束或
OperationTimeout 才回包：本项目实测（win-75，Windows 11 26100）连上 ``cmd.exe /K``
后第一次 ``get_command_output()`` **阻塞 150 秒以上**；同一台机器上
``winrm.Session.run_cmd``（每次短命令、自己会退出）**秒回**。

所以这里的策略是：**保持一个持久 WSMan shell，但每条用户输入都跑一条自己会退出的
短命令**（``powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand …``）。
代价是每条命令一个新进程：``cd``（当前目录）由桥接层自己跟踪并回填，而环境变量、
自定义变量/函数、``pushd`` 位置栈等**进程内状态不跨命令保留**；``more`` / ``pause`` /
``Read-Host`` 这类交互式程序也不可用。这些限制都写在网页终端首屏提示里。
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass, field

from ..crypto import decrypt

log = logging.getLogger(__name__)

WINRM_DEFAULT_PORT = 5985
WINRM_DEFAULT_SSL_PORT = 5986
WINRM_TRANSPORT_DEFAULT = "ntlm"
WINRM_TRANSPORTS = ("ntlm", "basic")
CODEPAGE_UTF8 = 65001
POWERSHELL = "powershell.exe"


class WinrmError(RuntimeError):
    """WinRM 连接/执行失败；message 是给最终用户看的中文。"""


@dataclass
class WinrmTarget:
    host: str
    port: int = WINRM_DEFAULT_PORT
    username: str = ""
    password: str = ""
    transport: str = WINRM_TRANSPORT_DEFAULT
    use_ssl: bool = False
    server_cert_validation: str = "ignore"
    connect_timeout: int = 15
    command_timeout: int = 60
    host_label: str = ""

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}/wsman"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class ScriptResult:
    """一条命令的执行结果。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int = 0


@dataclass
class WinrmConnection:
    protocol: object
    target: WinrmTarget
    shell_id: str = ""
    last_command_id: str = ""


def build_target(
    host,
    account,
    *,
    port: int | None = None,
    winrm_transport: str | None = None,
    connect_timeout: int = 15,
    command_timeout: int = 60,
) -> WinrmTarget:
    """把 (Host, HostAccount) 变成连接参数；口令在这里就地解密。

    ``port`` / ``winrm_transport`` 显式传入时优先于主机字段：一台主机可能有多个协议
    端点（Windows 常见 RDP + WinRM），端口与认证方式都属于**端点**。
    """
    auth_type = (getattr(account, "auth_type", "") or "password").strip().lower()
    if auth_type == "key":
        raise WinrmError("WinRM 不支持私钥登录，请把该资产账号改成「用户名 + 口令」凭据")
    username = (getattr(account, "username", "") or "").strip()
    password = decrypt(getattr(account, "secret_enc", None))
    if not username or not password:
        raise WinrmError("该资产账号没有可用凭据（用户名 + 口令），请先在后台补全")
    port = int(port or getattr(host, "port", 0) or WINRM_DEFAULT_PORT)
    transport = (
        winrm_transport or getattr(host, "winrm_transport", "") or WINRM_TRANSPORT_DEFAULT
    ).strip().lower()
    if transport not in WINRM_TRANSPORTS:
        transport = WINRM_TRANSPORT_DEFAULT
    return WinrmTarget(
        host=(getattr(host, "address", "") or "").strip(),
        port=port,
        username=username,
        password=password,
        transport=transport,
        use_ssl=port == WINRM_DEFAULT_SSL_PORT,
        connect_timeout=max(int(connect_timeout or 15), 3),
        command_timeout=max(int(command_timeout or 60), 5),
        host_label=(getattr(host, "name", "") or "").strip(),
    )


def decode_bytes(data: bytes | None) -> str:
    """WinRM 回来的字节：先按 UTF-8，再退回中文 Windows 常见的 GBK，最后容错解码。"""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def _friendly(error: Exception, target: WinrmTarget) -> str:
    """把 pywinrm 的英文异常翻成能直接给用户看的中文。"""
    text = str(error) or error.__class__.__name__
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return f"连接目标主机 {target.address} 超时：目标机可能没有启用 WinRM，或 5985/5986 被防火墙挡住"
    if "refused" in low or "reset" in low or "unreachable" in low or "no route" in low:
        return f"目标主机 {target.address} 拒绝了连接：目标机可能没有启用 WinRM（管理员执行 Enable-PSRemoting -Force）"
    if "401" in low or "unauthorized" in low:
        return f"目标主机 WinRM 认证失败（账号 {target.username}）：用户名或口令不对"
    if "403" in low or "forbidden" in low:
        return (
            f"账号 {target.username} 没有远程管理权限：需要是目标机的本机管理员，"
            "或加入目标机的「Remote Management Users」组"
        )
    if "ssl" in low and ("certificate" in low or "verify" in low):
        return f"目标主机 {target.address} 的 WinRM 证书校验失败（自签证书请用 5985 明文端口）"
    return f"连接目标主机 {target.address} 失败：{text}"


def connect(target: WinrmTarget) -> WinrmConnection:
    """建 Protocol + 打开持久 WSMan shell；失败抛 WinrmError（中文）。"""
    try:
        import winrm
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise WinrmError('后端未安装 pywinrm，无法连接 Windows 主机；请执行 pip install "pywinrm[ntlm]"') from exc

    operation = min(max(int(target.command_timeout or 60), 5), 55)
    try:
        protocol = winrm.Protocol(
            endpoint=target.endpoint,
            transport=target.transport,
            username=target.username,
            password=target.password,
            server_cert_validation=target.server_cert_validation,
            operation_timeout_sec=operation,
            read_timeout_sec=operation + 15,
        )
        shell_id = protocol.open_shell(codepage=CODEPAGE_UTF8)
    except WinrmError:
        raise
    except Exception as exc:  # noqa: BLE001 - pywinrm 的异常种类很多，统一转中文
        raise WinrmError(_friendly(exc, target)) from exc
    return WinrmConnection(protocol=protocol, target=target, shell_id=shell_id)


def _open_shell(connection: WinrmConnection) -> str:
    try:
        return connection.protocol.open_shell(codepage=CODEPAGE_UTF8)
    except Exception as exc:  # noqa: BLE001
        raise WinrmError(_friendly(exc, connection.target)) from exc


def run_script(
    connection: WinrmConnection,
    script: str,
    *,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
) -> ScriptResult:
    """在持久 shell 上跑一条短命令。

    ``timeout`` 秒内没结束就调 ``cleanup_command`` 请求中断（WinRS 的 Ctrl-C 等价物）；
    ``cancel`` 事件被置位（用户在网页终端按 Ctrl-C）走同一条路。两者都不会让调用方
    无限等：最多再等 3 秒收尾。
    """
    if not connection.shell_id:
        connection.shell_id = _open_shell(connection)
    started = time.monotonic()
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    try:
        command_id = connection.protocol.run_command(connection.shell_id, POWERSHELL, args)
    except Exception as exc:  # noqa: BLE001
        connection.shell_id = ""
        raise WinrmError(_friendly(exc, connection.target)) from exc
    connection.last_command_id = command_id

    holder: dict[str, object] = {}

    def _collect() -> None:
        try:
            holder["result"] = connection.protocol.get_command_output(connection.shell_id, command_id)
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc

    thread = threading.Thread(target=_collect, name=f"wrx-{command_id[:8]}", daemon=True)
    thread.start()
    timed_out = False
    cancelled = False
    while thread.is_alive():
        thread.join(0.25)
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if timeout and timeout > 0 and (time.monotonic() - started) > timeout:
            timed_out = True
            break

    if thread.is_alive():
        # 请求中断：WinRS 侧等价于 Ctrl-C，命令会被终结，采集线程随之返回。
        try:
            connection.protocol.cleanup_command(connection.shell_id, command_id)
        except Exception:  # noqa: BLE001
            log.debug("cleanup_command 请求中断失败（忽略）", exc_info=True)
        thread.join(3.0)

    result = ScriptResult(timed_out=timed_out, cancelled=cancelled)
    if thread.is_alive():
        # 命令还在目标机上跑：这个 shell 可能仍被占用，下一次命令重开一个，避免互相干扰。
        result.stderr = "命令未在超时前结束，已请求中断（目标机可能还在执行）"
        result.exit_code = 1
        connection.shell_id = ""
        connection.last_command_id = ""
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    error = holder.get("error")
    if error is not None:
        connection.shell_id = ""
        raise WinrmError(_friendly(error, connection.target)) from error  # type: ignore[misc]
    out, err, status = holder.get("result") or (b"", b"", 0)  # type: ignore[misc]
    result.stdout = decode_bytes(out)
    result.stderr = decode_bytes(err)
    result.exit_code = int(status or 0)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    try:
        connection.protocol.cleanup_command(connection.shell_id, command_id)
    except Exception:  # noqa: BLE001
        log.debug("cleanup_command 失败（忽略）", exc_info=True)
    connection.last_command_id = ""
    return result


def close(connection: WinrmConnection) -> None:
    """关闭会话；不抛异常（收尾路径上失败只记 debug）。"""
    shell_id = connection.shell_id
    connection.shell_id = ""
    connection.last_command_id = ""
    if not shell_id:
        return
    try:
        connection.protocol.close_shell(shell_id)
    except Exception:  # noqa: BLE001
        log.debug("close_shell 失败（忽略）", exc_info=True)


PROBE_SCRIPT = "\n".join(
    [
        '"user=" + (whoami)',
        '"host=" + $env:COMPUTERNAME',
        '"ps=" + $PSVersionTable.PSVersion.ToString()',
        '"os=" + [System.Environment]::OSVersion.VersionString',
    ]
)


def test_connection(host, account, *, timeout: int = 15) -> str:
    """「测试连接」用：连上去跑一条只读探测命令，返回多行文本结果。"""
    target = build_target(host, account, connect_timeout=timeout, command_timeout=timeout)
    connection = connect(target)
    try:
        result = run_script(connection, PROBE_SCRIPT, timeout=max(int(timeout), 10))
    finally:
        close(connection)
    if result.timed_out:
        raise WinrmError(f"目标主机 {target.address} 没有在 {timeout} 秒内响应 WinRM 命令")
    text = (result.stdout or "").strip()
    if not text:
        text = (result.stderr or "").strip()
    if not text:
        raise WinrmError(f"目标主机 {target.address} 没有返回任何内容（WinRM 命令退出码 {result.exit_code}）")
    return text
