"""文件管理器接口层回归测试（HTTP + 真 SFTP 目标机）。

服务层已被 `tests/test_files_service.py` 用真 SFTP 钉住，这里只验证**接口契约**：
信封格式、权限码、参数校验、以及「列表/试算/上传/下载/删除/审计/策略」这些前端要
依赖的返回结构。同样不 mock SFTP——假目标机是真的 paramiko 服务端。
"""

from __future__ import annotations

import io
import re

import pytest

from app.models import FileLog, FilePolicy

from .sftp_target import sftp_target  # noqa: F401 - pytest 夹具


@pytest.fixture(autouse=True)
def _app_context(app):
    with app.app_context():
        yield


def _grant_access(make_user, make_host, make_account, make_grant, target, *, username="api-filer", **grant_kwargs):
    user_id = make_user(username, role_code="ops")
    host_id = make_host(f"api-sftp-{username}", address=target.host, port=target.port)
    account_id = make_account(
        host_id, name="root", username=target.username, password=target.password
    )
    grant_kwargs.setdefault("can_sftp", True)
    grant_kwargs.setdefault("can_upload", True)
    grant_kwargs.setdefault("can_download", True)
    grant_kwargs.setdefault("can_file_write", True)
    make_grant(user_id, host_id, account_id, **grant_kwargs)
    return user_id, host_id, account_id


def _open_via_api(client, headers, host_id, account_id=None):
    body = {"hostId": host_id}
    if account_id:
        body["accountId"] = account_id
    resp = client.post("/api/files/sessions", json=body, headers=headers)
    return resp


