"""路由注册的防回归守卫。

事故背景：``hosts.py`` 与 ``policies.py`` 各自模块里定义了**两个**蓝图
（主机 + 主机分组、命令策略 + 策略规则），但 ``register_blueprints()`` 只注册了第一个。
症状不是报错，而是 ``/api/host-groups`` 与 ``/api/policy-rules`` 整组接口静默 404 ——
测试里只有管理员建机器那几条会 404，如果用例没覆盖到，前端页面就是死链而没人知道。

所以这里不写死路径清单，而是**反射式**检查：api 模块里导出的每个 Blueprint
都必须在 app 上注册。以后新增模块漏注册，这个用例会直接红。
"""

from __future__ import annotations

import importlib
import re

import pytest
from flask import Blueprint

API_MODULES = [
    "auth",
    "users",
    "roles",
    "hosts",
    "grants",
    "policies",
    "sessions",
    "audits",
    "settings",
]


def test_every_api_blueprint_is_registered(app):
    missing: list[str] = []
    for module_name in API_MODULES:
        module = importlib.import_module(f"app.api.{module_name}")
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, Blueprint) and obj.name not in app.blueprints:
                missing.append(f"app/api/{module_name}.py::{attr}（蓝图名 {obj.name}）")
    assert not missing, "以下蓝图没有注册，整组接口会静默 404：" + "；".join(missing)


def test_url_map_has_no_duplicate_rule_method_pairs(app):
    """同一 (路径, 方法) 不能注册两次 —— 后者会被忽略，接口行为与代码不符。"""
    seen: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []
    for rule in app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            key = (str(rule), method)
            if key in seen and seen[key] != rule.endpoint:
                duplicates.append(f"{method} {rule} 同时属于 {seen[key]} 与 {rule.endpoint}")
            else:
                seen[key] = rule.endpoint
    assert not duplicates, "；".join(duplicates)


@pytest.mark.parametrize("path", ["/api/health", "/api/hosts", "/api/policy-rules"])
def test_core_paths_are_not_404(client, admin_headers, path):
    resp = client.get(path, headers=admin_headers)
    assert resp.status_code != 404, f"{path} 返回 404 —— 蓝图没注册或路径写错"


# --------------------------------------------------------------------------
# 全接口 5xx 冒烟：把「模型列名写错 / 属性不存在」这类错误一次全抓出来
#
# 事故背景：CommandLog 的时间列是 started_at、AuditLog 的是 ts，但代码里按
# ``created_at`` 查过两次，只有被用例打到的那条才暴露。这类错误的特点是
# **只有运行时走到那一行才炸**，靠单点用例赌运气。这里改成遍历 url_map 打全量。
# --------------------------------------------------------------------------

_CONVERTER_RE = re.compile(r"<[^<>]+>")


def _api_rules(app, method: str):
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api"):
            continue
        if method not in (rule.methods - {"HEAD", "OPTIONS"}):
            continue
        yield path, _CONVERTER_RE.sub("1", path)


def test_all_get_endpoints_do_not_5xx(app, client, admin_headers):
    failures = []
    for path, url in _api_rules(app, "GET"):
        resp = client.get(url, headers=admin_headers)
        if resp.status_code >= 500:
            failures.append(f"GET {url}（规则 {path}）→ {resp.status_code}")
    assert not failures, "以下接口 5xx，多半是模型列名/属性写错：" + "；".join(failures)


def test_all_post_endpoints_do_not_5xx(app, client, admin_headers):
    """空 body 打一遍所有 POST：期望 2xx/4xx，只要不炸成 5xx 就算过。

    空 body 触发的 400 是这个用例的**正常结果**，它证明校验分支没抛异常。
    """
    failures = []
    for path, url in _api_rules(app, "POST"):
        resp = client.post(url, json={}, headers=admin_headers)
        if resp.status_code >= 500:
            failures.append(f"POST {url}（规则 {path}）→ {resp.status_code}")
    assert not failures, "以下接口 5xx（空 body 校验或查询写错）：" + "；".join(failures)


# --------------------------------------------------------------------------
# Socket.IO 事件注册守卫
#
# 事故背景：flask_socketio 的 ``SocketIO.on`` 在 ``self.server`` 已存在时**直接注册
# 到那个 server 对象**；而 ``init_app()`` 每次调用都会重建 ``socketio.server``。
# 结果：同一个进程里第二次 ``create_app`` 拿到的 server 上**一个 webterm 处理器都没有**
# —— 连接能建立（engineio 握手成功、客户端 is_connected() 为 True），
# 但 ``terminal:ready`` 永远不下发，表象是「网页终端连上了却什么都不响」。
# 这个用例连续建两个 app，逐一断言处理器都在。
# --------------------------------------------------------------------------


def test_webterm_events_registered_on_every_app():
    from app import create_app
    from app.config import TestConfig
    from app.extensions import socketio
    from app.webterm import events as webterm_events

    expected = {name for name, _handler in webterm_events._EVENTS}
    assert {"connect", "disconnect", "terminal:open", "terminal:input"} <= expected, (
        f"webterm 事件登记表不完整：{sorted(expected)}"
    )

    for index in (1, 2):
        create_app(TestConfig)
        registered = set(socketio.server.handlers.get("/", {}))
        missing = expected - registered
        assert not missing, f"第 {index} 个 app 的 socketio server 缺少事件处理器：{sorted(missing)}"
