"""SFTP 版假目标机：真 paramiko 服务端 + 真文件后端（``tests/`` 专用夹具）。

文件管理器的测试**不 mock paramiko**：这里起一个真的 SSH 服务端，挂上真的
``sftp`` 子系统，客户端走真 SFTP 协议去 list/stat/open/rename/rmdir/chmod。
只有这样才能证明「浏览、上传、下载、改名、移动、复制、删除、改权限、打包下载」
在真机上成立，而不是在一个假对象里自洽。

服务端实现复用 ``tools/sftp_backend.py``（与演示目标机同一份代码）。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import paramiko
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from sftp_backend import install_sftp_subsystem, seed_demo_root  # noqa: E402

_HOST_KEY: paramiko.RSAKey | None = None
_HOST_KEY_LOCK = threading.Lock()


def _host_key() -> paramiko.RSAKey:
    global _HOST_KEY
    with _HOST_KEY_LOCK:
        if _HOST_KEY is None:
            _HOST_KEY = paramiko.RSAKey.generate(2048)
        return _HOST_KEY


class _SFTPOnlyServer(paramiko.ServerInterface):
    """只开放 sftp 子系统（文件管理器不需要 shell）。"""

    def __init__(self, username: str = "root", password: str = "s3cret") -> None:
        self.username = username
        self.password = password
        self.sftp_ready = threading.Event()
        self.sftp_done = threading.Event()

    def check_auth_password(self, username, password):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):  # noqa: ARG002
        return "password"

    def check_channel_request(self, kind, chanid):  # noqa: ARG002
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):  # noqa: ANN001, ARG002
        """sftp 交回 paramiko 默认实现（它才负责实例化子系统 handler 并起线程）。"""
        if name == "sftp":
            self.sftp_ready.set()
            return super().check_channel_subsystem_request(channel, name)
        return False


class FakeSFTPTarget:
    """监听 127.0.0.1 的真 SSH 服务端，只提供 sftp 子系统。"""

    def __init__(self, root, username: str = "root", password: str = "s3cret") -> None:
        self.root = os.path.abspath(str(root))
        seed_demo_root(self.root)
        self.username = username
        self.password = password
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.host, self.port = self.sock.getsockname()
        self.server_interface = _SFTPOnlyServer(username, password)
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-sftp", daemon=True)
        self._thread.start()

    # -- 断言辅助 -----------------------------------------------------------
    def local(self, remote_path: str) -> str:
        """把远端路径映射成本地真实路径（断言文件真的被写/删了）。"""
        return os.path.join(self.root, str(remote_path).lstrip("/"))

    def exists(self, remote_path: str) -> bool:
        return os.path.exists(self.local(remote_path))

    def read(self, remote_path: str) -> bytes:
        with open(self.local(remote_path), "rb") as handle:
            return handle.read()

    def write(self, remote_path: str, content: bytes | str) -> None:
        target = self.local(remote_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)

    # -- 服务端 -------------------------------------------------------------
    def _serve(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                client, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client) -> None:
        transport = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(_host_key())
            install_sftp_subsystem(transport, self.root, on_end=self.server_interface.sftp_done.set)
            transport.start_server(server=self.server_interface)
            channel = transport.accept(10)
            if channel is None:
                return
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.server_interface.sftp_ready.is_set():
                time.sleep(0.02)
            self.server_interface.sftp_done.wait(30)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture()
def sftp_target(tmp_path):
    target = FakeSFTPTarget(tmp_path / "sftp-root")
    try:
        yield target
    finally:
        target.stop()
