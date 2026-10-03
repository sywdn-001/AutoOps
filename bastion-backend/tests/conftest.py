"""pytest 公共夹具。

原则：夹具只返回**主键/令牌**这类可跨 app_context 传递的纯数据，
不把 ORM 对象带出 with 块（避免 DetachedInstanceError 污染测试结论）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models import Grant, Host, HostAccount, HostGroup, Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"

    class TestCfg(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False, "timeout": 30}
        }

    application = create_app(TestCfg)
    try:
        yield application
    finally:
        with application.app_context():
            _db.session.remove()
            _db.engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# 登录工具
# ---------------------------------------------------------------------------
def pro_login(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD):
    """POST /api/login/account，返回**完整信封** {success,message,data}。"""
    resp = client.post(
        "/api/login/account",
        json={"username": username, "password": password, "type": "account", "autoLogin": True},
    )
    return resp.get_json()


def pro_login_data(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD) -> dict:
    body = pro_login(client, username, password) or {}
    return body.get("data") or {}


def token_of(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD) -> str:
    data = pro_login_data(client, username, password)
    assert data.get("token"), f"登录失败：{data}"
    return data["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client):
    return token_of(client)


@pytest.fixture()
def admin_headers(admin_token):
    return auth(admin_token)


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------
@pytest.fixture()
def make_user(app):
    def _make(username, role_code="viewer", password="User1234", **kwargs):
        with app.app_context():
            role = Role.query.filter_by(code=role_code).first()
            assert role is not None, f"角色不存在：{role_code}"
            user = User(
                username=username,
                password_hash=hash_password(password),
                display_name=username,
                role_id=role.id,
                status=kwargs.pop("status", "active"),
                **kwargs,
            )
            _db.session.add(user)
            _db.session.commit()
            return user.id

    return _make


@pytest.fixture()
def make_host(app):
    def _make(name="web-01", address="127.0.0.1", port=22, **kwargs):
        with app.app_context():
            group = HostGroup.query.first()
            host = Host(
                name=name,
                address=address,
                port=port,
                protocol=kwargs.pop("protocol", "ssh"),
                os_type=kwargs.pop("os_type", "linux"),
                group_id=group.id if group else None,
                status=kwargs.pop("status", "active"),
                description=kwargs.pop("description", ""),
                **kwargs,
            )
            _db.session.add(host)
            _db.session.commit()
            return host.id

    return _make


@pytest.fixture()
def make_account(app):
    def _make(host_id, name="root", username="root", password="s3cret", auth_type="password", **kwargs):
        with app.app_context():
            account = HostAccount(
                host_id=host_id,
                name=name,
                username=username,
                auth_type=auth_type,
                secret_enc=encrypt(password) if password else "",
                description=kwargs.pop("description", ""),
                **kwargs,
            )
            _db.session.add(account)
            _db.session.commit()
            return account.id

    return _make


@pytest.fixture()
def make_grant(app):
    def _make(user_id, host_id, account_id=None, policy_id=None, **kwargs):
        with app.app_context():
            grant = Grant(
                user_id=user_id,
                host_id=host_id,
                host_account_id=account_id,
                policy_id=policy_id,
                can_login=kwargs.pop("can_login", True),
                can_sftp=kwargs.pop("can_sftp", False),
                can_upload=kwargs.pop("can_upload", False),
                can_download=kwargs.pop("can_download", False),
                can_port_forward=kwargs.pop("can_port_forward", False),
                can_webterm=kwargs.pop("can_webterm", True),
                weekdays=kwargs.pop("weekdays", list(ALL_WEEKDAYS)),
                enabled=kwargs.pop("enabled", True),
                remark=kwargs.pop("remark", ""),
                **kwargs,
            )
            _db.session.add(grant)
            _db.session.commit()
            return grant.id

    return _make


@pytest.fixture()
def default_policy_id(app):
    with app.app_context():
        from app.models import CommandPolicy

        policy = CommandPolicy.query.filter_by(is_default=True).first()
        return policy.id if policy else None
