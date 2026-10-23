"""扩展模块运行时的端到端测试。

覆盖契约里最容易回归的路径：安装校验/Zip Slip、实验性确认、设置项接入、
模块路由分发、启停与卸载、以及 async SDK 的凭据解密与审计。
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AuditLog, SystemSetting
from app.modules import init_modules, registry, spec
from tests.conftest import auth, token_of

MODULE_ID = "hello-mod"
MODULE_CODE = """
from autoops import ModuleApiError, bastion


def setup(ctx):
    @ctx.route("/ping", methods=("GET",))
    def ping():
        return {
            "pong": True,
            "text": ctx.get_setting("text", "?"),
            "now": bastion.util.now().isoformat(),
        }

    @ctx.route("/boom", methods=("GET",))
    def boom():
        raise ModuleApiError("模块主动报错", 418, "TEAPOT")

    @ctx.route("/", methods=("GET",))
    def root():
        return {"root": True}

    # 契约要求 ctx.blueprint 也是可用的；这条路由只能通过 Blueprint 捕获生效。
    bp = ctx.blueprint()
    @bp.get("/blueprint/ping")
    def blueprint_ping():
        return {"via": "blueprint"}

    # 同时覆盖 ctx.on 与顶层 on_enable 两套钩子写法。
    ctx.on("install", lambda c: (c.data_dir / "install.txt").write_text("1", encoding="utf-8"))
    ctx.on("enable", lambda c: (c.data_dir / "ctx-enabled.txt").write_text("1", encoding="utf-8"))


def on_enable(ctx):
    (ctx.data_dir / "enabled.txt").write_text("1", encoding="utf-8")
