"""审计清除接口（需求：审计可以清除、可以按选择清除）。

覆盖三件事：
1. **权限**：只有管理员（`admin_required`）能清除 —— 非管理员一律 403、匿名 401/403；
2. **范围必须显式**：空 body / 只给筛选条件一律 400，杜绝「手一抖清空全库」；
3. **清除动作本身留痕**：删完必须写一条 `purge_*` 审计（谁、清了多少条、什么范围），
   并且「清空筛选结果」删掉的必须是用户看得见的那批数据（列表与清除共用筛选口径）。
"""

from __future__ import annotations

from datetime import timedelta

from app.models import AuditLog, CommandLog, SessionRecord, utcnow
from tests.conftest import auth, token_of

PURGE_PATHS = ("/api/audits/delete", "/api/commands/delete", "/api/sessions/delete")


def _post(client, path, payload, headers):
    return client.post(path, json=payload, headers=headers)


def _add_audits(app, count: int, category: str = "asset") -> list[int]:
    with app.app_context():
        ids = []
        for index in range(count):
            row = AuditLog(
                ts=utcnow(),
                category=category,
                action="create_host",
                actor_username="admin",
                message=f"审计行 {index}",
            )
            from app.extensions import db

            db.session.add(row)
            db.session.flush()
            ids.append(row.id)
        db.session.commit()
        return ids


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------
def test_purge_endpoints_require_admin(client, make_user):
    ops_id = make_user("purgeops", role_code="ops")
    viewer_id = make_user("purgeviewer", role_code="viewer")
    assert ops_id and viewer_id
    ops_headers = auth(token_of(client, "purgeops", "User1234"))
    viewer_headers = auth(token_of(client, "purgeviewer", "User1234"))

    for path in PURGE_PATHS:
        response = _post(client, path, {"ids": [1]}, ops_headers)
        assert response.status_code == 403, f"{path} 竟然允许 ops 清除：{response.status_code}"
        response = _post(client, path, {"ids": [1]}, viewer_headers)
        assert response.status_code == 403, f"{path} 竟然允许 viewer 清除"
        anonymous = client.post(path, json={"ids": [1]})
        assert anonymous.status_code in (401, 403), f"{path} 匿名竟然可调用"


def test_purge_endpoints_forbid_anonymous_and_accept_admin(client, admin_headers):
    for path in PURGE_PATHS:
        response = _post(client, path, {"all": True}, admin_headers)
        assert response.status_code == 200, f"{path} 管理员清除失败：{response.get_json()}"


# ---------------------------------------------------------------------------
# 范围必须显式声明
# ---------------------------------------------------------------------------
def test_purge_refuses_empty_or_filter_only_body(client, admin_headers):
    for path in PURGE_PATHS:
        response = _post(client, path, {}, admin_headers)
        assert response.status_code == 400, f"{path} 空 body 竟然被接受"
        assert response.get_json()["code"] == "INVALID_ARGUMENT"

        # 只给筛选条件、不给 all=true：同样拒绝（否则「按条件清空」会变成静默全清）
        response = _post(client, path, {"keyword": "whoami"}, admin_headers)
        assert response.status_code == 400, f"{path} 只给筛选条件竟然被接受"


def test_purge_rejects_garbage_ids_and_before(client, admin_headers):
    for path in PURGE_PATHS:
        response = _post(client, path, {"ids": "1,2"}, admin_headers)
        assert response.status_code == 400 and response.get_json()["code"] == "INVALID_ARGUMENT"
        response = _post(client, path, {"ids": ["a"]}, admin_headers)
        assert response.status_code == 400
        response = _post(client, path, {"all": True, "before": "昨天"}, admin_headers)
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# 审计日志：按选择清除 / 清空 / 筛选清空 / 留痕
# ---------------------------------------------------------------------------
def test_purge_audits_by_ids_removes_only_selected(client, app, admin_headers):
    ids = _add_audits(app, 3)
    with app.app_context():
        before = AuditLog.query.count()

    response = _post(client, "/api/audits/delete", {"ids": ids[:2]}, admin_headers)
    body = response.get_json()
    assert response.status_code == 200
    assert body["data"]["deleted"] == 2
    assert "按选择清除" in body["data"]["scope"]

    with app.app_context():
        remaining = {row.id for row in AuditLog.query.all()}
        assert ids[2] in remaining, "没被选中的行不该被删"
        assert not ({ids[0], ids[1]} & remaining)
        # 清除动作本身留痕：删掉 2 行，新增 1 行 purge_audits
        assert AuditLog.query.count() == before - 2 + 1
        purge_row = AuditLog.query.filter_by(action="purge_audits").one()
        assert purge_row.detail["deleted"] == 2
        assert purge_row.actor_username == "admin"
        assert purge_row.category == "audit"


def test_purge_audits_all_keeps_the_purge_trace(client, app, admin_headers):
    _add_audits(app, 4)
    with app.app_context():
        total = AuditLog.query.count()

    response = _post(client, "/api/audits/delete", {"all": True}, admin_headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] == total

    with app.app_context():
        rows = AuditLog.query.all()
        assert len(rows) == 1, "清空后必须只剩下这条清除动作本身的审计"
        assert rows[0].action == "purge_audits"
        assert rows[0].detail["deleted"] == total
        assert rows[0].message.startswith("清除审计日志")


