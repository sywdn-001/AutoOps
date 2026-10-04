"""「一台主机多协议」（`host_protocols` 端点表）的回归测试。

背景：Windows 机器常常同时有 RDP 与 WinRM 两个入口，过去只能登记成两条主机
（`win-75` 与 `win-75-winrm`），使用者看到的是「两台机器」。现在协议下沉成**端点**：
一台主机挂多个端点，列表按机器合并成一行、连接时选协议；`hosts.protocol` / `port` /
`winrm_transport` 退化成「主端点镜像」，只为兼容老代码路径与老库数据。

两条产品口径必须钉死：

* 没有任何端点行的老主机（`make_host()` 直接建模型、老库还没回填）必须继续可用 ——
  ``Host.protocol_endpoints()`` 退化成主机镜像字段的兜底视图；
* 字符入口（网关菜单 / 网页终端 / `/check`）只认 `ssh` 与 `winrm` 端点，
  `rdp` 端点只出现在远程桌面入口。
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import Grant, Host, HostProtocol, SessionRecord, User

# --------------------------------------------------------------------------
# 模型层：端点视图与兜底
# --------------------------------------------------------------------------


def test_model_falls_back_to_host_mirror_fields(app, make_host):
    """没有端点行的老主机：退化成一条只读兜底端点，端口/协议取主机镜像字段。"""
    host_id = make_host(name="legacy-01", address="10.0.0.1", port=2222, protocol="ssh")

    with app.app_context():
        host = db.session.get(Host, host_id)
        items = host.protocol_endpoints()
        assert [item.protocol for item in items] == ["ssh"]
        assert items[0].port == 2222
        assert items[0].persisted is False  # 兜底视图，不是数据库行
        assert host.protocol_names() == ["ssh"]
        assert host.supports_protocol("ssh") is True
        assert host.supports_protocol("winrm") is False
        endpoint = host.endpoint_for()
        assert endpoint is not None
        assert (endpoint.protocol, endpoint.port) == ("ssh", 2222)
        # to_dict() 也要把它吐出来：前端编辑表单靠 `protocols` 回显
        assert host.to_dict()["protocols"][0]["protocol"] == "ssh"


def test_model_endpoints_come_from_rows_when_present(app, make_host):
    """有端点行时以行为准，且能按协议取、能列名字、能对齐镜像字段。"""
    host_id = make_host(name="multi-01", address="10.0.0.2", port=22, protocol="ssh")

    with app.app_context():
        host = db.session.get(Host, host_id)
        db.session.add_all(
            [
                HostProtocol(host_id=host.id, protocol="ssh", port=22),
                HostProtocol(host_id=host.id, protocol="winrm", port=5985, winrm_transport="basic"),
            ]
        )
        db.session.commit()
        db.session.expire(host, ["protocols"])

        assert host.protocol_names() == ["ssh", "winrm"]
        assert host.endpoint_for("winrm").port == 5985
        assert host.endpoint_for("winrm").winrm_transport == "basic"
        assert host.endpoint_for("rdp") is None
        assert [item["protocol"] for item in host.to_dict()["protocols"]] == ["ssh", "winrm"]

        # 镜像字段对齐到「同名端点」
        host.protocol = "winrm"
        host.mirror_primary_endpoint()
        assert (host.protocol, host.port, host.winrm_transport) == ("winrm", 5985, "basic")

        # 端点表有行时不再兜底：把 winrm 停用后它就不出现在可用列表里
        host.protocols[1].status = "disabled"
        db.session.commit()
        db.session.expire(host, ["protocols"])
        assert host.protocol_names() == ["ssh"]
        assert host.protocol_names(include_disabled=True) == ["ssh", "winrm"]


# --------------------------------------------------------------------------
# 资产接口：protocols[] 的增删改与校验
# --------------------------------------------------------------------------


def _host_rows(host_id: int) -> list[HostProtocol]:
    return (
        HostProtocol.query.filter_by(host_id=host_id).order_by(HostProtocol.id.asc()).all()
    )


def _token_of(client, username: str, password: str = "User1234") -> str:
    """用运维账号登录换 token。

    本仓库 conftest 只提供 `make_user` 而没有现成的 `token_of` 夹具
    （`tests/test_rdp_gateway.py` 里也是自己登录），所以这里照 `POST /api/login/account`
    的信封自己取 `data.token`。
    """
    resp = client.post(
        "/api/login/account",
        json={"username": username, "password": password, "type": "account", "autoLogin": True},
    )
    assert resp.status_code == 200, resp.get_json()
    token = (resp.get_json().get("data") or {}).get("token")
    assert token, resp.get_json()
    return token


def test_create_host_with_multiple_protocols(app, client, admin_headers):
    """`protocols` 数组（对象写法）→ 一条主机、多条端点，镜像字段跟随主端点。"""
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={
            "name": "win-multi",
            "address": "10.0.0.75",
            "osType": "windows",
            "protocol": "rdp",
            "protocols": [
                {"protocol": "rdp", "port": 3389},
                {"protocol": "winrm", "port": 5985, "winrmTransport": "ntlm"},
            ],
            "description": "一台机器两个入口",
        },
    )
    assert resp.status_code in (200, 201), resp.get_json()
    data = resp.get_json()["data"]
    assert [item["protocol"] for item in data["protocols"]] == ["rdp", "winrm"]
    # 主端点镜像：请求里 protocol=rdp，所以镜像字段是 rdp/3389
    assert (data["protocol"], data["port"]) == ("rdp", 3389)

    with app.app_context():
        rows = _host_rows(data["id"])
        assert [(row.protocol, row.port) for row in rows] == [("rdp", 3389), ("winrm", 5985)]
        assert rows[1].winrm_transport == "ntlm"
        assert rows[1].status == "active"


def test_create_host_accepts_plain_protocol_names(app, client, admin_headers):
    """简写 `["ssh", "winrm"]` 也能用：端口取各协议默认值。"""
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": "multi-plain", "address": "10.0.0.76", "protocols": ["ssh", "winrm"]},
    )
    assert resp.status_code in (200, 201), resp.get_json()
    data = resp.get_json()["data"]
    ports = {item["protocol"]: item["port"] for item in data["protocols"]}
    assert ports == {"ssh": 22, "winrm": 5985}
    assert data["protocol"] == "ssh"  # 没指定 protocol，第一条当主端点


def test_primary_endpoint_follows_requested_protocol(app, client, admin_headers):
    """`protocol` 指向端点列表里的第二条时，镜像字段跟着它走。"""
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={
            "name": "multi-primary",
            "address": "10.0.0.77",
            "protocol": "winrm",
            "protocols": [{"protocol": "ssh", "port": 22}, {"protocol": "winrm", "port": 5986}],
        },
    )
    assert resp.status_code in (200, 201), resp.get_json()
    data = resp.get_json()["data"]
    assert (data["protocol"], data["port"]) == ("winrm", 5986)


def test_update_host_protocols_add_change_remove(app, client, admin_headers):
    """更新端点表：加一个、改端口、删一个，删掉主协议后镜像字段必须重挑。"""
    created = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": "multi-edit", "address": "10.0.0.78", "protocols": ["ssh"]},
    ).get_json()["data"]
    host_id = created["id"]

    # 加一个 winrm 端点，并把 ssh 端口改掉
    resp = client.put(
        f"/api/hosts/{host_id}",
        headers=admin_headers,
        json={
            "protocols": [
                {"protocol": "ssh", "port": 2200},
                {"protocol": "winrm", "port": 5985},
            ]
        },
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert {item["protocol"]: item["port"] for item in data["protocols"]} == {
        "ssh": 2200,
        "winrm": 5985,
    }

    # 删掉 ssh（原主协议）→ 只剩 winrm，镜像字段必须跟着变成 winrm/5985
    resp = client.put(
        f"/api/hosts/{host_id}", headers=admin_headers, json={"protocols": ["winrm"]}
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert [item["protocol"] for item in data["protocols"]] == ["winrm"]
    assert (data["protocol"], data["port"]) == ("winrm", 5985)

    with app.app_context():
        rows = _host_rows(host_id)
        assert [row.protocol for row in rows] == ["winrm"]


def test_update_without_protocols_keeps_legacy_single_protocol_path(
    app, client, admin_headers, make_host
):
    """老客户端不带 `protocols` 时：改主机镜像字段，且不会凭空建端点行。"""
    host_id = make_host(name="legacy-edit", address="10.0.0.79", port=22, protocol="ssh")

    resp = client.put(
        f"/api/hosts/{host_id}",
        headers=admin_headers,
        json={"description": "只改描述", "port": 2222},
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["port"] == 2222
    with app.app_context():
        assert _host_rows(host_id) == []
        host = db.session.get(Host, host_id)
        assert [item.protocol for item in host.protocol_endpoints()] == ["ssh"]


@pytest.mark.parametrize(
    "protocols,keyword",
    [
        ([], "至少"),
        (["telnet"], "主机协议只能是"),
        (["ssh", "ssh"], "重复"),
        ([{"protocol": "ssh", "port": 0}], "端口"),
        ([{"protocol": "ssh", "port": 70000}], "端口"),
        ([{"protocol": "winrm", "winrmTransport": "kerberos"}], "WinRM 认证方式"),
        ("ssh", "必须是数组"),
    ],
)
def test_protocols_validation_rejects_bad_input(
    client, admin_headers, protocols, keyword
):
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": f"bad-{abs(hash(str(protocols))) % 100000}", "address": "10.0.0.99", "protocols": protocols},
    )
    assert resp.status_code == 400, resp.get_json()
    assert keyword in resp.get_json()["message"]


# --------------------------------------------------------------------------
# 入口页：按端点展开 + 按协议过滤
# --------------------------------------------------------------------------


def _make_multi_host(client, admin_headers, *, name, address, protocols, primary=None):
    payload = {"name": name, "address": address, "protocols": protocols}
    if primary:
        payload["protocol"] = primary
    resp = client.post("/api/hosts", headers=admin_headers, json=payload)
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()["data"]


def test_terminal_targets_expand_one_row_per_endpoint(client, admin_headers):
    """一台机器两个字符端点 → 入口页两条（同一个 hostId），各带自己的端口与协议。"""
    host = _make_multi_host(
        client,
        admin_headers,
        name="multi-term",
        address="10.0.0.80",
        protocols=[{"protocol": "ssh", "port": 22}, {"protocol": "winrm", "port": 5985}],
    )

    body = client.get("/api/terminal/targets", headers=admin_headers).get_json()
    rows = [item for item in body["data"] if item["hostId"] == host["id"]]
    assert len(rows) == 2
    assert {row["protocol"]: row["port"] for row in rows} == {"ssh": 22, "winrm": 5985}
    # 每行都带上完整端点表：前端靠它把同一个 hostId 合成一行、每协议一个按钮
    assert [item["protocol"] for item in rows[0]["endpoints"]] == ["ssh", "winrm"]
    assert rows[0]["protocols"] == ["ssh", "winrm"]
    assert rows[0]["address"] == "10.0.0.80"


def test_terminal_targets_hide_rdp_endpoint_of_multi_protocol_host(client, admin_headers, make_account):
    """rdp + winrm 的机器：字符校验只认 winrm，rdp 端点只在远程桌面入口。

    注意入口**列表**本身对管理员是三条都给的 —— 管理员同时有 `terminal:use` 与
    `rdp:use`，`terminal_targets()` 会把 ssh/winrm 与 rdp 都列出来；「只给字符权限的
    角色看不到 rdp」由 ``test_ssh_only_role_sees_no_rdp_endpoint_of_multi_protocol_host``
    覆盖，这里只钉「点名 rdp 做字符准入必须被拒」。
    """
    host = _make_multi_host(
        client,
        admin_headers,
        name="multi-win",
        address="10.0.0.81",
        protocols=[{"protocol": "rdp", "port": 3389}, {"protocol": "winrm", "port": 5985}],
        primary="rdp",
    )
    make_account(host["id"], name="Administrator", username="Administrator")

    rows = [
        item
        for item in client.get("/api/terminal/targets", headers=admin_headers).get_json()["data"]
        if item["hostId"] == host["id"]
    ]
    assert sorted(row["protocol"] for row in rows) == ["rdp", "winrm"]
    assert {row["protocol"]: row["port"] for row in rows} == {"rdp": 3389, "winrm": 5985}

    # 明确点名 rdp 端点做准入校验 → 必须拒绝并说清原因
    resp = client.post(
        f"/api/terminal/targets/{host['id']}/check",
        headers=admin_headers,
        json={"protocol": "rdp"},
    )
    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()["data"]
    assert payload["allowed"] is False
    assert "rdp" in payload["reason"]

    # 点名 winrm 端点 → 放行
    ok = client.post(
        f"/api/terminal/targets/{host['id']}/check",
        headers=admin_headers,
        json={"protocol": "winrm"},
    ).get_json()["data"]
    assert ok["allowed"] is True

    # 远程桌面入口正常列出这台机器
    rdp_rows = [
        item
        for item in client.get("/api/rdp/targets", headers=admin_headers).get_json()["data"]
        if item["hostId"] == host["id"]
    ]
    assert [row["protocol"] for row in rdp_rows] == ["rdp"]
    assert rdp_rows[0]["port"] == 3389


def test_rdp_ticket_uses_the_rdp_endpoint_port(app, client, admin_headers, make_account):
    """远程桌面票据的端口取 rdp 端点，不是主机镜像字段/3389 常量。"""
    host = _make_multi_host(
        client,
        admin_headers,
        name="multi-rdp-port",
        address="10.0.0.82",
        protocols=[{"protocol": "rdp", "port": 4444}, {"protocol": "winrm", "port": 5985}],
        primary="winrm",  # 镜像字段故意指向 5985，票据仍必须用 4444
    )
    account_id = make_account(host["id"], name="Administrator", username="Administrator")

    resp = client.post(
        "/api/rdp/sessions",
        headers=admin_headers,
        json={"hostId": host["id"], "accountId": account_id},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["account"]["id"] == account_id

    with app.app_context():
        from app.models import AuditLog

        row = (
            AuditLog.query.filter_by(action="rdp_ticket")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.detail["destination"] == "10.0.0.82:4444"


def test_ssh_only_role_sees_no_rdp_endpoint_of_multi_protocol_host(
    app, client, make_host, make_account, make_grant, make_user
):
    """只有 `terminal:use` 的角色：看得到 winrm 端点，看不到同一台机器的 rdp 端点。"""
    with app.app_context():
        from app.models import Role

        role = Role.query.filter_by(code="rdp-test-ssh-only").first()
        if role is None:
            role = Role(
                code="rdp-test-ssh-only",
                name="rdp-test-ssh-only",
                description="测试用最小角色",
                permissions=["dashboard:view", "terminal:use"],
            )
            db.session.add(role)
            db.session.commit()
    user_id = make_user("multi-ssh-only", role_code="rdp-test-ssh-only")
    host_id = make_host(name="multi-perm", address="10.0.0.83", port=3389, protocol="rdp")
    make_account(host_id, name="Administrator", username="Administrator")
    with app.app_context():
        db.session.add(HostProtocol(host_id=host_id, protocol="rdp", port=3389))
        db.session.add(HostProtocol(host_id=host_id, protocol="winrm", port=5985))
        db.session.commit()
    make_grant(user_id, host_id)

    body = client.get(
        "/api/terminal/targets",
        headers={"Authorization": f"Bearer {_token_of(client, 'multi-ssh-only')}"},
    ).get_json()
    rows = [item for item in body["data"] if item["hostId"] == host_id]
    assert [row["protocol"] for row in rows] == ["winrm"]


# --------------------------------------------------------------------------
# 合并接口：把历史遗留的「同机两条」收成一台
# --------------------------------------------------------------------------


def test_merge_host_moves_accounts_grants_and_endpoints(
    app, client, admin_headers, make_host, make_account, make_grant, make_user
):
    """`win-75`(rdp) + `win-75-winrm`(winrm) → 一台两端口，账号/授权/端点全搬过去。"""
    user_id = make_user("merge-viewer")
    target_id = make_host(name="win-75", address="10.0.0.75", port=3389, protocol="rdp", os_type="windows")
    source_id = make_host(
        name="win-75-winrm", address="10.0.0.75", port=5985, protocol="winrm", os_type="windows"
    )
    kept_account = make_account(target_id, name="Administrator", username="Administrator")
    twin_account = make_account(source_id, name="Administrator", username="Administrator")
    other_account = make_account(source_id, name="ops", username="ops")
    make_grant(user_id, source_id, twin_account)  # 与目标机同名同凭据 ⇒ 授权改挂到目标机账号
    make_grant(user_id, source_id, other_account)  # 新账号 ⇒ 搬过来

    # `make_host()` 直接建模型、不写端点行，所以这里显式给两台机器配上端点
    with app.app_context():
        db.session.add_all(
            [
                HostProtocol(host_id=target_id, protocol="rdp", port=3389),
                HostProtocol(host_id=source_id, protocol="winrm", port=5985),
            ]
        )
        db.session.commit()

    resp = client.post(
        f"/api/hosts/{target_id}/merge", headers=admin_headers, json={"sourceId": source_id}
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert [item["protocol"] for item in data["protocols"]] == ["rdp", "winrm"]
    assert {item["name"] for item in data["accounts"]} == {"Administrator", "ops"}

    with app.app_context():
        assert db.session.get(Host, source_id) is None  # 来源主机已删除
        assert _host_rows(source_id) == []
        target_rows = _host_rows(target_id)
        assert [(row.protocol, row.port) for row in target_rows] == [("rdp", 3389), ("winrm", 5985)]
        grants = Grant.query.filter_by(host_id=target_id).all()
        assert len(grants) == 2
        accounts = {item.id: item for item in db.session.get(Host, target_id).accounts}
        assert grants[0].host_account_id in accounts
        assert grants[1].host_account_id in accounts
        assert kept_account in accounts  # 复用的那个账号还是原来那条
        from app.models import AuditLog

        assert (
            AuditLog.query.filter_by(action="merge_host").order_by(AuditLog.id.desc()).first()
            is not None
        )


def test_merge_host_keeps_a_same_named_account_with_other_credential(
    app, client, admin_headers, make_host, make_account, make_grant, make_user
):
    """同名账号但口令不同 ⇒ 两份都要留下（改名搬过来），不能因为重名把凭据丢掉。"""
    user_id = make_user("merge-viewer2")
    target_id = make_host(name="win-76", address="10.0.0.76", port=3389, protocol="rdp")
    source_id = make_host(name="win-76-winrm", address="10.0.0.76", port=5985, protocol="winrm")
    make_account(target_id, name="Administrator", username="Administrator", password="first-pwd")
    other = make_account(
        source_id, name="Administrator", username="Administrator", password="second-pwd"
    )
    make_grant(user_id, source_id, other)

    resp = client.post(
        f"/api/hosts/{target_id}/merge", headers=admin_headers, json={"sourceId": source_id}
    )
    assert resp.status_code == 200, resp.get_json()
    names = {item["name"] for item in resp.get_json()["data"]["accounts"]}
    assert names == {"Administrator", f"Administrator-{source_id}"}

    with app.app_context():
        target = db.session.get(Host, target_id)
        assert len(target.accounts) == 2
        grants = Grant.query.filter_by(host_id=target_id).all()
        assert len(grants) == 1
        moved = next(row for row in target.accounts if row.name != "Administrator")
        assert grants[0].host_account_id == moved.id  # 授权跟着改名的账号走
        from app.crypto import decrypt

        assert decrypt(moved.secret_enc) == "second-pwd"  # 来源凭据没被覆盖也没丢


def test_merge_host_rejects_bad_requests(client, admin_headers, make_host):
    """地址不同 / 自己并自己 / 缺参数 / 来源有在线会话 —— 都必须明确拒绝。"""
    a_id = make_host(name="merge-a", address="10.0.0.90", port=22, protocol="ssh")
    b_id = make_host(name="merge-b", address="10.0.0.91", port=22, protocol="ssh")
    # 与 a 同地址的第三台：专门用来验「来源有在线会话」这条（前面的地址校验会先拦住）
    c_id = make_host(name="merge-c", address="10.0.0.90", port=22, protocol="ssh")

    assert (
        client.post(f"/api/hosts/{a_id}/merge", headers=admin_headers, json={"sourceId": b_id}).status_code
        == 400
    )
    assert (
        client.post(f"/api/hosts/{a_id}/merge", headers=admin_headers, json={"sourceId": a_id}).status_code
        == 400
    )
    assert client.post(f"/api/hosts/{a_id}/merge", headers=admin_headers, json={}).status_code == 400
    assert (
        client.post(f"/api/hosts/{a_id}/merge", headers=admin_headers, json={"sourceId": 999999}).status_code
        == 404
    )

    with client.application.app_context():
        db.session.add(
            SessionRecord(
                sid="merge-active-sid",
                user_id=1,
                username="admin",
                host_id=c_id,
                host_name="merge-c",
                host_address="10.0.0.90",
                source="web",
                protocol="ssh",
                status="active",
            )
        )
        db.session.commit()
    blocked = client.post(f"/api/hosts/{a_id}/merge", headers=admin_headers, json={"sourceId": c_id})
    assert blocked.status_code == 409, blocked.get_json()
    assert "在线会话" in blocked.get_json()["message"]


# --------------------------------------------------------------------------
# 老库回填
# --------------------------------------------------------------------------


def test_backfill_creates_missing_endpoints_and_is_idempotent(app, make_host):
    host_id = make_host(name="legacy-backfill", address="10.0.0.92", port=2222, protocol="ssh")

    with app.app_context():
        from app.schema_sync import backfill_host_protocols

        assert HostProtocol.query.filter_by(host_id=host_id).count() == 0
        assert backfill_host_protocols() >= 1
        rows = HostProtocol.query.filter_by(host_id=host_id).all()
        assert len(rows) == 1
        assert (rows[0].protocol, rows[0].port, rows[0].status) == ("ssh", 2222, "active")
        # 幂等：再跑一次什么都补不了（已经有端点的机器一律跳过）
        assert backfill_host_protocols() == 0
        # 手工配过多端点的机器不会被覆盖
        host = db.session.get(Host, host_id)
        db.session.add(HostProtocol(host_id=host.id, protocol="winrm", port=5985))
        db.session.commit()
        assert backfill_host_protocols() == 0
        assert sorted(host.protocol_names()) == ["ssh", "winrm"]


def test_legacy_host_without_endpoints_still_usable(app, client, admin_headers, make_host, make_account):
    """端点为空的机器（老库/直接建模型）在入口页仍然是一条，端口取主机字段。"""
    host_id = make_host(name="legacy-term", address="10.0.0.93", port=2222, protocol="ssh")
    make_account(host_id, name="root", username="root")

    rows = [
        item
        for item in client.get("/api/terminal/targets", headers=admin_headers).get_json()["data"]
        if item["hostId"] == host_id
    ]
    assert len(rows) == 1
    assert (rows[0]["protocol"], rows[0]["port"]) == ("ssh", 2222)
    checked = client.post(
        f"/api/terminal/targets/{host_id}/check", headers=admin_headers, json={"protocol": "ssh"}
    ).get_json()["data"]
    assert checked["allowed"] is True
