"""认证 / 会话管理单测：登录、锁定、令牌注销、Pro 兼容协议。"""

from __future__ import annotations

from app.extensions import db
from app.models import User
from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    auth,
    pro_login,
    pro_login_data,
    token_of,
)


def test_admin_login_success(client):
    data = pro_login_data(client)
    assert data["status"] == "ok"
    assert data["token"]
    assert data["currentAuthority"] == "admin"
    assert data["user"]["username"] == ADMIN_USERNAME


def test_login_wrong_password_reports_remaining_attempts(client):
    body = pro_login(client, ADMIN_USERNAME, "wrong-password")
    assert body["success"] is True
    assert body["data"]["status"] == "error"
    assert body["data"]["currentAuthority"] == "guest"
    message = body["message"]
    assert "还剩" in message or "剩余" in message, message
    assert "4" in message, message  # 5 次上限 - 1 次失败 = 还剩 4 次


def test_login_unknown_user_rejected(client):
    assert pro_login_data(client, "nobody", "whatever")["status"] == "error"


def test_login_locks_after_max_failures(client, app):
    for _ in range(5):
        pro_login(client, ADMIN_USERNAME, "bad-password")
    body = pro_login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert body["data"]["status"] == "error"
    assert "锁定" in body["message"]

    with app.app_context():
        user = User.query.filter_by(username=ADMIN_USERNAME).first()
        assert user.locked_until is not None


def test_custom_login_endpoint_returns_token_and_permissions(client):
    resp = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["data"]["token"]
    assert body["data"]["isAdmin"] is True
    assert "*" in body["data"]["permissions"]


def test_login_requires_both_fields(client):
    resp = client.post("/api/auth/login", json={"username": "admin"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_me_endpoint_requires_token(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.get_json()["success"] is False


def test_me_endpoint_with_token(client, admin_headers):
    resp = client.get("/api/auth/me", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["username"] == ADMIN_USERNAME
    assert data["isAdmin"] is True
    assert "*" in data["permissions"]
    assert "recentActivities" in data


def test_pro_current_user_shape(client, admin_headers):
    resp = client.get("/api/currentUser", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    for key in ("name", "access", "userid", "title", "group", "tags"):
        assert key in data, key
    assert data["access"] == "admin"


def test_logout_revokes_token(client):
    token = token_of(client)
    headers = auth(token)
    assert client.get("/api/currentUser", headers=headers).status_code == 200

    assert client.post("/api/login/outLogin", headers=headers).status_code == 200
    assert client.get("/api/currentUser", headers=headers).status_code == 401, "登出后的令牌必须失效"


def test_change_password_rejects_weak_password(client, admin_headers):
    resp = client.post(
        "/api/auth/password",
        json={"oldPassword": ADMIN_PASSWORD, "newPassword": "123"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "WEAK_PASSWORD"


def test_change_password_rejects_wrong_old_password(client, admin_headers):
    resp = client.post(
        "/api/auth/password",
        json={"oldPassword": "not-my-password", "newPassword": "Str0ngPass!"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_PASSWORD"


def test_change_password_happy_path_clears_must_change(client, app, admin_headers):
    with app.app_context():
        assert User.query.filter_by(username=ADMIN_USERNAME).first().must_change_password is True

    resp = client.post(
        "/api/auth/password",
        json={"oldPassword": ADMIN_PASSWORD, "newPassword": "Str0ngPass!"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    with app.app_context():
        assert User.query.filter_by(username=ADMIN_USERNAME).first().must_change_password is False

    assert pro_login_data(client, ADMIN_USERNAME, "Str0ngPass!")["status"] == "ok"
    assert pro_login_data(client, ADMIN_USERNAME, ADMIN_PASSWORD)["status"] == "error"


def test_disabled_user_cannot_login(client, make_user):
    make_user("frozen", "ops", status="disabled")
    body = pro_login(client, "frozen", "User1234")
    assert body["data"]["status"] == "error"
    assert "停用" in body["message"]


def test_notices_endpoint_returns_login_audit(client, admin_headers):
    resp = client.get("/api/notices", headers=admin_headers)
    assert resp.status_code == 200
    payload = resp.get_json()["data"]
    assert payload["total"] >= 1
    assert any("admin" in item["title"] for item in payload["list"])


def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_unknown_api_returns_json_envelope(client):
    resp = client.get("/api/definitely-not-here")
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_login_is_audited(client, app):
    from app.models import AuditLog

    pro_login(client, ADMIN_USERNAME, "wrong")
    pro_login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    with app.app_context():
        rows = AuditLog.query.filter_by(category="auth").all()
    assert rows, "登录行为必须写入审计日志"
    assert any(row.result == "failure" for row in rows), "登录失败必须留痕"
    assert any(row.result == "success" for row in rows), "登录成功必须留痕"


def test_failed_login_counter_resets_on_success(client, app):
    pro_login(client, ADMIN_USERNAME, "bad-1")
    pro_login(client, ADMIN_USERNAME, "bad-2")
    with app.app_context():
        assert User.query.filter_by(username=ADMIN_USERNAME).first().failed_attempts == 2

    pro_login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    with app.app_context():
        user = User.query.filter_by(username=ADMIN_USERNAME).first()
        assert user.failed_attempts == 0
        assert user.locked_until is None
        assert user.last_login_at is not None


_ = db
