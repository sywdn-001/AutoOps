"""文件管理器服务层集成测试：真 SFTP 服务端 + 真策略 + 真审计落库。

覆盖要点（对应需求「每一个操作全部加入审计和访问控制」）：
* 浏览 / 读取 / 写入 / 上传 / 下载 / 新建目录 / 改名 / 移动 / 复制 / 删除 / 改权限 / 打包；
* 每一个动作都必须在 ``file_logs`` 里留下一条记录（含成功与被拦）；
* 被拦的动作：策略命中 deny → ``FileError(FILE_DENIED)`` + ``result="denied"`` + 命中规则 ID；
* 授权开关（can_upload / can_download / can_file_write）与文件策略是**与**关系；
* 双路径校验：把文件复制进系统目录，目标路径也要过策略；
* 会话关闭后 ``session_records`` 收口、在线注册表清空。
"""

from __future__ import annotations

import io
import re
import time

import pytest

from app.extensions import db
from app.files import service as files
from app.models import AuditLog, FileLog, FilePolicy, Grant, SessionRecord, User
from .sftp_target import sftp_target  # noqa: F401 - pytest 夹具

pytestmark = pytest.mark.usefixtures("sftp_target")


@pytest.fixture(autouse=True)
def _app_context(app):
    """整模块跑在应用上下文里：服务层要落审计（db.session + log_event），
    没有上下文就会 `RuntimeError: Working outside of application context`。"""
    with app.app_context():
        yield


def _session_for(
    app,
    sftp_target,
    make_user,
    make_host,
    make_account,
    make_grant,
    *,
    can_upload=True,
    can_download=True,
    can_file_write=True,
    file_policy_id=None,
    user_kwargs=None,
):
    """造一台指向假目标机的授权，返回 (user_id, host_id, account_id, grant_id)。"""
    username = "filers"
    user_id = make_user(username, role_code="ops", **(user_kwargs or {}))
    host_id = make_host("sftp-01", address=sftp_target.host, port=sftp_target.port)
    account_id = make_account(
        host_id,
        name="root",
        username=sftp_target.username,
        password=sftp_target.password,
    )
    grant_id = make_grant(
        user_id,
        host_id,
        account_id,
        can_sftp=True,
        can_upload=can_upload,
        can_download=can_download,
        can_file_write=can_file_write,
        file_policy_id=file_policy_id,
    )
    return user_id, host_id, account_id, grant_id


def _open(app, user_id, host_id, account_id):
    with app.app_context():
        user = db.session.get(User, user_id)
        return files.open_file_session(app, user=user, host_id=host_id, account_id=account_id)


def _ops_of(sid: str) -> list[tuple[str, str, str]]:
    """返回该会话的 (operation, result, action) 列表（按 id 升序）。"""
    rows = FileLog.query.filter_by(sid=sid).order_by(FileLog.id.asc()).all()
    return [(r.operation, r.result, r.action) for r in rows]


def test_open_session_lists_root_and_audits(app, sftp_target, make_user, make_host, make_account, make_grant):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        assert session.sid and session.home_dir == "/"
        listing = files.list_dir(session, "/")
        names = {entry["name"] for entry in listing["entries"]}
        assert {"readme.txt", "docs", "logs", "data", ".ssh"} <= names
        assert listing["path"] == "/"

        rows = _ops_of(session.sid)
        assert ("list", "success", "allow") in rows
    finally:
        files.close_file_session(session.sid)


