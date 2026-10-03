"""需求③硬约束：**只有管理员**能添加机器、设置访问权限和其他设置。

这个文件是需求③的验收证据，不能删改。

判定口径：
* **有副作用的写操作**（增/删/改机器、账号、授权、策略、规则、用户、角色、设置、
  终止会话）一律只允许 admin —— 就是下面 ``WRITE_ENDPOINTS``。
* **无副作用的试算/模拟**（``/api/grants/preview``、``/api/policies/<id>/test``、
  ``/api/policies/evaluate``）按对应的 ``*:view`` 权限开放，见
  ``SIMULATION_ENDPOINTS`` —— 它们不改任何状态，读得到策略的人就能试算。
* ``/api/sessions/<id>/terminate`` 是写操作，但按设计属于 ``session:terminate``
  权限（ops 角色内置），不是 admin 专属，因此单独用
  ``test_session_terminate_follows_permission`` 验收。
"""

from __future__ import annotations

import pytest

from tests.conftest import auth, token_of

WRITE_ENDPOINTS = [
    ("post", "/api/hosts", {"name": "hack-01", "address": "10.0.0.9"}),
    ("put", "/api/hosts/1", {"name": "hack-01"}),
    ("delete", "/api/hosts/1", None),
    ("post", "/api/host-groups", {"name": "hack-group"}),
    ("put", "/api/host-groups/1", {"name": "hack-group"}),
    ("delete", "/api/host-groups/1", None),
    ("post", "/api/hosts/1/accounts", {"name": "root", "username": "root", "password": "x"}),
    ("put", "/api/hosts/1/accounts/1", {"name": "root"}),
    ("delete", "/api/hosts/1/accounts/1", None),
    ("post", "/api/grants", {"userId": 1, "hostId": 1}),
    ("put", "/api/grants/1", {"canLogin": True}),
    ("delete", "/api/grants/1", None),
    ("post", "/api/grants/batch", {"userIds": [1], "hostIds": [1]}),
    ("post", "/api/policies", {"name": "hack-policy", "defaultAction": "allow"}),
    ("put", "/api/policies/1", {"name": "hack-policy"}),
    ("delete", "/api/policies/1", None),
    ("post", "/api/policies/reset-builtin", None),
    ("post", "/api/policy-rules", {"policyId": 1, "pattern": ".*", "action": "allow"}),
    ("put", "/api/policy-rules/1", {"pattern": ".*"}),
    ("delete", "/api/policy-rules/1", None),
    ("post", "/api/users", {"username": "hacker", "roleId": 2, "password": "Str0ngPass!"}),
    ("put", "/api/users/1", {"displayName": "hacked"}),
    ("delete", "/api/users/1", None),
    ("post", "/api/users/1/password", {"newPassword": "Str0ngPass!"}),
    ("post", "/api/users/1/unlock", None),
    ("post", "/api/roles", {"code": "hack", "name": "hack", "permissions": []}),
    ("put", "/api/roles/1", {"name": "hacked"}),
    ("delete", "/api/roles/1", None),
    ("put", "/api/settings", {"site_name": "hacked"}),
    ("post", "/api/settings/reset", None),
    ("post", "/api/settings/maintenance/reconcile", None),
    ("post", "/api/settings/maintenance/seed", None),
    # 审计清除属于高危写操作，同样只有管理员可用（见 tests/test_api_purge.py）
    ("post", "/api/audits/delete", {"all": True}),
    ("post", "/api/commands/delete", {"all": True}),
    ("post", "/api/sessions/delete", {"all": True}),
]

#: 无副作用试算端点：有 view 权限的非管理员应当可用。
SIMULATION_ENDPOINTS = [
    ("post", "/api/grants/preview", {"command": "ls"}),
    ("post", "/api/policies/1/test", {"command": "ls"}),
    ("post", "/api/policies/evaluate", {"command": "ls"}),
]


@pytest.fixture()
def ops_headers(client, make_user):
    make_user("ops-user", "ops")
    return auth(token_of(client, "ops-user", "User1234"))


@pytest.fixture()
def viewer_headers(client, make_user):
    make_user("viewer-user", "viewer")
    return auth(token_of(client, "viewer-user", "User1234"))


