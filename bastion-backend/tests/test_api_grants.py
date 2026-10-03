"""主机授权接口（谁能碰哪台机器）的**正常路径**回归测试。

为什么单独一个文件：`POST /api/grants` 曾经 500（`'str' object is not callable`，
`Grant.host_account_username` 是 @property 却被当方法调用），而 225 个用例全绿都没抓到——
因为既有用例只验证了「非管理员必须 403」，没有一条真正以管理员身份走通授权的增删改查。
SOP：**每个写接口至少要有一条 admin 正常路径用例**，否则契约坏了也没人知道。
"""

from __future__ import annotations

from tests.conftest import ALL_WEEKDAYS


def _payload(user_id, host_id, **overrides):
    data = {
        "userId": user_id,
        "hostId": host_id,
        "canLogin": True,
        "canWebterm": True,
        "canSftp": False,
        "canUpload": False,
        "canDownload": False,
        "weekdays": ALL_WEEKDAYS,
    }
    data.update(overrides)
    return data


def test_admin_creates_grant_via_api_and_reads_it_back(
    client, admin_headers, make_user, make_host, make_account, default_policy_id
):
    user_id = make_user("grant-ops", role_code="ops")
    host_id = make_host(name="grant-host-01", address="10.1.1.1")
    account_id = make_account(host_id, name="root", username="root")

    resp = client.post(
        "/api/grants",
        json=_payload(
            user_id,
            host_id,
            hostAccountId=account_id,
            policyId=default_policy_id,
            maxSessions=2,
        ),
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    created = body["data"]
    assert created["userId"] == user_id
    assert created["hostId"] == host_id
    assert created["hostAccountId"] == account_id
    # 账号名与账号登录名必须都在（旧 bug 正是在这里炸掉的）
    assert created["accountName"] == "root"
    assert created["accountUsername"] == "root"
    assert created["hostName"] == "grant-host-01"
    assert created["hostAddress"] == "10.1.1.1"
    assert created["policyId"] == default_policy_id
    assert created["inWindow"] is True
    assert created["maxSessions"] == 2
    assert created["activeSessions"] == 0
    grant_id = created["id"]

    listed = client.get("/api/grants", headers=admin_headers)
    assert listed.status_code == 200, listed.get_data(as_text=True)
    page = listed.get_json()
    assert page["total"] == 1
    assert page["data"][0]["id"] == grant_id
    assert page["data"][0]["accountUsername"] == "root"

    single = client.get(f"/api/grants/{grant_id}", headers=admin_headers)
    assert single.status_code == 200
    assert single.get_json()["data"]["id"] == grant_id

    matrix = client.get("/api/grants/matrix", headers=admin_headers)
    assert matrix.status_code == 200, matrix.get_data(as_text=True)
    matrix_body = matrix.get_json()
    row = next(item for item in matrix_body["data"] if item["userId"] == user_id)
    assert row["grantCount"] == 1
    cell = next(item for item in row["cells"] if item["hostId"] == host_id)
    assert cell["granted"] is True
    assert cell["grantId"] == grant_id


def test_wildcard_grant_payload_uses_placeholder_account(
    client, admin_headers, make_user, make_host
):
    user_id = make_user("grant-wild", role_code="ops")
    host_id = make_host(name="grant-host-02", address="10.1.1.2")

    resp = client.post("/api/grants", json=_payload(user_id, host_id), headers=admin_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    created = resp.get_json()["data"]
    assert created["hostAccountId"] is None
    assert created["accountName"] == "（全部账号）"
    assert created["accountUsername"] == "*"


def test_grant_duplicate_is_rejected_by_api(
    client, admin_headers, make_user, make_host, make_account
):
    user_id = make_user("grant-dup", role_code="ops")
    host_id = make_host(name="grant-host-03", address="10.1.1.3")
    account_id = make_account(host_id)

    payload = _payload(user_id, host_id, hostAccountId=account_id)
    assert client.post("/api/grants", json=payload, headers=admin_headers).status_code == 200
    duplicated = client.post("/api/grants", json=payload, headers=admin_headers)
    assert duplicated.status_code == 409
    assert duplicated.get_json()["code"] == "DUPLICATED"


def test_grant_rejects_account_of_another_host(
    client, admin_headers, make_user, make_host, make_account
):
    user_id = make_user("grant-cross", role_code="ops")
    host_a = make_host(name="grant-host-04", address="10.1.1.4")
    host_b = make_host(name="grant-host-05", address="10.1.1.5")
    account_of_b = make_account(host_b, name="root", username="root")

    resp = client.post(
        "/api/grants",
        json=_payload(user_id, host_a, hostAccountId=account_of_b),
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_ARGUMENT"


def test_grant_update_and_delete_round_trip(
    client, admin_headers, make_user, make_host, make_account
):
    user_id = make_user("grant-edit", role_code="ops")
    host_id = make_host(name="grant-host-06", address="10.1.1.6")
    account_id = make_account(host_id)

    created = client.post(
        "/api/grants",
        json=_payload(user_id, host_id, hostAccountId=account_id),
        headers=admin_headers,
    ).get_json()["data"]
    grant_id = created["id"]

    updated = client.put(
        f"/api/grants/{grant_id}",
        json={"canWebterm": False, "canDownload": True, "maxSessions": 1, "enabled": False},
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.get_data(as_text=True)
    after = updated.get_json()["data"]
    assert after["canWebterm"] is False
    assert after["canDownload"] is True
    assert after["maxSessions"] == 1
    assert after["enabled"] is False
    assert after["inWindow"] is False  # 停用后必须报「授权已被停用」
    assert "停用" in after["windowReason"]

    deleted = client.delete(f"/api/grants/{grant_id}", headers=admin_headers)
    assert deleted.status_code == 200
    assert client.get(f"/api/grants/{grant_id}", headers=admin_headers).status_code == 404
    assert client.get("/api/grants", headers=admin_headers).get_json()["total"] == 0


def test_grant_batch_creates_and_skips(
    client, admin_headers, make_user, make_host, default_policy_id
):
    user_id = make_user("grant-batch", role_code="ops")
    first = make_host(name="grant-host-07", address="10.1.1.7")
    second = make_host(name="grant-host-08", address="10.1.1.8")
    third = make_host(name="grant-host-09", address="10.1.1.9")

    first_call = client.post(
        "/api/grants/batch",
        json={"userId": user_id, "hostIds": [first, second], "policyId": default_policy_id},
        headers=admin_headers,
    )
    assert first_call.status_code == 200, first_call.get_data(as_text=True)
    summary = first_call.get_json()["data"]
    assert sorted(summary["created"]) == sorted([first, second])
    assert summary["skipped"] == []

    second_call = client.post(
        "/api/grants/batch",
        json={"userId": user_id, "hostIds": [second, third]},
        headers=admin_headers,
    )
    assert second_call.status_code == 200
    summary2 = second_call.get_json()["data"]
    assert summary2["created"] == [third]
    assert len(summary2["skipped"]) == 1
    assert summary2["skipped"][0]["hostId"] == second

    assert client.get("/api/grants", headers=admin_headers).get_json()["total"] == 3


def test_grant_preview_returns_decision(
    client, admin_headers, make_host, make_account, make_user, default_policy_id
):
    """授权前试算：ops（只有 *:view）也必须能用，且返回结构化判定。"""
    make_grant_user = make_user("grant-preview", role_code="ops", password="Preview1234")
    host_id = make_host(name="grant-host-10", address="10.1.1.10")
    account_id = make_account(host_id)

    token = client.post(
        "/api/auth/login",
        json={"username": "grant-preview", "password": "Preview1234"},
    ).get_json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post(
        "/api/grants/preview",
        json={"policyId": default_policy_id, "command": "rm -rf /"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["allowed"] is False
    assert data["action"] == "deny"
    assert data["ruleId"] is not None
    assert data["segments"]

    granted = client.post(
        "/api/grants",
        json={
            "userId": make_grant_user,
            "hostId": host_id,
            "hostAccountId": account_id,
            "policyId": default_policy_id,
            "canLogin": True,
            "canWebterm": True,
            "weekdays": ALL_WEEKDAYS,
        },
        headers=admin_headers,
    )
    assert granted.status_code == 200