def test_read_text_and_denied_credential_read_is_audited(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        payload = files.read_text(session, "/readme.txt")
        assert "AutoOps 演示目标机文件区" in payload["content"]
        assert payload["truncated"] is False

        with pytest.raises(files.FileError) as excinfo:
            files.read_text(session, "/.ssh/id_rsa")
        assert excinfo.value.code == "FILE_DENIED"
        assert excinfo.value.status == 403

        rows = FileLog.query.filter_by(sid=session.sid).order_by(FileLog.id.asc()).all()
        denied = [row for row in rows if row.operation == "read" and row.result == "denied"]
        assert denied, "被拦的读取必须落一条 denied 记录"
        assert denied[-1].matched_rule_id is not None
        assert denied[-1].action == "deny"
        assert denied[-1].risk_level in {"high", "critical"}
        # 同时要有一条操作审计（category=file）
        audits = AuditLog.query.filter_by(category="file", action="file_read_denied").all()
        assert audits, "被拦操作要同时进操作审计"
        assert audits[-1].result == "failure"
    finally:
        files.close_file_session(session.sid)


def test_upload_write_download_roundtrip(app, sftp_target, make_user, make_host, make_account, make_grant):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        # 上传
        result = files.upload_file(
            session, "/tmp/upload.bin", io.BytesIO(b"hello-bastion" * 8), declared_size=104
        )
        assert result["size"] == 104
        assert sftp_target.read("/tmp/upload.bin") == b"hello-bastion" * 8

        # 编辑保存
        files.write_text(session, "/tmp/note.txt", "第一行\n第二行\n")
        assert "第二行" in files.read_text(session, "/tmp/note.txt")["content"]

        # 下载（流式）
        payload = files.download_file(session, "/tmp/upload.bin")
        chunks = list(files.stream_download(payload))
        assert b"".join(chunks) == b"hello-bastion" * 8

        ops = _ops_of(session.sid)
        for expected in ("upload", "write", "read", "download"):
            assert any(op == expected and res == "success" for op, res, _ in ops), f"缺少 {expected} 审计"
    finally:
        files.close_file_session(session.sid)


def test_rights_switch_blocks_upload_and_is_audited(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    ids = _session_for(
        app, sftp_target, make_user, make_host, make_account, make_grant, can_upload=False
    )
    session = _open(app, *ids[:3])
    try:
        with pytest.raises(files.FileError) as excinfo:
            files.upload_file(session, "/tmp/nope.txt", io.BytesIO(b"x"))
        assert excinfo.value.code in {"FILE_DENIED", "UPLOAD_FORBIDDEN"}
        assert not sftp_target.exists("/tmp/nope.txt")
        rows = _ops_of(session.sid)
        assert any(op == "upload" and res == "denied" for op, res, _ in rows)
    finally:
        files.close_file_session(session.sid)


def test_mkdir_rename_copy_delete_chmod_are_all_audited(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        files.mkdir(session, "/tmp/work", parents=True)
        files.write_text(session, "/tmp/work/a.txt", "content-a")
        files.rename_path(session, "/tmp/work/a.txt", "b.txt")
        files.copy_path(session, "/tmp/work/b.txt", "/tmp/work/c.txt")
        chmod_result = files.chmod_path(session, "/tmp/work/c.txt", 0o600)
        assert chmod_result["requested"] == "0600"
        # after 是回读的实际权限（Windows 上 os.chmod 只能切只读位，因此不能断言等于请求值）
        assert chmod_result["after"] == files.stat_path(session, "/tmp/work/c.txt")["modeOctal"]
        assert re.fullmatch(r"\d{4}", chmod_result["after"])
        removed = files.delete_paths(session, ["/tmp/work"], recursive=True)
        assert removed["deleted"] == ["/tmp/work"]
        assert not sftp_target.exists("/tmp/work")

        ops = {op for op, _res, _action in _ops_of(session.sid)}
        assert {"mkdir", "write", "rename", "copy", "chmod", "delete"} <= ops
    finally:
        files.close_file_session(session.sid)


def test_copy_into_system_dir_is_blocked_on_target_path(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        with pytest.raises(files.FileError) as excinfo:
            files.copy_path(session, "/readme.txt", "/etc/passwd")
        assert excinfo.value.code == "FILE_DENIED"
        assert not sftp_target.exists("/etc/passwd")
        rows = FileLog.query.filter_by(sid=session.sid, operation="copy").all()
        assert rows and rows[-1].result == "denied"
        assert rows[-1].target_path == "/etc/passwd"
    finally:
        files.close_file_session(session.sid)


def test_check_operation_never_writes_audit(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        allowed = files.check_operation(session, "upload", "/tmp/x.txt")
        assert allowed["allowed"] is True
        denied = files.check_operation(session, "upload", "/etc/cron.d/evil")
        assert denied["allowed"] is False
        assert denied["reason"]
        assert denied["ruleId"] is not None
        # 试算（按钮置灰）不应该产生审计噪声
        assert FileLog.query.filter_by(sid=session.sid).count() == 0
    finally:
        files.close_file_session(session.sid)


def test_archive_paths_builds_zip(app, sftp_target, make_user, make_host, make_account, make_grant):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        payload = files.archive_paths(session, ["/docs"])
        assert payload["filename"].endswith(".zip")
        assert payload["entries"] >= 2
        import zipfile

        with zipfile.ZipFile(payload["tempPath"]) as archive:
            names = archive.namelist()
        assert any(name.endswith("notes.txt") for name in names)
        assert any(op == "archive" for op, _res, _action in _ops_of(session.sid))
        files._cleanup_temp(payload["tempPath"])
    finally:
        files.close_file_session(session.sid)


def test_readonly_file_policy_denies_writes(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    with app.app_context():
        policy = FilePolicy.query.filter_by(name="只读文件策略").first()
        assert policy is not None
        policy_id = policy.id

    ids = _session_for(
        app, sftp_target, make_user, make_host, make_account, make_grant, file_policy_id=policy_id
    )
    session = _open(app, *ids[:3])
    try:
        # 只读策略白名单放行根目录浏览
        listing = files.list_dir(session, "/")
        assert listing["entries"]
        with pytest.raises(files.FileError) as excinfo:
            files.write_text(session, "/tmp/nope.txt", "x")
        assert excinfo.value.code == "FILE_DENIED"
        with pytest.raises(files.FileError):
            files.mkdir(session, "/tmp/newdir")
    finally:
        files.close_file_session(session.sid)


def test_close_session_reconciles_record_and_registry(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    sid = session.sid
    assert files.get_file_session(sid) is not None

    assert files.close_file_session(sid) is True
    assert files.get_file_session(sid) is None
    with app.app_context():
        record = SessionRecord.query.filter_by(sid=sid).first()
        assert record is not None
        assert record.status in {"closed", "terminated"}
        assert record.protocol == "sftp"
        assert record.source == "web"
        assert record.command_count >= 0


def test_grant_changes_apply_to_the_open_session(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    """授权开关每次操作都重新读授权：管理员补开/撤销「改文件」在已打开的窗口立即生效。

    背景（真机联调抓到的缺陷）：会话打开时把 can_file_write/can_upload 拍平成字段，
    管理员随后 PUT 授权对**已打开**的窗口无效 —— 撤销高危能力必须当场生效，
    补开能力也不该逼用户关窗口重开。
    """
    ids = _session_for(
        app, sftp_target, make_user, make_host, make_account, make_grant, can_file_write=False
    )
    user_id, host_id, account_id, grant_id = ids
    session = _open(app, user_id, host_id, account_id)
    try:
        with pytest.raises(files.FileError) as denied:
            files.mkdir(session, "/docs/denied-dir")
        assert denied.value.code == "FILE_DENIED"
        assert denied.value.status == 403
        assert not sftp_target.exists("/docs/denied-dir")

        with app.app_context():
            grant = db.session.get(Grant, grant_id)
            grant.can_file_write = True
            db.session.commit()
        assert files.capabilities(session)["canFileWrite"] is True
        assert files.mkdir(session, "/docs/allowed-dir")["path"] == "/docs/allowed-dir"
        assert sftp_target.exists("/docs/allowed-dir")

        with app.app_context():
            grant = db.session.get(Grant, grant_id)
            grant.can_file_write = False
            db.session.commit()
        with pytest.raises(files.FileError) as revoked:
            files.mkdir(session, "/docs/revoked-dir")
        assert revoked.value.code == "FILE_DENIED"
        assert not sftp_target.exists("/docs/revoked-dir")
    finally:
        files.close_file_session(session.sid)


def test_archive_missing_path_fails_cleanly_and_is_audited(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    """打包一个不存在的路径：返回 404 业务错误（不是 500），并且失败也要留审计。

    背景（真机联调抓到的缺陷）：`_collect_archive_members` 直接调 sftp.stat，
    远端 ENOENT 抛成未处理异常 → HTTP 500，且这条失败操作在审计里完全消失。
    """
    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        with pytest.raises(files.FileError) as excinfo:
            files.archive_paths(session, ["/data/never-existed.txt"])
        assert excinfo.value.code == "FILE_NOT_FOUND"
        assert excinfo.value.status == 404

        rows = FileLog.query.filter_by(sid=session.sid, operation="archive").all()
        assert len(rows) == 1
        assert rows[0].result == "failure"
        assert rows[0].action == "allow"
        assert rows[0].message
    finally:
        files.close_file_session(session.sid)


def test_abandoned_file_session_is_reaped_by_idle_sweeper(
    app, sftp_target, make_user, make_host, make_account, make_grant
):
    """窗口被遗弃（关标签页 / 断网）后由空闲清理收口。

    背景（真机实测）：关掉文件管理器标签页不会触发卸载清理，8 条会话泄漏在注册表里，
    正好撞满 MAX_SESSIONS_PER_USER → 打不开新窗口。参数设置里的会话空闲超时原来
    没有任何地方读取，这里把它钉住。
    """
    from app import session_registry
    from app.idle_sweeper import IDLE_REASON, sweep_once

    ids = _session_for(app, sftp_target, make_user, make_host, make_account, make_grant)
    session = _open(app, *ids[:3])
    try:
        assert session.sid in [row["sid"] for row in files.list_live_sessions()]
        # 生产里 FileSession.touch() 会同时更新注册表；测试直接把两边都拨到 1 小时前
        session.last_active = time.time() - 3600
        with session_registry._lock:
            session_registry._sessions[session.sid]["last_active"] = time.time() - 3600

        closed = sweep_once(600)

        assert [row["sid"] for row in closed] == [session.sid]
        assert closed[0]["kind"] == "file"
        assert closed[0]["username"] == "filers"
        assert session.sid not in [row["sid"] for row in files.list_live_sessions()]
        assert session_registry.get(session.sid) is None
        record = db.session.get(SessionRecord, session.record_id)
        assert record is not None
        assert record.status == "terminated"
        assert record.end_reason == IDLE_REASON
    finally:
        files.close_file_session(session.sid)


def test_failure_audit_message_is_framed_in_chinese_but_keeps_the_raw_error():
    """失败原因套一层中文框，异常原文一字不改。

    背景（用户报）：审计事件是给人看的，可库里抛出来的异常是英文（`EOF`、
    `Permission denied`），运维得先猜「这是哪个操作失败了」；而 `operation` /
    `action` / `result` 这些**给机器看的**字段必须继续是英文。
    """
    assert files._human_error("chmod", "Permission denied") == "修改文件权限失败：Permission denied"
    # 本来就带中文（策略拒绝原因）不动它，避免中文再套一层中文
    assert files._human_error("chmod", "该文件在系统目录，禁止修改") == "该文件在系统目录，禁止修改"
    # 没话说的时候不硬造一句
    assert files._human_error("chmod", "") == ""
    assert files._human_error("chmod", "   ") == "   "
