"""SSH 握手两端版本的回归用例（服务器版本 / 客户端版本）。

SSH 协议握手时两端都要声明自己：目标机在 `SSH-2.0-...` 里声明**服务端**版本
（paramiko 的 `transport.remote_version`），堡垒机作为**客户端**也要声明自己的版本
（`transport.local_version`，形如 `SSH-2.0-Paramiko_5.0.0`）。

用户需求（「添加客户端版本（版本号和图标）」）：这两个值都要在界面上看得到 ——
系统设置 → SSH 网关（`GET /api/settings/gateway`）与主机账号连通性测试
（`POST /api/hosts/<id>/accounts/<aid>/test`）两处。过去只有服务端版本，
排查「对端不认我们的客户端版本」这类问题时没有可观测字段，所以补齐并上护栏。
"""

from __future__ import annotations

import types

import paramiko

from app import ssh_client
from app.api import hosts as hosts_api
from app.ssh_client import ConnectionInfo, SSHTarget, connect, default_client_version


class _FakeTransport:
    """只实现 `connect()` 会碰到的几个成员。"""

    def __init__(self, remote_version="", local_version=""):
        self.remote_version = remote_version
        self.local_version = local_version
        self.keepalive = None

    def get_remote_server_key(self):
        return types.SimpleNamespace(fingerprint="SHA256:fakefingerprint")

    def set_keepalive(self, seconds):
        self.keepalive = seconds


def _patch_ssh_client(monkeypatch, transport):
    """把 `paramiko.SSHClient` 换成不联网的替身（`transport=None` 模拟拿不到传输层）。"""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.transport = transport

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.connect_kwargs = kwargs

        def get_transport(self):
            return self.transport

        def close(self):
            self.closed = True

    monkeypatch.setattr(ssh_client.paramiko, "SSHClient", _FakeClient)
    return _FakeClient


def test_default_client_version_is_the_paramiko_ident():
    """兜底真值必须是真的 SSH 标识串，不能是空串或自造格式（小写 `paramiko` 与之一致）。"""
    version = default_client_version()
    assert version.startswith("SSH-2.0-paramiko")
    assert paramiko.__version__ in version


def test_connect_reports_both_ends_of_the_ssh_handshake(monkeypatch):
    transport = _FakeTransport(
        remote_version="SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5",
        local_version="SSH-2.0-paramiko_5.0.0",
    )
    _patch_ssh_client(monkeypatch, transport)

    info = connect(SSHTarget(host="127.0.0.1", username="root", password="secret"))

    # 服务端版本 = 对端（目标机）声明的；客户端版本 = 本端（堡垒机）声明的。
    assert info.server_version == "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5"
    assert info.client_version == "SSH-2.0-paramiko_5.0.0"
    assert info.fingerprint == "SHA256:fakefingerprint"
    assert transport.keepalive == 30, "keepalive 仍要设置，别为了加字段丢掉行为"


def test_connect_falls_back_when_transport_has_no_local_version(monkeypatch):
    """老版本 paramiko / 替身传输层没有 `local_version` 时不能给出空字符串。"""
    _patch_ssh_client(monkeypatch, _FakeTransport(remote_version="SSH-2.0-dropbear"))

    info = connect(SSHTarget(host="h", username="u", password="p"))

    assert info.server_version == "SSH-2.0-dropbear"
    assert info.client_version == default_client_version()


def test_connect_without_transport_still_reports_client_version(monkeypatch):
    """传输层拿不到（连接刚断）时，客户端版本仍要可读。"""
    _patch_ssh_client(monkeypatch, None)

    info = connect(SSHTarget(host="h", username="u", password="p"))

    assert info.server_version == ""
    assert info.client_version == default_client_version()


def test_gateway_status_exposes_client_version(client, app, admin_headers):
    """系统设置 → SSH 网关：服务器版本（网关自己）与客户端版本（它去连目标机时用）都要在。"""
    resp = client.get("/api/settings/gateway", headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]

    assert data["serverVersion"] == app.config["GATEWAY_SERVER_VERSION"]
    assert data["clientVersion"] == default_client_version()
    assert data["serverVersion"] != data["clientVersion"]


def test_account_connectivity_test_reports_client_version(
    client, admin_headers, make_host, make_account, monkeypatch
):
    """主机 → 账号 → 连接测试：两个版本都要回给前端（前端弹窗里并排展示）。"""
    host_id = make_host(name="client-version-host")
    account_id = make_account(host_id, name="root", password="s3cret")

    info = ConnectionInfo(
        client=types.SimpleNamespace(close=lambda: None),
        fingerprint="SHA256:fake",
        server_version="SSH-2.0-OpenSSH_9.6p1",
        client_version="SSH-2.0-paramiko_9.9.9",
    )
    monkeypatch.setattr(hosts_api, "connect", lambda target: info)
    monkeypatch.setattr(
        hosts_api, "run_single_command", lambda i, command, timeout=0: (0, "Linux demo\n", "")
    )

    resp = client.post(
        f"/api/hosts/{host_id}/accounts/{account_id}/test", headers=admin_headers
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]

    assert data["serverVersion"] == "SSH-2.0-OpenSSH_9.6p1"
    assert data["clientVersion"] == "SSH-2.0-paramiko_9.9.9"
    assert data["fingerprint"] == "SHA256:fake"


def test_account_test_returns_structured_error_when_the_command_fails(
    client, app, admin_headers, make_host, make_account, monkeypatch
):
    """握手成功、但测试命令执行失败时必须是结构化错误，不能 500。

    历史缺陷：`test_account` 只护住了 `connect()`，「跑 uname -a」这一步抛异常会一路冒到
    Flask 兜底 → 前端拿到 `HTTP 500 服务端异常：Channel closed.`（内部英文异常、没有审计）。
    默认目标机早期版本拒绝 exec 请求时就是这个现象；AI 的 `run_command` 亦同源。
    """
    host_id = make_host(name="exec-fail-host")
    account_id = make_account(host_id, name="root", password="s3cret")

    info = ConnectionInfo(
        client=types.SimpleNamespace(close=lambda: None),
        fingerprint="SHA256:fake2",
        server_version="SSH-2.0-OpenSSH_9.6p1",
        client_version="SSH-2.0-Paramiko_9.9.9",
    )
    monkeypatch.setattr(hosts_api, "connect", lambda target: info)

    def _boom(command_target, command, timeout=0.0):
        raise paramiko.SSHException("Channel closed.")

    monkeypatch.setattr(hosts_api, "run_single_command", _boom)

    resp = client.post(
        f"/api/hosts/{host_id}/accounts/{account_id}/test", headers=admin_headers
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()

    assert body["code"] == "SSH_TEST_COMMAND_FAILED"
    assert "Channel closed." in body["message"]
    assert body["data"]["clientVersion"] == "SSH-2.0-Paramiko_9.9.9"
    assert body["data"]["serverVersion"] == "SSH-2.0-OpenSSH_9.6p1"
    assert body["data"]["fingerprint"] == "SHA256:fake2"

    from app.models import AuditLog

    with app.app_context():
        rows = AuditLog.query.filter_by(action="test_account", result="failure").all()
    assert rows, "失败也必须落审计（否则「谁测过哪台机器」查不到）"
