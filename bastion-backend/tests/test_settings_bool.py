"""布尔设置项回归测试。

线上 Bug：`PUT /api/settings` 把 bool 键 parse 成 Python True/False，`set_setting()`
又用 `str()` 落库成 `"True"/"False"`；前端只认 `"1"/"true"`，于是「保存一次之后
所有开关都变成关闭」。这里锁死三件事：

1. 写入必须落成 `"1"/"0"`；
2. 读取（含历史脏数据）必须归一化成 `"1"/"0"`；
3. `ensure_defaults()` 要把老库里的 `"True"/"False"` 洗掉。
"""

from __future__ import annotations

from app.extensions import db
from app.models import SystemSetting
from app.settings_store import ensure_defaults, get_setting


def _items(client, headers) -> dict:
    body = client.get("/api/settings", headers=headers).get_json()
    return {item["key"]: item for item in body["data"]["items"]}


def test_bool_put_stores_canonical(client, admin_headers, app):
    resp = client.put(
        "/api/settings",
        json={"values": {"notify_on_deny": "1", "record_raw_transcript": True}},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["values"]["notify_on_deny"] == "1"
    assert data["values"]["record_raw_transcript"] == "1"

    items = _items(client, admin_headers)
    assert items["notify_on_deny"]["type"] == "bool"
    assert items["notify_on_deny"]["value"] == "1"
    assert items["record_raw_transcript"]["value"] == "1"

    with app.app_context():
        assert db.session.get(SystemSetting, "notify_on_deny").value == "1"
        assert db.session.get(SystemSetting, "record_raw_transcript").value == "1"
        assert get_setting("notify_on_deny") == "1"


def test_bool_put_false_and_true_toggle(client, admin_headers, app):
    client.put("/api/settings", json={"values": {"notify_on_deny": "1"}}, headers=admin_headers)
    client.put("/api/settings", json={"values": {"notify_on_deny": "0"}}, headers=admin_headers)
    with app.app_context():
        assert db.session.get(SystemSetting, "notify_on_deny").value == "0"
    # 传 "False" 这种历史字符串也必须判假，不能因为非空就当真
    client.put("/api/settings", json={"values": {"notify_on_deny": "False"}}, headers=admin_headers)
    with app.app_context():
        assert db.session.get(SystemSetting, "notify_on_deny").value == "0"


def test_legacy_true_false_normalized_on_read(client, admin_headers, app):
    with app.app_context():
        db.session.get(SystemSetting, "notify_on_deny").value = "True"
        db.session.get(SystemSetting, "record_raw_transcript").value = "False"
        db.session.commit()
        assert get_setting("notify_on_deny") == "1"
        assert get_setting("record_raw_transcript") == "0"

    items = _items(client, admin_headers)
    assert items["notify_on_deny"]["value"] == "1"
    assert items["record_raw_transcript"]["value"] == "0"


def test_ensure_defaults_scrubs_legacy_rows(app):
    with app.app_context():
        db.session.get(SystemSetting, "notify_on_deny").value = "False"
        db.session.get(SystemSetting, "record_raw_transcript").value = "True"
        db.session.commit()
        ensure_defaults()
        assert db.session.get(SystemSetting, "notify_on_deny").value == "0"
        assert db.session.get(SystemSetting, "record_raw_transcript").value == "1"


def test_partial_put_keeps_other_switches(client, admin_headers, app):
    """只提交一个开关时，其它开关的库值不能被连带改掉。"""
    client.put(
        "/api/settings",
        json={"values": {"notify_on_deny": "1", "record_raw_transcript": "1"}},
        headers=admin_headers,
    )
    client.put("/api/settings", json={"values": {"notify_on_deny": "0"}}, headers=admin_headers)
    with app.app_context():
        assert db.session.get(SystemSetting, "notify_on_deny").value == "0"
        assert db.session.get(SystemSetting, "record_raw_transcript").value == "1"