def test_admin_opens_file_session_and_lists_directory(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    resp = _open_via_api(client, admin_headers, host_id, account_id)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    envelope = resp.get_json()
    assert envelope["success"] is True
    data = envelope["data"]
    sid = data["sid"]
    assert data["canUpload"] is True
    assert data["canFileWrite"] is True
    assert "list" in data["operations"]

    listing = client.get(
        f"/api/files/sessions/{sid}/list", query_string={"path": "/"}, headers=admin_headers
    ).get_json()
    assert listing["success"] is True
    names = {item["name"] for item in listing["data"]["entries"]}
    assert {"readme.txt", "docs", "data"} <= names
    assert listing["data"]["path"] == "/"

    # 会话可查询、可关闭
    assert client.get(f"/api/files/sessions/{sid}", headers=admin_headers).status_code == 200
    closed = client.delete(f"/api/files/sessions/{sid}", headers=admin_headers).get_json()
    assert closed["success"] is True
    assert closed["data"]["sid"] == sid
    assert client.get(f"/api/files/sessions/{sid}", headers=admin_headers).status_code == 404


def test_check_endpoint_explains_denied_operation(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]

    allowed = client.post(
        f"/api/files/sessions/{sid}/check",
        json={"operation": "upload", "path": "/tmp/ok.bin"},
        headers=admin_headers,
    ).get_json()["data"]
    assert allowed["allowed"] is True

    denied = client.post(
        f"/api/files/sessions/{sid}/check",
        json={"operation": "read", "path": "/.ssh/id_rsa"},
        headers=admin_headers,
    ).get_json()["data"]
    assert denied["allowed"] is False
    assert denied["ruleId"] is not None
    assert "禁止" in denied["reason"] or "拦截" in denied["reason"]

    # 试算不能产生审计噪声
    assert FileLog.query.filter_by(sid=sid).count() == 0


def test_upload_download_delete_roundtrip_over_http(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]

    upload = client.post(
        f"/api/files/sessions/{sid}/upload",
        data={
            "path": "/tmp",
            "file": (io.BytesIO("接口层上传内容".encode()), "hello.txt"),
        },
        content_type="multipart/form-data",
        headers=admin_headers,
    )
    assert upload.status_code == 200, upload.get_data(as_text=True)
    payload = upload.get_json()["data"]
    assert payload["uploaded"], payload
    assert sftp_target.read("/tmp/hello.txt") == "接口层上传内容".encode()

    download = client.get(
        f"/api/files/sessions/{sid}/download",
        query_string={"path": "/tmp/hello.txt"},
        headers=admin_headers,
    )
    assert download.status_code == 200
    assert download.data == "接口层上传内容".encode()
    assert download.headers["Content-Length"] == str(len(download.data))

    removed = client.post(
        f"/api/files/sessions/{sid}/delete",
        json={"paths": ["/tmp/hello.txt"]},
        headers=admin_headers,
    ).get_json()["data"]
    assert removed["deleted"] == ["/tmp/hello.txt"]
    assert not sftp_target.exists("/tmp/hello.txt")

    ops = {row.operation for row in FileLog.query.filter_by(sid=sid).all()}
    assert {"upload", "download", "delete"} <= ops


def test_mkdir_write_read_rename_copy_chmod_through_api(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]
    base = f"/api/files/sessions/{sid}"

    assert client.post(f"{base}/mkdir", json={"path": "/tmp/api-dir"}, headers=admin_headers).status_code == 200
    assert (
        client.post(
            f"{base}/write",
            json={"path": "/tmp/api-dir/a.txt", "content": "hello 报表"},
            headers=admin_headers,
        ).status_code
        == 200
    )
    read = client.get(
        f"{base}/read", query_string={"path": "/tmp/api-dir/a.txt"}, headers=admin_headers
    ).get_json()["data"]
    assert read["content"] == "hello 报表"

    renamed = client.post(
        f"{base}/rename",
        json={"path": "/tmp/api-dir/a.txt", "newName": "b.txt"},
        headers=admin_headers,
    ).get_json()["data"]
    assert renamed["targetPath"] == "/tmp/api-dir/b.txt"

    copied = client.post(
        f"{base}/copy",
        json={"path": "/tmp/api-dir/b.txt", "targetPath": "/tmp/api-dir/c.txt"},
        headers=admin_headers,
    ).get_json()["data"]
    assert copied["path"] == "/tmp/api-dir/b.txt"
    assert sftp_target.read("/tmp/api-dir/c.txt") == "hello 报表".encode()

    chmod = client.post(
        f"{base}/chmod",
        json={"path": "/tmp/api-dir/c.txt", "mode": "640"},
        headers=admin_headers,
    ).get_json()["data"]
    assert chmod["requested"] == "0640"

    stat = client.get(
        f"{base}/stat", query_string={"path": "/tmp/api-dir/c.txt"}, headers=admin_headers
    ).get_json()["data"]
    # 注意：开发机是 Windows，os.chmod 只能切只读位，st_mode 不会真的变成 0640，
    # 所以这里只钉子字段契约（四位八进制字符串）+「after 必须是回读到的实际值」。
    assert chmod["after"] == stat["modeOctal"]
    assert re.fullmatch(r"\d{4}", stat["modeOctal"])

    ops = {row.operation for row in FileLog.query.filter_by(sid=sid).all()}
    assert {"mkdir", "write", "read", "rename", "copy", "chmod"} <= ops


def test_write_conflict_is_reported_as_409(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]
    base = f"/api/files/sessions/{sid}"

    client.post(f"{base}/write", json={"path": "/tmp/c.txt", "content": "v1"}, headers=admin_headers)
    stat = client.get(f"{base}/stat", query_string={"path": "/tmp/c.txt"}, headers=admin_headers).get_json()
    mtime = stat["data"]["mtime"]

    resp = client.post(
        f"{base}/write",
        json={"path": "/tmp/c.txt", "content": "v2", "expectedMtime": 1},
        headers=admin_headers,
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FILE_CONFLICT"
    assert mtime  # 契约上必须有 mtime 字段供前端做冲突检测


def test_archive_endpoint_returns_zip(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]

    resp = client.post(
        f"/api/files/sessions/{sid}/archive",
        json={"paths": ["/docs"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/zip")
    import zipfile

    with zipfile.ZipFile(io.BytesIO(resp.data)) as archive:
        assert any(name.endswith("notes.txt") for name in archive.namelist())


def test_permission_code_is_required(client, make_user, make_host, make_account, make_grant, sftp_target):
    """没有 `file:use` 的角色不能碰文件管理器。"""
    user_id = make_user("viewer-filer", role_code="viewer")
    host_id = make_host("api-sftp-viewer", address=sftp_target.host, port=sftp_target.port)
    account_id = make_account(
        host_id, name="root", username=sftp_target.username, password=sftp_target.password
    )
    make_grant(user_id, host_id, account_id, can_sftp=True)

    from tests.conftest import token_of

    token = token_of(client, "viewer-filer", "User1234")
    resp = client.post(
        "/api/files/sessions",
        json={"hostId": host_id, "accountId": account_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "FORBIDDEN"


def test_sftp_disabled_grant_is_rejected(
    client, sftp_target, make_user, make_host, make_account, make_grant
):
    """授权里没有 can_sftp 时，文件管理器必须拒绝（admin 有超管兜底，所以用普通账号验证）。"""
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target, username="no-sftp", can_sftp=False
    )
    from tests.conftest import token_of

    token = token_of(client, "no-sftp", "User1234")
    resp = client.post(
        "/api/files/sessions",
        json={"hostId": host_id, "accountId": account_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["success"] is False
    assert "SFTP" in body["message"] or "文件" in body["message"]


def test_file_logs_are_listed_and_filterable(
    client, admin_headers, sftp_target, make_user, make_host, make_account, make_grant
):
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]
    client.get(f"/api/files/sessions/{sid}/list", query_string={"path": "/"}, headers=admin_headers)
    client.get(
        f"/api/files/sessions/{sid}/read",
        query_string={"path": "/.ssh/id_rsa"},
        headers=admin_headers,
    )

    listing = client.get(
        "/api/audits/files", query_string={"sid": sid, "pageSize": 50}, headers=admin_headers
    ).get_json()
    assert listing["success"] is True
    assert listing["total"] >= 2
    operations = {item["operation"] for item in listing["data"]}
    assert {"list", "read"} <= operations
    denied = [item for item in listing["data"] if item["result"] == "denied"]
    assert denied and denied[0]["matchedRuleId"] is not None

    only_denied = client.get(
        "/api/audits/files",
        query_string={"sid": sid, "result": "denied"},
        headers=admin_headers,
    ).get_json()
    assert all(item["result"] == "denied" for item in only_denied["data"])
    assert only_denied["total"] == len(denied)

    options = client.get("/api/audits/files/options", headers=admin_headers).get_json()["data"]
    assert "read" in options["operations"]
    assert "denied" in options["results"]

    purged = client.post(
        "/api/audits/files/delete", json={"ids": [denied[0]["id"]]}, headers=admin_headers
    ).get_json()
    assert purged["success"] is True
    assert purged["data"]["deleted"] == 1


def test_file_policy_crud_and_evaluate(client, admin_headers):
    listing = client.get("/api/file-policies", headers=admin_headers).get_json()
    assert listing["success"] is True
    names = {item["name"] for item in listing["data"]}
    assert "默认文件策略·敏感路径拦截" in names
    assert "只读文件策略" in names

    operations = client.get("/api/file-policies/operations", headers=admin_headers).get_json()["data"]
    assert isinstance(operations, list)
    values = {item["value"] for item in operations}
    assert {"upload", "delete", "chmod", "archive"} <= values

    evaluate = client.post(
        "/api/file-policies/evaluate",
        json={"operation": "read", "path": "/etc/shadow"},
        headers=admin_headers,
    ).get_json()["data"]
    assert evaluate["allowed"] is False
    assert evaluate["ruleId"] is not None

    created = client.post(
        "/api/file-policies",
        json={"name": "接口测试策略", "description": "仅测试", "defaultAction": "deny"},
        headers=admin_headers,
    ).get_json()["data"]
    policy_id = created["id"]
    assert created["defaultAction"] == "deny"

    rule = client.post(
        "/api/file-rules",
        json={
            "policyId": policy_id,
            "priority": 10,
            "action": "allow",
            "operation": "list",
            "matchType": "regex",
            "pathPattern": r"^/var/log(/.*)?$",
            "riskLevel": "low",
            "description": "允许看日志目录",
        },
        headers=admin_headers,
    )
    assert rule.status_code == 200, rule.get_data(as_text=True)
    rule_id = rule.get_json()["data"]["id"]

    allowed = client.post(
        f"/api/file-policies/{policy_id}/test",
        json={"operation": "list", "path": "/var/log"},
        headers=admin_headers,
    ).get_json()["data"]
    assert allowed["allowed"] is True
    blocked = client.post(
        f"/api/file-policies/{policy_id}/test",
        json={"operation": "list", "path": "/tmp"},
        headers=admin_headers,
    ).get_json()["data"]
    assert blocked["allowed"] is False

    assert client.delete(f"/api/file-rules/{rule_id}", headers=admin_headers).status_code == 200
    assert client.delete(f"/api/file-policies/{policy_id}", headers=admin_headers).status_code == 200

    default_policy = FilePolicy.query.filter_by(is_default=True).first()
    assert default_policy is not None
    refused = client.delete(f"/api/file-policies/{default_policy.id}", headers=admin_headers)
    assert refused.status_code == 400


def test_invalid_operation_is_rejected(client, admin_headers):
    body = client.post(
        "/api/file-policies/evaluate",
        json={"operation": "explode", "path": "/tmp"},
        headers=admin_headers,
    )
    assert body.status_code == 400
    assert body.get_json()["success"] is False


def test_missing_session_returns_404(client, admin_headers):
    assert client.get("/api/files/sessions/does-not-exist", headers=admin_headers).status_code == 404
    assert (
        client.get(
            "/api/files/sessions/does-not-exist/list", query_string={"path": "/"}, headers=admin_headers
        ).status_code
        == 404
    )


def test_other_user_cannot_touch_my_file_session(
    client, admin_headers, app, sftp_target, make_user, make_host, make_account, make_grant
):
    """会话归属校验：拿别人的 sid 必须 403，而不是替别人操作。"""
    _uid, host_id, account_id = _grant_access(
        make_user, make_host, make_account, make_grant, sftp_target, username="owner-filer"
    )
    sid = _open_via_api(client, admin_headers, host_id, account_id).get_json()["data"]["sid"]

    from tests.conftest import token_of

    other_id = make_user("intruder-filer", role_code="ops")
    make_grant(other_id, host_id, account_id, can_sftp=True, can_upload=True)
    token = token_of(client, "intruder-filer", "User1234")
    resp = client.get(
        f"/api/files/sessions/{sid}/list",
        query_string={"path": "/"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
