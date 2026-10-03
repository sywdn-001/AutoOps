"""审计链式哈希 507 +8 = 至少 8 例。"""

from __future__ import annotations

import pytest


def pytest_configure():
    pass


def test_new_audit_event_has_both_hashes(app):
    """新写入的审计流水应当 prev_hash/entry_hash 都非空，且第 1 条 prev_hash 为空。"""
    from app.audit import log_event

    with app.app_context():
        log_event("console", "test:chain-1", message="hello 1")
        log_event("console", "test:chain-2", message="hello 2")

        from app.models import AuditLog

        rows = AuditLog.query.order_by(AuditLog.id).all()
        assert len(rows) >= 2
        first, second = rows[-2], rows[-1]
        assert first.entry_hash, "第 1 条 entry_hash 应非空"
        assert second.prev_hash == first.entry_hash
        assert second.entry_hash and second.entry_hash != first.entry_hash


def test_audit_chain_self_consistent_after_multiple_writes(app):
    """连写 15 条，每条 prev_hash 必须严格等于上一条 entry_hash。"""
    from app.audit import log_event

    with app.app_context():
        for i in range(15):
            log_event("console", f"test:chain-sample-{i}", message=f"hello-{i}")

        from app.models import AuditLog

        rows = AuditLog.query.order_by(AuditLog.id).all()
        # 只关心本测试刚写入的最后 15 条
        tail = rows[-15:]
        for idx in range(1, len(tail)):
            assert tail[idx].prev_hash == tail[idx - 1].entry_hash, (
                f"第 {idx} 条 prev_hash 不匹配上一条的 entry_hash"
            )


def test_verify_table_chain_passes_for_clean_writes(app):
    """写一批后 verify_table_chain 必须 ok=True。"""
    from app.audit import log_event, verify_table_chain

    with app.app_context():
        for i in range(6):
            log_event("auth", f"login:{i}", message=f"signin-{i}")

        from app.models import AuditLog

        result = verify_table_chain(AuditLog)
        assert result["ok"] is True
        assert result["verified"] >= 6
        assert result["first_bad"] is None
        assert result["errors"] == []


def test_tampered_message_detected_as_entry_hash_mismatch(app):
    """手动篡改 message，verify_table_chain 应返回 entry_hash 校验失败。"""
    from app.audit import log_event, verify_table_chain
    from app.extensions import db

    with app.app_context():
        for i in range(4):
            log_event("console", f"t{i}", message=f"before-{i}")

        from app.models import AuditLog

        victim = AuditLog.query.order_by(AuditLog.id.desc()).offset(1).first()
        victim_id = victim.id
        original_msg = victim.message
        victim.message = "已被篡改！不应该通过校验"
        db.session.commit()

        result = verify_table_chain(AuditLog)
        assert result["ok"] is False
        assert result["first_bad"] == victim_id
        ids = [e["id"] for e in result["errors"]]
        assert victim_id in ids
        reasons = [e["reason"] for e in result["errors"] if e["id"] == victim_id]
        assert any("entry_hash 校验失败" in r for r in reasons), f"reasons={reasons}"
        victim.message = original_msg
        db.session.commit()


def test_rewiring_prev_hash_detected(app):
    """把第 2 条的 prev_hash 改成与第 1 条无关的串，应被识别为 prev_hash 不匹配。"""
    from app.audit import log_event, verify_table_chain
    from app.extensions import db

    with app.app_context():
        for i in range(5):
            log_event("console", f"r{i}", message=f"row-{i}")

        from app.models import AuditLog

        rows = AuditLog.query.order_by(AuditLog.id).all()
        first, second = rows[-5], rows[-4]
        second_id = second.id
        second.prev_hash = "0" * 64
        db.session.commit()

        result = verify_table_chain(AuditLog)
        assert result["ok"] is False
        assert result["first_bad"] == second_id


