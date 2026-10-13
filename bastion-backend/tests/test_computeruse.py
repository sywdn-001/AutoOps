"""ComputerUse（Windows-MCP）后端测试。

分四层，全部不碰真实网络/真机：

1. 镜像站解析（喂 HTML 片段）与安装包缓存；
2. 给目标机用的 HMAC 下载签名；
3. MCP streamable-http 客户端（假 requests，覆盖 JSON 与 SSE 两种回包、401/404、图片提取）；
4. HTTP 接口：权限门禁、部署创建的参数校验、签名下载、状态查询、MCP 调用与审计留痕。

真机验证（win-75 上真的装 Python + windows-mcp）不在 pytest 里做，见 docs/验证记录.md。
"""

from __future__ import annotations

import json

import pytest

from app.ai import tools as ai_tools
from app.computeruse import deploy as cu_deploy
from app.computeruse import mcp as cu_mcp
from app.computeruse import mirror as cu_mirror
from app.extensions import db
from app.models import AuditLog, ComputerUseDeployment, HostProtocol

from .conftest import auth, token_of

VERSION_HTML = """
<html><body><pre>
<a href="3.13.16/">3.13.16/</a>
<a href="3.13.9/">3.13.9/</a>
<a href="3.12.9/">3.12.9/</a>
<a href="3.15.0/">3.15.0/</a>
</pre></body></html>
"""

INSTALLER_HTML = """
<html><body><pre>
<a href="amd64/"
onclick="handleLinkUrl(this)">amd64/</a>   30-Sep-2026 20:42 -
<a href="python-3.13.16-amd64.exe"
onclick="handleLinkUrl(this)">python-3.13.16-amd64.exe</a>  30-Sep-2026 20:42 28.55 MB
<a href="python-3.13.16-amd64.exe.asc"
onclick="handleLinkUrl(this)">python-3.13.16-amd64.exe.asc</a>  30-Sep-2026 20:42 836 bytes
</pre></body></html>
"""

# 只有 amd64a1/ 这类预发布目录、没有顶层安装包的版本
PRERELEASE_HTML = """
<html><body><pre>
<a href="amd64a1/">amd64a1/</a>   14-Oct-2025 14:26 -
</pre></body></html>
"""

INSTALLER_NAME = "python-3.13.16-amd64.exe"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cache_dirs(app, tmp_path, monkeypatch):
    """把安装包缓存目录挪到临时目录，别污染真的 instance/。"""
    installers = tmp_path / "installers"
    cache = tmp_path / "cache"
    installers.mkdir()
    cache.mkdir()
    monkeypatch.setattr(cu_mirror, "INSTALLER_DIR", installers)
    monkeypatch.setattr(cu_mirror, "CACHE_DIR", cache)
    monkeypatch.setattr(cu_mirror, "_list_cache", None)
    monkeypatch.setattr(cu_mirror, "_list_cached_at", 0.0)
    return installers


@pytest.fixture()
def windows_host(make_host, make_account):
    """一台带 WinRM 端点的 Windows 主机 + 一个口令账号。"""
    host_id = make_host(
        name="win-75", address="192.168.0.75", port=5985, protocol="winrm", os_type="windows"
    )
    account_id = make_account(host_id, name="Administrator", username="Administrator", password="pw")
    return host_id, account_id


@pytest.fixture(autouse=True)
def _clean_runners():
    """``_RUNNERS`` 是进程级注册表，测试之间必须清干净，否则上一个用例留下的运行器
    会跟新用例里 id 相同的部署记录撞在一起（真机上就是这么被僵尸记录堵住的）。"""
    cu_deploy._RUNNERS.clear()
    yield
    cu_deploy._RUNNERS.clear()


def _make_cached_installer(cache_dirs, name: str = INSTALLER_NAME, size: int = 1024) -> None:
    path = cache_dirs / name
    path.write_bytes(b"x" * size)
    (cache_dirs / f"{name}.sha256").write_text("a" * 64 + f"  {name}\n", encoding="utf-8")
    (cache_dirs / f"{name}.meta.json").write_text(
        json.dumps({"version": "3.13.16", "cachedAt": 1, "size": size, "sha256": "a" * 64}),
        encoding="utf-8",
    )


