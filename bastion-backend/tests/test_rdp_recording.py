"""WebRDP（Windows 远程桌面）会话录像接口测试。

录像由浏览器录好、会话结束时 POST 上来，之后审计人员回看。这里覆盖：

1. **上传准入**：`rdp:use` 权限、hostId 必须在自己的 rdp 目标里、mime / 空文件 / 大小上限；
2. **落盘**：文件名由服务端 uuid 生成（请求里的文件名一个字符都不进路径）；
3. **可见性**：`session:view_all` / `audit:view` / 管理员看全部，普通操作员只看自己上传的；
4. **回放**：HTTP Range 必须真的返回 206 + `Content-Range` 且字节正确（否则 `<video>` 拖不动进度条），
   以及回放票据（`<video>` 带不了 Authorization 头，只能靠 `?ticket=`）；
5. **删除**：仅管理员，文件与行都要消失。

录像目录一律指到 ``tmp_path``，绝不碰真实 ``instance/``。
"""

from __future__ import annotations

import io
import os

import pytest

from app.extensions import db
from app.models import AuditLog, RdpRecording, Role, SessionRecord, User
from app.security import hash_password
from tests.conftest import token_of

WEBM = b"\x1a\x45\xdf\xa3webm-payload-0123456789"
PAYLOAD = bytes(range(256)) * 4  # 1024 字节，便于做精确的 Range 断言


@pytest.fixture()
def recording_dir(app, tmp_path):
    """把录像落盘目录指到 tmp_path，并把大小上限压到 1MB（测试里好造超限）。"""
    target = tmp_path / "rdp_recordings"
    app.config["RDP_RECORDING_DIR"] = str(target)
    app.config["RDP_RECORDING_MAX_MB"] = 1
    return target


def _upload(client, headers, host_id, data=WEBM, **fields):
    """POST /api/rdp/recordings 的 multipart 封装。"""
    form = {"hostId": str(host_id)}
    form.update({key: str(value) for key, value in fields.items()})
    form["file"] = (io.BytesIO(data), "sess.webm")
    return client.post(
        "/api/rdp/recordings",
        headers=headers,
        data=form,
        content_type="multipart/form-data",
    )


def _with_file(client, headers, host_id, name, mime, data=WEBM):
    """带自定义文件名 / Content-Type 的上传（用于 mime 与路径穿越用例）。"""
    return client.post(
        "/api/rdp/recordings",
        headers=headers,
        data={
            "hostId": str(host_id),
            "file": (io.BytesIO(data), name, mime),
        },
        content_type="multipart/form-data",
    )


def _rdp_host(make_host, name="win-rec", address="10.0.0.90"):
    return make_host(name=name, address=address, port=3389, protocol="rdp", os_type="windows")


def _ops_headers(client, make_user, username="rec-ops"):
    make_user(username, role_code="ops")
    return {"Authorization": f"Bearer {token_of(client, username, 'User1234')}"}


def _ops_with_access(app, client, make_grant, host_id, username):
    """建一个带 `rdp:use` 的普通操作员，并授权到指定主机，返回其请求头。

    内置 `ops` / `auditor` 角色同时带 `session:view_all` 与 `audit:view`，他们本来就该
    看全部录像，测不出「只看自己上传的」——所以这里现造一个只有 `rdp:use` 的角色。
    """
    user_id = _direct_user(app, username, "rdp-only")
    make_grant(user_id, host_id)
    return {"Authorization": f"Bearer {token_of(client, username, 'User1234')}"}


@pytest.fixture(autouse=True)
def rdp_only_role(app):
    """每个用例都保证存在一个「只能远程桌面、看不到别人录像」的角色。"""
    with app.app_context():
        if Role.query.filter_by(code="rdp-only").first() is None:
            db.session.add(
                Role(
                    code="rdp-only",
                    name="远程桌面操作员",
                    description="只能发起远程桌面并上传自己的录像",
                    permissions=["dashboard:view", "rdp:use", "session:view"],
                    is_builtin=False,
                )
            )
            db.session.commit()
    return "rdp-only"


