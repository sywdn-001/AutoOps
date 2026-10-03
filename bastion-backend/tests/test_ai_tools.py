"""AI 运维工具注册表测试。

重点不是"跑通"，而是**把声明式映射钉死**：

* 每个工具的 ``METHOD + path`` 必须真的存在于本项目的路由表里（写错路径 = 上线才发现 404）；
* 权限过滤必须严格（普通用户拿不到写入/执行工具）；
* 参数解析（必填、类型、spread）与请求构造（path/query/body）必须符合 REST 约定。
"""

from __future__ import annotations

import pytest

from app.ai import tools as registry


def _sample_args(tool) -> dict:
    values = {
        "int": 1,
        "str": "1",
        "bool": True,
        "number": 1,
        "list": [],
        "dict": {},
    }
    return {arg.name: values.get(arg.type, "1") for arg in tool.args}


def test_tool_count_is_way_above_fifty():
    assert len(registry.TOOLS) >= 50
    assert len(registry.TOOL_INDEX) == len(registry.TOOLS)
    names = [tool.name for tool in registry.TOOLS]
    assert len(set(names)) == len(names), "工具名不能重复"


def test_every_tool_name_is_a_valid_function_name():
    for tool in registry.TOOLS:
        assert tool.name.isidentifier(), tool.name
        assert tool.description, f"{tool.name} 缺少描述"


def test_every_tool_path_exists_in_the_route_table(app):
    """把每个工具的 path 拿去路由表里 match —— 抓「工具写了个不存在的接口」。"""
    adapter = app.url_map.bind("localhost")
    missing: list[str] = []
    for tool in registry.TOOLS:
        path, _query, _body = registry.build_request(tool, _sample_args(tool))
        try:
            adapter.match(path, method=tool.method)
        except Exception as exc:  # noqa: BLE001 - NotFound / MethodNotAllowed
            missing.append(f"{tool.method} {path} <- {tool.name} ({type(exc).__name__})")
    assert not missing, "工具指向了不存在的接口：\n" + "\n".join(missing)


def test_every_tool_declares_a_known_permission():
    for tool in registry.TOOLS:
        assert tool.permission in (registry.READ, registry.WRITE, registry.EXEC), tool.name
        assert tool.category, f"{tool.name} 缺少分类"


def test_tools_for_permission_filters_by_granted_permissions():
    read_only = registry.tools_for_permission({registry.READ}, is_admin=False)
    assert read_only, "只读权限也应当拿到工具"
    assert all(tool.permission == registry.READ for tool in read_only)

    full = registry.tools_for_permission(
        {registry.READ, registry.WRITE, registry.EXEC}, is_admin=False
    )
    assert len(full) == len(registry.TOOLS)
    assert len(full) > len(read_only)

    # 管理员拿全集（含未显式授权的执行类工具）
    assert len(registry.tools_for_permission(set(), is_admin=True)) == len(registry.TOOLS)
    # 没有任何 AI 权限 -> 一个工具都不给
    assert registry.tools_for_permission(set(), is_admin=False) == []


def test_sensitive_tools_are_marked_and_require_privileged_permissions():
    sensitive = [tool for tool in registry.TOOLS if tool.sensitive]
    assert sensitive, "必须存在需要管理员密码确认的敏感工具"
    for tool in sensitive:
        assert tool.permission in (registry.WRITE, registry.EXEC), tool.name
    names = {tool.name for tool in sensitive}
    for expected in ("create_host", "delete_host", "create_user", "delete_user", "run_command"):
        assert expected in names, f"{expected} 应当是需要确认的敏感操作"


def test_read_tools_are_never_sensitive():
    for tool in registry.TOOLS:
        if tool.permission == registry.READ:
            assert not tool.sensitive, tool.name


def test_build_request_puts_path_args_in_url_and_rest_in_body():
    tool = registry.TOOL_INDEX["create_host"]
    path, query, body = registry.build_request(tool, {"name": "web-01", "address": "10.0.0.1"})
    assert path == "/api/hosts"
    assert query == {}
    assert body["name"] == "web-01" and body["address"] == "10.0.0.1"

    tool = registry.TOOL_INDEX["get_host"]
    path, _query, _body = registry.build_request(tool, {"host_id": 7})
    assert path == "/api/hosts/7"


def test_build_request_routes_query_and_spread_arguments():
    tool = registry.TOOL_INDEX["list_hosts"]
    path, query, body = registry.build_request(tool, {"keyword": "db", "pageSize": 5})
    assert path == "/api/hosts"
    assert query.get("keyword") == "db" and query.get("pageSize") == 5
    assert body == {}

    spread = next(tool for tool in registry.TOOLS if any(a.spread for a in tool.args))
    sample = {arg.name: ({"a": 1} if arg.spread else "x") for arg in spread.args}
    _path, _query, body = registry.build_request(spread, sample)
    assert body == {"a": 1}, f"{spread.name} 的 dict 参数应当展开进 body"


