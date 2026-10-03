"""回归：老库升级时，内置角色权限与内置策略必须真的落库。

背景（真实缺陷，实测踩到）：
`seed_data()` 只在「本次新建了角色」时才置 `changed` 并提交，而老库里角色都已存在，
于是**角色权限的更新被丢掉**；`seed_policies()` / `seed_file_policies()` 也只 `flush` 不 `commit`。
后果：升级到带文件管理器的版本后，`ops` 角色拿不到 `file:use`（文件管理器接口全 403），
文件策略表为空（试算器输出「未绑定文件策略，按审计放行处理」，把 `/etc/passwd` 的写操作判成放行）。

本用例把库改造成「老库」状态，再跑一次 `seed_data()`，断言内置角色权限与内置策略都补回来。
"""

from __future__ import annotations

from app import seed_data
from app.extensions import db as _db
from app.file_policy import evaluate_file_policy
from app.models import FilePolicy, FileRule, Role, SystemSetting


def _make_legacy_database() -> dict:
    """把当前库改造成「升级前」的样子：无文件策略、ops 角色是旧权限集合。"""
    FileRule.query.delete()
    FilePolicy.query.delete()
    _db.session.query(SystemSetting).filter_by(key="builtin_file_policy_rev").delete()
    ops = Role.query.filter_by(code="ops").one()
    ops.permissions = [p for p in (ops.permissions or []) if not p.startswith("file")]
    _db.session.commit()
    return {"filePolicies": FilePolicy.query.count(), "opsPerms": list(ops.permissions or [])}


def test_reseed_on_existing_database_repairs_roles_and_policies(app):
    with app.app_context():
        legacy = _make_legacy_database()
        assert legacy["filePolicies"] == 0
        assert "file:use" not in legacy["opsPerms"]

        # 等价于「升级后重启进程」：老库里角色都在，种子必须把新权限与新策略补上
        seed_data(app)
        _db.session.expire_all()

        ops = Role.query.filter_by(code="ops").one()
        assert "file:use" in (ops.permissions or [])
        assert "filepolicy:view" in (ops.permissions or [])
        assert (Role.query.filter_by(code="admin").one().permissions or []) == ["*"]

        policies = FilePolicy.query.order_by(FilePolicy.id).all()
        assert len(policies) >= 2
        assert any(policy.is_default for policy in policies)
        assert sum(len(policy.rules or []) for policy in policies) > 0

        rev = _db.session.get(SystemSetting, "builtin_file_policy_rev")
        assert rev is not None and str(rev.value or "") != ""


def test_reseeded_default_policy_still_blocks_writing_into_system_dirs(app):
    """升级后默认文件策略必须真的能拦：写 /etc/passwd 不能落到「未绑定策略按放行处理」。"""
    with app.app_context():
        _make_legacy_database()
        seed_data(app)
        _db.session.expire_all()

        default_policy = FilePolicy.query.filter_by(is_default=True).one()
        decision = evaluate_file_policy(default_policy, "write", "/etc/passwd")
        assert decision.allowed is False
        assert decision.rule_id is not None
        assert decision.risk_level in ("high", "critical")