def _direct_user(app, username, role_code, password="User1234"):
    """直接建用户（conftest 的 make_user 只能选内置角色）。"""
    with app.app_context():
        role = Role.query.filter_by(code=role_code).one()
        user = User(
            username=username,
            password_hash=hash_password(password),
            display_name=username,
            role_id=role.id,
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        return user.id


# --------------------------------------------------------------------------- #
# 1. 上传
# --------------------------------------------------------------------------- #
def test_upload_saves_row_file_and_audit(
    app, client, admin_headers, make_host, recording_dir
):
    host_id = _rdp_host(make_host)
    resp = _upload(
        client,
        admin_headers,
        host_id,
        sessionId="",
        durationSeconds=42,
        width=1280,
        height=720,
        startedAt="2026-02-03T04:05:06Z",
    )
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["success"] is True
    data = body["data"]
    assert data["id"] > 0
    assert data["hostId"] == host_id
    assert data["hostName"] == "win-rec"
    assert data["hostAddress"] == "10.0.0.90"
    assert data["username"] == "admin"
    assert data["durationSeconds"] == 42
    assert data["width"] == 1280
    assert data["height"] == 720
    assert data["sizeBytes"] == len(WEBM)
    assert data["mimeType"] == "video/webm"
    assert data["startedAt"] == "2026-02-03T04:05:06Z"
    assert data["createdAt"]
    assert data["url"] == f"/api/rdp/recordings/{data['id']}/file"

    # 行存在
    with app.app_context():
        row = RdpRecording.query.one()
        assert row.id == data["id"]
        assert row.size_bytes == len(WEBM)
        assert row.session_id is None  # 空 sessionId 不写坏值

    # 文件名是服务端 uuid，与上传时的 "sess.webm" 无关
    assert data["filename"].endswith(".webm")
    assert data["filename"] != "sess.webm"
    assert os.path.basename(data["filename"]) == data["filename"]

    # 文件真的落了盘且内容一致
    stored = recording_dir / data["filename"]
    assert stored.is_file()
    assert stored.read_bytes() == WEBM

    # 审计
    with app.app_context():
        entry = AuditLog.query.filter_by(action="rdp_recording_saved").one()
        assert entry.actor_username == "admin"
        assert entry.target_id == str(host_id)
        assert entry.detail["recordingId"] == data["id"]
        assert entry.detail["sizeBytes"] == len(WEBM)


def test_upload_links_an_existing_session(
    app, client, admin_headers, make_host, recording_dir
):
    """带上真实存在的 sessionId 时要关联上（审计要能把录像挂回会话）。"""
    host_id = _rdp_host(make_host)
    with app.app_context():
        session = SessionRecord(
            sid="sid-rec-1", username="admin", host_id=host_id, host_name="win-rec"
        )
        db.session.add(session)
        db.session.commit()
        session_id = session.id

    resp = _upload(client, admin_headers, host_id, sessionId=session_id)
    assert resp.status_code == 201
    assert resp.get_json()["data"]["sessionId"] == session_id


def test_upload_rejects_missing_file(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    resp = client.post(
        "/api/rdp/recordings",
        headers=admin_headers,
        data={"hostId": str(host_id)},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "录像" in resp.get_json()["message"]


def test_upload_rejects_empty_file(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    resp = _upload(client, admin_headers, host_id, data=b"")
    assert resp.status_code == 400
    assert "空" in resp.get_json()["message"]


def test_upload_rejects_missing_host_id(client, admin_headers, recording_dir):
    resp = _upload(client, admin_headers, 0)
    assert resp.status_code == 400
    assert "主机" in resp.get_json()["message"]


def test_upload_rejects_unknown_host(client, admin_headers, recording_dir):
    resp = _upload(client, admin_headers, 999999)
    assert resp.status_code == 403
    assert "远程桌面" in resp.get_json()["message"]


def test_upload_rejects_unsupported_mime(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    resp = _with_file(client, admin_headers, host_id, "a.mp4", "video/mp4")
    assert resp.status_code == 400
    assert "webm" in resp.get_json()["message"]


def test_upload_accepts_octet_stream_as_webm(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    resp = _with_file(client, admin_headers, host_id, "a.bin", "application/octet-stream")
    assert resp.status_code == 201
    assert resp.get_json()["data"]["mimeType"] == "video/webm"


def test_upload_rejects_oversize_file(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    too_big = b"x" * (1024 * 1024 + 1024)
    resp = _upload(client, admin_headers, host_id, data=too_big)
    assert resp.status_code == 413
    body = resp.get_json()
    assert body["success"] is False
    assert body["code"] == "TOO_LARGE"
    with client.application.app_context():
        assert db.session.query(RdpRecording).count() == 0
    assert list(recording_dir.glob("*.webm")) == []


def test_upload_requires_the_permission(client, app, make_host, recording_dir, make_user):
    host_id = _rdp_host(make_host)
    make_user("rec-viewer", role_code="viewer")
    headers = {"Authorization": f"Bearer {token_of(client, 'rec-viewer', 'User1234')}"}
    resp = _upload(client, headers, host_id)
    assert resp.status_code == 403
    assert resp.get_json()["message"] == "权限不足，需要：rdp:use"


def test_upload_requires_auth(client, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    assert _upload(client, {}, host_id).status_code == 401


def test_upload_rejects_unauthorized_host(
    app, client, make_user, make_host, make_grant, recording_dir
):
    """有 `rdp:use` 但没这台机器的授权 → 403（不能往别人机器名下塞录像）。"""
    allowed_id = _rdp_host(make_host, name="win-allowed", address="10.0.0.91")
    other_id = _rdp_host(make_host, name="win-other", address="10.0.0.92")
    user_id = make_user("rec-ops-grant", role_code="ops")
    headers = {"Authorization": f"Bearer {token_of(client, 'rec-ops-grant', 'User1234')}"}
    make_grant(user_id, allowed_id)

    assert _upload(client, headers, allowed_id).status_code == 201
    denied = _upload(client, headers, other_id)
    assert denied.status_code == 403
    assert "远程桌面" in denied.get_json()["message"]


def test_upload_rejects_ssh_host(client, admin_headers, make_host, recording_dir):
    host_id = make_host(name="linux-rec", address="10.0.0.3", port=22, protocol="ssh")
    resp = _upload(client, admin_headers, host_id)
    assert resp.status_code == 403
    assert "远程桌面" in resp.get_json()["message"]


def test_upload_filename_never_comes_from_the_request(
    client, admin_headers, make_host, recording_dir
):
    """恶意文件名 / 字段不得影响落盘位置（一律服务端 uuid）。"""
    host_id = _rdp_host(make_host)
    resp = _with_file(client, admin_headers, host_id, "../../evil.webm", "video/webm")
    assert resp.status_code == 201
    data = resp.get_json()["data"]
    assert "/" not in data["filename"] and "\\" not in data["filename"]
    assert ".." not in data["filename"]
    assert (recording_dir / data["filename"]).is_file()
    assert not (recording_dir.parent / "evil.webm").exists()


# --------------------------------------------------------------------------- #
# 2. 列表与可见性
# --------------------------------------------------------------------------- #
def test_list_requires_auth(client, recording_dir):
    assert client.get("/api/rdp/recordings").status_code == 401


def test_list_visibility_owner_vs_admin(
    app, client, admin_headers, make_host, make_grant, recording_dir
):
    host_id = _rdp_host(make_host)
    headers = _ops_with_access(app, client, make_grant, host_id, "rec-owner")

    mine = _upload(client, headers, host_id, durationSeconds=11).get_json()["data"]
    admin_row = _upload(client, admin_headers, host_id, durationSeconds=22).get_json()["data"]
    assert (mine["username"], admin_row["username"]) == ("rec-owner", "admin")

    # 普通操作员：只看到自己上传的
    body = client.get("/api/rdp/recordings", headers=headers).get_json()
    assert body["success"] is True
    assert body["total"] == 1
    assert [item["id"] for item in body["data"]] == [mine["id"]]
    assert body["page"] == 1 and body["pageSize"] == 20

    # 管理员（超管）：看到全部
    admin_body = client.get("/api/rdp/recordings", headers=admin_headers).get_json()
    assert admin_body["total"] == 2
    assert {item["id"] for item in admin_body["data"]} == {mine["id"], admin_row["id"]}


def test_list_visibility_for_auditor(
    client, admin_headers, make_user, make_host, recording_dir
):
    """`audit:view` / `session:view_all` 的人能看全部（审计人员要回看别人的录像）。"""
    host_id = _rdp_host(make_host)
    _upload(client, admin_headers, host_id)
    make_user("rec-auditor", role_code="auditor")
    headers = {"Authorization": f"Bearer {token_of(client, 'rec-auditor', 'User1234')}"}
    body = client.get("/api/rdp/recordings", headers=headers).get_json()
    assert body["total"] == 1
    assert body["data"][0]["username"] == "admin"


def test_list_filters_and_ordering(
    app, client, admin_headers, make_host, recording_dir
):
    first_host = _rdp_host(make_host, name="win-first", address="10.0.0.101")
    second_host = _rdp_host(make_host, name="win-second", address="10.0.0.102")
    older = _upload(client, admin_headers, first_host).get_json()["data"]
    newer = _upload(client, admin_headers, second_host).get_json()["data"]

    body = client.get("/api/rdp/recordings", headers=admin_headers).get_json()
    assert [item["id"] for item in body["data"]] == [newer["id"], older["id"]]  # created_at 倒序

    filtered = client.get(
        f"/api/rdp/recordings?hostId={second_host}", headers=admin_headers
    ).get_json()
    assert filtered["total"] == 1
    assert filtered["data"][0]["hostName"] == "win-second"

    by_address = client.get(
        "/api/rdp/recordings?keyword=10.0.0.101", headers=admin_headers
    ).get_json()
    assert by_address["total"] == 1
    assert by_address["data"][0]["id"] == older["id"]

    by_owner = client.get(
        "/api/rdp/recordings?keyword=admin", headers=admin_headers
    ).get_json()
    assert by_owner["total"] == 2

    missing = client.get(
        "/api/rdp/recordings?keyword=不存在的机器", headers=admin_headers
    ).get_json()
    assert missing["total"] == 0 and missing["data"] == []


def test_list_pagination(app, client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    for _ in range(3):
        assert _upload(client, admin_headers, host_id).status_code == 201
    page1 = client.get("/api/rdp/recordings?page=1&pageSize=2", headers=admin_headers).get_json()
    page2 = client.get("/api/rdp/recordings?page=2&pageSize=2", headers=admin_headers).get_json()
    assert page1["total"] == 3 and len(page1["data"]) == 2
    assert page2["total"] == 3 and len(page2["data"]) == 1
    assert {item["id"] for item in page1["data"]}.isdisjoint({item["id"] for item in page2["data"]})


# --------------------------------------------------------------------------- #
# 3. 回放（含 Range）
# --------------------------------------------------------------------------- #
def test_file_full_download_and_view_audit(app, client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id, data=PAYLOAD).get_json()["data"]

    resp = client.get(f"/api/rdp/recordings/{data['id']}/file", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.data == PAYLOAD
    assert resp.headers["Content-Type"].startswith("video/webm")
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Content-Disposition"].startswith("inline")
    assert resp.headers["Content-Length"] == str(len(PAYLOAD))

    with app.app_context():
        entry = AuditLog.query.filter_by(action="rdp_recording_viewed").one()
        assert entry.actor_username == "admin"
        assert entry.detail["recordingId"] == data["id"]


def test_file_range_request_returns_206_and_exact_bytes(
    app, client, admin_headers, make_host, recording_dir
):
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id, data=PAYLOAD).get_json()["data"]

    resp = client.get(
        f"/api/rdp/recordings/{data['id']}/file",
        headers={**admin_headers, "Range": "bytes=16-31"},
    )
    assert resp.status_code == 206
    assert resp.data == PAYLOAD[16:32]
    assert resp.headers["Content-Range"] == "bytes 16-31/1024"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Content-Length"] == "16"

    # 从中间拖动进度条不该刷审计（一次播放会发几十次 Range 请求）
    with app.app_context():
        assert AuditLog.query.filter_by(action="rdp_recording_viewed").count() == 0


def test_file_range_from_zero_is_audited(
    app, client, admin_headers, make_host, recording_dir
):
    """Range 起点为 0（播放器起播）算一次「回看」，要留痕。"""
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id, data=PAYLOAD).get_json()["data"]
    resp = client.get(
        f"/api/rdp/recordings/{data['id']}/file",
        headers={**admin_headers, "Range": "bytes=0-99"},
    )
    assert resp.status_code == 206
    assert resp.data == PAYLOAD[:100]
    with app.app_context():
        assert AuditLog.query.filter_by(action="rdp_recording_viewed").count() == 1


def test_file_range_suffix_bytes(client, admin_headers, make_host, recording_dir):
    """`bytes=-N` 取末尾 N 字节（有些播放器用这种写法探时长）。"""
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id, data=PAYLOAD).get_json()["data"]
    resp = client.get(
        f"/api/rdp/recordings/{data['id']}/file",
        headers={**admin_headers, "Range": "bytes=-10"},
    )
    assert resp.status_code == 206
    assert resp.data == PAYLOAD[-10:]
    assert resp.headers["Content-Range"] == "bytes 1014-1023/1024"


def test_file_range_out_of_bounds_returns_416(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id, data=PAYLOAD).get_json()["data"]
    resp = client.get(
        f"/api/rdp/recordings/{data['id']}/file",
        headers={**admin_headers, "Range": "bytes=99999-"},
    )
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */1024"


def test_file_visibility_respects_owner(
    app, client, admin_headers, make_host, make_grant, recording_dir
):
    host_id = _rdp_host(make_host)
    mine = _upload(client, admin_headers, host_id).get_json()["data"]
    headers = _ops_with_access(app, client, make_grant, host_id, "rec-other")

    denied = client.get(f"/api/rdp/recordings/{mine['id']}/file", headers=headers)
    assert denied.status_code == 403
    assert denied.get_json()["message"] == "无权查看该录像"


def test_file_owner_can_play_back_own_recording(
    app, client, admin_headers, make_host, make_grant, recording_dir
):
    host_id = _rdp_host(make_host)
    headers = _ops_with_access(app, client, make_grant, host_id, "rec-self")
    mine = _upload(client, headers, host_id, data=PAYLOAD).get_json()["data"]
    resp = client.get(f"/api/rdp/recordings/{mine['id']}/file", headers=headers)
    assert resp.status_code == 200
    assert resp.data == PAYLOAD


def test_file_requires_auth(client, admin_headers, make_host, recording_dir):
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id).get_json()["data"]
    assert client.get(f"/api/rdp/recordings/{data['id']}/file").status_code == 401


def test_file_missing_returns_404(client, admin_headers, recording_dir):
    resp = client.get("/api/rdp/recordings/424242/file", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "NOT_FOUND"


def test_file_reports_missing_blob(
    app, client, admin_headers, make_host, recording_dir
):
    """库里还有行、磁盘上文件没了 → 404 + 明确提示（不是 500）。"""
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id).get_json()["data"]
    os.remove(recording_dir / data["filename"])
    resp = client.get(f"/api/rdp/recordings/{data['id']}/file", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "FILE_MISSING"


# --------------------------------------------------------------------------- #
# 4. 删除
# --------------------------------------------------------------------------- #
def test_delete_removes_file_row_and_audits(
    app, client, admin_headers, make_host, recording_dir
):
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id).get_json()["data"]
    path = recording_dir / data["filename"]
    assert path.is_file()

    resp = client.delete(f"/api/rdp/recordings/{data['id']}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert not path.exists()

    with app.app_context():
        assert db.session.query(RdpRecording).count() == 0
        entry = AuditLog.query.filter_by(action="rdp_recording_deleted").one()
        assert entry.detail["recordingId"] == data["id"]
    assert client.get("/api/rdp/recordings", headers=admin_headers).get_json()["total"] == 0


def test_delete_requires_admin(
    app, client, admin_headers, make_host, make_grant, recording_dir
):
    host_id = _rdp_host(make_host)
    headers = _ops_with_access(app, client, make_grant, host_id, "rec-ops-del")
    data = _upload(client, headers, host_id).get_json()["data"]
    denied = client.delete(f"/api/rdp/recordings/{data['id']}", headers=headers)
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "ADMIN_ONLY"
    # 未登录同样不能删
    assert client.delete(f"/api/rdp/recordings/{data['id']}").status_code == 401


def test_delete_missing_returns_404(client, admin_headers, recording_dir):
    resp = client.delete("/api/rdp/recordings/424242", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# 5. 回放票据
# --------------------------------------------------------------------------- #
def _ticket(client, headers, recording_id):
    return client.post(f"/api/rdp/recordings/{recording_id}/ticket", headers=headers)


def test_ticket_signs_and_audits_once(
    app, client, admin_headers, make_host, recording_dir
):
    """换票返回 path + 10 分钟有效期；「要看」这条审计在签票时就记下（只记一条）。"""
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id).get_json()["data"]

    resp = _ticket(client, admin_headers, data["id"])
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    ticket = payload["data"]["ticket"]
    assert ticket
    assert payload["data"]["path"] == f"/api/rdp/recordings/{data['id']}/file"
    assert payload["data"]["expiresIn"] == 600

    with app.app_context():
        entries = AuditLog.query.filter_by(action="rdp_recording_viewed").all()
        assert len(entries) == 1
        assert entries[0].detail["recordingId"] == data["id"]
        assert entries[0].detail["ticket"] is True


def test_ticket_plays_back_without_authorization_header(
    app, client, admin_headers, make_host, recording_dir
):
    """`<video>` 发不出 Authorization 头：带 `?ticket=` 必须能全量取回、也能 Range 取回。"""
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id, data=PAYLOAD).get_json()["data"]
    ticket = _ticket(client, admin_headers, data["id"]).get_json()["data"]["ticket"]
    path = f"/api/rdp/recordings/{data['id']}/file"

    full = client.get(f"{path}?ticket={ticket}")
    assert full.status_code == 200
    assert full.data == PAYLOAD
    assert full.headers["Accept-Ranges"] == "bytes"

    part = client.get(f"{path}?ticket={ticket}", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.data == PAYLOAD[100:200]
    assert part.headers["Content-Range"] == f"bytes 100-199/{len(PAYLOAD)}"

    # 播放器会连发一串 Range 请求，票据这条路上不再各记一条审计
    with app.app_context():
        assert AuditLog.query.filter_by(action="rdp_recording_viewed").count() == 1


def test_ticket_is_bound_to_one_recording(
    client, admin_headers, make_host, recording_dir
):
    """票绑定 recordingId：拿 A 的票去拉 B 的录像必须 401。"""
    host_id = _rdp_host(make_host)
    first = _upload(client, admin_headers, host_id).get_json()["data"]
    second = _upload(client, admin_headers, host_id).get_json()["data"]
    ticket = _ticket(client, admin_headers, first["id"]).get_json()["data"]["ticket"]

    assert client.get(f"/api/rdp/recordings/{first['id']}/file?ticket={ticket}").status_code == 200
    assert (
        client.get(f"/api/rdp/recordings/{second['id']}/file?ticket={ticket}").status_code == 401
    )


def test_ticket_rejects_expired_wrong_scope_and_login_token(
    app, client, admin_headers, make_host, recording_dir
):
    """票据是专门的凭据：过期、scope 不符、拿登录令牌冒充、乱码，全部 401。"""
    from datetime import timedelta

    from flask_jwt_extended import create_access_token

    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id).get_json()["data"]
    path = f"/api/rdp/recordings/{data['id']}/file"

    with app.app_context():
        expired = create_access_token(
            identity="1",
            additional_claims={"scope": "rdp-recording", "recordingId": data["id"]},
            expires_delta=timedelta(seconds=-5),
        )
        wrong_scope = create_access_token(
            identity="1",
            additional_claims={"scope": "something-else", "recordingId": data["id"]},
        )

    assert client.get(f"{path}?ticket={expired}").status_code == 401
    assert client.get(f"{path}?ticket={wrong_scope}").status_code == 401
    assert client.get(f"{path}?ticket=not-a-jwt").status_code == 401
    login_token = admin_headers["Authorization"].split(" ", 1)[1]
    assert client.get(f"{path}?ticket={login_token}").status_code == 401


def test_ticket_requires_login_and_respects_visibility(
    app, client, admin_headers, make_host, make_grant, recording_dir
):
    """签票同样过一遍可见性：别人的录像签不出来，自己的可以；未登录 401。"""
    host_id = _rdp_host(make_host)
    headers = _ops_with_access(app, client, make_grant, host_id, "rec-ops-ticket")
    mine = _upload(client, headers, host_id).get_json()["data"]
    admin_data = _upload(client, admin_headers, host_id).get_json()["data"]

    assert _ticket(client, headers, mine["id"]).status_code == 200
    denied = _ticket(client, headers, admin_data["id"])
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "FORBIDDEN"
    assert client.post(f"/api/rdp/recordings/{mine['id']}/ticket").status_code == 401


def test_ticket_reports_missing_blob(
    app, client, admin_headers, make_host, recording_dir
):
    """库里还有行、磁盘上文件没了：签票就报 404，别给一张注定 404 的票。"""
    host_id = _rdp_host(make_host)
    data = _upload(client, admin_headers, host_id).get_json()["data"]
    os.remove(recording_dir / data["filename"])
    resp = _ticket(client, admin_headers, data["id"])
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "FILE_MISSING"


def test_ticket_missing_recording_returns_404(client, admin_headers, recording_dir):
    resp = _ticket(client, admin_headers, 424242)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "NOT_FOUND"