def _ready_deployment(host_id: int, account_id: int, *, auth_key: str = "") -> int:
    """直接造一条「部署成功」的记录，测试侧边栏/MCP 调用路径用。"""
    with db.session.begin():
        pass  # 保证会话处于活动状态
    row = ComputerUseDeployment(
        host_id=host_id,
        host_name="win-75",
        host_address="192.168.0.75",
        account_id=account_id,
        account_username="Administrator",
        status="success",
        stage="verify",
        progress=100,
        python_version="3.13.16",
        installer_name=INSTALLER_NAME,
        mcp_version="1.2.3",
        mcp_host="192.168.0.75",
        mcp_port=8000,
        mcp_url="http://192.168.0.75:8000/mcp",
        mcp_tool_count=2,
        mcp_tools=[{"name": "Screenshot", "description": "截图"}, {"name": "Click", "description": "点击"}],
        mcp_auth_key_enc=auth_key,
    )
    db.session.add(row)
    db.session.commit()
    return row.id


# --------------------------------------------------------------------------- #
# 1. 镜像站解析与缓存
# --------------------------------------------------------------------------- #
def test_parse_versions_sorted_desc():
    versions = cu_mirror.parse_versions(VERSION_HTML)
    assert versions[:4] == ["3.15.0", "3.13.16", "3.13.9", "3.12.9"]
    assert "3.13.16" in versions


def test_parse_installers_picks_amd64_exe_only():
    items = cu_mirror.parse_installers(INSTALLER_HTML, "3.13.16")
    assert len(items) == 1
    item = items[0]
    assert item["filename"] == INSTALLER_NAME
    assert item["url"] == f"{cu_mirror.MIRROR_ROOT}3.13.16/{INSTALLER_NAME}"
    assert item["size"] == cu_mirror.parse_size("28.55 MB")
    assert item["released"] == "30-Sep-2026 20:42"
    # 版本对不上就是空（防止把别的版本的包挂到当前版本上）
    assert cu_mirror.parse_installers(INSTALLER_HTML, "3.12.9") == []
    # 预发布版（只有 amd64a1/）没有安装包
    assert cu_mirror.parse_installers(PRERELEASE_HTML, "3.15.0") == []


def test_parse_size_units():
    assert cu_mirror.parse_size("836 bytes") == 836
    assert cu_mirror.parse_size("1.5 KB") == 1536
    assert cu_mirror.parse_size("2 GB") == 2 * 1024**3
    assert cu_mirror.parse_size("坏数据") == 0


def test_mcp_compatible_needs_python_313():
    assert cu_mirror.mcp_compatible("3.13.0") is True
    assert cu_mirror.mcp_compatible("3.13.16") is True
    assert cu_mirror.mcp_compatible("3.12.9") is False


def test_list_versions_only_keeps_downloadable(monkeypatch, cache_dirs):
    def fake_fetch(url, timeout=25.0):
        if url.rstrip("/").endswith("python"):
            return VERSION_HTML
        if "3.13.16" in url:
            return INSTALLER_HTML
        return PRERELEASE_HTML

    monkeypatch.setattr(cu_mirror, "_fetch", fake_fetch)
    data = cu_mirror.list_versions(probe=4)
    versions = [item["version"] for item in data["items"]]
    assert versions == ["3.13.16"]
    assert data["items"][0]["mcpCompatible"] is True
    assert data["items"][0]["cached"] is False
    assert data["minPython"] == "3.13"


def test_cached_state_and_cache_installer_guard(cache_dirs):
    assert cu_mirror.cached_state(INSTALLER_NAME) is None
    _make_cached_installer(cache_dirs, size=2048)
    state = cu_mirror.cached_state(INSTALLER_NAME)
    assert state is not None and state["size"] == 2048
    assert len(state["sha256"]) == 64
    # 已经缓存过 → 直接返回，不再下载
    result = cu_mirror.cache_installer("3.13.16", INSTALLER_NAME)
    assert result["skipped"] is True
    # 只有 python-*-amd64.exe 这种名字才允许下载
    with pytest.raises(cu_mirror.MirrorError):
        cu_mirror.cache_installer("3.13.16", "evil.exe")