def _call(client, method, url, payload, headers):
    func = getattr(client, method)
    if payload is None:
        return func(url, headers=headers)
    return func(url, json=payload, headers=headers)


@pytest.mark.parametrize("method,url,payload", WRITE_ENDPOINTS)
def test_ops_cannot_write_anything(client, ops_headers, method, url, payload):
    resp = _call(client, method, url, payload, ops_headers)
    assert resp.status_code == 403, f"{method.upper()} {url} 应被拒绝，实际 {resp.status_code}"
    assert resp.get_json()["success"] is False


@pytest.mark.parametrize("method,url,payload", WRITE_ENDPOINTS)
def test_viewer_cannot_write_anything(client, viewer_headers, method, url, payload):
    resp = _call(client, method, url, payload, viewer_headers)
    assert resp.status_code == 403, f"{method.upper()} {url} 应被拒绝，实际 {resp.status_code}"


@pytest.mark.parametrize("method,url,payload", WRITE_ENDPOINTS)
def test_anonymous_cannot_write_anything(client, method, url, payload):
    resp = _call(client, method, url, payload, None)
    assert resp.status_code in (401, 403), f"{method.upper()} {url} 未鉴权，实际 {resp.status_code}"


@pytest.mark.parametrize("method,url,payload", SIMULATION_ENDPOINTS)
def test_ops_can_use_readonly_simulators(client, ops_headers, method, url, payload):
    """试算端点没有副作用，有 view 权限的 ops 应该能用；关键是不能是 403。"""
    resp = _call(client, method, url, payload, ops_headers)
    assert resp.status_code == 200, f"{method.upper()} {url} 实际 {resp.status_code}"
    assert resp.get_json()["success"] is True


@pytest.mark.parametrize("method,url,payload", SIMULATION_ENDPOINTS)
def test_anonymous_cannot_use_simulators(client, method, url, payload):
    resp = _call(client, method, url, payload, None)
    assert resp.status_code in (401, 403), f"{method.upper()} {url} 实际 {resp.status_code}"


def test_session_terminate_follows_permission(client, ops_headers, viewer_headers):
    """终止会话按 ``session:terminate`` 授权，不是 admin 专属。"""
    # 不存在的会话：ops 有权限 -> 404（而不是被拦成 403）
    assert client.post("/api/sessions/9999/terminate", headers=ops_headers).status_code == 404
    # viewer 没有 session:terminate -> 403
    assert client.post("/api/sessions/9999/terminate", headers=viewer_headers).status_code == 403


def test_host_groups_and_policy_rules_are_reachable(client, admin_headers):
    """双蓝图模块（host-groups / policy-rules）必须真的注册，不能静默 404。"""
    for url in (
        "/api/host-groups",
        "/api/host-groups/options",
        "/api/policy-rules",
    ):
        resp = client.get(url, headers=admin_headers)
        assert resp.status_code == 200, f"GET {url} 实际 {resp.status_code}"
        assert resp.get_json()["success"] is True

    created = client.post("/api/host-groups", json={"name": "回归分组"}, headers=admin_headers)
    assert created.status_code in (200, 201), created.get_json()

    policy = client.get("/api/policies/options", headers=admin_headers).get_json()["data"]
    # /options 统一返回 Pro 风格的 {label, value}
    policy_id = policy[0]["value"]
    rule = client.post(
        "/api/policy-rules",
        json={"policyId": policy_id, "pattern": "^uptime$", "action": "allow", "matchType": "regex"},
        headers=admin_headers,
    )
    assert rule.status_code in (200, 201), rule.get_json()


def test_ops_can_read_asset_inventory(client, ops_headers):
    assert client.get("/api/hosts", headers=ops_headers).status_code == 200
    assert client.get("/api/grants", headers=ops_headers).status_code == 200
    assert client.get("/api/policies", headers=ops_headers).status_code == 200
    assert client.get("/api/sessions", headers=ops_headers).status_code == 200
    assert client.get("/api/audits", headers=ops_headers).status_code == 200


