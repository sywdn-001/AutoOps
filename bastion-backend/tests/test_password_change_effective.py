"""改口令必须真的生效（回归）。

用户报障：管理员重置口令、用户自助改口令、AI 重置口令三条路，改完新口令登不进去。
本文件把三条路都钉在「新口令必须能登录、旧口令必须失效、must_change_password 必须被正确处理」上。
"""

from __future__ import annotations

from tests.conftest import auth, pro_login, token_of


def _login_ok(client, username: str, password: str) -> bool:
    """Pro 兼容登录接口的信封 success 只表示「请求被处理」，结果在 data.status/token 上。"""
    body = pro_login(client, username, password) or {}
    data = body.get("data") or {}
    return data.get("status") == "ok" and bool(data.get("token"))


def test_admin_reset_password_takes_effect_for_login(client, admin_headers, make_user):
    """管理员走 POST /api/users/<id>/password（AI 的 reset_user_password 工具也打这里）。"""
    user_id = make_user("pw-admin-reset", password="Oldpass123")

    resp = client.post(
        f"/api/users/{user_id}/password",
        json={"password": "Newpass123"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()

    assert _login_ok(client, "pw-admin-reset", "Newpass123") is True, "重置后的新口令必须能登录"
    assert _login_ok(client, "pw-admin-reset", "Oldpass123") is False, "旧口令必须失效"


def test_self_change_password_takes_effect_for_login(client, make_user):
    """用户自助改口令：POST /api/auth/password（前端「个人设置」走这条）。"""
    make_user("pw-self", password="Oldpass123")
    token = token_of(client, "pw-self", "Oldpass123")

    resp = client.post(
        "/api/auth/password",
        json={"oldPassword": "Oldpass123", "newPassword": "Newpass123"},
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.get_json()

    assert _login_ok(client, "pw-self", "Newpass123") is True, "自助改密后的新口令必须能登录"
    assert _login_ok(client, "pw-self", "Oldpass123") is False, "旧口令必须失效"


def test_ai_reset_password_tool_payload_takes_effect(client, admin_headers, make_user):
    """AI 工具 reset_user_password 的报文就是 {"password": ...}（tools.py 的路径模板）。"""
    user_id = make_user("pw-ai-reset", password="Oldpass123")

    resp = client.post(
        f"/api/users/{user_id}/password",
        json={"password": "Aipass123"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    assert _login_ok(client, "pw-ai-reset", "Aipass123") is True


def test_reset_password_keeps_must_change_flag_but_still_allows_login(client, admin_headers, make_user):
    """重置后既要能登进（拿令牌），也要把「下次登录须改密」的标记立起来。"""
    user_id = make_user("pw-flag", password="Oldpass123", must_change_password=False)
    client.post(f"/api/users/{user_id}/password", json={"password": "Newpass123"}, headers=admin_headers)

    body = pro_login(client, "pw-flag", "Newpass123") or {}
    data = body.get("data") or {}
    assert data.get("status") == "ok", body
    assert data.get("token"), body
    assert (data.get("user") or {}).get("mustChangePassword") is True, f"重置后应要求下次改密：{data}"


def test_update_user_endpoint_ignores_password_field(client, admin_headers, make_user):
    """PUT /api/users/<id> 不处理 password —— 前端编辑弹窗若把口令塞进这里就会静默失效。"""
    user_id = make_user("pw-put", password="Oldpass123")

    resp = client.put(
        f"/api/users/{user_id}",
        json={"displayName": "改个名字", "password": "Newpass123"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()

    assert _login_ok(client, "pw-put", "Newpass123") is False, (
        "PUT /api/users/<id> 收到 password 却没改库：这正是「改口令不生效」的静默路径；"
        "要么在接口里处理它，要么在文档/前端里杜绝这条路径"
    )
