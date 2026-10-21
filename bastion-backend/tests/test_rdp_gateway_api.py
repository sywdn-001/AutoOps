"""RDP 网关后端接口的回归：选单只给 RDP 授权，选中后开会话记录 + 审计。"""

from __future__ import annotations

from app.models import AuditLog, SessionRecord

GRANT_OWNER_ROLE = "ops"


def _host_with_grant(make_host, make_account, make_grant, user_id):
    host_id = make_host(
        name="rdp-box",
        address="10.20.30.40",
        port=3389,
        protocol="rdp",
        os_type="windows",
    )
    account_id = make_account(host_id, name="Administrator", username="Administrator")
    make_grant(user_id, host_id, account_id, can_webterm=True)
    return host_id, account_id


def test_gateway_hosts_only_lists_rdp_targets(
    app, client, admin_headers, make_host, make_account, make_grant, make_user
):
    user_id = make_user("rdpgw-user", role_code=GRANT_OWNER_ROLE)
    rdp_host, _ = _host_with_grant(make_host, make_account, make_grant, user_id)
    ssh_host = make_host(
        name="linux-box", address="10.1.1.2", port=22, protocol="ssh", os_type="linux"
    )
    make_account(ssh_host, name="root", username="root")
    make_grant(user_id, ssh_host, None, can_webterm=True)

    resp = client.get("/api/rdp-gateway/hosts", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    names = [row["hostName"] for row in data["hosts"]]
    assert "rdp-box" in names, "RDP 主机要出现在选单里"
    assert "linux-box" not in names, "SSH 主机不能混进 RDP 选单"
    row = next(item for item in data["hosts"] if item["hostName"] == "rdp-box")
    assert row["hostId"] == rdp_host and row["port"] == 3389
    assert row["accounts"][0]["username"] == "Administrator"


def test_gateway_open_session_writes_record_and_audit(
    app, client, admin_headers, make_host, make_account
):
    host_id = make_host(
        name="rdp-open", address="10.20.30.41", port=3389, protocol="rdp", os_type="windows"
    )
    account_id = make_account(host_id, name="administrator", username="administrator")

    resp = client.post(
        "/api/rdp-gateway/sessions",
        json={"hostId": host_id, "accountId": account_id},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["sid"]
    assert data["accountUsername"] == "administrator"

    with app.app_context():
        record = SessionRecord.query.filter_by(sid=data["sid"]).first()
        assert record is not None
        assert record.source == "gateway" and record.protocol == "rdp"
        assert record.status == "active"
        assert record.host_name == "rdp-open"
        audit = AuditLog.query.filter_by(action="rdp_gateway_open").first()
        assert audit is not None and "rdp-open" in (audit.message or "")


def test_gateway_open_session_rejects_missing_host(client, admin_headers):
    resp = client.post("/api/rdp-gateway/sessions", json={}, headers=admin_headers)
    assert resp.status_code == 400


def test_gateway_requires_login(client):
    assert client.get("/api/rdp-gateway/hosts").status_code == 401
    assert client.post("/api/rdp-gateway/sessions", json={}).status_code == 401

def test_gateway_status_reports_port_and_listening(client, admin_headers, monkeypatch):
    from app.api import rdp_gateway as api

    monkeypatch.setattr(api, "_listening", lambda port: True)
    resp = client.get("/api/rdp-gateway/status", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["listening"] is True
    assert data["port"] >= 1
    assert "binaryExists" in data


def test_gateway_start_is_idempotent_when_already_listening(
    client, admin_headers, monkeypatch
):
    from app.api import rdp_gateway as api

    monkeypatch.setattr(api, "_listening", lambda port: True)
    resp = client.post("/api/rdp-gateway/start", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["started"] is False


def test_gateway_start_reports_missing_binary(client, admin_headers, monkeypatch):
    from pathlib import Path

    from app.api import rdp_gateway as api

    monkeypatch.setattr(api, "_listening", lambda port: False)
    monkeypatch.setattr(api, "GATEWAY_BIN", Path("D:/definitely-missing/gateway.exe"))
    resp = client.post("/api/rdp-gateway/start", headers=admin_headers)
    assert resp.status_code == 400
    assert "找不到网关程序" in resp.get_json()["message"]


def test_gateway_stop_reports_not_running(client, admin_headers, monkeypatch):
    from app.api import rdp_gateway as api

    monkeypatch.setattr(api, "_read_pid", lambda: None)
    monkeypatch.setattr(api, "_pid_alive", lambda pid: False)
    resp = client.post("/api/rdp-gateway/stop", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["stopped"] is False

def test_frame_without_bridge_is_204(client, admin_headers):
    resp = client.get("/api/rdp-gateway/frame?sid=nope", headers=admin_headers)
    assert resp.status_code == 204


def test_input_without_bridge_is_404(client, admin_headers):
    resp = client.post(
        "/api/rdp-gateway/input", json={"sid": "nope", "events": []}, headers=admin_headers
    )
    assert resp.status_code == 404


def test_connect_unknown_session_is_404(client, admin_headers):
    resp = client.post(
        "/api/rdp-gateway/connect", json={"sid": "does-not-exist"}, headers=admin_headers
    )
    assert resp.status_code == 404


def test_bridge_tile_diff_only_sends_changed_tiles():
    """桥的脏块编码：首帧全量，第二帧不变返回 None，改一个块只回那一块。"""
    from app.rdp.window_bridge import TILE, TargetBridge

    bridge = TargetBridge("127.0.0.1", 3389, "u", "p", width=TILE * 2, height=TILE)
    first = bytes(TILE * 2 * TILE * 4)
    payload = bridge._encode_tiles(first, None)
    assert payload is not None
    count = int.from_bytes(payload[:4], "little")
    assert count == 2, "首帧应该回全部 2 个块"
    assert bridge._encode_tiles(first, first) is None, "没变化不该回包"

    changed = bytearray(first)
    changed[0] = 255  # 第一个块左上角像素变了
    payload2 = bridge._encode_tiles(bytes(changed), first)
    assert payload2 is not None
    assert int.from_bytes(payload2[:4], "little") == 1, "只有 1 个块变了"
    # 块头：x=0,y=0,w=TILE,h=TILE
    assert payload2[4:12] == bytes([0, 0, 0, 0, TILE, 0, TILE, 0])
