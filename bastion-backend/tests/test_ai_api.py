"""AI 运维接口测试：流式对话、工具调用、敏感操作确认、审计落库与远程执行。

这里用**假的 DeepSeek 客户端**驱动整条链路（不联网、不烧 token），
但工具执行、权限校验、审计落库走的都是真实代码路径。
"""

from __future__ import annotations

import json

import pytest

from app.ai import service as ai_service
from app.ai import tools as registry
from app.ai.service import Caller
from app.extensions import db
from app.models import (
    AiConversation,
    AiMessage,
    AiToolCall,
    AuditLog,
    CommandLog,
    Host,
    SessionRecord,
    User,
)


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #
def _enable_ai(app):
    app.config["AI_ENABLED"] = True
    app.config["DEEPSEEK_API_KEY"] = "test-key"
    app.config["AI_MODEL"] = "deepseek-flash"


class FakeClient:
    """按脚本吐事件的假客户端：每次 stream_chat 消费一段脚本。"""

    model = "deepseek-flash"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def stream_chat(self, messages, *, tools=None, max_tokens=None, temperature=None, model=None):
        self.calls.append({"messages": messages, "tools": tools or []})
        events = self.script.pop(0) if self.script else []
        for event in events:
            yield event


def _patch_client(monkeypatch, script) -> FakeClient:
    fake = FakeClient(script)
    monkeypatch.setattr("app.api.ai.create_client", lambda config: fake)
    return fake


def _events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                continue
    return out


def _types(events) -> list[str]:
    return [event.get("type", "") for event in events]


def _chat(client, headers, **payload):
    resp = client.post("/api/ai/chat", json=payload, headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return _events(resp.get_data(as_text=True))


def _text_tool_call(name: str, arguments: dict, call_id: str = "call_1") -> list[dict]:
    return [
        {"type": "content", "text": "我来处理。"},
        {"type": "tool_call", "index": 0, "id": call_id, "name": name,
         "arguments": json.dumps(arguments, ensure_ascii=False)},
        {"type": "finish", "reason": "tool_calls"},
    ]


def _simple_answer(text: str) -> list[dict]:
    return [{"type": "content", "text": text}, {"type": "finish", "reason": "stop"}]


# --------------------------------------------------------------------------- #
# 状态 / 工具目录
# --------------------------------------------------------------------------- #
def test_status_reports_configuration_and_tool_count(app, client, admin_headers):
    _enable_ai(app)
    body = client.get("/api/ai/status", headers=admin_headers).get_json()
    assert body["success"] is True
    data = body["data"]
    assert data["enabled"] is True and data["configured"] is True
    assert data["model"] == "deepseek-flash"
    assert data["toolCount"] >= 50
    assert data["totalToolCount"] == len(registry.TOOLS)
    assert data["canUse"] is True


def test_status_flags_missing_api_key(app, client, admin_headers):
    app.config["AI_ENABLED"] = True
    app.config["DEEPSEEK_API_KEY"] = ""
    data = client.get("/api/ai/status", headers=admin_headers).get_json()["data"]
    assert data["enabled"] is False
    assert data["configured"] is False


def test_tools_endpoint_filters_by_permission(app, client, admin_headers, make_user):
    _enable_ai(app)
    admin_data = client.get("/api/ai/tools", headers=admin_headers).get_json()["data"]
    assert admin_data["available"] == len(registry.TOOLS)
    assert len(admin_data["groups"]) >= 5

    make_user("ai-reader", role_code="viewer")
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="viewer").first()
        role.permissions = ["ai:view", "ai:tool"]
        db.session.commit()
    token = client.post(
        "/api/login/account", json={"username": "ai-reader", "password": "User1234"}
    ).get_json()["data"]["token"]
    data = client.get(
        "/api/ai/tools", headers={"Authorization": f"Bearer {token}"}
    ).get_json()["data"]
    assert 0 < data["available"] < len(registry.TOOLS)
    assert all(item["permission"] == registry.READ for item in data["items"])


# --------------------------------------------------------------------------- #
# 流式对话
# --------------------------------------------------------------------------- #
def test_chat_streams_reasoning_content_and_tool_results(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    fake = _patch_client(
        monkeypatch,
        [_text_tool_call("list_hosts", {}), _simple_answer("现在有若干台主机。")],
    )

    events = _chat(client, admin_headers, message="现在有哪些主机？")
    kinds = _types(events)
    assert kinds[0] == "open"
    for expected in ("start", "content", "tool_call", "tool_result", "message_end"):
        assert expected in kinds, f"缺少 {expected} 事件：{kinds}"

    tool_call = next(event for event in events if event["type"] == "tool_call")
    assert tool_call["name"] == "list_hosts" and tool_call["sensitive"] is False
    tool_result = next(event for event in events if event["type"] == "tool_result")
    assert tool_result["ok"] is True

    # 模型拿到的是真实的工具 schema 与工具结果
    assert fake.calls, "必须真的调用过模型"
    assert any(schema["function"]["name"] == "list_hosts" for schema in fake.calls[0]["tools"])
    second = fake.calls[1]["messages"]
    assert any(message.get("role") == "tool" for message in second)
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in second)

    # 对话、消息、工具调用三张表都要落库（这就是"AI 对话进审计"）
    with app.app_context():
        conversation = AiConversation.query.one()
        assert conversation.source == "web" and conversation.message_count >= 3
        roles = [row.role for row in AiMessage.query.order_by(AiMessage.id).all()]
        assert roles[0] == "user" and "assistant" in roles and "tool" in roles
        call = AiToolCall.query.one()
        assert call.tool_name == "list_hosts" and call.status == "success" and call.sensitive is False
        assert call.user_id == conversation.user_id
        actions = [row.action for row in AuditLog.query.filter_by(category="ai").all()]
        assert "ai_chat" in actions and "ai_tool_call" in actions