"""


def _description(**overrides):
    data = {
        "id": MODULE_ID,
        "name": {"zh": "测试模块", "en": "Hello Module"},
        "version": "1.0.0",
        "author": "Tester",
        "description": {"zh": "用于测试", "en": "For tests"},
        "tags": ["monitor"],
        "entry": {"web": "web/index.html"},
        "nav": [
            {
                "key": "main",
                "title": {"zh": "测试页", "en": "Test Page"},
                "path": "/",
                "icon": "AppstoreOutlined",
                "type": "iframe",
                "permission": f"module:{MODULE_ID}:use",
            }
        ],
        "settings": [
            {"key": "greeting", "label": "问候", "type": "bool", "default": "1"},
            {
                "key": "count",
                "label": "次数",
                "type": "int",
                "default": "5",
                "min": 1,
                "max": 10,
            },
            {"key": "text", "label": "文本", "type": "string", "default": "hi"},
        ],
        "permissions": [f"module:{MODULE_ID}:use"],
        "requires": [],
    }
    data.update(overrides)
    return data


def _zip_bytes(description: dict | None = None, files: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if description is not None:
            archive.writestr(
                "description.json", json.dumps(description, ensure_ascii=False)
            )
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _package(description: dict | None = None, files: dict[str, str] | None = None) -> bytes:
    payload = {"modules/__init__.py": MODULE_CODE, "web/index.html": "<h1>hi</h1>"}
    payload.update(files or {})
    return _zip_bytes(description or _description(), payload)


def _install(client, headers, payload: bytes, filename="hello-mod-1.0.0.mod.ops_ext", ack=""):
    data = {"file": (io.BytesIO(payload), filename)}
    if ack:
        data["ack"] = ack
    return client.post(
        "/api/modules/install",
        data=data,
        headers=headers,
        content_type="multipart/form-data",
    )


@pytest.fixture()
def mod_app(app, tmp_path):
    """把模块根目录指到本用例的 tmp_path，并重新扫描。"""
    app.config["MODULES_DIR"] = str(tmp_path / "modules")
    app.config["MODULES_SRC_DIR"] = ""
    app.config["APP_VERSION"] = "1.0.0"
    with app.app_context():
        init_modules(app)
    return app


@pytest.fixture()
def mod_client(mod_app):
    return mod_app.test_client()


@pytest.fixture()
def mod_admin_headers(mod_client):
    return auth(token_of(mod_client))


def _data(response):
    body = response.get_json() or {}
    return body.get("data")


def test_install_ok_and_filesystem_layout(mod_client, mod_admin_headers, mod_app):
    response = _install(mod_client, mod_admin_headers, _package())
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = _data(response)
    assert payload["id"] == MODULE_ID
    assert payload["loaded"] is True
    assert payload["enabled"] is True
    assert payload["errors"] == []
    assert payload["official"] is False

    module_dir = Path(mod_app.config["MODULES_DIR"]) / MODULE_ID
    assert (module_dir / "package" / "description.json").is_file()
    assert (module_dir / "package" / "modules" / "__init__.py").is_file()
    assert (module_dir / "data").is_dir()
    state = json.loads((module_dir / "state.json").read_text(encoding="utf-8"))
    assert state["enabled"] is True
    assert state["version"] == "1.0.0"
    # install 钩子必须在安装成功后执行。
    assert (module_dir / "data" / "install.txt").is_file()

    blueprint_route = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/blueprint/ping", headers=mod_admin_headers
    )
    assert blueprint_route.status_code == 200, blueprint_route.get_data(as_text=True)
    assert _data(blueprint_route) == {"via": "blueprint"}

    listed = mod_client.get("/api/modules", headers=mod_admin_headers)
    assert listed.status_code == 200
    items = _data(listed)
    assert [item["id"] for item in items] == [MODULE_ID]
    assert items[0]["nav"][0]["path"] == f"/m/{MODULE_ID}/"
    assert items[0]["nav"][0]["entry"] == "web/index.html"
    assert items[0]["settings"][0]["key"].startswith(f"mod.{MODULE_ID}.")
    assert items[0]["images"] == []


@pytest.mark.parametrize("payload", [
    _zip_bytes(files={"modules/__init__.py": MODULE_CODE}),
    _zip_bytes(_description(id="Bad_ID"), {"modules/__init__.py": MODULE_CODE}),
    _zip_bytes(_description(version="1.0"), {"modules/__init__.py": MODULE_CODE}),
    _zip_bytes(_description(), {"modules/__init__.py": MODULE_CODE, "../evil.txt": "x"}),
])
def test_install_invalid_packages_leave_no_residue(
    mod_client, mod_admin_headers, mod_app, payload
):
    response = _install(mod_client, mod_admin_headers, payload)
    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert body["success"] is False
    assert body["code"] == "MODULE_INVALID_PACKAGE"
    root = Path(mod_app.config["MODULES_DIR"])
    assert not (root / MODULE_ID).exists()
    assert not (root.parent / "evil.txt").exists()


def test_install_rollback_when_registration_fails(
    mod_client, mod_admin_headers, mod_app, monkeypatch
):
    """目录已原子改名、但登记失败时必须回滚，不能留下不可管理的半成品。"""
    from app.modules import registry as registry_singleton

    def boom(*args, **kwargs):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(registry_singleton, "add_installed", boom)
    response = _install(mod_client, mod_admin_headers, _package())
    assert response.status_code == 500
    assert response.get_json()["code"] == "MODULE_INSTALL_FAILED"
    root = Path(mod_app.config["MODULES_DIR"])
    assert not (root / MODULE_ID).exists()
    keys = {
        item["key"]
        for item in _data(
            mod_client.get("/api/settings", headers=mod_admin_headers)
        )["items"]
    }
    assert not any(key.startswith(f"mod.{MODULE_ID}.") for key in keys)


def test_install_reserved_id_and_existing_module(mod_client, mod_admin_headers, mod_app):
    reserved = _package(_description(id="admin"))
    response = _install(mod_client, mod_admin_headers, reserved)
    assert response.status_code == 400
    assert response.get_json()["code"] == "MODULE_RESERVED_ID"

    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200
    again = _install(mod_client, mod_admin_headers, _package())
    assert again.status_code == 409
    assert again.get_json()["code"] == "MODULE_EXISTS"


def test_experimental_requires_exact_ack(mod_client, mod_admin_headers):
    experimental = _package(_description(experimental=True))
    missing = _install(mod_client, mod_admin_headers, experimental)
    assert missing.status_code == 400
    assert missing.get_json()["code"] == "EXPERIMENTAL_NOT_ACKED"

    wrong = _install(
        mod_client, mod_admin_headers, experimental, ack="我知道这是实验性功能"
    )
    assert wrong.status_code == 400
    assert wrong.get_json()["code"] == "EXPERIMENTAL_NOT_ACKED"

    ok = _install(
        mod_client,
        mod_admin_headers,
        experimental,
        ack="  我知道这是实验性功能，使用不当的话也许会造成系统损毁  ",
    )
    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert _data(ok)["experimental"] is True
    assert "experimental" in _data(ok)["tags"]


def test_nav_filtered_by_permission(mod_client, mod_admin_headers, make_user):
    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200
    admin_nav = mod_client.get("/api/modules/nav", headers=mod_admin_headers)
    assert admin_nav.status_code == 200
    nav = _data(admin_nav)
    assert len(nav) == 1
    assert nav[0]["moduleId"] == MODULE_ID
    assert nav[0]["path"] == f"/m/{MODULE_ID}/"
    assert nav[0]["entry"] == "web/index.html"
    assert nav[0]["loaded"] is True

    make_user("module-viewer", "viewer")
    viewer_token = token_of(mod_client, "module-viewer", "User1234")
    viewer_nav = mod_client.get("/api/modules/nav", headers=auth(viewer_token))
    assert viewer_nav.status_code == 200
    assert _data(viewer_nav) == []


def test_module_permission_assignable_to_role(
    mod_client, mod_admin_headers, make_user
):
    """模块权限不在 ALL_PERMISSIONS 里，但管理员仍要能把它授给自定义角色。"""
    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200
    permission = f"module:{MODULE_ID}:use"

    catalog = mod_client.get("/api/roles/permissions", headers=mod_admin_headers)
    assert catalog.status_code == 200
    codes = [
        item["code"]
        for group in _data(catalog)
        for item in group.get("items", [])
    ]
    assert permission in codes

    created = mod_client.post(
        "/api/roles",
        json={"code": "module-role", "name": "模块角色", "permissions": [permission]},
        headers=mod_admin_headers,
    )
    assert created.status_code == 200, created.get_data(as_text=True)
    assert _data(created)["permissions"] == [permission]

    make_user("module-user", role_code="module-role")
    token = token_of(mod_client, "module-user", "User1234")
    headers = auth(token)
    nav = mod_client.get("/api/modules/nav", headers=headers)
    assert nav.status_code == 200
    assert [item["moduleId"] for item in _data(nav)] == [MODULE_ID]
    ping = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/ping", headers=headers
    )
    assert ping.status_code == 200, ping.get_data(as_text=True)


def test_module_settings_visible_and_writable(mod_client, mod_admin_headers):
    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200
    settings = mod_client.get("/api/settings", headers=mod_admin_headers)
    assert settings.status_code == 200
    items = {item["key"]: item for item in _data(settings)["items"]}
    key = f"mod.{MODULE_ID}.greeting"
    assert key in items
    assert items[key]["scope"] == "module"
    assert items[key]["moduleId"] == MODULE_ID
    assert items[key]["type"] == "bool"
    assert items[key]["value"] == "1"

    updated = mod_client.put(
        "/api/settings",
        json={"values": {key: False, f"mod.{MODULE_ID}.count": 8}},
        headers=mod_admin_headers,
    )
    assert updated.status_code == 200, updated.get_data(as_text=True)
    items = {
        item["key"]: item
        for item in _data(mod_client.get("/api/settings", headers=mod_admin_headers))[
            "items"
        ]
    }
    assert items[key]["value"] == "0"


def test_module_python_route_envelope_and_errors(mod_client, mod_admin_headers):
    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200

    response = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/ping", headers=mod_admin_headers
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["success"] is True
    assert body["data"]["pong"] is True
    assert body["data"]["text"] == "hi"
    assert body["data"]["now"].endswith("+00:00")

    root = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/", headers=mod_admin_headers
    )
    assert root.status_code == 200, root.get_data(as_text=True)
    assert _data(root) == {"root": True}

    error = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/boom", headers=mod_admin_headers
    )
    assert error.status_code == 418
    assert error.get_json()["code"] == "TEAPOT"

    missing = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/nope", headers=mod_admin_headers
    )
    assert missing.status_code == 404


def test_disable_enable_and_uninstall(mod_client, mod_admin_headers, mod_app):
    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200
    module_dir = Path(mod_app.config["MODULES_DIR"]) / MODULE_ID
    assert (module_dir / "data").is_dir()

    disabled = mod_client.post(
        f"/api/modules/{MODULE_ID}/disable", headers=mod_admin_headers
    )
    assert disabled.status_code == 200
    assert _data(disabled)["loaded"] is False
    assert _data(disabled)["enabled"] is False
    assert (
        mod_client.get(
            f"/api/modules/{MODULE_ID}/api/ping", headers=mod_admin_headers
        ).status_code
        == 404
    )
    state = json.loads((module_dir / "state.json").read_text(encoding="utf-8"))
    assert state["enabled"] is False

    enabled = mod_client.post(
        f"/api/modules/{MODULE_ID}/enable", headers=mod_admin_headers
    )
    assert enabled.status_code == 200, enabled.get_data(as_text=True)
    assert _data(enabled)["loaded"] is True
    assert (module_dir / "data" / "enabled.txt").read_text(encoding="utf-8") == "1"
    assert (module_dir / "data" / "ctx-enabled.txt").read_text(encoding="utf-8") == "1"
    assert (
        mod_client.get(
            f"/api/modules/{MODULE_ID}/api/ping", headers=mod_admin_headers
        ).status_code
        == 200
    )

    removed = mod_client.delete(
        f"/api/modules/{MODULE_ID}", headers=mod_admin_headers
    )
    assert removed.status_code == 200, removed.get_data(as_text=True)
    assert not module_dir.exists()
    keys = {
        item["key"]
        for item in _data(
            mod_client.get("/api/settings", headers=mod_admin_headers)
        )["items"]
    }
    assert not any(key.startswith(f"mod.{MODULE_ID}.") for key in keys)
    with mod_app.app_context():
        assert (
            SystemSetting.query.filter(
                SystemSetting.key.like(f"mod.{MODULE_ID}.%")
            ).count()
            == 0
        )


def test_async_credentials_get_decrypts_and_audits(
    mod_app, mod_client, mod_admin_headers, make_host, make_account
):
    from app.modules import sdk

    assert _install(mod_client, mod_admin_headers, _package()).status_code == 200
    host_id = make_host("cred-host", "10.0.0.9", 22)
    account_id = make_account(host_id, "root", "root", "s3cret!")

    with mod_app.app_context():
        before = AuditLog.query.filter_by(action="module_credential").count()
        with sdk.module_scope(MODULE_ID):
            credentials = asyncio.run(
                sdk.credentials.get(host_id, account_id=account_id)
            )
        after = AuditLog.query.filter_by(action="module_credential").count()

    assert credentials.host_id == host_id
    assert credentials.account_id == account_id
    assert credentials.username == "root"
    assert credentials.password == "s3cret!"
    assert "s3cret!" not in repr(credentials)
    assert after == before + 1
    with mod_app.app_context():
        entry = (
            AuditLog.query.filter_by(action="module_credential")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry.detail["moduleId"] == MODULE_ID
        assert entry.detail["hasPassword"] is True
        assert "s3cret!" not in json.dumps(entry.detail, ensure_ascii=False)


def test_static_asset_only_exposes_web_images_readme(mod_client, mod_admin_headers):
    payload = _package(
        files={
            "web/index.html": "<h1>页面</h1>",
            "images/logo.png": "not-a-real-png",
            "README.md": "# hello",
            "secret.txt": "nope",
        }
    )
    assert _install(mod_client, mod_admin_headers, payload).status_code == 200

    assert (
        mod_client.get(f"/api/modules/{MODULE_ID}/asset/web/index.html").status_code
        == 200
    )
    assert (
        mod_client.get(f"/api/modules/{MODULE_ID}/asset/README.md").status_code
        == 200
    )
    assert (
        mod_client.get(f"/api/modules/{MODULE_ID}/asset/secret.txt").status_code
        == 404
    )
    assert (
        mod_client.get(f"/api/modules/{MODULE_ID}/asset/../description.json").status_code
        == 404
    )


def test_upload_check_does_not_install(mod_client, mod_admin_headers, mod_app):
    response = mod_client.post(
        "/api/modules/upload-check",
        data={"file": (io.BytesIO(_package()), "hello.mod.ops_ext")},
        headers=mod_admin_headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = _data(response)
    assert payload["id"] == MODULE_ID
    assert payload["installable"] is True
    assert payload["images"] == []
    assert not (Path(mod_app.config["MODULES_DIR"]) / MODULE_ID).exists()


def test_upload_check_marks_experimental_as_installable(
    mod_client, mod_admin_headers
):
    """实验性只要求逐字确认，不代表装不进去：installable 必须仍为 True。"""
    response = mod_client.post(
        "/api/modules/upload-check",
        data={
            "file": (
                io.BytesIO(_package(_description(experimental=True))),
                "hello.mod.ops_ext",
            )
        },
        headers=mod_admin_headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = _data(response)
    assert payload["installable"] is True
    assert payload["warnings"]
    assert any("逐字确认" in item for item in payload["warnings"])
    assert payload["issues"] == payload["warnings"]


def test_upload_check_blocks_already_installed(mod_client, mod_admin_headers):
    """已安装才是真正的阻塞项，installable=False 且 warnings 为空。"""
    payload = _package()
    assert (
        _install(mod_client, mod_admin_headers, payload).status_code == 200
    )
    response = mod_client.post(
        "/api/modules/upload-check",
        data={"file": (io.BytesIO(payload), "hello.mod.ops_ext")},
        headers=mod_admin_headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    data = _data(response)
    assert data["installable"] is False
    assert data["warnings"] == []
    assert any("已安装" in item for item in data["issues"])


def test_pure_frontend_module_is_available(mod_client, mod_admin_headers):
    """纯前端模块没有 Python 入口，但对外必须是可用状态，否则宿主页会误判。"""
    payload = _zip_bytes(_description(), {"web/index.html": "<h1>ui</h1>"})
    response = _install(mod_client, mod_admin_headers, payload)
    assert response.status_code == 200, response.get_data(as_text=True)
    data = _data(response)
    assert data["loaded"] is True
    assert data["enabled"] is True
    assert data["errors"] == []

    nav = mod_client.get("/api/modules/nav", headers=mod_admin_headers)
    assert nav.status_code == 200
    assert _data(nav)[0]["loaded"] is True

    # 纯前端模块没有 Python API，分发器仍应明确返回不存在。
    api = mod_client.get(
        f"/api/modules/{MODULE_ID}/api/ping", headers=mod_admin_headers
    )
    assert api.status_code == 404


def test_builtin_package_is_installed_on_startup(mod_app, tmp_path):
    """契约 §3：modules-src/<id>/*.mod.ops_ext 启动时复制进模块根目录。"""
    source = tmp_path / "builtin-src" / MODULE_ID
    source.mkdir(parents=True)
    (source / f"{MODULE_ID}-1.0.0.mod.ops_ext").write_bytes(_package())
    mod_app.config["MODULES_SRC_DIR"] = str(source.parent)

    with mod_app.app_context():
        init_modules(mod_app)
        record = registry.get(MODULE_ID)

    assert record is not None
    assert record.builtin is True
    assert record.loaded is not None
    assert (Path(mod_app.config["MODULES_DIR"]) / MODULE_ID / "state.json").is_file()


def test_enable_load_failure_keeps_module_disabled(
    mod_client, mod_admin_headers, mod_app
):
    broken = _package(
        files={"modules/__init__.py": "def setup(ctx):\n    raise RuntimeError('boom')\n"}
    )
    response = _install(mod_client, mod_admin_headers, broken)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert _data(response)["loaded"] is False

    assert (
        mod_client.post(
            f"/api/modules/{MODULE_ID}/disable", headers=mod_admin_headers
        ).status_code
        == 200
    )
    response = mod_client.post(
        f"/api/modules/{MODULE_ID}/enable", headers=mod_admin_headers
    )
    assert response.status_code == 400, response.get_data(as_text=True)
    assert (response.get_json() or {}).get("code") == "MODULE_LOAD_FAILED"

    detail = _data(
        mod_client.get(f"/api/modules/{MODULE_ID}", headers=mod_admin_headers)
    )
    assert detail["enabled"] is False
    assert detail["loaded"] is False


def test_pack_tool_produces_installable_archive(tmp_path):
    from tools import pack_module

    source = tmp_path / "src" / MODULE_ID
    (source / "modules").mkdir(parents=True)
    (source / "web").mkdir()
    (source / "modules" / "__init__.py").write_text(MODULE_CODE, encoding="utf-8")
    (source / "web" / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    (source / "description.json").write_text(
        json.dumps(_description(), ensure_ascii=False), encoding="utf-8"
    )
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")

    output_dir = tmp_path / "dist"
    assert pack_module.main([str(source), "-o", str(output_dir)]) == 0
    output = output_dir / f"{MODULE_ID}-1.0.0.mod.ops_ext"
    assert output.is_file()
    archive = spec.parse_archive(output)
    assert not any("__pycache__" in entry.name for entry in archive.entries)
    assert spec.validate(output)["id"] == MODULE_ID

    assert pack_module.main([str(source), "--check"]) == 0