def test_required_arguments_are_marked_in_the_schema():
    tool = registry.TOOL_INDEX["create_host"]
    schema = tool.schema()
    assert schema["function"]["name"] == "create_host"
    required = schema["function"]["parameters"]["required"]
    assert "name" in required and "address" in required
    assert "port" not in required


def test_openai_schemas_are_well_formed():
    schemas = registry.openai_schemas(registry.TOOLS[:5])
    for item in schemas:
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_catalog_groups_tools_by_category():
    groups = registry.grouped_catalog(registry.TOOLS)
    assert len(groups) >= 5
    total = sum(len(group["tools"]) for group in groups)
    assert total == len(registry.TOOLS)
    for group in groups:
        assert group["category"] and group["tools"]
        assert group["count"] == len(group["tools"])
        assert set(group["tools"][0]) >= {"name", "description", "permission"}


def test_summarize_builds_a_card_for_list_results():
    tool = registry.TOOL_INDEX["list_hosts"]
    summary, text, card = registry.summarize(
        tool,
        {
            "ok": True,
            "status": 200,
            "data": [{"id": 1, "name": "web-01", "address": "10.0.0.1"}],
            "message": "ok",
        },
    )
    assert summary
    assert "web-01" in text
    assert card is not None and card["type"] == "table"
    assert "web-01" in str(card)


def test_summarize_reports_failures_without_a_card():
    tool = registry.TOOL_INDEX["get_host"]
    summary, text, card = registry.summarize(
        tool, {"ok": False, "status": 404, "data": None, "message": "主机不存在"}
    )
    assert "主机不存在" in summary
    # 失败原因必须同时进到「给模型看的文本」里，否则模型只会看到一段空内容
    assert "主机不存在" in text
    assert card is None


def test_summary_speaks_plain_chinese_to_humans():
    """人看的摘要必须是中文直白：审计事件里不该出现「AI 调用工具 X：ok / OK」。"""
    tool = registry.TOOL_INDEX["list_file_logs"]
    summary, _text, _card = registry.summarize(
        tool,
        {"ok": True, "status": 200, "data": {"success": True, "data": [], "total": 0}, "message": "ok"},
    )
    assert summary == "成功（共 0 条）"
    summary, _text, _card = registry.summarize(
        tool, {"ok": True, "status": 200, "data": {"success": True, "data": [], "total": 3}, "message": "OK"}
    )
    assert summary == "成功（共 3 条）"
    # 失败但上游没给人话时，也要给出中文原因（HTTP 码是系统字段，保留）
    summary, _text, _card = registry.summarize(
        tool, {"ok": False, "status": 500, "data": None, "message": ""}
    )
    assert summary == "失败：服务端返回 HTTP 500"


def test_machine_words_become_chinese_but_system_codes_stay_english():
    assert registry._human_message("ok", True) == "成功"
    assert registry._human_message("OK", True) == "成功"
    assert registry._human_message("success", True) == "成功"
    assert registry._human_message("", True) == "成功"
    assert registry._human_message("", False, 502) == "服务端返回 HTTP 502"
    # 系统字段保持英文原样，中文说明原样透传
    assert registry._human_message("权限不足，需要：policy:view", False) == "权限不足，需要：policy:view"


def test_unknown_tool_is_rejected_before_calling_the_api():
    with pytest.raises(KeyError):
        registry.TOOL_INDEX["no_such_tool"]


def test_invoke_denies_when_token_has_no_permission(app, client, admin_token):
    """AI 工具是"带调用者 JWT 打自家 REST"，没有权限就必须被后端拒掉。"""
    from app.ai import service as ai_service
    from app.security import hash_password
    from app.extensions import db
    from app.models import Role, User

    with app.app_context():
        role = Role.query.filter_by(code="viewer").first()
        assert role is not None
        role.permissions = ["dashboard:view"]
        db.session.add(
            User(
                username="ai-viewer",
                password_hash=hash_password("User1234"),
                role_id=role.id,
                status="active",
            )
        )
        db.session.commit()

    client_ = app.test_client()
    token = (
        client_.post(
            "/api/login/account",
            json={"username": "ai-viewer", "password": "User1234", "type": "account"},
        ).get_json()
        or {}
    )["data"]["token"]

    tool = registry.TOOL_INDEX["create_host"]
    result = registry.invoke(app, tool, {"name": "x", "address": "1.1.1.1"}, token)
    assert result["ok"] is False
    assert result["status"] == 403

    _ = (ai_service, admin_token)