def test_purge_audits_with_filter_only_removes_matching_rows(client, app, admin_headers):
    _add_audits(app, 2, category="auth")
    _add_audits(app, 3, category="asset")
    with app.app_context():
        # 登录本身也会写 auth 审计，所以这里只断言「清除前后 auth 行数不变」
        auth_before = AuditLog.query.filter_by(category="auth").count()

    response = _post(client, "/api/audits/delete", {"all": True, "category": "asset"}, admin_headers)
    body = response.get_json()
    assert response.status_code == 200
    assert body["data"]["deleted"] == 3
    assert "category=asset" in body["data"]["scope"]

    with app.app_context():
        remaining = {row.id for row in AuditLog.query.filter_by(category="asset").all()}
        assert not remaining, "筛选命中的行没删干净"
        assert AuditLog.query.filter_by(category="auth").count() == auth_before, "没命中的行被误删"


def test_purge_audits_before_only_removes_older_rows(client, app, admin_headers):
    from app.extensions import db

    with app.app_context():
        old = AuditLog(ts=utcnow() - timedelta(days=30), category="auth", action="login")
        new = AuditLog(ts=utcnow(), category="auth", action="login")
        db.session.add_all([old, new])
        db.session.commit()
        old_id, new_id = old.id, new.id

    cutoff = (utcnow() - timedelta(days=7)).isoformat() + "Z"
    response = _post(
        client, "/api/audits/delete", {"all": True, "before": cutoff}, admin_headers
    )
    assert response.status_code == 200

    with app.app_context():
        remaining = {row.id for row in AuditLog.query.all()}
        assert old_id not in remaining
        assert new_id in remaining


# ---------------------------------------------------------------------------
# 命令日志
# ---------------------------------------------------------------------------
def _add_commands(app, session_id: int, commands: list[tuple[str, str]]) -> list[int]:
    with app.app_context():
        from app.extensions import db

        ids = []
        for seq, (command, action) in enumerate(commands, start=1):
            row = CommandLog(
                session_id=session_id,
                sid="sid-purge",
                seq=seq,
                username="purgeuser",
                host_name="purge-host",
                command=command,
                output="out",
                action=action,
                risk_level="low",
                started_at=utcnow(),
            )
            db.session.add(row)
            db.session.flush()
            ids.append(row.id)
        db.session.commit()
        return ids


def _add_session(app, *, status: str = "closed", transcript_path: str = "") -> int:
    with app.app_context():
        from app.extensions import db

        row = SessionRecord(
            sid=f"sid-{status}-{utcnow().timestamp()}",
            username="purgeuser",
            host_name="purge-host",
            host_address="127.0.0.1:22",
            source="web",
            status=status,
            started_at=utcnow(),
            transcript_path=transcript_path,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def test_purge_commands_by_ids_and_filter(client, app, admin_headers):
    session_id = _add_session(app)
    ids = _add_commands(app, session_id, [("whoami", "allow"), ("cat /etc/shadow", "deny")])

    response = _post(client, "/api/commands/delete", {"ids": [ids[0]]}, admin_headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] == 1
    with app.app_context():
        assert CommandLog.query.filter_by(id=ids[1]).count() == 1

    # 清空筛选结果：只清 deny
    response = _post(
        client, "/api/commands/delete", {"all": True, "action": "deny"}, admin_headers
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body["data"]["deleted"] == 1
    assert "action=deny" in body["data"]["scope"]
    with app.app_context():
        assert CommandLog.query.count() == 0
        assert AuditLog.query.filter_by(action="purge_commands").count() == 2


# ---------------------------------------------------------------------------
# 会话记录：级联命令日志 + 录像文件；在线会话不删
# ---------------------------------------------------------------------------
def test_purge_sessions_cascades_commands_and_transcripts(client, app, admin_headers, tmp_path):
    transcript = tmp_path / "sid-purge.log"
    transcript.write_text('{"t":"session_start"}\n', encoding="utf-8")
    session_id = _add_session(app, transcript_path=str(transcript))
    command_ids = _add_commands(app, session_id, [("whoami", "allow")])
    assert command_ids and transcript.exists()

    response = _post(client, "/api/sessions/delete", {"ids": [session_id]}, admin_headers)
    body = response.get_json()
    assert response.status_code == 200
    assert body["data"]["deleted"] == 1
    assert body["data"]["commands"] == 1
    assert body["data"]["transcripts"] == 1
    assert body["data"]["skippedActive"] == 0
    assert not transcript.exists(), "录像文件必须一起删掉，否则留下孤儿文件"

    with app.app_context():
        assert SessionRecord.query.filter_by(id=session_id).count() == 0
        assert CommandLog.query.filter_by(session_id=session_id).count() == 0
        assert AuditLog.query.filter_by(action="purge_sessions").count() == 1


def test_purge_sessions_never_deletes_active_sessions(client, app, admin_headers):
    active_id = _add_session(app, status="active")
    closed_id = _add_session(app, status="closed")

    response = _post(client, "/api/sessions/delete", {"ids": [active_id, closed_id]}, admin_headers)
    body = response.get_json()
    assert response.status_code == 200
    assert body["data"]["deleted"] == 1
    assert body["data"]["skippedActive"] == 1
    assert "跳过 1 条进行中" in body["message"]

    with app.app_context():
        assert SessionRecord.query.filter_by(id=active_id).count() == 1, "在线会话被删了"
        assert SessionRecord.query.filter_by(id=closed_id).count() == 0
