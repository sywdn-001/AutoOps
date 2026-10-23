"""RDP 网关扩展模块的接口回归测试。

把旧的 ``tests/test_rdp_gateway_api.py`` 迁移到模块级：先按契约打包
``modules-src/rdp-gateway``，经 ``POST /api/modules/install`` 安装、启用，
再通过模块统一前缀 ``/api/modules/rdp-gateway/api/...`` 打真实接口。

测试只覆盖不依赖真实远程桌面的路径：``/status``、``/settings``、``/hosts``、
``/sessions``。浏览器画面桥（``/connect``）依赖真实 Windows 会话，这里不测。
为了保持测试快速，打包时跳过 ``bin/``（可选二进制）与 ``tests/`` 自身；
模块在缺 exe 时也会正常加载，这正是 setup() 必须容忍的场景。

本文件位于 ``modules-src/rdp-gateway`` 下，不能被 ``bastion-backend/tests/conftest.py``
覆盖，因此自己创建应用与数据（仍然复用宿主同一套 ``TestConfig``）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app import create_app
from app.config import TestConfig
from app.crypto import encrypt
from app.extensions import db
from app.models import AuditLog, Host, HostAccount, HostGroup, SessionRecord
from app.modules import init_modules, spec

MODULE_ID = "rdp-gateway"
MODULE_API = f"/api/modules/{MODULE_ID}/api"
ACK = "我知道这是实验性功能，使用不当的话也许会造成系统损毁"
SOURCE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def package_bytes() -> bytes:
    """按契约把模块源码目录打成 ZIP（与应用内安装走同一套文件遍历规则）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, full in spec.iter_source_files(SOURCE_DIR):
            if relative.startswith(("bin/", "tests/")):
                continue
            archive.write(full, relative)
    return buffer.getvalue()