def test_chat_requires_ai_use_permission(app, client, make_user):
    _enable_ai(app)
    make_user("no-ai", role_code="viewer")
    token = client.post(
        "/api/login/account", json={"username": "no-ai", "password": "User1234"}
    ).get_json()["data"]["token"]
    resp = client.post(
        "/api/ai/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_chat_reports_disabled_feature_with_actionable_message(app, client, admin_headers):
    app.config["AI_ENABLED"] = False
    resp = client.post("/api/ai/chat", json={"message": "hi"}, headers=admin_headers)
    assert resp.status_code == 403
    assert "AI_ENABLED" in resp.get_json()["message"]


def test_chat_reports_missing_api_key(app, client, admin_headers):
    app.config["AI_ENABLED"] = True
    app.config["DEEPSEEK_API_KEY"] = ""
    resp = client.post("/api/ai/chat", json={"message": "hi"}, headers=admin_headers)
    assert resp.status_code == 400
    assert "DEEPSEEK_API_KEY" in resp.get_json()["message"]


def test_chat_forwards_provider_errors_to_the_stream(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    from app.ai.client import AiError

    class Boom:
        def stream_chat(self, *args, **kwargs):
            raise AiError("模型把 token 预算都花在思考上了")
            yield  # pragma: no cover - 让它是生成器

    monkeypatch.setattr("app.api.ai.create_client", lambda config: Boom())
    events = _chat(client, admin_headers, message="你好")
    error = next(event for event in events if event["type"] == "error")
    assert "token" in error["message"]


def test_chat_rejects_empty_message(app, client, admin_headers):
    _enable_ai(app)
    resp = client.post("/api/ai/chat", json={"message": "  "}, headers=admin_headers)
    assert resp.status_code == 400


def test_chat_accepts_host_context_from_rdp_console(
    app, client, admin_headers, monkeypatch, make_host
):
    """RDP 侧边栏是带着 hostId 提问的，这条路以前必然 500。

    接口层取到的是 ORM 的 ``Host``，而 ``create_conversation`` 早期只按 dict 读
    （``(host or {}).get("id")``）——主界面从不传 hostId，所以一直没被发现，
    真机上表现为「发消息没有任何反应」（前端那时还会把 500 信封当 SSE 流读掉）。
    """
    _enable_ai(app)
    _patch_client(monkeypatch, [_simple_answer("这台是 Windows 机器。")])
    host_id = make_host(
        name="win-75", address="192.168.0.75", port=3389, protocol="rdp", os_type="windows"
    )

    events = _chat(
        client,
        admin_headers,
        message="这台机器装的是什么版本的 Python？",
        hostId=host_id,
        source="rdp-console",
    )
    assert "message_end" in _types(events)

    with app.app_context():
        conversation = AiConversation.query.one()
        assert conversation.host_id == host_id
        assert conversation.host_name == "win-75"
        assert conversation.host_address == "192.168.0.75"
        assert conversation.source == "rdp-console"


def test_host_snapshot_accepts_orm_dict_and_none(app, make_host):
    """两种「主机上下文」都要认：网页/网关给 ORM 对象，回读已有会话时给 dict。"""
    assert ai_service.host_snapshot(None) == (None, "", "")
    assert ai_service.host_snapshot({"id": 7, "name": "web-01", "address": "10.0.0.7"}) == (
        7,
        "web-01",
        "10.0.0.7",
    )
    assert ai_service.host_snapshot({"id": 8}) == (8, "", "")

    host_id = make_host(name="win-75", address="192.168.0.75")
    with app.app_context():
        host = db.session.get(Host, host_id)
        assert ai_service.host_snapshot(host) == (host_id, "win-75", "192.168.0.75")


def test_unknown_tool_call_is_reported_to_the_model_and_audited(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_text_tool_call("no_such_tool", {}), _simple_answer("好的")])
    events = _chat(client, admin_headers, message="帮我执行一个不存在的工具")
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["ok"] is False and "不存在" in result["summary"]
    with app.app_context():
        assert not AiToolCall.query.count()


def test_tool_without_permission_is_denied_and_audited(app, client, make_user, monkeypatch):
    _enable_ai(app)
    make_user("ai-limited", role_code="viewer")
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="viewer").first()
        role.permissions = ["ai:view", "ai:use", "ai:tool"]
        db.session.commit()
    token = client.post(
        "/api/login/account", json={"username": "ai-limited", "password": "User1234"}
    ).get_json()["data"]["token"]

    _patch_client(monkeypatch, [_text_tool_call("create_host", {"name": "x", "address": "1.1.1.1"})])
    events = _chat(
        client, {"Authorization": f"Bearer {token}"}, message="帮我加一台机器"
    )
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["ok"] is False
    with app.app_context():
        record = AiToolCall.query.one()
        assert record.status == "denied"
        actions = [row.action for row in AuditLog.query.filter_by(category="ai").all()]
        assert "ai_tool_denied" in actions
        assert Host.query.filter_by(name="x").first() is None


