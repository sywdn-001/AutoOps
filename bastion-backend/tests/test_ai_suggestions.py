"""AI 输入框示例建议的接口回归：双语、按状态生成、可换一批。"""

from __future__ import annotations


def test_ai_suggestions_are_bilingual_and_rotate(app, client, admin_headers):
    first = client.get("/api/ai/suggestions?scope=chat&nonce=0", headers=admin_headers)
    assert first.status_code == 200
    data = first.get_json()["data"]
    assert data["bucket"], "要带当天的桶，前端按它判断 24 小时一轮"
    assert len(data["items"]) == 4
    for item in data["items"]:
        assert item["zh"] and item["en"], "每条建议都必须中英双语"
        assert item["zh"] != item["en"], "英文不该和中文一样（漏译）"

    rotated = client.get("/api/ai/suggestions?scope=chat&nonce=1", headers=admin_headers)
    assert rotated.status_code == 200
    assert rotated.get_json()["data"]["items"] != data["items"], "nonce+1 应该换一批"

    same_day = client.get("/api/ai/suggestions?scope=chat&nonce=0", headers=admin_headers)
    assert same_day.get_json()["data"]["items"] == data["items"], "同一天同一批要稳定"


def test_computer_use_suggestions_are_about_the_machine(app, client, admin_headers):
    resp = client.get(
        "/api/ai/suggestions?scope=computeruse", headers=admin_headers
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert len(data["items"]) == 3
    assert any("屏幕" in item["zh"] or "截图" in item["zh"] for item in data["items"])


def test_suggestions_require_login(client):
    assert client.get("/api/ai/suggestions").status_code == 401
