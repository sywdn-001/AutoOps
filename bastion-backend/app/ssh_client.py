"""基于 paramiko 的 SSH 客户端封装（堡垒机 -> 目标主机）。"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import paramiko

from .crypto import decrypt


class SSHError(RuntimeError):
    """连接 / 认证 / 通道类错误，消息可直接展示给用户。"""


@dataclass
class SSHTarget:
    host: str
    port: int = 22
    username: str = ""
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None
    sudo_command: str = ""
    connect_timeout: int = 15
    banner_timeout: int = 20
    keepalive: int = 30
    host_label: str = ""

    def display(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"


@dataclass
class ConnectionInfo:
    client: paramiko.SSHClient
    fingerprint: str = ""
    server_version: str = ""
    client_version: str = ""
    authenticated_at: float = field(default_factory=time.time)


def default_client_version() -> str:
    """堡垒机作为 SSH **客户端**对外通告的版本串（`SSH-2.0-paramiko_x.y.z`）。

    SSH 握手有两端版本：服务端在 `SSH-2.0-...` 里声明自己（目标机的 `remote_version`），
    客户端同样要声明自己（本机的 `local_version`）。运维排障时两端都要看得到，
    所以这里给它一个不依赖连接对象的兜底真值（网关登录横幅/系统设置页也用同一个口径）。

    **大小写与 paramiko 保持一致**：paramiko 的 `Transport.local_version` 实际是
    `SSH-2.0-paramiko_<版本>`（小写），兜底真值若写成大写 `Paramiko`，同一个字段在
    「拿得到传输层」和「拿不到传输层」两种情况下会出现两种字样，界面与脚本断言都会打架。
    """
    version = getattr(paramiko, "__version__", "") or ""
    return f"SSH-2.0-paramiko_{version}" if version else "SSH-2.0-paramiko"


def build_target(
    host, account, *, port=None, connect_timeout=15, banner_timeout=20, keepalive=30
) -> SSHTarget:
    """由 ORM 对象构造连接目标，凭据字段就地解密。

    ``port`` 显式传入时优先于 ``host.port``：一台主机可能有多个协议端点（例如同时开
    SSH 与 WinRM），端口属于**端点**而不是主机。
    """
    password = None
    private_key = None
    passphrase = None
    if account.auth_type == "key":
        private_key = decrypt(account.private_key_enc) or None
        passphrase = decrypt(account.passphrase_enc) or None
    else:
        password = decrypt(account.secret_enc) or None
    return SSHTarget(
        host=host.address,
        port=int(port or host.port or 22),
        username=account.username,
        password=password,
        private_key=private_key,
        passphrase=passphrase,
        sudo_command=account.sudo_command or "",
        connect_timeout=connect_timeout,
        banner_timeout=banner_timeout,
        keepalive=keepalive,
        host_label=f"{host.name}({host.address})",
    )


def load_private_key(key_text: str, passphrase: str | None = None) -> paramiko.PKey:
    """依次尝试常见密钥类型解析私钥。"""
    last_error: Exception | None = None
    for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(key_text), password=passphrase or None)
        except Exception as exc:  # noqa: BLE001 - 逐个类型试探
            last_error = exc
    raise SSHError(f"私钥解析失败（已尝试 ed25519 / ecdsa / rsa）：{last_error}")


def connect(target: SSHTarget, sock=None) -> ConnectionInfo:
    """建立到目标主机的 SSH 连接。"""
    if not target.password and not target.private_key:
        raise SSHError("该资产账号没有可用凭据（口令或私钥），请先在后台补全")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": target.host,
        "port": target.port,
        "username": target.username,
        "timeout": target.connect_timeout,
        "banner_timeout": target.banner_timeout,
        "auth_timeout": max(target.connect_timeout, 15),
        "allow_agent": False,
        "look_for_keys": False,
        "compress": True,
    }
    if sock is not None:
        kwargs["sock"] = sock
    if target.private_key:
        kwargs["pkey"] = load_private_key(target.private_key, target.passphrase)
    else:
        kwargs["password"] = target.password

    try:
        client.connect(**kwargs)
    except paramiko.AuthenticationException as exc:
        raise SSHError(f"目标主机认证失败（账号 {target.username}）：{exc}") from exc
    except paramiko.BadHostKeyException as exc:
        raise SSHError(f"目标主机密钥校验失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SSHError(f"连接目标主机 {target.host}:{target.port} 失败：{exc}") from exc

    transport = client.get_transport()
    fingerprint = ""
    server_version = ""
    client_version = ""
    if transport is not None:
        try:
            fingerprint = transport.get_remote_server_key().fingerprint
        except Exception:  # noqa: BLE001
            fingerprint = ""
        server_version = getattr(transport, "remote_version", "") or ""
        # 对端（目标机）声明自己是服务端；本端（堡垒机）声明自己是客户端。
        client_version = getattr(transport, "local_version", "") or ""
        if target.keepalive:
            transport.set_keepalive(target.keepalive)

    return ConnectionInfo(
        client=client,
        fingerprint=fingerprint or "",
        server_version=server_version,
        client_version=client_version or default_client_version(),
    )


def open_shell(info: ConnectionInfo, *, term="xterm-256color", width=120, height=32):
    """打开交互式 shell 通道（非阻塞读）。"""
    try:
        channel = info.client.invoke_shell(term=term, width=width, height=height)
    except Exception as exc:  # noqa: BLE001
        raise SSHError(f"打开远端 shell 失败：{exc}") from exc
    channel.settimeout(0.0)
    return channel


def close(info: ConnectionInfo | None) -> None:
    if info is None:
        return
    try:
        info.client.close()
    except Exception:  # noqa: BLE001
        pass


def run_single_command(
    info: ConnectionInfo, command: str, timeout: float = 30.0
) -> tuple[int, str, str]:
    """一次性执行（用于连通性自检 / SFTP 之外的场景）。"""
    stdin, stdout, stderr = info.client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    status = stdout.channel.recv_exit_status()
    return status, out, err