# --------------------------------------------------------------------------- #
# 敏感操作：管理员密码确认
# --------------------------------------------------------------------------- #
def test_sensitive_tool_waits_for_admin_password_then_executes(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(
        monkeypatch,
        [
            _text_tool_call("create_host", {"name": "ai-created", "address": "10.9.9.9"}),
            _simple_answer("已经创建好了。"),
        ],
    )
    events = _chat(client, admin_headers, message="新增主机 ai-created 10.9.9.9")
    confirm = next(event for event in events if event["type"] == "confirm_required")
    assert confirm["tool"] == "create_host"
    assert confirm["sensitive"] is True
    tool_call_event = next(event for event in events if event["type"] == "tool_call")
    assert tool_call_event["status"] == "pending"
    with app.app_context():
        conversation_id = AiConversation.query.one().id
        assert AiToolCall.query.one().status == "pending"
        assert Host.query.filter_by(name="ai-created").first() is None, "未确认前绝不能执行"

    # 1) 取消：不写库、状态 rejected
    resp = client.post(
        "/api/ai/confirm",
        json={"conversationId": conversation_id, "approve": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200 and resp.get_json()["data"]["rejected"] == 1
    with app.app_context():
        assert AiToolCall.query.one().status == "rejected"
        assert Host.query.filter_by(name="ai-created").first() is None

    # 重新发起一次并走正确流程
    _patch_client(
        monkeypatch,
        [
            _text_tool_call("create_host", {"name": "ai-created", "address": "10.9.9.9"}, "call_2"),
            _simple_answer("已经创建好了。"),
        ],
    )
    # 继续同一个对话（不传 conversationId 会被当成一次新对话，挂起记录就对不上了）
    events = _chat(client, admin_headers, message="再来一次", conversationId=conversation_id)
    assert "confirm_required" in _types(events)

    # 2) 密码错误：403，且不执行
    resp = client.post(
        "/api/ai/confirm",
        json={
            "conversationId": conversation_id,
            "adminUsername": "admin",
            "adminPassword": "wrong-password",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 403, resp.get_data(as_text=True)
    with app.app_context():
        assert Host.query.filter_by(name="ai-created").first() is None

    # 3) 密码正确：批准
    resp = client.post(
        "/api/ai/confirm",
        json={
            "conversationId": conversation_id,
            "adminUsername": "admin",
            "adminPassword": "admin123",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["approved"] == 1
    with app.app_context():
        approved = AiToolCall.query.filter_by(status="approved").one()
        assert approved.confirmed_by == "admin"
        assert approved.confirmed_at is not None

    # 4) 恢复执行：工具真的跑了，并且结果回到流里
    events = _chat(client, admin_headers, conversationId=conversation_id, resume=True)
    kinds = _types(events)
    assert "tool_result" in kinds and "message_end" in kinds
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["ok"] is True
    with app.app_context():
        assert Host.query.filter_by(name="ai-created").first() is not None
        assert AiToolCall.query.filter_by(status="success").count() == 1
        actions = [row.action for row in AuditLog.query.filter_by(category="ai").all()]
        assert "ai_tool_confirm" in actions
        assert "ai_tool_reject" in actions


def test_new_message_while_a_sensitive_call_is_pending_is_not_silent(
    app, client, admin_headers, monkeypatch
):
    """挂起确认时又发一条：要给出可读说明。

    回归背景：这里以前只回一个 ``confirm_required`` 就 return，前端拿到一个空白气泡 ——
    用户看到的现象是「发了消息，AI 毫无反应」，而真机上确实撞到过（RDP 侧边栏实测）。
    """
    _enable_ai(app)
    _patch_client(
        monkeypatch,
        [
            _text_tool_call(
                "create_host", {"name": "ai-pending", "address": "10.8.8.8"}
            )
        ],
    )
    events = _chat(client, admin_headers, message="加台机器 ai-pending 10.8.8.8")
    assert "confirm_required" in _types(events)
    with app.app_context():
        conversation_id = AiConversation.query.one().id
        assert AiToolCall.query.one().status == "pending"

    # 用户没确认、也没点「继续」，而是又问了一句
    fake = _patch_client(monkeypatch, [_simple_answer("这次不该轮到模型说话。")])
    events = _chat(client, admin_headers, message="在吗？", conversationId=conversation_id)
    kinds = _types(events)
    assert "confirm_required" in kinds, "挂起中的敏感操作必须继续出现在流里"
    text = "".join(
        event.get("delta") or "" for event in events if event["type"] == "content"
    )
    assert "等管理员确认" in text, "要给一句人话，而不是空白气泡"
    end = next(event for event in events if event["type"] == "message_end")
    assert end["status"] == "awaiting_confirm"
    assert fake.calls == [], "还没确认就不该再问模型"
    with app.app_context():
        notes = [row.content for row in AiMessage.query.filter_by(role="assistant").all()]
        assert any("等管理员确认" in note for note in notes), "这句话也要落进对话记录"
        assert AiToolCall.query.one().status == "pending"
        assert Host.query.filter_by(name="ai-pending").first() is None


def _protocol_violations(messages: list[dict]) -> list[str]:
    """校验 OpenAI 兼容协议：assistant.tool_calls 后面必须紧跟同 id 的 tool 响应，且不能有孤儿。"""
    problems: list[str] = []
    pending: list[str] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            if pending:
                problems.append(f"{index}: 上一条 assistant 的 tool_calls 没配齐 {pending}")
            pending = [
                str((call or {}).get("id") or "")
                for call in (message.get("tool_calls") or [])
            ]
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                problems.append(f"{index}: 孤儿 tool 消息 {call_id}")
            else:
                pending.remove(call_id)
        elif pending:
            problems.append(f"{index}: {role} 打断了没配齐的 tool_calls {pending}")
            pending = []
    if pending:
        problems.append(f"末尾 tool_calls 没配齐 {pending}")
    return problems


def test_build_messages_keeps_tool_responses_right_after_their_assistant(app):
    """管理员隔了一轮才拒绝时，那条 tool 消息在库里排在后面，还原成报文要挪回原位。

    回归背景：真机上「拒绝 → 再说话」之后，DeepSeek 每次都回 HTTP 400（历史上
    assistant.tool_calls 悬空），整个对话报废 —— 只补「缺失」的 tool 响应不够，
    还得把「迟到」的那条挪到声明它的 assistant 后面。
    """
    _enable_ai(app)
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        conversation = AiConversation(
            user_id=admin.id, username=admin.username, source="test", title="迟到 tool"
        )
        db.session.add(conversation)
        db.session.commit()

        ai_service.add_message(conversation, "user", "先看一眼桌面")
        ai_service.add_message(
            conversation,
            "assistant",
            "我调两个工具。",
            tool_calls=[
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "observe_computer", "arguments": "{}"},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                },
            ],
            status="awaiting_confirm",
        )
        ai_service.add_message(conversation, "tool", "截图失败", tool_call_id="call_a")
        ai_service.add_message(conversation, "user", "不许跑命令")
        ai_service.add_message(conversation, "assistant", "上一轮还有 1 个敏感操作在等确认。")
        ai_service.add_message(
            conversation,
            "tool",
            "[工具 run_command] 未执行：管理员拒绝了该操作。",
            tool_call_id="call_b",
            status="rejected",
        )
        ai_service.add_message(conversation, "user", "那我换个说法")

        caller = Caller(
            id=admin.id,
            username=admin.username,
            display_name="管理员",
            role_code="admin",
            is_admin=True,
        )
        messages = ai_service.build_messages(conversation, caller, limit=40, tool_count=5)

        assert _protocol_violations(messages) == []
        assistant_index = next(
            index for index, row in enumerate(messages) if row.get("tool_calls")
        )
        assert messages[assistant_index + 1]["tool_call_id"] == "call_a"
        assert messages[assistant_index + 2]["tool_call_id"] == "call_b"
        assert (
            messages[assistant_index + 2]["content"]
            == "[工具 run_command] 未执行：管理员拒绝了该操作。"
        )
        assert messages[-1]["content"] == "那我换个说法"


def test_sensitive_confirmation_needs_a_real_admin(
    app, client, admin_headers, make_user, monkeypatch
):
    _enable_ai(app)
    make_user("ops-user", role_code="ops", password="Ops12345")
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="ops").first()
        role.permissions = ["ai:view", "ai:use", "ai:tool", "ai:tool_write", "host:manage"]
        db.session.commit()
    token = client.post(
        "/api/login/account", json={"username": "ops-user", "password": "Ops12345"}
    ).get_json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    _patch_client(
        monkeypatch,
        [_text_tool_call("create_host", {"name": "ai-via-ops", "address": "10.1.1.1"})],
    )
    events = _chat(client, headers, message="加台机器")
    assert "confirm_required" in _types(events)
    with app.app_context():
        conversation_id = AiConversation.query.one().id

    # 普通用户自己确认 -> 拒绝（不是管理员）
    resp = client.post(
        "/api/ai/confirm",
        json={
            "conversationId": conversation_id,
            "adminUsername": "ops-user",
            "adminPassword": "Ops12345",
        },
        headers=headers,
    )
    assert resp.status_code == 403
    with app.app_context():
        assert Host.query.filter_by(name="ai-via-ops").first() is None


def test_confirm_accepts_a_blank_account_and_falls_back_to_the_current_user(
    app, client, admin_headers, monkeypatch
):
    """回归用户报的 bug（m10456）：账号框留空 + 只填密码 → 曾经 400「请输入管理员账号与密码」。

    修复后的口径：**只有密码为空才 400**；账号留空就用**当前登录者**复验口令，
    `is_admin()` 与口令校验一步都不放松（见下一条测试）。
    """
    _enable_ai(app)
    _patch_client(
        monkeypatch,
        [_text_tool_call("create_host", {"name": "ai-blank", "address": "10.8.8.8"})],
    )
    _chat(client, admin_headers, message="新增主机 ai-blank")
    with app.app_context():
        conversation_id = AiConversation.query.one().id

    # 1) 密码为空 → 400，且明确提示要的是密码
    resp = client.post(
        "/api/ai/confirm",
        json={"conversationId": conversation_id, "adminUsername": "admin", "adminPassword": ""},
        headers=admin_headers,
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "INVALID_ARGUMENT"
    assert "管理员密码" in resp.get_json()["message"]

    # 2) 账号留空 + 密码正确 → 通过，确认人就是当前登录者
    resp = client.post(
        "/api/ai/confirm",
        json={"conversationId": conversation_id, "adminUsername": "", "adminPassword": "admin123"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["approved"] == 1
    with app.app_context():
        row = AiToolCall.query.one()
        assert row.status == "approved"
        assert row.confirmed_by == "admin"


def test_blank_account_still_requires_an_admin_password(app, client, make_user, monkeypatch):
    """账号留空 ≠ 放行：用当前登录者身份，管理员权限与口令照样要过。"""
    _enable_ai(app)
    make_user("ops-blank", role_code="ops", password="Ops12345")
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="ops").first()
        role.permissions = ["ai:view", "ai:use", "ai:tool", "ai:tool_write", "host:manage"]
        db.session.commit()
    token = client.post(
        "/api/login/account", json={"username": "ops-blank", "password": "Ops12345"}
    ).get_json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    _patch_client(
        monkeypatch,
        [_text_tool_call("create_host", {"name": "ai-blank-ops", "address": "10.7.7.7"})],
    )
    _chat(client, headers, message="加台机器")
    with app.app_context():
        conversation_id = AiConversation.query.one().id

    # 账号留空 + 自己的口令：普通用户不是管理员 → 403，且工具保持 pending、没写库
    resp = client.post(
        "/api/ai/confirm",
        json={"conversationId": conversation_id, "adminPassword": "Ops12345"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.get_data(as_text=True)
    with app.app_context():
        assert Host.query.filter_by(name="ai-blank-ops").first() is None
        assert AiToolCall.query.one().status == "pending"


def test_confirm_without_pending_operation_is_rejected(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_simple_answer("不用调工具")])
    _chat(client, admin_headers, message="你好")
    with app.app_context():
        conversation_id = AiConversation.query.one().id
    resp = client.post(
        "/api/ai/confirm",
        json={"conversationId": conversation_id, "adminUsername": "admin", "adminPassword": "admin123"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "没有待确认" in resp.get_json()["message"]


# --------------------------------------------------------------------------- #
# 卡片协议
# --------------------------------------------------------------------------- #
def test_ai_card_blocks_are_extracted_into_cards_event(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    card = {
        "type": "table",
        "title": "主机",
        "columns": [{"key": "name", "title": "名称"}],
        "rows": [{"name": "web-01"}],
    }
    fence = "```"
    answer = (
        "这是主机清单：\n\n"
        + fence
        + "ai-card\n"
        + json.dumps(card, ensure_ascii=False)
        + "\n"
        + fence
        + "\n\n"
        + fence
        + "ai-card\n{nope}\n"
        + fence
        + "\n"
    )
    _patch_client(monkeypatch, [[{"type": "content", "text": answer}, {"type": "finish", "reason": "stop"}]])
    events = _chat(client, admin_headers, message="给我一张卡片")
    cards = next(event for event in events if event["type"] == "cards")["cards"]
    assert len(cards) == 1 and cards[0]["type"] == "table"
    with app.app_context():
        assistant = AiMessage.query.filter_by(role="assistant").one()
        assert assistant.cards and assistant.cards[0]["title"] == "主机"


# --------------------------------------------------------------------------- #
# 对话审计接口
# --------------------------------------------------------------------------- #
def test_conversation_listing_and_detail(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_text_tool_call("list_hosts", {}), _simple_answer("好了")])
    _chat(client, admin_headers, message="有哪些主机")
    with app.app_context():
        conversation_id = AiConversation.query.one().id

    body = client.get("/api/ai/conversations", headers=admin_headers).get_json()
    assert body["total"] == 1 and body["data"][0]["id"] == conversation_id
    assert body["data"][0]["toolCount"] == 1

    detail = client.get(f"/api/ai/conversations/{conversation_id}", headers=admin_headers).get_json()["data"]
    assert len(detail["messages"]) >= 3
    assert detail["toolCalls"][0]["toolName"] == "list_hosts"

    calls = client.get("/api/ai/tool-calls", headers=admin_headers).get_json()
    assert calls["total"] == 1
    filtered = client.get(
        "/api/ai/tool-calls?toolName=no_such_tool", headers=admin_headers
    ).get_json()
    assert filtered["total"] == 0


def test_conversations_are_private_unless_view_all(app, client, admin_headers, make_user, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_simple_answer("管理员私聊")])
    _chat(client, admin_headers, message="秘密")
    with app.app_context():
        conversation_id = AiConversation.query.one().id

    make_user("other-user", role_code="viewer")
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="viewer").first()
        role.permissions = ["ai:view", "ai:use"]
        db.session.commit()
    token = client.post(
        "/api/login/account", json={"username": "other-user", "password": "User1234"}
    ).get_json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}

    listed = client.get("/api/ai/conversations", headers=headers).get_json()
    assert listed["total"] == 0
    assert client.get(f"/api/ai/conversations/{conversation_id}", headers=headers).status_code == 403


def test_delete_conversation_removes_children_and_audits(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_text_tool_call("list_hosts", {}), _simple_answer("好了")])
    _chat(client, admin_headers, message="有哪些主机")
    with app.app_context():
        conversation_id = AiConversation.query.one().id

    resp = client.delete(f"/api/ai/conversations/{conversation_id}", headers=admin_headers)
    assert resp.status_code == 200
    with app.app_context():
        assert AiConversation.query.count() == 0
        assert AiMessage.query.count() == 0
        assert AiToolCall.query.count() == 0
        assert AuditLog.query.filter_by(action="ai_conversation_delete").count() == 1


def test_ai_settings_requires_manage_permission(app, client, admin_headers):
    _enable_ai(app)
    body = client.get("/api/ai/settings", headers=admin_headers).get_json()
    assert body["data"]["envFile"] == "bastion-backend/.env"
    assert body["data"]["toolCount"] == len(registry.TOOLS)


# --------------------------------------------------------------------------- #
# 远程执行通道（AI 的 run_command 后端）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def exec_ready(app, make_user, make_host, make_account, make_grant):
    """造一个「有 ai:tool_exec + terminal:use + 主机授权」的普通用户。"""
    user_id = make_user("ai-exec", role_code="viewer")
    host_id = make_host(name="exec-host", address="10.0.0.9")
    account_id = make_account(host_id, name="root", username="root")
    make_grant(user_id, host_id, account_id=account_id)
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="viewer").first()
        role.permissions = ["terminal:use", "ai:view", "ai:use", "ai:tool", "ai:tool_exec"]
        db.session.commit()
    return {"user": user_id, "host": host_id, "account": account_id}


def _login(client, username, password):
    return client.post(
        "/api/login/account", json={"username": username, "password": password}
    ).get_json()["data"]["token"]


def test_terminal_exec_requires_the_ai_exec_permission(app, client, make_host, make_account, make_grant, make_user):
    user_id = make_user("no-exec", role_code="viewer")
    host_id = make_host(name="noexec-host", address="10.0.0.8")
    account_id = make_account(host_id)
    make_grant(user_id, host_id, account_id=account_id)
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="viewer").first()
        role.permissions = ["terminal:use"]
        db.session.commit()
    token = _login(client, "no-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={"hostId": host_id, "accountId": account_id, "command": "whoami"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "ai:tool_exec" in resp.get_json()["message"]


def test_terminal_exec_blocks_command_by_policy_and_audits_the_denial(
    app, client, exec_ready
):
    token = _login(client, "ai-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={"hostId": exec_ready["host"], "accountId": exec_ready["account"], "command": "cat /etc/shadow"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "COMMAND_DENIED"
    assert body["data"]["allowed"] is False
    with app.app_context():
        record = CommandLog.query.one()
        assert record.action == "deny" and record.command == "cat /etc/shadow"
        assert record.user_id == exec_ready["user"]
        session = SessionRecord.query.one()
        assert session.protocol == "exec" and session.status == "closed"
        assert session.host_id == exec_ready["host"]


def test_terminal_exec_runs_command_and_records_output(app, client, exec_ready, monkeypatch):
    captured: dict = {}

    class FakeConnection:
        pass

    def fake_connect(target):
        captured["target"] = target
        return FakeConnection()

    def fake_run(info, command, timeout=30.0):
        captured["command"] = command
        captured["timeout"] = timeout
        return 0, "uid=0(root)\n", ""

    monkeypatch.setattr("app.ssh_client.connect", fake_connect)
    monkeypatch.setattr("app.ssh_client.run_single_command", fake_run)
    monkeypatch.setattr("app.ssh_client.close", lambda info: captured.update(closed=True))

    token = _login(client, "ai-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={"hostId": exec_ready["host"], "accountId": exec_ready["account"], "command": "whoami"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["allowed"] is True and data["exitStatus"] == 0
    assert "uid=0(root)" in data["stdout"]
    assert captured["command"] == "whoami" and captured.get("closed") is True
    with app.app_context():
        log = CommandLog.query.one()
        assert log.action == "allow" and log.exit_status == 0 and "uid=0(root)" in log.output
        assert SessionRecord.query.one().status == "closed"


def test_terminal_exec_rejects_hosts_without_grant(app, client, exec_ready, make_host):
    other_host = make_host(name="other-host", address="10.0.0.7")
    token = _login(client, "ai-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={"hostId": other_host, "command": "whoami"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "权限" in resp.get_json()["message"]


def test_terminal_exec_routes_winrm_hosts_over_winrm(
    app, client, exec_ready, make_host, make_account, make_grant, monkeypatch
):
    """WinRM 主机必须走 WinRM 客户端。

    历史缺陷：这里无条件用 SSH 客户端建连，WinRM 主机的镜像端口是 5985，于是
    AI 运维在 Windows 主机上跑命令会报「Error reading SSH protocol banner」。
    """
    user_id = exec_ready["user"]
    host_id = make_host(name="win-exec", address="10.0.0.20", protocol="winrm", port=5985)
    account_id = make_account(host_id, name="Administrator", username="Administrator")
    make_grant(user_id, host_id, account_id=account_id)

    captured: dict = {}

    class FakeWinrmConnection:
        pass

    def fake_winrm_connect(target):
        captured["target"] = target
        return FakeWinrmConnection()

    def fake_winrm_run(connection, script, timeout=None):
        from app.winrm import ScriptResult

        captured["script"] = script
        captured["timeout"] = timeout
        return ScriptResult(stdout="WIN-930NKGJCOED\r\n", stderr="", exit_code=0)

    def fail_ssh_connect(target):  # pragma: no cover - 只用来断言「没走 SSH」
        captured["ssh"] = target
        raise AssertionError("WinRM 主机不该走 SSH 通道")

    monkeypatch.setattr("app.winrm.connect", fake_winrm_connect)
    monkeypatch.setattr("app.winrm.run_script", fake_winrm_run)
    monkeypatch.setattr("app.winrm.close", lambda conn: captured.update(closed=True))
    monkeypatch.setattr("app.ssh_client.connect", fail_ssh_connect)

    token = _login(client, "ai-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={"hostId": host_id, "accountId": account_id, "command": "hostname"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["channel"] == "winrm" and data["protocol"] == "winrm"
    assert "WIN-930NKGJCOED" in data["stdout"]
    assert "ssh" not in captured
    assert captured["target"].port == 5985
    assert captured["script"] == "hostname" and captured.get("closed") is True
    with app.app_context():
        log = CommandLog.query.one()
        assert log.action == "allow" and log.exit_status == 0
        assert "WIN-930NKGJCOED" in log.output


def test_terminal_exec_can_be_pinned_to_one_endpoint(
    app, client, exec_ready, make_host, make_account, make_grant, monkeypatch
):
    """同一台机器既有 ssh 又有 winrm 时，payload 里的 protocol 点名哪个就走哪个。"""
    from app.extensions import db
    from app.models import HostProtocol

    user_id = exec_ready["user"]
    host_id = make_host(
        name="multi-exec", address="10.0.0.22", protocol="winrm", port=5985
    )
    account_id = make_account(host_id, name="root", username="root")
    make_grant(user_id, host_id, account_id=account_id)
    # 再挂一个 ssh 端点：这台机器两套入口（make_host 只写镜像字段，不建端点行）
    with app.app_context():
        db.session.add(
            HostProtocol(host_id=host_id, protocol="ssh", port=2222, status="active")
        )
        db.session.commit()

    captured: dict = {}

    class FakeSshConnection:
        pass

    def fake_ssh_connect(target):
        captured["target"] = target
        return FakeSshConnection()

    def fake_run(connection, command, timeout=None):
        captured["command"] = command
        return 0, "up 3 days\n", ""

    def fail_winrm_connect(target):  # pragma: no cover - 只用来断言「没走 WinRM」
        captured["winrm"] = target
        raise AssertionError("点名 ssh 时不该走 WinRM 通道")

    monkeypatch.setattr("app.ssh_client.connect", fake_ssh_connect)
    monkeypatch.setattr("app.ssh_client.run_single_command", fake_run)
    monkeypatch.setattr("app.ssh_client.close", lambda conn: captured.update(closed=True))
    monkeypatch.setattr("app.winrm.connect", fail_winrm_connect)

    token = _login(client, "ai-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={
            "hostId": host_id,
            "accountId": account_id,
            "command": "uptime",
            "protocol": "ssh",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["channel"] == "ssh" and data["protocol"] == "ssh"
    assert captured["target"].port == 2222  # 用的是 ssh 端点的端口，不是镜像的 5985
    assert "winrm" not in captured
    assert captured.get("closed") is True

    # 点名一个跑不了命令的端点：要说清缺哪个端点、有哪些可用，而不是拿 SSH 硬试
    resp = client.post(
        "/api/terminal/exec",
        json={
            "hostId": host_id,
            "accountId": account_id,
            "command": "uptime",
            "protocol": "rdp",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "NO_CHARACTER_ENDPOINT"
    assert "rdp" in body["message"]


def test_terminal_exec_rejects_rdp_only_hosts(app, client, exec_ready, make_host, make_account, make_grant):
    """只有 rdp 端点的机器是图形桌面，跑不了命令 —— 要回可读原因而不是拿 SSH 去试。"""
    user_id = exec_ready["user"]
    host_id = make_host(name="rdp-only", address="10.0.0.21", protocol="rdp", port=3389)
    account_id = make_account(host_id, name="Administrator", username="Administrator")
    make_grant(user_id, host_id, account_id=account_id)

    token = _login(client, "ai-exec", "User1234")
    resp = client.post(
        "/api/terminal/exec",
        json={"hostId": host_id, "accountId": account_id, "command": "whoami"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "NO_CHARACTER_ENDPOINT"
    assert "rdp" in body["message"]


def test_run_command_tool_is_marked_sensitive_and_maps_to_the_exec_endpoint():
    tool = registry.TOOL_INDEX["run_command"]
    assert tool.sensitive is True
    assert tool.permission == registry.EXEC
    path, _query, body = registry.build_request(
        tool, {"hostId": 3, "accountId": 4, "command": "uptime"}
    )
    assert path == "/api/terminal/exec"
    assert body["hostId"] == 3 and body["command"] == "uptime"


# --------------------------------------------------------------------------- #
# 历史协议修复：悬空的 tool_calls 会让整个对话 HTTP 400
# --------------------------------------------------------------------------- #
def _protocol_problems(messages) -> list[str]:
    """按 OpenAI 兼容协议逐条走一遍，返回不合法之处（空列表 = 合法）。

    规则：assistant 声明的每个 ``tool_call_id`` 必须紧跟一条同 id 的 ``role=tool``
    响应；``role=tool`` 的 id 必须能对上前面声明过的 id；不能悬挂到下一轮用户消息。
    DeepSeek 对不合法报文直接回 HTTP 400，而历史是累积的 —— 一旦不合法，这个对话
    之后每次请求都会 400（这正是 `/ask-ai` 报「DeepSeek 返回 HTTP 400」的形态）。
    """
    problems: list[str] = []
    open_ids: list[str] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not call_id:
                problems.append(f"#{index} tool 消息缺 tool_call_id")
            elif call_id not in open_ids:
                problems.append(f"#{index} tool 消息对不上任何 tool_calls：{call_id}")
            else:
                open_ids.remove(call_id)
            continue
        if open_ids:
            problems.append(f"#{index} {role} 之前的 tool_calls 没等到结果：{open_ids}")
            open_ids = []
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = str((call or {}).get("id") or "")
                if not call_id:
                    problems.append(f"#{index} assistant.tool_calls 缺 id")
                else:
                    open_ids.append(call_id)
    if open_ids:
        problems.append(f"结尾仍有未答复的 tool_calls：{open_ids}")
    return problems


def _admin_caller() -> Caller:
    return Caller.from_user(User.query.filter_by(username="admin").first(), token="test", source="web")


def _drop_tool_messages(conversation_id: int) -> None:
    """模拟修复前留下的坏历史：assistant 声明了 tool_calls，但库里没有 tool 响应。"""
    for row in AiMessage.query.filter_by(conversation_id=conversation_id, role="tool").all():
        db.session.delete(row)
    db.session.commit()


def test_build_messages_pairs_a_tool_call_that_never_gets_a_result(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_text_tool_call("create_host", {"name": "never", "address": "10.1.1.1"})])
    _chat(client, admin_headers, message="准备加一台机器")
    with app.app_context():
        conversation = AiConversation.query.one()
        _drop_tool_messages(conversation.id)
        call = AiToolCall.query.one()
        call.status = "rejected"
        call.error = "管理员拒绝了该操作"
        db.session.commit()

        messages = ai_service.build_messages(conversation, _admin_caller())
        assert not _protocol_problems(messages), messages
        placeholders = [item for item in messages if item.get("role") == "tool"]
        assert len(placeholders) == 1
        assert placeholders[0]["tool_call_id"] == "call_1"
        assert "未执行" in placeholders[0]["content"]


def test_build_messages_drops_orphan_tool_messages_from_a_cut_window(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(
        monkeypatch, [_text_tool_call("list_hosts", {}, "call_a"), _simple_answer("查完了。")]
    )
    _chat(client, admin_headers, message="有哪些主机？")
    with app.app_context():
        conversation = AiConversation.query.one()
        caller = _admin_caller()
        whole = ai_service.build_messages(conversation, caller, limit=0)
        assert not _protocol_problems(whole), whole
        # 窗口只留最后 2 条：正好从 tool 响应开始，它声明的 assistant 已被切掉
        trimmed = ai_service.build_messages(conversation, caller, limit=2)
        assert not [item for item in trimmed if item.get("role") == "tool"], trimmed
        assert not _protocol_problems(trimmed), trimmed


def test_build_messages_skips_the_error_placeholder(app, client, admin_headers, monkeypatch):
    _enable_ai(app)
    _patch_client(monkeypatch, [_simple_answer("我在这儿。")])
    _chat(client, admin_headers, message="你好")
    with app.app_context():
        conversation = AiConversation.query.one()
        ai_service.add_message(
            conversation, "assistant", "（调用模型失败：DeepSeek 返回 HTTP 400）", status="error"
        )
        messages = ai_service.build_messages(conversation, _admin_caller(), limit=0)
        assert all("调用模型失败" not in (item.get("content") or "") for item in messages)
        assert any((item.get("content") or "") == "我在这儿。" for item in messages)


def test_rejecting_a_sensitive_operation_keeps_the_conversation_usable(
    app, client, admin_headers, monkeypatch
):
    """拒绝敏感操作后同一对话还能继续问（修复前：之后每次都 400）。"""
    _enable_ai(app)
    _patch_client(
        monkeypatch, [_text_tool_call("create_host", {"name": "rejected-host", "address": "10.2.2.2"})]
    )
    _chat(client, admin_headers, message="帮我加一台机器")
    with app.app_context():
        conversation_id = AiConversation.query.one().id
        rejected_id = AiToolCall.query.one().id

    resp = client.post(
        "/api/ai/confirm",
        json={"conversationId": conversation_id, "approve": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200 and resp.get_json()["data"]["rejected"] == 1
    with app.app_context():
        assert db.session.get(AiToolCall, rejected_id).status == "rejected"
        # 拒绝 = 这个 tool_call 的结局，必须写回一条 tool 响应
        row = AiMessage.query.filter_by(
            conversation_id=conversation_id, role="tool", tool_call_id="call_1"
        ).one()
        assert "未执行" in row.content and "拒绝" in row.content

    fake = _patch_client(monkeypatch, [_simple_answer("那我先不动它。")])
    events = _chat(client, admin_headers, message="那算了，先看看别的", conversationId=conversation_id)
    assert "error" not in _types(events), _types(events)
    assert "message_end" in _types(events)
    sent = fake.calls[0]["messages"]
    assert not _protocol_problems(sent), sent
    assert any(
        "未执行" in (item.get("content") or "") for item in sent if item.get("role") == "tool"
    )


def test_a_legacy_dangling_tool_call_is_repaired_on_the_next_turn(app, client, admin_headers, monkeypatch):
    """库里已经躺着的坏历史（修复前产生的对话）在下一轮被自动补齐。"""
    _enable_ai(app)
    _patch_client(
        monkeypatch, [_text_tool_call("create_host", {"name": "legacy", "address": "10.3.3.3"})]
    )
    _chat(client, admin_headers, message="加台机器")
    with app.app_context():
        conversation_id = AiConversation.query.one().id
        _drop_tool_messages(conversation_id)
        call = AiToolCall.query.one()
        call.status = "rejected"
        call.confirmed_by = "admin"
        call.error = "管理员拒绝（历史遗留）"
        db.session.commit()

    fake = _patch_client(monkeypatch, [_simple_answer("知道了。")])
    events = _chat(client, admin_headers, message="继续", conversationId=conversation_id)
    assert "error" not in _types(events), _types(events)
    sent = fake.calls[0]["messages"]
    assert not _protocol_problems(sent), sent
    assert any(
        "未执行" in (item.get("content") or "") for item in sent if item.get("role") == "tool"
    )


# --------------------------------------------------------------------------- #
# 单轮工具调用上限：参数设置项 ai_max_tool_rounds（原本写死 8）
# --------------------------------------------------------------------------- #
def test_tool_round_limit_comes_from_the_system_setting(app, client, admin_headers, monkeypatch):
    """「AI 任务最大调用轮数」是参数设置里的项：改成 2 就真只跑 2 轮，不再是写死的 8。"""
    _enable_ai(app)
    with app.app_context():
        assert ai_service.max_tool_rounds(app) == 8

    resp = client.put("/api/settings", json={"ai_max_tool_rounds": 2}, headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()
    with app.app_context():
        assert ai_service.max_tool_rounds(app) == 2
    status = client.get("/api/ai/status", headers=admin_headers).get_json()["data"]
    assert status["maxToolRounds"] == 2

    # 每轮都让模型要工具：脚本给足 6 轮，看看它到底问了模型几次
    fake = _patch_client(
        monkeypatch, [_text_tool_call("list_hosts", {}, f"call_{i}") for i in range(6)]
    )
    events = _chat(client, admin_headers, message="一直查主机")
    assert len(fake.calls) == 2
    assert len([event for event in events if event["type"] == "tool_call"]) == 2

    end = [event for event in events if event["type"] == "message_end"][-1]
    assert end["status"] == "max_rounds"
    text = "".join(event.get("delta", "") for event in events if event["type"] == "content")
    assert "已达到单轮工具调用上限 2 次" in text


def test_every_round_event_carries_its_round_number(app, client, admin_headers, monkeypatch):
    """排版要按轮交错（思考|工具|思考|工具|…|回答）：每轮的事件都带 round，前端据此切块。"""
    _enable_ai(app)
    _patch_client(
        monkeypatch,
        [
            [{"type": "reasoning", "text": "先看一眼主机"}, *_text_tool_call("list_hosts", {}, "call_a")],
            [{"type": "reasoning", "text": "再看一眼"}, *_text_tool_call("list_hosts", {}, "call_b")],
            [{"type": "reasoning", "text": "够了"}, *_simple_answer("两台都看过了。")],
        ],
    )
    events = _chat(client, admin_headers, message="看看主机")
    kinds = ("reasoning", "content", "tool_call", "tool_result", "cards")
    stamped = [event for event in events if event["type"] in kinds]
    assert stamped, _types(events)
    assert all(isinstance(event.get("round"), int) for event in stamped), stamped
    rounds = [event["round"] for event in stamped]
    # 轮次只增不减，0/1/2 三轮都出现过（第三轮是纯回答）
    assert rounds == sorted(rounds), rounds
    assert set(rounds) == {0, 1, 2}, rounds
    last_content = [event for event in events if event["type"] == "content"][-1]
    assert last_content["round"] == 2
    assert last_content["delta"] == "两台都看过了。"
    # 一次调用与它的结果必须在同一轮（否则卡片会跑到别的块里）
    call_round = {
        event["callId"]: event["round"] for event in events if event["type"] == "tool_call"
    }
    for event in events:
        if event["type"] == "tool_result" and event.get("callId") in call_round:
            assert event["round"] == call_round[event["callId"]], event


def test_resume_tool_result_has_no_round_so_it_lands_in_the_pending_block(
    app, client, admin_headers, monkeypatch
):
    """确认后续流：循环外补执行那条 tool_result 故意不带 round —— 前端把它并进最后一块，
    正好把「待确认」那张卡片翻成成功；之后的第 0 轮事件也并进同一块。"""
    _enable_ai(app)
    _patch_client(
        monkeypatch,
        [_text_tool_call("create_host", {"name": "ai-rounds", "address": "10.7.7.7"})],
    )
    events = _chat(client, admin_headers, message="新增主机 ai-rounds 10.7.7.7")
    pending = next(event for event in events if event["type"] == "tool_call")
    assert pending["status"] == "pending" and pending["round"] == 0

    with app.app_context():
        conversation_id = AiConversation.query.one().id
    resp = client.post(
        "/api/ai/confirm",
        json={
            "conversationId": conversation_id,
            "adminUsername": "admin",
            "adminPassword": "admin123",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200 and resp.get_json()["data"]["approved"] == 1

    _patch_client(monkeypatch, [_simple_answer("已经创建好了。")])
    events = _chat(client, admin_headers, conversationId=conversation_id, resume=True)
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["ok"] is True
    assert "round" not in result, result
    assert result["callId"] == pending["callId"]
    content = [event for event in events if event["type"] == "content"]
    assert content and all(event["round"] == 0 for event in content), content


def test_tool_round_limit_clamps_absurd_values_and_falls_back_to_env(app, monkeypatch):
    """手填离谱值（或绕过接口直接写库）会被夹进 1..50；设置留空则回落到 .env 那份。"""
    from app.settings_store import set_setting

    with app.app_context():
        set_setting("ai_max_tool_rounds", "999")
        db.session.commit()
        assert ai_service.max_tool_rounds(app) == ai_service.MAX_TOOL_ROUNDS_CAP

        monkeypatch.setitem(app.config, "AI_MAX_TOOL_ROUNDS", 3)
        set_setting("ai_max_tool_rounds", "0")
        db.session.commit()
        assert ai_service.max_tool_rounds(app) == 3

        set_setting("ai_max_tool_rounds", "")
        db.session.commit()
        assert ai_service.max_tool_rounds(app) == 3

        set_setting("ai_max_tool_rounds", "5")
        db.session.commit()
        assert ai_service.max_tool_rounds(app) == 5


# --------------------------------------------------------------------------- #
# SSE 收尾（真机回归：关掉 AI 面板时控制台刷 GeneratorExit 报错）
# --------------------------------------------------------------------------- #
def test_ai_stream_says_goodbye_and_survives_an_early_disconnect(app, monkeypatch):
    """一条 AI SSE 流的两个收尾约束。

    ① 正常跑完时最后一条事件必须是 `close`（前端据此把气泡收尾）；
    ② 客户端半路断开（Flask 对生成器调 close()）时不许抛
       `RuntimeError: generator ignored GeneratorExit` —— 真机上关面板/断网就是这么刷屏的。
    """
    from app.api import ai as ai_api

    _enable_ai(app)

    def fake_run_turn(*args, **kwargs):  # noqa: ANN002, ANN003 - 只关心事件序列
        yield {"type": "content", "delta": "甲"}
        yield {"type": "content", "delta": "乙"}
        yield {"type": "message_end", "messageId": 1, "status": "done"}

    monkeypatch.setattr(ai_service, "run_turn", fake_run_turn)

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        conversation = AiConversation(
            user_id=admin.id, username=admin.username, source="test", title="流收尾"
        )
        db.session.add(conversation)
        db.session.commit()

        stream = ai_api._stream_turn(conversation.id, None, None, text="你好", resume=False)
        assert next(stream).startswith(": aiops stream opened")
        assert '"type": "open"' in next(stream)
        rest = list(stream)
        assert '"type": "close"' in rest[-1], "最后一条事件必须是把气泡收尾的 close"

        # 半路断开：Reader 走掉，Flask 调 close()，GeneratorExit 落在 yield 上
        early = ai_api._stream_turn(conversation.id, None, None, text="你好", resume=False)
        next(early)
        next(early)
        next(early)
        problem = None
        try:
            early.close()
        except RuntimeError as exc:  # pragma: no cover - 改回 finally: yield 就会走这里
            problem = exc
        assert problem is None, f"客户端断开时不允许抛 RuntimeError：{problem}"