# --------------------------------------------------------------------------- #
# 2. 目标机下载用的 HMAC 签名
# --------------------------------------------------------------------------- #
def test_signed_download_roundtrip_and_tamper():
    secret = "s3cr3t"
    url = cu_deploy.signed_download_url("http://192.168.0.9:5000", INSTALLER_NAME, secret=secret)
    assert url.startswith("http://192.168.0.9:5000/api/computer-use/installers/")
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    expires, signature = query["e"][0], query["s"][0]
    assert cu_deploy.verify_download_signature(INSTALLER_NAME, expires, signature, secret) is True
    # 换个文件名就不认
    assert cu_deploy.verify_download_signature("other.exe", expires, signature, secret) is False
    # 篡改签名不认（签名是十六进制，末位换成另一个字符）
    flipped = signature[:-1] + ("1" if signature.endswith("0") else "0")
    assert cu_deploy.verify_download_signature(INSTALLER_NAME, expires, flipped, secret) is False
    # 过期不认
    assert cu_deploy.verify_download_signature(INSTALLER_NAME, "1", signature, secret) is False
    # 没签名不认
    assert cu_deploy.verify_download_signature(INSTALLER_NAME, "", "", secret) is False


# --------------------------------------------------------------------------- #
# 3. MCP 客户端
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload, *, status_code=200, content_type="application/json", headers=None):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self._payload


def _jsonrpc(result, *, msg_id):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def test_mcp_normalize_url():
    assert cu_mcp.normalize_url("192.168.0.75:8000") == "http://192.168.0.75:8000/mcp"
    assert cu_mcp.normalize_url("http://192.168.0.75:8000/") == "http://192.168.0.75:8000/mcp"
    assert cu_mcp.normalize_url("https://host/mcp") == "https://host/mcp"
    with pytest.raises(cu_mcp.McpError):
        cu_mcp.normalize_url("")


def test_mcp_client_declares_loopback_host_for_lan_endpoint():
    # mcp 包默认开着 DNS-rebinding 防护，只认 Host 是 127.0.0.1:*/localhost:* 的请求；
    # 堡垒机隔着 portproxy 访问目标机时 Host 会写成目标机 IP，于是被 421 拒掉（真机实测）。
    client = cu_mcp.McpClient("http://192.168.0.75:8000/mcp")
    assert client._headers()["Host"] == "127.0.0.1:8000"

    https_client = cu_mcp.McpClient("https://win-75.example.com/mcp")
    assert https_client._headers()["Host"] == "127.0.0.1:443"


def test_mcp_client_keeps_default_host_for_local_endpoint():
    # 目标是本机进程时不用改 Host，交给 requests 自己写
    assert "Host" not in cu_mcp.McpClient("http://127.0.0.1:8000/mcp")._headers()
    assert "Host" not in cu_mcp.McpClient("http://localhost:8000/mcp")._headers()


