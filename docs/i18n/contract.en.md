# AutoOps Extension Module System — Internal Implementation Contract v1

> This document is the **implementation contract** (jointly observed by the backend / frontend / packaging tool / official modules).
> Documentation for third-party developers is in `docs/modules/WHITEPAPER.md` (derived from this contract).

> 中文版 / Chinese: [../modules/CONTRACT.md](../modules/CONTRACT.md)

## 0. Design principles

1. **The filesystem is the truth**: modules are installed in `bastion-backend/instance/modules/<id>/`, and no new database table is added.
Inside the directory, `state.json` records the enabled state and the installation time, and `package/` is the extracted package.
2. **Declarative first**: navigation, settings and permissions are all written in `description.json`; Python only
registers runtime routes / lifecycle hooks. That way the install page can preview **without executing any module code**.
3. **Modules are trusted code**: Python extensions run inside the bastion process, are equivalent to plugins, and inherently have full access to
internal data (this is a requirement, not a vulnerability). No process-isolation sandbox is provided; the whitepaper must state this clearly.
4. **Static assets need no login, module APIs must be authenticated**: `/api/modules/<id>/asset/**` returns files directly;
the permissions of the module's own `/api/modules/<id>/api/**` are declared by the module, defaulting to `module:<id>:use`.
5. **Failures must be loud**: a failed install must roll back to "not installed" and give a readable reason; a module that fails to load must be
`loaded=false` + `errors[]`, and must not take the whole backend down with it.

## 1. Package format `.mod.ops_ext`

ZIP (deflate). The file name is `<id>-<version>.mod.ops_ext`, but **at install time the id/version in description.json take precedence**;
the file name does not take part in validation.

```
description.json        required, package root
modules/                optional, Python extension root (if present it must contain __init__.py, and it must be able to setup(ctx))
  __init__.py
images/                 optional, demo images
web/                    optional, static frontend assets (any HTML/JS/CSS)
data/                   optional, initial data, copied to instance/modules/<id>/data on install
README.md               optional
bin/                    optional, executables (such as the RDP gateway), in-package path instance/modules/<id>/package/bin/
```

Security constraints (hard checks at install time):

* Reject absolute paths, `..`, backslashes, drive letters, NUL, and symlinks/device files (ZIP entries may only be regular files and directories).
* Total extracted size per package ≤ 256 MB, entry count ≤ 20000, `description.json` ≤ 256 KB.
* Illegal characters in a file name → fail immediately.
* **Never import module code during install**. Loading happens after a successful install, inside a separate try/except.

## 2. `description.json`

```jsonc
{
  "id": "rdp-gateway",                 // required ^[a-z][a-z0-9-]{2,39}$, and must not be a reserved word
  "name": {"zh": "RDP 网关", "en": "RDP Gateway"},   // required; a plain string is also accepted
  "version": "1.0.0",                  // required, strict semver (MAJOR.MINOR.PATCH[-pre][+build])
  "author": "AutoOps",                 // required; author == "AutoOps" (case/whitespace-insensitive) = official
  "description": {"zh": "...", "en": "..."},        // optional, empty by default
  "tags": ["experimental", "rdp"],     // optional, string array; experimental triggers the beaker icon
  "experimental": true,                // optional, equivalent to having experimental in tags (the two are unioned)
  "icon": "AppstoreOutlined",          // optional, sidebar icon (antd icon name)
  "homepage": "",                      // optional
  "license": "MIT",                    // optional
  "minBastionVersion": "1.0.0",        // optional, compared against the backend config APP_VERSION
  "images": ["images/demo-1.png"],     // optional, in-package relative path, shown on the install page
  "entry": {"web": "web/index.html"},  // optional, defaults to web/index.html
  "nav": [                             // optional, sidebar entry
    {
      "key": "console",                // required, unique within the module
      "title": {"zh": "RDP 网关", "en": "RDP Gateway"},
      "path": "/",                     // optional, defaults to "/"; relative path, final URL = /m/<id><path>
      "entry": "web/index.html",       // optional, defaults to entry.web
      "type": "iframe",                // "iframe" (default) | "js"
      "icon": "DesktopOutlined",       // optional
      "permission": "module:rdp-gateway:use"  // optional, defaults to module:<id>:use
    }
  ],
  "settings": [                        // optional, automatically appears on the "Parameters" page
    {
      "key": "enabled",                // required ^[a-zA-Z][a-zA-Z0-9_]{0,39}$
      "label": {"zh": "启用 RDP 网关"},
      "type": "bool",                  // bool | int | string
      "default": "0",
      "min": 0, "max": 65535,          // optional for int
      "description": {"zh": "..."}     // optional
    }
  ],
  "permissions": ["module:rdp-gateway:use"],  // optional, declared for use by the role configuration page
  "requires": ["some-module"]          // optional, depends on other module ids
}
```

