"""网关主机密钥指纹的回归测试。

背景（真实缺陷）：`run.py` 启动横幅用 `getattr(gateway, "fingerprint", "")` 取指纹，
但 `GatewayServer` 只有一个返回 MD5 hex 的 `status()` 字典，**没有 `fingerprint` 属性**
→ 横幅里的「网关主机指纹」永远是空串，用户无法核对主机密钥；而 API
`GET /api/settings/gateway` 走的是另一段代码，两处格式还可能不一致。

现在统一为 OpenSSH 风格 `SHA256:<base64 无填充>`，与
`ssh-keyscan -p <port> <host> | ssh-keygen -lf -` 的输出一致。
"""

from __future__ import annotations

import base64
import hashlib

import paramiko
import pytest

from app.gateway.server import GatewayServer, fingerprint_of, host_key_fingerprint

_KEY = None


def _key():
    """模块级缓存密钥对象：RSA 生成较慢，避免每个用例都重算。"""
    global _KEY
    if _KEY is None:
        _KEY = paramiko.RSAKey.generate(2048)
    return _KEY


def _expected(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def test_fingerprint_of_matches_openssh_format():
    key = _key()
    value = fingerprint_of(key)
    assert value == _expected(key)
    assert value.startswith("SHA256:")
    assert not value.endswith("=")  # OpenSSH 不补 '=' 填充
    assert " " not in value and ":" in value


def test_host_key_fingerprint_reads_private_key_file(tmp_path):
    key = _key()
    path = tmp_path / "gateway_host_rsa.key"
    key.write_private_key_file(str(path))
    assert host_key_fingerprint(str(path)) == fingerprint_of(key)


def test_host_key_fingerprint_tolerates_missing_and_broken_file(tmp_path):
    assert host_key_fingerprint("") == ""
    assert host_key_fingerprint(str(tmp_path / "nope.key")) == ""
    broken = tmp_path / "broken.key"
    broken.write_text("not a private key", encoding="utf-8")
    assert host_key_fingerprint(str(broken)) == ""


def test_server_property_and_status_agree(app, tmp_path, monkeypatch):
    """启动横幅读的 property 与 status() 必须是同一格式同一值。"""
    key = _key()
    server = GatewayServer(app)
    assert server.fingerprint == ""  # 未加载密钥
    server._host_key = key
    assert server.fingerprint == fingerprint_of(key)
    assert server.status()["fingerprint"] == server.fingerprint


def test_settings_api_reports_same_fingerprint_as_banner(app, client, admin_headers, tmp_path, monkeypatch):
    """API 与 run.py 横幅（property）不能各算一套。"""
    key = _key()
    path = tmp_path / "gw.key"
    key.write_private_key_file(str(path))
    monkeypatch.setitem(app.config, "GATEWAY_HOST_KEY", str(path))

    body = client.get("/api/settings/gateway", headers=admin_headers).get_json()
    assert body["success"] is True
    assert body["data"]["hostKeyFingerprint"] == fingerprint_of(key)

    server = GatewayServer(app)
    server._host_key = key
    assert body["data"]["hostKeyFingerprint"] == server.fingerprint


def test_settings_api_returns_empty_when_key_absent(app, client, admin_headers, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "GATEWAY_HOST_KEY", str(tmp_path / "absent.key"))
    body = client.get("/api/settings/gateway", headers=admin_headers).get_json()
    assert body["data"]["hostKeyFingerprint"] == ""


@pytest.mark.parametrize("missing", ["", "gateway_host_rsa.key"])
def test_host_key_fingerprint_never_raises(missing):
    assert isinstance(host_key_fingerprint(missing), str)