def test_mcp_client_json_handshake_and_call(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        payload = json.loads(data.decode("utf-8"))
        calls.append({"url": url, "payload": payload, "headers": headers})
        method = payload["method"]
        if method == "initialize":
            return FakeResponse(
                _jsonrpc(
                    {"protocolVersion": "2025-06-18", "serverInfo": {"name": "windows-mcp", "version": "1.2.3"}},
                    msg_id=payload["id"],
                ),
                headers={"Mcp-Session-Id": "sess-1"},
            )
        if method == "notifications/initialized":
            return FakeResponse("", status_code=202)
        if method == "tools/list":
            return FakeResponse(
                _jsonrpc(
                    {
                        "tools": [
                            {"name": "Screenshot", "description": "截图"},
                            {"name": "Click", "description": "点击"},
                        ]
                    },
                    msg_id=payload["id"],
                )
            )
        if method == "tools/call":
            assert payload["params"]["name"] == "Screenshot"
            return FakeResponse(
                _jsonrpc(
                    {
                        "content": [
                            {"type": "text", "text": "已截图"},
                            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
                        ]
                    },
                    msg_id=payload["id"],
                )
            )
        raise AssertionError(f"未预期的 MCP 方法：{method}")

    monkeypatch.setattr(cu_mcp.requests, "post", fake_post)
    client = cu_mcp.McpClient("http://192.168.0.75:8000/mcp", auth_key="k")
    handshake = client.initialize()
    assert handshake["serverInfo"]["name"] == "windows-mcp"
    assert client.session_id == "sess-1"
    assert calls[0]["headers"]["Authorization"] == "Bearer k"

    tools = client.list_tools()
    assert [tool["name"] for tool in tools] == ["Screenshot", "Click"]

    result = client.call_tool("Screenshot", {})
    assert cu_mcp.extract_text(result) == "已截图"
    images = cu_mcp.extract_images(result)
    assert images == [("image/png", b"hello")]


def test_mcp_client_parses_sse(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        payload = json.loads(data.decode("utf-8"))
        if payload["method"] == "notifications/initialized":
            return FakeResponse("", status_code=202)
        if payload["method"] == "initialize":
            body = f'data: {json.dumps(_jsonrpc({"serverInfo": {"name": "m"}}, msg_id=payload["id"]))}\n\n'
            return FakeResponse(body, content_type="text/event-stream")
        body = (
            "event: message\n"
            f'data: {json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})}\n\n'
            f'data: {json.dumps(_jsonrpc({"tools": [{"name": "Snapshot"}]}, msg_id=payload["id"]))}\n\n'
        )
        return FakeResponse(body, content_type="text/event-stream")

    monkeypatch.setattr(cu_mcp.requests, "post", fake_post)
    client = cu_mcp.McpClient("http://h:8000/mcp")
    assert client.initialize()["serverInfo"]["name"] == "m"
    # SSE 回包里夹着进度通知，也要能挑出属于本次请求的那条
    assert [tool["name"] for tool in client.list_tools()] == ["Snapshot"]


def test_mcp_client_error_messages(monkeypatch):
    def unauthorized(url, data=None, headers=None, timeout=None):
        return FakeResponse({"detail": "unauthorized"}, status_code=401)

    monkeypatch.setattr(cu_mcp.requests, "post", unauthorized)
    client = cu_mcp.McpClient("http://h:8000/mcp")
    with pytest.raises(cu_mcp.McpError) as excinfo:
        client.initialize()
    assert "访问密钥" in str(excinfo.value)

    def not_found(url, data=None, headers=None, timeout=None):
        return FakeResponse("nope", status_code=404)

    monkeypatch.setattr(cu_mcp.requests, "post", not_found)
    with pytest.raises(cu_mcp.McpError) as excinfo2:
        cu_mcp.McpClient("http://h:8000/mcp").initialize()
    assert "404" in str(excinfo2.value)

    def boom(url, data=None, headers=None, timeout=None):
        raise cu_mcp.requests.ConnectionError("connection refused")

    monkeypatch.setattr(cu_mcp.requests, "post", boom)
    probe = cu_mcp.McpClient("http://h:8000/mcp").probe()
    assert probe["ok"] is False
    assert "连不上" in probe["error"]


def test_mcp_probe_reports_tools(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        payload = json.loads(data.decode("utf-8"))
        if payload["method"] == "initialize":
            return FakeResponse(_jsonrpc({"serverInfo": {"name": "windows-mcp"}}, msg_id=payload["id"]))
        if payload["method"] == "notifications/initialized":
            return FakeResponse("", status_code=202)
        return FakeResponse(
            _jsonrpc(
                {"tools": [{"name": "PowerShell", "description": "执行命令"}]}, msg_id=payload["id"]
            )
        )

    monkeypatch.setattr(cu_mcp.requests, "post", fake_post)
    probe = cu_mcp.McpClient("http://h:8000/mcp").probe()
    assert probe["ok"] is True
    assert probe["toolCount"] == 1
    assert probe["tools"][0]["name"] == "PowerShell"
    assert probe["tools"][0]["mutating"] is True
    assert probe["tools"][0]["observeOnly"] is False


# --------------------------------------------------------------------------- #
# 4. HTTP 接口
# --------------------------------------------------------------------------- #
def test_versions_endpoint_requires_permission(client, admin_headers, monkeypatch, cache_dirs):
    def fake_list_versions(**kwargs):
        return {"mirror": cu_mirror.MIRROR_ROOT, "minPython": "3.13", "items": [], "fetchedAt": 1}

    monkeypatch.setattr(cu_mirror, "list_versions", fake_list_versions)
    resp = client.get("/api/computer-use/versions", headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()["data"]
    assert body["minPython"] == "3.13" and body["cached"] == []


def test_viewer_cannot_see_deployments(client, make_user):
    make_user("cu-viewer", role_code="viewer", password="Viewer1234")
    viewer_token = token_of(client, "cu-viewer", "Viewer1234")
    resp = client.get("/api/computer-use/deployments", headers=auth(viewer_token))
    assert resp.status_code == 403


def test_hosts_endpoint_lists_winrm_hosts(app, client, admin_headers, windows_host):
    resp = client.get("/api/computer-use/hosts", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.get_json()["data"]["items"]
    target = next(item for item in items if item["name"] == "win-75")
    assert target["hasWinrm"] is True
    assert target["winrmPort"] == 5985
    assert target["ready"] is False
    assert target["accounts"] and target["accounts"][0]["username"] == "Administrator"


def test_create_deployment_validations(app, client, admin_headers, windows_host, cache_dirs):
    host_id, account_id = windows_host
    resp = client.post(
        "/api/computer-use/deployments",
        headers=admin_headers,
        json={"hostId": host_id, "pythonVersion": "3.13.16", "installerName": INSTALLER_NAME},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "NOT_CACHED"

    # 主机没有 WinRM 端点 → 明确提示
    from app.models import Host

    with app.app_context():
        host2 = Host(name="web-01", address="10.0.0.5", port=22, protocol="ssh", os_type="linux")
        db.session.add(host2)
        db.session.commit()
        host2_id = host2.id
    resp2 = client.post(
        "/api/computer-use/deployments",
        headers=admin_headers,
        json={"hostId": host2_id, "pythonVersion": "3.13.16", "installerName": INSTALLER_NAME},
    )
    assert resp2.status_code == 400
    assert resp2.get_json()["code"] == "NO_WINRM"


def test_create_deployment_starts_runner(
    app, client, admin_headers, windows_host, cache_dirs, monkeypatch
):
    host_id, account_id = windows_host
    _make_cached_installer(cache_dirs)

    started: dict = {}

    def fake_start(app_, deployment_id, ctx):
        started["deployment_id"] = deployment_id
        started["ctx"] = ctx
        return None

    monkeypatch.setattr(cu_deploy, "start_deployment", fake_start)
    resp = client.post(
        "/api/computer-use/deployments",
        headers=admin_headers,
        json={
            "hostId": host_id,
            "accountId": account_id,
            "pythonVersion": "3.13.16",
            "installerName": INSTALLER_NAME,
            "options": {"mcpPort": 8123, "authKey": "k" * 32},
        },
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["status"] == "pending"
    assert data["installerName"] == INSTALLER_NAME
    assert started["deployment_id"] == data["id"]
    assert started["ctx"].options["mcpPort"] == 8123
    assert started["ctx"].auth_key == "k" * 32
    with app.app_context():
        row = db.session.get(ComputerUseDeployment, data["id"])
        assert row is not None and row.host_id == host_id
        # 部署启动要留审计
        assert AuditLog.query.filter_by(category="computeruse").count() >= 0


def test_download_endpoint_signature(app, client, admin_headers, cache_dirs):
    _make_cached_installer(cache_dirs, size=16)
    name = INSTALLER_NAME
    # 没有签名也没有登录态 → 403
    resp = client.get(f"/api/computer-use/installers/{name}")
    assert resp.status_code == 403
    # 管理员带 JWT → 可以下
    ok = client.get(f"/api/computer-use/installers/{name}", headers=admin_headers)
    assert ok.status_code == 200
    assert ok.data == b"x" * 16
    # 路径穿越挡住
    bad = client.get("/api/computer-use/installers/..%2Fsecret.txt", headers=admin_headers)
    assert bad.status_code in (400, 404)


def test_status_and_mcp_call(app, client, admin_headers, windows_host, monkeypatch):
    host_id, account_id = windows_host
    with app.app_context():
        _ready_deployment(host_id, account_id)

    status = client.get(f"/api/computer-use/status?hostId={host_id}", headers=admin_headers)
    assert status.status_code == 200
    data = status.get_json()["data"]
    assert data["ready"] is True
    assert data["mcpUrl"] == "http://192.168.0.75:8000/mcp"
    assert data["toolCount"] == 2

    # 未部署的主机 → ready False
    other = client.get("/api/computer-use/status?hostId=9999", headers=admin_headers)
    assert other.status_code == 404

    # MCP 调用：换成假的客户端，断言审计与返回形状
    class FakeClient:
        def call_tool(self, name, arguments=None, timeout=None):
            return {
                "content": [
                    {"type": "text", "text": f"done {name}"},
                    {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
                ]
            }

    monkeypatch.setattr("app.api.computeruse._client_for", lambda row: FakeClient())
    resp = client.post(
        "/api/computer-use/mcp/call",
        headers=admin_headers,
        json={"hostId": host_id, "tool": "Screenshot", "arguments": {}},
    )
    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()["data"]
    assert payload["tool"] == "Screenshot"
    assert payload["observeOnly"] is True
    assert payload["text"] == "done Screenshot"
    assert payload["images"][0]["dataUrl"].startswith("data:image/png;base64,")
    with app.app_context():
        entry = (
            AuditLog.query.filter_by(category="computeruse", action="mcp_call")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        assert "Screenshot" in entry.message


def test_mcp_call_rejects_unknown_tool_and_not_deployed(app, client, admin_headers, windows_host):
    host_id, account_id = windows_host
    not_deployed = client.post(
        "/api/computer-use/mcp/call",
        headers=admin_headers,
        json={"hostId": host_id, "tool": "Screenshot"},
    )
    assert not_deployed.status_code == 400
    assert not_deployed.get_json()["code"] == "NOT_DEPLOYED"

    with app.app_context():
        _ready_deployment(host_id, account_id)
    unknown = client.post(
        "/api/computer-use/mcp/call",
        headers=admin_headers,
        json={"hostId": host_id, "tool": "FormatC"},
    )
    assert unknown.status_code == 400
    assert unknown.get_json()["code"] == "UNKNOWN_TOOL"


def test_mcp_call_flat_arguments_shape(app, client, admin_headers, windows_host, monkeypatch):
    """AI 工具会把 dict 参数展开进请求体，所以平铺形状也要能用。"""
    host_id, account_id = windows_host
    with app.app_context():
        _ready_deployment(host_id, account_id)

    seen: dict = {}

    class FakeClient:
        def call_tool(self, name, arguments=None, timeout=None):
            seen["arguments"] = arguments
            return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("app.api.computeruse._client_for", lambda row: FakeClient())
    resp = client.post(
        "/api/computer-use/mcp/call",
        headers=admin_headers,
        json={"hostId": host_id, "tool": "Click", "x": 10, "y": 20, "loc": [10, 20]},
    )
    assert resp.status_code == 200, resp.get_json()
    assert seen["arguments"] == {"x": 10, "y": 20, "loc": [10, 20]}


def test_failed_probe_keeps_recorded_tools(app, client, admin_headers, windows_host, monkeypatch):
    """探活失败不能把已记录的工具清单擦成 0：侧边栏拿这份清单渲染工具列表。"""
    host_id, account_id = windows_host
    with app.app_context():
        _ready_deployment(host_id, account_id)

    class DownClient:
        def probe(self):
            return {"ok": False, "toolCount": 0, "tools": [], "error": "连不上目标机的 MCP 服务"}

    monkeypatch.setattr("app.api.computeruse._client_for", lambda row: DownClient())
    resp = client.post("/api/computer-use/mcp/probe", headers=admin_headers, json={"hostId": host_id})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["ok"] is False
    after = client.get(f"/api/computer-use/status?hostId={host_id}", headers=admin_headers).get_json()["data"]
    assert after["toolCount"] == 2, after
    assert len(after["tools"]) == 2, after
    with app.app_context():
        entry = (
            AuditLog.query.filter_by(category="computeruse", action="mcp_probe")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None and entry.result == "failure"

    class UpClient:
        def probe(self):
            return {
                "ok": True,
                "toolCount": 1,
                "tools": [{"name": "Screenshot", "description": "截图"}],
                "endpoint": "http://192.168.0.75:8000/mcp",
                "latencyMs": 12,
            }

    monkeypatch.setattr("app.api.computeruse._client_for", lambda row: UpClient())
    again = client.post("/api/computer-use/mcp/probe", headers=admin_headers, json={"hostId": host_id})
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["data"]["ok"] is True
    fresh = client.get(f"/api/computer-use/status?hostId={host_id}", headers=admin_headers).get_json()["data"]
    assert fresh["toolCount"] == 1, fresh
    assert [tool["name"] for tool in fresh["tools"]] == ["Screenshot"], fresh


def test_deployment_logs_replay_without_runner(app, client, admin_headers, windows_host):
    host_id, account_id = windows_host
    with app.app_context():
        row_id = _ready_deployment(host_id, account_id)
        row = db.session.get(ComputerUseDeployment, row_id)
        row.log_tail = "[info] prepare 环境体检通过"
        db.session.commit()
    resp = client.get(
        f"/api/computer-use/deployments/{row_id}/logs?since=0", headers=admin_headers
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["Content-Type"]
    body = resp.get_data(as_text=True)
    assert '"type": "hello"' in body
    assert "环境体检通过" in body
    assert '"type": "end"' in body


# --------------------------------------------------------------------------- #
# 5. AI 工具注册
# --------------------------------------------------------------------------- #
def test_computeruse_ai_tools_registered():
    names = set(ai_tools.TOOL_INDEX)
    for name in (
        "list_computeruse_hosts",
        "get_computeruse_status",
        "list_computeruse_tools",
        "observe_computer",
        "control_computer",
    ):
        assert name in names
    control = ai_tools.TOOL_INDEX["control_computer"]
    assert control.sensitive is True
    assert control.permission == ai_tools.EXEC
    assert control.category == "ComputerUse"
    observe = ai_tools.TOOL_INDEX["observe_computer"]
    assert observe.sensitive is False

    # 有 ai:tool_exec 的人才能看到这五个工具
    allowed = {
        tool.name for tool in ai_tools.tools_for_permission(["ai:tool_exec"], is_admin=False)
    }
    assert "control_computer" in allowed
    denied = {tool.name for tool in ai_tools.tools_for_permission(["ai:tool"], is_admin=False)}
    assert "control_computer" not in denied


def test_build_request_spreads_dict_arguments():
    tool = ai_tools.TOOL_INDEX["control_computer"]
    path, query, body = ai_tools.build_request(tool, {"hostId": 5, "tool": "Click", "arguments": {"x": 1}})
    assert path == "/api/computer-use/mcp/call"
    assert query == {}
    # dict 参数会展开进 body（接口侧同时兼容嵌套与平铺两种形状）
    assert body == {"hostId": 5, "tool": "Click", "x": 1}


# --------------------------------------------------------------------------- #
# 6. 僵尸 running 记录（真机：部署线程崩了/后端重启，记录永远停在 running，
#    同一台主机就再也发不出新部署）
# --------------------------------------------------------------------------- #
def test_stale_running_deployment_is_failed_and_replaced(
    app, client, admin_headers, windows_host, cache_dirs, monkeypatch
):
    host_id, account_id = windows_host
    _make_cached_installer(cache_dirs)
    with app.app_context():
        stale = ComputerUseDeployment(
            host_id=host_id,
            host_name="win-75",
            host_address="192.168.0.75",
            account_id=account_id,
            account_username="Administrator",
            status="running",
            stage="upload",
            progress=8,
            python_version="3.13.16",
            installer_name=INSTALLER_NAME,
        )
        db.session.add(stale)
        db.session.commit()
        stale_id = stale.id

    monkeypatch.setattr(cu_deploy, "start_deployment", lambda *a, **k: None)
    resp = client.post(
        "/api/computer-use/deployments",
        headers=admin_headers,
        json={
            "hostId": host_id,
            "accountId": account_id,
            "pythonVersion": "3.13.16",
            "installerName": INSTALLER_NAME,
        },
    )
    assert resp.status_code == 200, resp.get_json()
    new_id = resp.get_json()["data"]["id"]
    assert new_id != stale_id
    with app.app_context():
        old = db.session.get(ComputerUseDeployment, stale_id)
        assert old.status == "failed"
        assert "中断" in (old.last_error or "")
        assert old.finished_at is not None
        assert (
            AuditLog.query.filter_by(category="computeruse", action="deploy_stale").count() == 1
        )


def test_live_running_deployment_still_blocks_new_one(
    app, client, admin_headers, windows_host, cache_dirs, monkeypatch
):
    """有活着的线程时不能抢：老记录保持 running（跑着的那次不许被顶掉）。"""
    host_id, account_id = windows_host
    _make_cached_installer(cache_dirs)

    class FakeRunner:
        pass

    with app.app_context():
        live = ComputerUseDeployment(
            host_id=host_id,
            host_name="win-75",
            host_address="192.168.0.75",
            account_id=account_id,
            account_username="Administrator",
            status="running",
            stage="upload",
            progress=8,
        )
        db.session.add(live)
        db.session.commit()
        live_id = live.id

    monkeypatch.setattr(cu_deploy, "get_runner", lambda deployment_id: FakeRunner())
    resp = client.post(
        "/api/computer-use/deployments",
        headers=admin_headers,
        json={
            "hostId": host_id,
            "accountId": account_id,
            "pythonVersion": "3.13.16",
            "installerName": INSTALLER_NAME,
        },
    )
    assert resp.status_code == 409
    assert "进行中" in resp.get_json()["message"]
    with app.app_context():
        assert db.session.get(ComputerUseDeployment, live_id).status == "running"