def test_command_and_file_logs_also_have_chain_hashes(app):
    """command_logs / file_logs 也要带上两个哈希。"""
    from app.audit import log_command, log_file_op, new_session

    with app.app_context():
        from app.models import CommandLog, FileLog

        session = new_session(
            username="opsadmin",
            role_code="ops",
            host_name="demo",
            status="closed",
        )
        for i in range(4):
            log_command(
                session=session,
                command=f"echo cmd-{i}",
                output=f"out-{i}\n",
            )
        for i in range(3):
            log_file_op(
                session=session,
                operation="read",
                path=f"/tmp/f{i}.txt",
            )

        cmds = CommandLog.query.order_by(CommandLog.id).all()[-4:]
        files = FileLog.query.order_by(FileLog.id).all()[-3:]
        for c in cmds:
            assert c.entry_hash, f"command id={c.id} 缺 entry_hash"
        for f in files:
            assert f.entry_hash, f"file id={f.id} 缺 entry_hash"
        for idx in range(1, len(cmds)):
            assert cmds[idx].prev_hash == cmds[idx - 1].entry_hash
        for idx in range(1, len(files)):
            assert files[idx].prev_hash == files[idx - 1].entry_hash


def test_verify_all_chains_reports_tables_and_heads(app):
    """verify_all_chains 应返回三张表的状态 + heads。"""
    from app.audit import log_event, verify_all_chains

    with app.app_context():
        log_event("asset", "x", message="sample")

        result = verify_all_chains()
        assert "ok" in result
        assert "tables" in result
        assert "heads" in result
        for key in ("audit_logs", "command_logs", "file_logs"):
            assert key in result["tables"]
            assert key in result["heads"]
            info = result["tables"][key]
            assert set(info.keys()) >= {"ok", "total", "verified", "first_bad", "errors"}
            head = result["heads"][key]
            assert "lastId" in head
            assert "lastEntryHash" in head


def test_chain_compute_is_deterministic():
    """compute_entry_hash 对相同输入必须稳定，对不同输入必须不同。"""
    from app.models import compute_entry_hash

    key = "my-test-secret"
    fields = [("id", 1), ("msg", "hello"), ("ts", 1700000000)]
    h1 = compute_entry_hash(
        table="audit_logs", fields=fields, prev_hash="a" * 64, secret_key=key
    )
    h2 = compute_entry_hash(
        table="audit_logs", fields=fields, prev_hash="a" * 64, secret_key=key
    )
    h3 = compute_entry_hash(
        table="audit_logs", fields=list(fields) + [("x", 1)], prev_hash="a" * 64, secret_key=key
    )
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64
    h4 = compute_entry_hash(
        table="audit_logs", fields=fields, prev_hash="a" * 64, secret_key="another-key"
    )
    assert h1 != h4


def test_legacy_blank_rows_are_skipped_only_before_the_chain_starts(app):
    """老库迁移补列后：**链起步之前**的空 entry_hash 行 verify 跳过、不报错。

    语义修订（2026-10，对抗测试驱动）：空哈希行只允许出现在链的前缀 —— 那才是「升级前遗留」。
    链一旦起步再出现空哈希行（例如有人把某行哈希清空、冒充遗留数据来藏篡改）会被判为异常，
    见 tests/test_audit_chain_tamper.py::test_blanking_the_last_rows_hashes_must_be_reported_as_tampering。
    """
    from app.audit import log_event, verify_table_chain
    from app.extensions import db

    with app.app_context():
        from app.models import AuditLog

        # 造一个「升级前」的库：先清掉夹具里那几条已哈希的种子行，再放两行无哈希的老数据
        AuditLog.query.delete()
        db.session.commit()
        for i in range(2):
            db.session.add(
                AuditLog(
                    category="legacy",
                    action=f"legacy{i}",
                    result="success",
                    message=f"legacy row {i}",
                    prev_hash="",
                    entry_hash="",
                )
            )
        db.session.commit()

        log_event("console", "new-one", message="链内新记录")
        log_event("console", "new-two", message="链内新记录-2")

        result = verify_table_chain(AuditLog)

    assert result["ok"] is True, result
    assert result["prefix"] == 2, result
    assert result["pending_inside"] == 0, result
    assert result["verified"] == 2, result
    assert result["errors"] == [], result
