"""AI 输入框示例建议的接口回归：调模型现生成、缓存一轮、手动换一批、失败兜底。"""

from __future__ import annotations

from app.api import ai as ai_api


def _stub_cache(monkeypatch):
    store: dict[str, dict] = {}
    monkeypatch.setattr(ai_api, "_read_suggestion_cache", lambda scope: store.get(scope))
    monkeypatch.setattr(
        ai_api,
        "_write_suggestion_cache",
        lambda scope, payload: store.__setitem__(scope, payload),
    )
    return store


def test_suggestions_are_generated_by_the_model_and_cached(
    app, client, admin_headers, monkeypatch
):
    _stub_cache(monkeypatch)
    calls = []

    def fake_generate(scope, facts, size, avoid):
        calls.append((scope, size, list(avoid)))
        base = len(calls)
        return [
            {"zh": f"模型建议{base}-{i}", "en": f"Model hint {base}-{i}"}
            for i in range(size)
        ]

    monkeypatch.setattr(ai_api, "_generate_items", fake_generate)

    first = client.get("/api/ai/suggestions?scope=chat&nonce=0", headers=admin_headers)
    assert first.status_code == 200
    data = first.get_json()["data"]
    assert data["source"] == "ai"
    assert len(data["items"]) == 4
    assert all(item["zh"] and item["en"] for item in data["items"])
    assert len(calls) == 1

    again = client.get("/api/ai/suggestions?scope=chat&nonce=0", headers=admin_headers)
    assert again.get_json()["data"]["items"] == data["items"]
    assert len(calls) == 1, "同一天同一批必须走缓存，不能每次都打模型"

    rotated = client.get("/api/ai/suggestions?scope=chat&nonce=1", headers=admin_headers)
    assert rotated.get_json()["data"]["items"] != data["items"]
    assert len(calls) == 2, "换一批要真的重新生成"
    assert calls[1][2], "重新生成时要把上一批带上去重"


def test_suggestions_fall_back_to_static_hints_without_ai(
    client, admin_headers, monkeypatch
):
    _stub_cache(monkeypatch)
    monkeypatch.setattr(ai_api, "_generate_items", lambda *a, **k: [])

    resp = client.get("/api/ai/suggestions?scope=computeruse", headers=admin_headers)
    data = resp.get_json()["data"]
    assert data["source"] == "fallback"
    assert len(data["items"]) == 3
    assert all(item["zh"] and item["en"] for item in data["items"])


def test_computer_use_suggestions_are_model_generated_too(
    client, admin_headers, monkeypatch
):
    _stub_cache(monkeypatch)
    seen = {}

    def fake_generate(scope, facts, size, avoid):
        seen["scope"] = scope
        seen["size"] = size
        return [{"zh": f"点一下{i}", "en": f"Click {i}"} for i in range(size)]

    monkeypatch.setattr(ai_api, "_generate_items", fake_generate)
    resp = client.get("/api/ai/suggestions?scope=computeruse", headers=admin_headers)
    data = resp.get_json()["data"]
    assert seen == {"scope": "computeruse", "size": 3}
    assert data["source"] == "ai"
    assert len(data["items"]) == 3


def test_suggestions_require_login(client):
    assert client.get("/api/ai/suggestions").status_code == 401
