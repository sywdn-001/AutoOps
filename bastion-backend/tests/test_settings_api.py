"""系统设置接口的回归用例（浏览器实测揪出的两个真缺陷）。

1. 空 `default_policy_id` 被拒 → 参数设置页会**整页保存失败**。
   该键的默认值本身就是空串（未指定默认命令策略），必须允许留空。
2. `update_settings()` 旧实现返回的是「被忽略的键」，而调用方把它当成「发生变化的键」
   → 保存成功也提示「设置无变化」，且**永不写审计**（谁改了系统设置查不到）。
"""

from __future__ import annotations

from app.extensions import db
from app.models import AuditLog
from app.settings_store import get_setting


def test_empty_default_policy_is_accepted(client, admin_headers):
    """默认命令策略留空 = 未指定，不能因此让整份设置保存失败。"""
    resp = client.put(
        "/api/settings",
        json={"site_name": "AutoOps 堡垒机", "default_policy_id": ""},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    values = client.get("/api/settings", headers=admin_headers).get_json()["data"]["values"]
    assert values["default_policy_id"] == ""
    assert values["site_name"] == "AutoOps 堡垒机"


def test_none_default_policy_is_accepted(client, admin_headers):
    resp = client.put(
        "/api/settings",
        json={"site_name": "AutoOps 堡垒机", "default_policy_id": None},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()


def test_non_empty_default_policy_is_still_validated(client, admin_headers):
    """留空放行，但填了不存在的策略仍然要拦。"""
    resp = client.put(
        "/api/settings", json={"default_policy_id": 999999}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert "默认策略不存在" in resp.get_json()["message"]


def test_required_int_setting_still_rejects_empty(client, admin_headers):
    resp = client.put("/api/settings", json={"gateway_port": ""}, headers=admin_headers)
    assert resp.status_code == 400
    assert "必须是整数" in resp.get_json()["message"]


def test_changed_settings_are_reported_and_audited(client, app, admin_headers):
    resp = client.put(
        "/api/settings", json={"site_name": "AutoOps 堡垒机-审计"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert "已更新 1 项设置" in resp.get_json()["message"]

    with app.app_context():
        rows = AuditLog.query.filter_by(action="update_settings").all()
        assert rows, "修改系统设置必须留审计"
        assert "site_name" in (rows[-1].message or "")
        assert get_setting("site_name") == "AutoOps 堡垒机-审计"

    # 值没变时不谎报「已更新」
    again = client.put(
        "/api/settings", json={"site_name": "AutoOps 堡垒机-审计"}, headers=admin_headers
    )
    assert again.status_code == 200
    assert "设置无变化" in again.get_json()["message"]
    _ = db
