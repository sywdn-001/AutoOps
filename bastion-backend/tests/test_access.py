"""授权与时间窗单测：需求①「能碰哪台机器不能碰哪台机器」的判定逻辑。"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from app.access import (
    accessible_targets,
    active_grants,
    check_session_quota,
    current_session_count,
    find_access,
    grant_accounts,
    in_time_window,
    serialize_target,
)
from app.extensions import db
from app.models import Grant, Host, SessionRecord, User, utcnow

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


def _grant(app, grant_id):
    with app.app_context():
        return db.session.get(Grant, grant_id)


def _user(app, user_id):
    with app.app_context():
        return db.session.get(User, user_id)


# --------------------------------------------------------------------------- 时间窗
def test_disabled_grant_is_rejected(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops1", "ops")
    grant_id = make_grant(user_id, host_id, account_id, enabled=False)

    with app.app_context():
        ok, reason = in_time_window(db.session.get(Grant, grant_id))

    assert ok is False
    assert "停用" in reason


def test_expired_grant_is_rejected(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops2", "ops")
    grant_id = make_grant(
        user_id, host_id, account_id, expire_at=datetime.utcnow() - timedelta(days=1)
    )

    with app.app_context():
        ok, reason = in_time_window(db.session.get(Grant, grant_id))

    assert ok is False
    assert "过期" in reason


def test_weekday_window(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops3", "ops")
    grant_id = make_grant(user_id, host_id, account_id, weekdays=[0, 1, 2, 3, 4])

    monday = datetime(2026, 8, 3, 10, 0)  # 2026-08-03 是星期一
    saturday = datetime(2026, 8, 8, 10, 0)
    assert monday.weekday() == 0 and saturday.weekday() == 5

    with app.app_context():
        grant = db.session.get(Grant, grant_id)
        assert in_time_window(grant, monday)[0] is True
        ok, reason = in_time_window(grant, saturday)
        assert ok is False
        assert "星期" in reason


def test_cross_midnight_window(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops4", "ops")
    grant_id = make_grant(
        user_id, host_id, account_id, time_start=time(22, 0), time_end=time(6, 0)
    )

    with app.app_context():
        grant = db.session.get(Grant, grant_id)
        assert in_time_window(grant, datetime(2026, 8, 3, 23, 30))[0] is True
        assert in_time_window(grant, datetime(2026, 8, 3, 3, 0))[0] is True
        ok, reason = in_time_window(grant, datetime(2026, 8, 3, 12, 0))
        assert ok is False
        assert "时段" in reason


def test_daytime_window(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops5", "ops")
    grant_id = make_grant(
        user_id, host_id, account_id, time_start=time(9, 0), time_end=time(18, 0)
    )

    with app.app_context():
        grant = db.session.get(Grant, grant_id)
        assert in_time_window(grant, datetime(2026, 8, 3, 10, 0))[0] is True
        assert in_time_window(grant, datetime(2026, 8, 3, 8, 59))[0] is False
        assert in_time_window(grant, datetime(2026, 8, 3, 18, 1))[0] is False


# --------------------------------------------------------------------------- 授权解析
def test_active_grants_skips_disabled_host(app, make_host, make_account, make_user, make_grant):
    live_host = make_host(name="live-01")
    dead_host = make_host(name="dead-01", status="disabled")
    account_live = make_account(live_host)
    account_dead = make_account(dead_host)
    user_id = make_user("ops6", "ops")
    make_grant(user_id, live_host, account_live)
    make_grant(user_id, dead_host, account_dead)

    with app.app_context():
        user = db.session.get(User, user_id)
        hosts = {grant.host.name for grant in active_grants(user)}

    assert hosts == {"live-01"}


def test_find_access_prefers_exact_account(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_a = make_account(host_id, name="deploy", username="deploy")
    account_b = make_account(host_id, name="root", username="root")
    user_id = make_user("ops7", "ops")
    make_grant(user_id, host_id, None)  # 通配：整机
    make_grant(user_id, host_id, account_b)  # 精确：root

    with app.app_context():
        user = db.session.get(User, user_id)
        resolved = find_access(user, host_id=host_id)
        assert resolved is not None
        assert resolved.account.id == account_b

        explicit = find_access(user, host_id=host_id, account_id=account_a)
        assert explicit is not None
        assert explicit.account.id == account_a


def test_find_access_respects_can_login(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops8", "ops")
    make_grant(user_id, host_id, account_id, can_login=False)

    with app.app_context():
        user = db.session.get(User, user_id)
        assert find_access(user, host_id=host_id) is None
        assert find_access(user, host_id=host_id, require_login=False) is not None


def test_find_access_unknown_host_returns_none(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops9", "ops")
    make_grant(user_id, host_id, account_id)

    with app.app_context():
        user = db.session.get(User, user_id)
        assert find_access(user, host_id=99999) is None
        assert find_access(user, host_name="不存在的主机") is None


def test_find_access_wildcard_covers_named_account(app, make_host, make_account, make_user, make_grant):
    """整机授权下，用户点名的账号必须能连——但必须是同一个账号，不能换人。"""
    host_id = make_host()
    account_root = make_account(host_id, name="root", username="root")
    account_app = make_account(host_id, name="app", username="app")
    user_id = make_user("ops15", "ops")
    make_grant(user_id, host_id, None)

    with app.app_context():
        user = db.session.get(User, user_id)
        resolved = find_access(user, host_id=host_id, account_id=account_app)
        assert resolved is not None
        assert resolved.account.id == account_app

        # 不指定账号时也要能解析出一个可用账号，而不是 None
        auto = find_access(user, host_id=host_id)
        assert auto is not None and auto.account is not None


def test_find_access_never_substitutes_another_account(
    app, make_host, make_account, make_user, make_grant
):
    """只授权了 root，用户却点名 app 账号 —— 必须拒绝，绝不能降级成 root。"""
    host_id = make_host()
    account_root = make_account(host_id, name="root", username="root")
    account_app = make_account(host_id, name="app", username="app")
    user_id = make_user("ops16", "ops")
    make_grant(user_id, host_id, account_root)

    with app.app_context():
        user = db.session.get(User, user_id)
        assert find_access(user, host_id=host_id, account_id=account_app) is None
        resolved = find_access(user, host_id=host_id)
        assert resolved is not None and resolved.account.id == account_root


def test_find_access_account_must_belong_to_target_host(
    app, make_host, make_account, make_user, make_grant
):
    other_host = make_host(name="other-01")
    foreign_account = make_account(other_host, name="root", username="root")
    host_id = make_host(name="target-01")
    user_id = make_user("ops17", "ops")
    make_grant(user_id, host_id, None)  # 目标主机整机授权

    with app.app_context():
        user = db.session.get(User, user_id)
        # 拿另一台机器的账号 ID 来连目标机 -> 必须拒绝
        assert find_access(user, host_id=host_id, account_id=foreign_account) is None


# --------------------------------------------------------------------------- 菜单 / 账号
def test_grant_accounts_wildcard_returns_all(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    make_account(host_id, name="root", username="root")
    make_account(host_id, name="deploy", username="deploy")
    user_id = make_user("ops10", "ops")
    grant_id = make_grant(user_id, host_id, None)

    with app.app_context():
        grant = db.session.get(Grant, grant_id)
        accounts = grant_accounts(db.session.get(User, user_id), grant)

    assert {account.username for account in accounts} == {"root", "deploy"}


def test_accessible_targets_merges_grants(app, make_host, make_account, make_user, make_grant):
    host_id = make_host(name="merge-01")
    account_root = make_account(host_id, name="root", username="root")
    account_app = make_account(host_id, name="app", username="app")
    user_id = make_user("ops11", "ops")
    make_grant(user_id, host_id, account_root, can_sftp=True, can_webterm=False, max_sessions=2)
    make_grant(user_id, host_id, account_app, can_download=True, can_webterm=True, max_sessions=3)

    with app.app_context():
        entries = accessible_targets(db.session.get(User, user_id))

    assert len(entries) == 1
    entry = entries[0]
    assert entry["canSftp"] is True
    assert entry["canDownload"] is True
    assert entry["canWebterm"] is True
    assert entry["maxSessions"] == 3
    assert len(entry["accounts"]) == 2
    assert len(entry["grantIds"]) == 2

    payload = serialize_target(entry)
    assert payload["hostName"] == "merge-01"
    assert payload["port"] == 22
    assert len(payload["accounts"]) == 2


def test_accessible_targets_sorted_by_name(app, make_host, make_account, make_user, make_grant):
    ids = [make_host(name=name) for name in ("zeta", "alpha", "mid")]
    user_id = make_user("ops12", "ops")
    for host_id in ids:
        make_grant(user_id, host_id, make_account(host_id))

    with app.app_context():
        entries = accessible_targets(db.session.get(User, user_id))

    assert [entry["hostName"] for entry in entries] == ["alpha", "mid", "zeta"]


# --------------------------------------------------------------------------- 并发额度
def test_session_quota_counts_active_sessions(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops13", "ops")
    grant_id = make_grant(user_id, host_id, account_id, max_sessions=1)

    with app.app_context():
        user = db.session.get(User, user_id)
        grant = db.session.get(Grant, grant_id)
        assert check_session_quota(user, grant)[0] is True

        db.session.add(
            SessionRecord(
                sid="SID-TEST-0001",
                user_id=user_id,
                username="ops13",
                host_id=host_id,
                host_name="web-01",
                grant_id=grant_id,
                source="web",
                status="active",
                started_at=utcnow(),
            )
        )
        db.session.commit()

        assert current_session_count(user_id, grant_id) == 1
        ok, reason = check_session_quota(user, grant)
        assert ok is False
        assert "上限" in reason


def test_session_quota_unlimited_when_zero(app, make_host, make_account, make_user, make_grant):
    host_id = make_host()
    account_id = make_account(host_id)
    user_id = make_user("ops14", "ops")
    grant_id = make_grant(user_id, host_id, account_id, max_sessions=0)

    with app.app_context():
        user = db.session.get(User, user_id)
        grant = db.session.get(Grant, grant_id)
        for index in range(3):
            db.session.add(
                SessionRecord(
                    sid=f"SID-TEST-{index:04d}",
                    user_id=user_id,
                    username="ops14",
                    host_id=host_id,
                    host_name="web-01",
                    grant_id=grant_id,
                    source="gateway",
                    status="active",
                    started_at=utcnow(),
                )
            )
        db.session.commit()
        assert check_session_quota(user, grant)[0] is True


# --------------------------------------------------------------------------- 超级管理员兜底
# 背景：管理员在授权表里没有记录时，如果直接判「无权限」，新部署的堡垒机里
# 管理员自己打不开网页终端、网关菜单也是空的（演示与首次排障全被挡住）。
# 兜底只放宽准入，不放宽审计与策略；且必须是**未入库的临时 Grant**。


def test_superuser_gets_access_without_any_grant(app, make_host, make_account, make_user):
    host_id = make_host(name="su-host-01")
    account_id = make_account(host_id, name="root", username="root")
    user_id = make_user("su1", "admin", is_superuser=True)

    with app.app_context():
        before = Grant.query.count()
        user = db.session.get(User, user_id)
        resolved = find_access(user, host_id=host_id)
        assert resolved is not None
        assert resolved.host.id == host_id
        assert resolved.account.id == account_id
        assert resolved.grant.id is None, "兜底授权必须是临时对象，不能落库"
        assert resolved.grant.can_webterm is True
        assert resolved.grant.max_sessions == 0
        # 若临时 Grant 被 host.grants 反向引用级联进 session，这次 commit 就会写库
        db.session.commit()
        assert Grant.query.count() == before


def test_superuser_can_pass_explicit_account_of_same_host(app, make_host, make_account, make_user):
    host_id = make_host(name="su-host-02")
    make_account(host_id, name="root", username="root")
    app_account_id = make_account(host_id, name="app", username="appuser")
    user_id = make_user("su2", "admin", is_superuser=True)

    with app.app_context():
        user = db.session.get(User, user_id)
        resolved = find_access(user, host_id=host_id, account_id=app_account_id)
        assert resolved is not None
        assert resolved.account.id == app_account_id


def test_superuser_never_borrows_account_from_another_host(app, make_host, make_account, make_user):
    host_a = make_host(name="su-host-03a")
    make_account(host_a, name="root", username="root")
    host_b = make_host(name="su-host-03b")
    foreign_account = make_account(host_b, name="root", username="root")
    user_id = make_user("su3", "admin", is_superuser=True)

    with app.app_context():
        user = db.session.get(User, user_id)
        assert find_access(user, host_id=host_a, account_id=foreign_account) is None


def test_superuser_fallback_needs_active_host_and_usable_account(app, make_host, make_account, make_user):
    disabled_host = make_host(name="su-host-04", status="disabled")
    make_account(disabled_host)
    empty_host = make_host(name="su-host-05")
    user_id = make_user("su4", "admin", is_superuser=True)

    with app.app_context():
        user = db.session.get(User, user_id)
        assert find_access(user, host_id=disabled_host) is None
        # 主机在启用状态、但没有任何资产账号 -> 不能凭空造一个账号出来
        assert find_access(user, host_id=empty_host) is None


def test_accessible_targets_lists_every_active_host_for_superuser(
    app, make_host, make_account, make_user
):
    host_a = make_host(name="su-a-01")
    make_account(host_a, name="root", username="root")
    host_b = make_host(name="su-b-01")
    user_id = make_user("su5", "admin", is_superuser=True)

    with app.app_context():
        entries = accessible_targets(db.session.get(User, user_id))

    names = [entry["hostName"] for entry in entries]
    assert names == ["su-a-01", "su-b-01"]
    by_name = {entry["hostName"]: entry for entry in entries}
    assert by_name["su-a-01"]["accounts"], "有账号的主机必须列出账号"
    assert by_name["su-b-01"]["accounts"] == []
    assert by_name["su-a-01"]["grantIds"] == []
    assert by_name["su-a-01"]["canWebterm"] is True
    # 对外序列化不能少字段
    serialized = serialize_target(entries[0])
    assert {"hostId", "hostName", "address", "port", "osType", "policyName"} <= set(serialized)


def test_regular_user_still_requires_grant(app, make_host, make_account, make_user):
    """兜底只给超级管理员；普通角色没有授权就是没有权限（防止这次改动放水）。"""
    host_id = make_host(name="plain-01")
    make_account(host_id)
    user_id = make_user("plain-op", "ops")

    with app.app_context():
        user = db.session.get(User, user_id)
        assert find_access(user, host_id=host_id) is None
        assert accessible_targets(user) == []