def test_ops_cannot_read_user_or_setting_management(client, ops_headers):
    assert client.get("/api/users", headers=ops_headers).status_code == 403
    assert client.get("/api/roles", headers=ops_headers).status_code == 403
    assert client.get("/api/settings", headers=ops_headers).status_code == 403


def test_viewer_cannot_see_audit_log(client, viewer_headers):
    assert client.get("/api/audits", headers=viewer_headers).status_code == 403


def test_auditor_can_see_audit_log_but_not_write(client, make_user):
    make_user("auditor-user", "auditor")
    headers = auth(token_of(client, "auditor-user", "User1234"))
    assert client.get("/api/audits", headers=headers).status_code == 200
    assert client.get("/api/hosts", headers=headers).status_code == 200
    resp = client.post("/api/hosts", json={"name": "nope", "address": "1.1.1.1"}, headers=headers)
    assert resp.status_code == 403


def test_admin_can_create_host_end_to_end(client, admin_headers):
    resp = client.post(
        "/api/hosts",
        json={"name": "prod-web-01", "address": "10.10.10.11", "port": 22, "description": "生产 Web"},
        headers=admin_headers,
    )
    assert resp.status_code in (200, 201), resp.get_json()
    body = resp.get_json()
    assert body["success"] is True
    host_id = body["data"]["id"]

    listing = client.get("/api/hosts", headers=admin_headers).get_json()
    assert any(item["name"] == "prod-web-01" for item in listing["data"])
    assert listing["total"] >= 1

    detail = client.get(f"/api/hosts/{host_id}", headers=admin_headers).get_json()["data"]
    assert detail["address"] == "10.10.10.11"


def test_admin_can_update_settings(client, admin_headers):
    resp = client.put("/api/settings", json={"site_name": "AutoOps 堡垒机"}, headers=admin_headers)
    assert resp.status_code == 200
    values = client.get("/api/settings", headers=admin_headers).get_json()["data"]["values"]
    assert values["site_name"] == "AutoOps 堡垒机"


def test_admin_self_delete_is_refused(client, app, admin_headers):
    from app.models import User
    from app.extensions import db

    with app.app_context():
        admin_id = User.query.filter_by(username="admin").first().id

    resp = client.delete(f"/api/users/{admin_id}", headers=admin_headers)
    assert resp.status_code == 400
    _ = db


def test_dashboard_mine_for_regular_user(client, ops_headers):
    resp = client.get("/api/dashboard/mine", headers=ops_headers)
    assert resp.status_code == 200
    assert "targets" in resp.get_json()["data"]


def test_dashboard_overview_for_admin(client, admin_headers):
    resp = client.get("/api/dashboard/overview", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert "cards" in data
    assert "trend" in data


def test_terminal_targets_needs_a_real_grant_to_expose_its_shape(
    client, make_user, make_host, make_account, make_grant
):
    """回归守卫：有授权的用户拉终端选单必须 200，且条目带 port/osType。

    曾经 `_target_payload` 直接读 accessible_targets() 的内部条目（没有 port/osType），
    只要用户真有授权就 KeyError → 500。空授权时循环体不执行，所以旧的 5xx 冒烟抓不到。
    """
    user_id = make_user("term-user", "ops")
    host_id = make_host("term-target-01", "10.0.0.9", 22)
    account_id = make_account(host_id, "root", "root")
    make_grant(user_id, host_id, account_id=account_id)
    headers = auth(token_of(client, "term-user", "User1234"))

    resp = client.get("/api/terminal/targets", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    items = resp.get_json()["data"]
    assert len(items) == 1
    item = items[0]
    assert item["hostName"] == "term-target-01"
    assert item["address"] == "10.0.0.9"
    assert item["port"] == 22
    assert "osType" in item and item["osType"] == "linux"
    assert item["accounts"] == [{"id": account_id, "name": "root", "username": "root"}]

    check = client.post(f"/api/terminal/targets/{host_id}/check", headers=headers)
    assert check.status_code == 200, check.get_data(as_text=True)
    body = check.get_json()["data"]
    assert body["allowed"] is True
    assert body["port"] == 22
    assert body["accounts"][0]["name"] == "root"
