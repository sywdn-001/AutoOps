"""口令哈希与登入链路的回归测试。

这个文件是针对一次 P0 事故的回归：
`verify_password(password, password_hash)` 的参数在**全部调用点**被传反，
导致 92 个用例里 10 failed / 76 errors —— 现象是「任何人都无法登录」。
单测里 hash/verify 自洽、策略与授权测试全绿，唯独没有一条用例真的走完
「管理员建号 -> 新号登录」这条路，所以整站登录被静默破坏。

结论（写进 SOP）：凡是有「创建凭据」的接口，必须配一条「用刚创建的凭据登入」的用例。
"""

from __future__ import annotations

import logging

from tests.conftest import auth, pro_login, pro_login_data, token_of


def test_hash_and_verify_roundtrip():
    from app.security import hash_password, verify_password

    stored = hash_password("User1234")
    assert stored.startswith("scrypt:")
    assert verify_password("User1234", stored) is True
    assert verify_password("user1234", stored) is False
    assert verify_password("", stored) is False


def test_verify_password_rejects_swapped_arguments(caplog):
    """参数传反必须「可观测地失败」，不能静默返回 False。"""
    from app.security import hash_password, verify_password

    stored = hash_password("User1234")
    with caplog.at_level(logging.WARNING):
        assert verify_password(stored, "User1234") is False
    assert any("参数顺序传反" in record.message for record in caplog.records)


def test_verify_password_handles_empty_and_garbage():
    from app.security import verify_password

    assert verify_password("x", "") is False
    assert verify_password("x", None) is False
    assert verify_password("x", "not-a-hash") is False


def test_admin_created_user_can_actually_log_in(client, admin_headers):
    """P0 回归：管理员建号之后，新账号必须能立刻用密码登录。"""
    resp = client.post(
        "/api/users",
        json={
            "username": "e2e-user",
            "displayName": "端到端用户",
            "roleId": None,
            "roleCode": "ops",
            "password": "Str0ngPass!",
        },
        headers=admin_headers,
    )
    assert resp.status_code in (200, 201), resp.get_json()

    # 1) Pro 兼容端点（前端登录页走这条）
    data = pro_login_data(client, "e2e-user", "Str0ngPass!")
    assert data.get("token"), f"新建账号无法登录：{data}"
    assert data["status"] == "ok"
    assert data["currentAuthority"] == "ops"

    # 2) 原生端点
    native = client.post(
        "/api/auth/login", json={"username": "e2e-user", "password": "Str0ngPass!"}
    )
    body = native.get_json()
    assert native.status_code == 200 and body["success"] is True, body
    assert body["data"]["token"]
    assert body["data"]["isAdmin"] is False

    # 3) 拿到的令牌真的能用
    me = client.get("/api/auth/me", headers=auth(data["token"]))
    assert me.status_code == 200
    assert me.get_json()["data"]["username"] == "e2e-user"


def test_wrong_password_is_rejected_for_created_user(client, admin_headers):
    client.post(
        "/api/users",
        json={"username": "e2e-bad", "roleCode": "ops", "password": "Str0ngPass!"},
        headers=admin_headers,
    )
    body = pro_login(client, "e2e-bad", "WrongPass1!")
    assert body["data"]["status"] == "error"
    assert body["data"]["currentAuthority"] == "guest"


def test_seeded_admin_can_log_in(client):
    assert token_of(client, "admin", "admin123")


def test_password_change_roundtrip(client, admin_headers):
    """改密之后：新口令能登录，旧口令不能。"""
    client.post(
        "/api/users",
        json={"username": "chg-user", "roleCode": "ops", "password": "Str0ngPass!"},
        headers=admin_headers,
    )
    token = token_of(client, "chg-user", "Str0ngPass!")
    resp = client.post(
        "/api/auth/password",
        json={"oldPassword": "Str0ngPass!", "newPassword": "An0therPass!"},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.get_json()
    assert token_of(client, "chg-user", "An0therPass!")
    assert pro_login_data(client, "chg-user", "Str0ngPass!").get("token") is None