`nav[].type` still allows `iframe` (the field comments above are a syntax example), but **the official example modules all use `js` mode now**; the normalized object below uses `rdp-gateway` as an example.

Normalized module object (the output of the backend's `spec.normalize_description()`, and also the element of `/api/modules`):

```jsonc
{
  "id": "rdp-gateway",
  "name": {"zh": "RDP 网关", "en": "RDP Gateway"},   // always normalized into a bilingual object
  "version": "1.0.0",
  "author": "AutoOps",
  "official": true,                    // derived from author
  "description": {"zh": "", "en": ""},
  "tags": ["experimental", "rdp"],
  "experimental": true,
  "icon": "AppstoreOutlined",
  "license": "", "homepage": "", "minBastionVersion": "",
  "images": [{"path": "images/demo-1.png", "url": "/api/modules/rdp-gateway/asset/images/demo-1.png"}],
  "nav": [{"key": "console", "title": {...}, "path": "/m/rdp-gateway/", "icon": "...", "type": "js", "permission": "module:rdp-gateway:use"}],
  "settings": [{"key": "mod.rdp-gateway.enabled", "rawKey": "enabled", "label": {...}, "type": "bool", "default": "0", "min": null, "max": null, "group": "RDP 网关"}],
  "permissions": ["module:rdp-gateway:use"],
  "requires": [],
  "reserved": false,
  "enabled": true,
  "loaded": true,
  "installedAt": "2026-10-23T15:04:05Z",
  "updatedAt": "...",
  "size": 14745600,
  "errors": []
}
```

Reserved ids (third parties may not take them): `core`, `system`, `admin`, `modules`.

Tag colors are fixed mappings on the frontend (the backend does not send colors, only tag identifiers):

|tag|color|icon|
|-|-|-|
|`experimental`|`magenta` + `ExperimentOutlined` (beaker)|explicit confirmation required before install|
|`official`|`gold`|added automatically by the `official` field, no need to write it into tags|
|`security`|`red`||
|`network` / `rdp` / `ssh`|`blue`||
|`monitor` / `audit`|`cyan`||
|`storage` / `file`|`green`||
|`ai`|`purple`||
|others|`default` (gray)||

The text must be **verbatim** equal to:

```
我知道这是实验性功能，使用不当的话也许会造成系统损毁
```

The frontend prompts the user to type it manually into the input box (paste is not accepted; the comparison is against the complete string after trimming leading and trailing whitespace),
and the backend validates the `ack` field a second time (exact match after trimming), returning 400 `EXPERIMENTAL_NOT_ACKED` on mismatch.

## 3. Backend runtime

New package `bastion-backend/app/modules/`:

|File|Responsibility|
|-|-|
|`spec.py`|constants (reserved ids, allowed suffixes, size limits), `parse_archive()`, `normalize_description()`, `validate()`|
|`store.py`|persistence: `instance/modules/<id>/{package,state.json,data}`; `install/remove/list/read_state/write_state`; atomic install (temp → rename)|
|`registry.py`|in-memory registry: installed modules, loaded modules, nav items, module setting items, Loader handles|
|`loader.py`|`sys.modules['autoops']` injection + `import` of the module package + `setup(ctx)` + unload|
|`context.py`|`ModuleContext` (see §4)|
|`sdk.py`|the `autoops.bastion` facade (see §5), all async|
|`api.py` or `app/api/modules.py`|REST API (see §6)|

### No built-in modules

The host **preinstalls and auto-installs no module at all**: a freshly installed instance is in the zero-module state, and every extension capability
(including the official examples `hello-bastion`, `js-demo`, `rdp-gateway`) must be uploaded by an administrator as
`*.mod.ops_ext` and installed, going through exactly the same code path as third-party extensions.

* `init_modules(app)` in `create_app` does exactly one thing: it scans the module directories that **already exist** under `instance/modules/`
  and loads them; it never looks for packages in any source directory, nor installs missing modules.
* The official modules' source lives in the `modules-src/` directory of the repository, the packaging command is in §8; the artifacts go to the repository root and are distributed with the repository.
* Uninstalling is a real uninstall (directory deleted, settings unregistered, in-process routes removed); reinstalling = uploading the same package again.
* `modules-src/` is only used **during development/packaging**; it is never read at runtime.

Startup order (in `create_app`, it must be called after `seed_data` and before the `socketio` event registration):

```python
from .modules import init_modules
init_modules(app)   # internally: scan → register setting defaults → load enabled modules
```

`init_modules` must **never raise** (errors from any module are contained in that module's `errors[]`).

### Settings integration

* Module settings are persisted to `SystemSetting`, key = `mod.<moduleId>.<key>`.
* `settings_store` adds:

```python
  def register_module_defaults(module_id: str, items: list[dict]) -> None
  def unregister_module_defaults(module_id: str) -> None
  def all_defaults() -> dict[str, str]
  def default_meta(key: str) -> dict | None      # {label, type, min, max, group}
  ```

* `update_settings()` / `ensure_defaults()` / `all_settings()` all use `all_defaults()` instead.
* Items from `GET /api/settings` gain an optional `group` field (module display name, Chinese preferred).
`PUT /api/settings` accepts `mod.*` keys, and type validation uses the type/min/max declared by the module.
* The old `DEFAULTS` constant is kept (core settings); `all_defaults()` = `DEFAULTS` + module defaults.

## 4. Python module entry

```python
# instance/modules/hello/package/modules/__init__.py
from autoops import bastion

def setup(ctx):
    """Called once when the module is loaded; raising an exception = load failure."""
    ctx.logger.info("hello from %s", ctx.id)

def teardown(ctx):
    """Called on uninstall / disable, optional."""

def on_enable(ctx): ...
def on_disable(ctx): ...
```

`ModuleContext` (`ctx`) fields and methods:

|Member|Description|
|-|-|
|`ctx.id` / `ctx.version` / `ctx.author` / `ctx.official`|read-only module metadata|
|`ctx.app`|Flask `app` (use with care)|
|`ctx.package_dir`|`Path`, package root directory|
|`ctx.data_dir`|`Path`, **module-private writable directory** (`instance/modules/<id>/data`, created at install)|
|`ctx.logger`|`logging.Logger("module.<id>")`|
|`ctx.get_setting(key, default=None)`|read a module setting (auto-adds the `mod.<id>.` prefix)|
|`ctx.set_setting(key, value)`|write a module setting (persist + commit)|
|`ctx.settings`|`dict[str, str]` snapshot (read-only semantics)|
|`ctx.route(path, methods=("GET",), permission=None)`|decorator that mounts the function at `/api/modules/<id>/api<path>`; returns the unified envelope|
|`ctx.blueprint(name="api")`|returns `Blueprint(url_prefix=f"/api/modules/{id}/api")` for mounting routes freely|
|`ctx.on(event)`|`event ∈ {"install","uninstall","enable","disable"}`, registers a hook|
|`ctx.emit(topic, payload)`|Socket.IO broadcast `module:<id>:<topic>`|

`ctx.route` wrapping convention: a function returning `dict/list/None` → `api_ok(data)`; raising `ModuleApiError(message, status)` →
`api_error`. `permission` defaults to the module's first nav permission, then to `module:<id>:use`.

## 5. SDK facade `autoops.bastion` (all async)

Module code uses it after `from autoops import bastion`. **All methods that touch the database/network are
`async def`**; synchronous versions with a `_sync` suffix are also provided for simple scripts.

```python
# Credentials (per requirement: must be async)
await bastion.credentials.get(host_id, account_id=None) -> Credentials
    # Credentials(host_id, account_id, host_name, address, protocol, port,
    #             username, password, private_key, passphrase)
    # password is already decrypted with Fernet; the plaintext exists only in the return value, never in logs
await bastion.credentials.list(host_id=None) -> list[CredentialMeta]   # no keys

# Assets
await bastion.hosts.list(protocols=None, keyword=None) -> list[dict]
await bastion.hosts.get(host_id) -> dict

# Accounts
await bastion.accounts.list(host_id=None) -> list[dict]
await bastion.accounts.reveal_password(account_id) -> str     # decrypt + write audit
await bastion.accounts.reveal_private_key(account_id) -> str

# Sessions / audit
await bastion.sessions.list_active() -> list[dict]
await bastion.sessions.open(host_id, account_id, protocol="ssh", source="module:<id>") -> dict
await bastion.audit.log(action, target_type=None, target_id=None, message="", detail=None) -> None
await bastion.audit.log_command(session_id, command, output=None, allowed=True, reason="") -> None

# Settings / users / policy / network
await bastion.settings.get(key) -> str
await bastion.settings.set(key, value) -> None
await bastion.users.list() -> list[dict]
await bastion.policy.check(account_id, command) -> dict   # {allowed, matched_rule, action}
await bastion.ssh.run(host_id, command, account_id=None, timeout=30) -> dict
    # {code, stdout, stderr, elapsed}; forced through the command policy; raises bastion.ModuleError on failure
await bastion.events.emit(topic, payload) -> None          # Socket.IO broadcast

# Misc
bastion.util.now() -> datetime          # aware UTC
await bastion.util.run_blocking(fn, *a, **kw) -> Any      # run blocking code in a thread with app context
```

Implementation notes:

* Async: `asyncio.get_running_loop().run_in_executor(_EXECUTOR, ...)`, where `_EXECUTOR` is
`ThreadPoolExecutor(max_workers=8, thread_name_prefix="module-io")`; inside the thread `with app.app_context():`.
* No synchronous DB calls inside any `async def` (they would stall the event loop).
* `credentials.get` writes one `audit_logs(action="module_credential", ...)` record, whose `detail` contains no plaintext.

## 6. REST API (host side)

Blueprint `modules_bp` (`app/api/modules.py`, which must be registered in `app/api/__init__.py`):

|Method|Path|Permission|Description|
|-|-|-|-|
|GET|`/api/modules`|`setting:view`|list (including `loaded`/`errors`), sorted by id|
|GET|`/api/modules/nav`|`login_required`|nav items visible to the current account (filtered by nav permission), for the frontend to build the sidebar|
|GET|`/api/modules/<id>`|`setting:view`|details (the same normalized object)|
|POST|`/api/modules/install`|`admin_required`|multipart: `file`; experimental modules additionally need `ack`. Returns the normalized object on success|
|POST|`/api/modules/<id>/enable`|`admin_required`|set enabled and try to load|
|POST|`/api/modules/<id>/disable`|`admin_required`|set disabled and teardown|
|DELETE|`/api/modules/<id>`|`admin_required`|uninstall (disable first, then delete the directory); the system has no built-in modules, so restoring after uninstall requires re-uploading the package|
|GET|`/api/modules/<id>/asset/<path:rel>`|no login|only `web/`, `images/`, `README.md` are allowed; `Cache-Control: no-cache`; uses `send_from_directory` to prevent traversal|
|GET|`/api/modules/<id>/logs`|`setting:view`|the last 200 lines of module logs (in-memory ring buffer)|
|GET|`/api/modules/preview/<token>/asset/<path:rel>`|no login|screenshots in the pre-install preview session (only `images/` allowed), token is short-lived, `Cache-Control: private, max-age=600`|
|POST|`/api/modules/preview/<token>/install`|`admin_required`|install directly from the preview window: JSON `{ack}`; returns the normalized object and destroys the session on success|
|POST|`/api/modules/upload-check`|`admin_required`|only parses description.json and returns a preview (no disk write, no execution), for display before install|

The `upload-check` response adds these fields on top of the module object:

|Field|Description|
|-|-|
|`installable`|determined only by **hard blockers** (already installed, reserved id…); experimental does not count as a blocker|
|`issues`|`issues + warnings`, kept for compatibility with older frontends|
|`warnings`|advisory items (e.g. "experimental modules require verbatim confirmation")|
|`readme` / `readmeName`|the README body from the package (≤20000 characters) and its path, used by the install preview window|
|`size` / `fileCount` / `folders`|total bytes after extraction, file count, top-level directories (`["web/","modules/"]`)|
|`previewToken`|32-hex-digit preview session token; used by both `install` and the screenshot URLs|
|`images[]`|`{path, size?, url?}`; `url` points to screenshots in the preview session (at most 8, each ≤4MB)|

**Pre-install preview session**: `upload-check` keeps the package itself and the screenshots under `images/` in
`instance/module-previews/<token>/{package.mod.ops_ext, asset/, meta.json}`, TTL 30 minutes
(`preview.SESSION_TTL_SECONDS`); expired directories are cleaned up lazily on the next upload/read.
`POST /api/modules/preview/<token>/install` installs directly from that package, so the user does not have to
pick the file again in the preview window; after a successful install the session is deleted, and a repeated install gets `410 MODULE_PREVIEW_EXPIRED`.

**Zero-module state**: this is the **default state** of a fresh instance (the host ships no built-in modules). In this state
`GET /api/modules` and `/api/modules/nav` return `[]`, the modules page shows an empty-state guide, and
`/system/settings` keeps only the two cards "Global parameters" and "Maintenance operations"; the rest of the host is unaffected.

Error codes: `MODULE_INVALID_PACKAGE` / `MODULE_RESERVED_ID` / `MODULE_EXISTS` / `MODULE_NOT_FOUND` /
`EXPERIMENTAL_NOT_ACKED` / `MODULE_LOAD_FAILED` / `MODULE_PREVIEW_EXPIRED` (all 4xx, with the same envelope as `api_error`).

## 7. Frontend contract

* **Sidebar**: in `getInitialState()` (when currentUser already exists) fetch `/api/modules/nav` and
store it in `initialState.modules`; `layout.menuDataRender` appends these items before the "System settings" group.
Icons: the `icon` field is an antd icon name, resolved dynamically with the existing `@ant-design/icons` (falling back to `AppstoreOutlined` when not found).
* **Routing**: add one static fallback route in `config/routes.ts`

```ts
  {
    path: '/m/:moduleId/*',
    name: 'Extended modules',
    hideInMenu: true,
    component: './bastion/modules/host',
  }
  ```

  It must come before the `path: '*'` 404.

* **Host page** `src/pages/bastion/modules/host.tsx`:

  * Unified shell: module content is always wrapped in `PageContainer` (title = module name, subtitle = module description,
`extra` shows the `v<version> / Official / Experimental` tags), so it stays visually consistent with the other bastion pages.
  * `iframe` mode: `<iframe src={assetUrl(moduleId, entry)}>`, `width:100%`, height
`calc(100vh - 180px)`, white background + 8px corner radius; same-origin, so the iframe can read `localStorage.bastion_token` directly.
**Not recommended** for official modules: hand-rolled HTML/CSS can hardly keep the same look and feel as the host.
  * `js` mode (**recommended**): dynamically inject `<script type="module" src=entry>`; the script takes
`{ React, ReactDOM, antd, request, getToken, message, t, history }` from `window.AutoOpsSDK`,
and then calls `window.AutoOpsModules[moduleId].mount(el, sdk)` (an ESM default export of `mount/unmount` also works).
Inside the module **use only antd components** (`Card/Statistic/Descriptions/Table/Tag/Button/Space/Alert/…`),
write no custom CSS, no gradients/Hero/hand-made cards, and let the host provide the page title;
`sdk.history.push('/system/settings')` performs SPA navigation, and `sdk.message` shows toasts.
  * Module disabled/missing → a friendly 403/404 card.
* **Service layer** `src/services/bastion/modules.ts` + additions to `types.ts`, must `unwrap()`.
* **Install page** `src/pages/bastion/modules/index.tsx` (mounted at "System settings → Extension modules"):

  * List: name (bilingual), version, author badge (`AutoOps` → golden "Official" badge), tags (palette see §2),
experimental beaker icon, enable switch, uninstall, update time, size, load error hint.
  * Uninstall: the Popconfirm warns "after uninstalling, the module files will be deleted; to restore it you must upload the install package again".
  * **No onboarding banner of any kind**: shipping no built-in modules is the default state, so an empty list just shows the table's own Empty (`No extension modules installed yet`); explanatory
Alerts such as "the system ships no built-in modules…" are not accepted, nor are permission banners such as "the current account can only view…" (button Tooltips suffice).
  * Install entry (key constraint): clicking "Install extension module" **does exactly one thing** — synchronously, inside the click event,
call `window.open(origin + '/module-preview#pick','_blank','width=1040,height=820')`; only when it returns `null` (genuinely blocked) call
`message.warning('The browser blocked the popup; please allow popups for this site and retry')`, and do not disturb the user otherwise.
  * **Picking a file must happen in the preview window, not with a casual `input.click()` from the main page**: `window.open()` consumes this click's
"transient user activation", so an `input[type=file].click()` immediately following in the same gesture cannot bring up the file picker — the browser silently ignores it, and the symptom is
"the window opened but the file dialog did not appear" (reproducible with CDP `Page.setInterceptFileChooserDialog`: the `fileChooserOpened` event never fires).
A button inside the window is a fresh user gesture, which is what can bring up the picker. The preview window in the `#pick` state therefore first renders a "Select extension package" card
(antd `Card` + primary button + hidden `input`); click the button → `input.click()` → `change` → `upload-check` →
write `localStorage['module-preview:<token>']` → this window swaps the hash for the token and renders the details in place (**no second window, no navigation, no blank page**).
  * **Forbidden** to call `window.open` in `Upload.beforeUpload` / after any `await` (by then the user gesture is gone and it will definitely be blocked).
  * The main page **shows no** preview/confirm Modal at all.
  * **Detail preview window** (`src/pages/bastion/modules/preview.tsx`, route `{ path: '/module-preview', layout: false }`,
data from `localStorage['module-preview:<token>']` + the token passed in the URL hash): shows the feature description (`readme`, with headings/lists/tables/quotes/code
rendered by antd components, **without pulling in a markdown dependency**), nav pages, settings, permissions and dependencies, in-package directories (shown with Tags),
size/file count, screenshots (`images[].url`, antd `Image`).
  * **The install action completes in the preview window**: the "Install" card at the bottom → experimental modules require the verbatim confirmation first → `POST /api/modules/preview/<token>/install`
→ on success write `localStorage['module-installed:<id>'] = Date.now()` and tell the user the window can be closed;
the main window listens with `window.addEventListener('storage', …)` (key prefix `module-installed:`) and refreshes the list/sidebar.
When there is no README, an Empty is shown.
  * i18n: every new string must be added in both Chinese and English (`src/i18n/`).

## 8. Packaging tool

`bastion-backend/tools/pack_module.py`:

```
python tools/pack_module.py <source-dir> [-o <output-dir>] [--check]
```

* Source directory = one complete package directory (containing `description.json`).
* `--check`: validate only, do not pack (equivalent to the install-time validation).
* Default output `modules-dist/<id>-<version>.mod.ops_ext`; `-o` may point to any directory
(**artifacts of the official RDP gateway module are always generated into the repository root**, for easy distribution).
* Packaging excludes `__pycache__`, `.git`, `node_modules`, `*.pyc`, `.DS_Store`.
* After packing it prints id/version/size/entry count, and runs `spec.validate` over the zip once more.

## 9. Official modules

* First official module: `modules-src/rdp-gateway/` (RDP gateway, `experimental: true`, `author: "AutoOps"`).
* The host's old `app/api/rdp_gateway.py` and `/api/rdp-gateway/*` endpoints, and the `/system/rdp-gateway` frontend page,
are provided by the module **after it is installed**; the host no longer ships them (the decoupling goal).
* Backward compatibility kept: the `RDP_GATEWAY_ENABLED`/`RDP_GATEWAY_PORT` environment variables are still read by the module.
