"""对抗测试（验收方编写）：链式哈希在「清哈希 / 截断 / 老库前缀」下到底守不守得住。

这里钉的是「审计流水不可篡改」这句话的**安全语义**，不是某一种实现写法。
用例红 = 当前实现与语义不符，就是施工图；修好它们，任务 A 才算验收通过。
依据：《修改记录》第 13 行「任意行字段被改 / 哈希被改 / 连接关系被改 → 逐行校验立即检出」、
第 113 行「老数据兼容：空串视为升级前遗留，避免上线即『红』」。
"""

from __future__ import annotations


def test_blanking_the_last_rows_hashes_must_be_reported_as_tampering(
    app, client, admin_headers
):
    """改字段后把本行两列哈希清空（伪装成「升级前遗留」）→ verify 必须判异常。

    验收现场：当前返回 ``{'ok': True, 'total': 5, 'verified': 4}``，篡改被隐藏。
    同一条记录在 ``GET /api/audits/chain`` 里又会因为 pending>0 判「不健康」——
    两条路结论互相矛盾；安全裁决以 verify（也就是 CLI 退出码）为准，所以必须是它红。
    修好之后：verify 与 /api/audits/chain 必须**同声**说「异常」（界面徽标与 CLI 退出码一致）。
    """
    from app.audit import log_event, verify_table_chain
    from app.extensions import db
    from app.models import AuditLog

    with app.app_context():
        for i in range(5):
            log_event("console", f"tamper-{i}", message=f"原始内容 {i}")

        victim = AuditLog.query.order_by(AuditLog.id.desc()).first()
        victim_id = victim.id
        victim.message = "被篡改后的内容"
        victim.prev_hash = ""
        victim.entry_hash = ""
        db.session.commit()

        result = verify_table_chain(AuditLog)

    assert result["ok"] is False, (
        f"清掉链尾那一行（id={victim_id}）的两列哈希后 verify 仍判 clean —— 篡改被隐藏：{result}"
    )
    assert any(str(victim_id) in str(err) for err in result["errors"]), (
        f"错误列表没点名出问题的行（id={victim_id}）：{result['errors']}"
    )
    assert result["pending_inside"] == 1, f"链内的空哈希行要单独计数：{result}"

    # 界面上的徽标（也就是管理员第一眼看到的东西）必须与 verify 同声
    body = client.get("/api/audits/chain", headers=admin_headers).get_json()
    assert body.get("success") is True, body
    data = body["data"]
    counts = data["counts"]["audit_logs"]
    assert data["healthy"] is False, f"verify 说异常，徽标却判健康：{data}"
    assert counts.get("pendingInside") == 1, counts


def test_legacy_prefix_must_not_make_a_healthy_chain_look_broken(app, client, admin_headers):
    """老库升级：前缀里那批「升级前遗留」空哈希行，不该把一条完好的链判成不健康。

    当前 ``/api/audits/chain`` 要求三张表 ``pending == 0``，而遗留行的空哈希永远不会被补上，
    于是只要库里有历史数据，徽标就**永远**是红的（正是《修改记录》第 113 行想避免的「上线即红」）。

    契约：空哈希行只允许出现在**链的前缀**（升级前遗留）；出现在链内即算异常，两者分开计数。
    """
    from app.audit import log_event
    from app.extensions import db
    from app.models import AuditLog

    with app.app_context():
        # 先造「升级前」的库：清掉夹具里那几条已哈希的种子行，否则遗留行会落在链**内部**
        AuditLog.query.delete()
        db.session.commit()
        for i in range(2):
            db.session.add(
                AuditLog(
                    category="console",
                    action="legacy",
                    result="success",
                    message=f"升级前遗留 {i}",
                    prev_hash="",
                    entry_hash="",
                )
            )
        db.session.commit()
        for i in range(3):
            log_event("console", f"after-upgrade-{i}", message=f"升级后 {i}")

    body = client.get("/api/audits/chain", headers=admin_headers).get_json()
    assert body.get("success") is True, body
    data = body["data"]
    counts = data["counts"]["audit_logs"]

    assert data["healthy"] is True, f"老库前缀把完好的链判成不健康（上线即红）：{counts}"
    assert counts.get("pendingInside") == 0, f"链内不该有空哈希行：{counts}"
    assert counts.get("pendingPrefix") == 2, f"前缀里的遗留行应被单独计数：{counts}"


def test_truncation_is_caught_only_against_an_out_of_db_anchor(app):
    """删尾行：库内自洽查不出来，必须拿「库外的锚点」比对。

    这条不是在指责实现，而是补齐链式哈希的固有边界：链只能证明「我手上这串是连续的」，
    证明不了「我没被砍掉尾巴」——后者需要锚点（每天把 head 抄到库外/远端）。

    建议契约：``verify_table_chain(model, *, expected_head=None)``；
    传了锚点而链尾对不上 → ``ok=False``，errors 里点名「锚点」。
    """
    from app.audit import log_event, verify_table_chain
    from app.extensions import db
    from app.models import AuditLog

    with app.app_context():
        for i in range(5):
            log_event("console", f"anchor-{i}", message=f"锚点样本 {i}")

        head = AuditLog.query.order_by(AuditLog.id.desc()).first().entry_hash
        assert head, "链尾应该有 entry_hash"

        # 攻击者：删掉最后两行
        for row in AuditLog.query.order_by(AuditLog.id.desc()).limit(2).all():
            db.session.delete(row)
        db.session.commit()

        no_anchor = verify_table_chain(AuditLog)
        with_anchor = verify_table_chain(AuditLog, expected_head=head)

    # 已知边界（这条保持绿）：库内自洽看不出截断
    assert no_anchor["ok"] is True, f"库内自洽本应看不出截断（这里在钉住这条已知边界）：{no_anchor}"
    # 施工图：锚点对不上必须判异常
    assert with_anchor["ok"] is False, f"锚点对不上却仍判 clean：{with_anchor}"
    assert any("锚点" in str(err) for err in with_anchor["errors"]), with_anchor["errors"]