@pytest.fixture()
def gateway(tmp_path, package_bytes):
    """独立数据库 + 模块目录，安装并启用模块，返回 (client, admin_headers, app)。"""
    db_path = tmp_path / "test.db"

    class TestCfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False, "timeout": 30}
        }
        MODULES_DIR = str(tmp_path / "modules")
        MODULES_SRC_DIR = ""

    application = create_app(TestCfg)
    with application.app_context():
        init_modules(application)

    client = application.test_client()
    login = client.post(
        "/api/login/account",
        json={"username": "admin", "password": "admin123", "type": "account", "autoLogin": True},
    ).get_json()
    headers = {"Authorization": f"Bearer {login['data']['token']}"}

    response = client.post(
        "/api/modules/install",
        data={
            "file": (io.BytesIO(package_bytes), "rdp-gateway-1.2.0.mod.ops_ext"),
            "ack": ACK,
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()["data"]
    assert payload["id"] == MODULE_ID
    assert payload["loaded"] is True, payload
    assert payload["errors"] == []

    enabled = client.post(f"/api/modules/{MODULE_ID}/enable", headers=headers)
    assert enabled.status_code == 200, enabled.get_data(as_text=True)

    try:
        yield client, headers, application
    finally:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()


def _make_host(app, *, name, address, port, protocol, os_type) -> int:
    with app.app_context():
        group = HostGroup.query.first()
        host = Host(
            name=name,
            address=address,
            port=port,
            protocol=protocol,
            os_type=os_type,
            group_id=group.id if group else None,
            status="active",
            description="",
        )
        db.session.add(host)
        db.session.commit()
        return host.id


def _make_account(app, host_id, *, name, username, password="s3cret") -> int:
    with app.app_context():
        account = HostAccount(
            host_id=host_id,
            name=name,
            username=username,
            auth_type="password",
            secret_enc=encrypt(password),
            description="",
        )
        db.session.add(account)
        db.session.commit()
        return account.id


def _data(response):
    return (response.get_json() or {}).get("data")


def test_status_and_settings(gateway):
    client, headers, _app = gateway

    response = client.get(f"{MODULE_API}/status", headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    data = _data(response)
    assert data["moduleId"] == MODULE_ID
    assert data["port"] == 4489
    assert data["enabled"] is True
    assert data["running"] is False
    assert isinstance(data["binary"], str) and data["binary"]
    assert isinstance(data["binaryExists"], bool)
    assert data["connectCommand"].startswith("mstsc /v:")
    assert data["currentUser"] == "admin"
    assert data["activeSessions"] == 0

    settings = _data(client.get(f"{MODULE_API}/settings", headers=headers))
    assert settings["settings"] == {"enabled": True, "port": 4489, "autoStart": False}
    assert settings["minPort"] == 1 and settings["maxPort"] == 65535

    updated = client.post(
        f"{MODULE_API}/settings", json={"port": 4490, "autoStart": True}, headers=headers
    )
    assert updated.status_code == 200, updated.get_data(as_text=True)
    data = _data(updated)
    assert data["settings"]["port"] == 4490
    assert data["settings"]["autoStart"] is True

    invalid = client.post(f"{MODULE_API}/settings", json={"port": 70000}, headers=headers)
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "INVALID_ARGUMENT"

    # 未登录访问模块 API 仍然是 401
    assert client.get(f"{MODULE_API}/status").status_code == 401


def test_hosts_only_lists_rdp_targets(gateway):
    client, headers, app = gateway
    rdp_host = _make_host(
        app, name="rdp-box", address="10.20.30.40", port=3389, protocol="rdp", os_type="windows"
    )
    _make_account(app, rdp_host, name="Administrator", username="Administrator")
    ssh_host = _make_host(
        app, name="linux-box", address="10.1.1.2", port=22, protocol="ssh", os_type="linux"
    )
    _make_account(app, ssh_host, name="root", username="root")

    response = client.get(f"{MODULE_API}/hosts", headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    data = _data(response)
    names = [row["hostName"] for row in data["hosts"]]
    assert "rdp-box" in names, "RDP 主机要出现在选单里"
    assert "linux-box" not in names, "SSH 主机不能混进 RDP 选单"

    row = next(item for item in data["hosts"] if item["hostName"] == "rdp-box")
    assert row["hostId"] == rdp_host
    assert row["port"] == 3389
    assert row["accounts"][0]["accountId"]
    assert row["accounts"][0]["username"] == "Administrator"


def test_open_session_writes_record_and_audit(gateway):
    client, headers, app = gateway
    host_id = _make_host(
        app, name="rdp-open", address="10.20.30.41", port=3389, protocol="rdp", os_type="windows"
    )
    account_id = _make_account(app, host_id, name="administrator", username="administrator")

    response = client.post(
        f"{MODULE_API}/sessions",
        json={"hostId": host_id, "accountId": account_id},
        headers=headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    data = _data(response)
    assert data["sid"]
    assert data["accountUsername"] == "administrator"
    assert data["credential"]["address"] == "10.20.30.41"
    assert data["credential"]["port"] == 3389
    assert data["credential"]["password"] == "s3cret"

    with app.app_context():
        record = SessionRecord.query.filter_by(sid=data["sid"]).first()
        assert record is not None
        assert record.source == "gateway" and record.protocol == "rdp"
        assert record.status == "active"
        assert record.host_name == "rdp-open"
        audit = AuditLog.query.filter_by(action="rdp_gateway_open").first()
        assert audit is not None and "rdp-open" in (audit.message or "")

    closed = client.post(
        f"{MODULE_API}/sessions/{data['sid']}/close", headers=headers
    )
    assert closed.status_code == 200, closed.get_data(as_text=True)
    assert _data(closed)["closed"] is True
    with app.app_context():
        record = SessionRecord.query.filter_by(sid=data["sid"]).first()
        assert record.status == "closed"
        assert record.end_reason == "用户在模块页面断开"


def test_open_session_rejects_missing_host(gateway):
    client, headers, _app = gateway
    response = client.post(f"{MODULE_API}/sessions", json={}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["code"] == "INVALID_ARGUMENT"
