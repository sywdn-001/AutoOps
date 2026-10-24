# AutoOps Extension Module Developer Whitepaper

> **Audience**: third-party engineers who want to build `.mod.ops_ext` extension modules for the AutoOps bastion.<br>
> **Prerequisites**: Python 3.10+, basic Flask, basic JavaScript / ES Modules.<br>
> **Authoritative contract**: [`CONTRACT.md`](../modules/CONTRACT.md) v1. This whitepaper is the developer-facing expansion of that contract; where the two conflict, the contract and the backend implementation win.<br>
> **Companion examples**: [`hello-bastion`](../../modules-src/hello-bastion/), [`rdp-gateway`](../../modules-src/rdp-gateway/), [`js-demo`](../../modules-src/js-demo/) (all three can be packaged and installed as-is; chapter 12 walks through `hello-bastion`).<br>
> **Applicable hosts**: AutoOps versions that implement `app/modules/` and the `autoops.bastion` facade; a module declares its minimum version through `description.json.minBastionVersion`.

> 中文版 / Chinese: [../modules/WHITEPAPER.md](../modules/WHITEPAPER.md)

> ⚠️ **Security boundary (read this first)**
>
> A Python extension runs with the identity of the bastion process = **fully trusted code, equivalent to editing the backend directly**; there is **no sandbox isolation**; install only modules from **signed, trusted sources**.
>
> This is not a disclaimer but an architectural fact: a module can `import app`, read the database, read `FERNET_KEY`, change global routes, spawn threads and access the network. The host does not restrict any of these. Static assets (`web/`, `images/`, `README.md`) are served **without login**, so never put secrets in them.

---

## Table of contents

> **How to use this table of contents**: for page-only work read chapters 2, 3 and 6; for backend-only work read chapters 4, 5 and 7; before publishing read chapters 9 and 10 plus appendix B. If this is your first contact, walk 1 → 2 → 3 → 4 → 6 → 9 in order; chapter 12 is a complete record you can follow step by step.

**Part 1 · Concepts and boundaries**

- [1. What you can and cannot do](#sec-1) — draw the boundary first: a module can mount routes, add settings, open pages and emit events; it cannot change frontend routes, and it cannot put secrets into static assets that need no login.
  - [1.1 What you can do](#sec-1-1) — the host capabilities a module can reach
  - [1.2 What you cannot do / must not do](#sec-1-2) — the explicit no-go areas, and why they cannot be done today
  - [1.3 Security model and responsibility boundary](#sec-1-3) — why we say "module code is fully trusted"

**Part 2 · Writing code**

- [2. Package layout: what is inside `.mod.ops_ext`](#sec-2) — the top-level layout of the ZIP, the on-disk layout after installation, the hard checks performed at install time (Zip Slip protection, size and entry limits), and a minimal working skeleton.
  - [2.1 ZIP top-level layout](#sec-2-1) — the mandatory `description.json` and `modules/`, the optional `web/`, `images/`, `data/`, `bin/`
  - [2.2 On-disk layout after installation](#sec-2-2) — `instance/modules/<id>/{package,data,state.json}`
  - [2.3 Install-time hard checks and size limits](#sec-2-3) — path traversal, symlinks, 256MB total size, 20000 entries
  - [2.4 What each directory is for, and its no-go rules](#sec-2-4) — which directories the host publishes as static assets
  - [2.5 Minimal skeleton](#sec-2-5) — what a page-only, Python-free module looks like
- [3. `description.json` field reference](#sec-3) — every key in the manifest file: required, optional, defaults, and how the host uses it when installing and displaying.
  - [3.1 Complete example](#sec-3-1) — a manifest that fills in every optional field
  - [3.2 Field table](#sec-3-2) — types, whether a field is required, defaults and notes
  - [3.3 id rules and reserved ids](#sec-3-3) — the regex, the length, and the ids you must not use
  - [3.4 Official releases and `author`](#sec-3-4) — how `author: "AutoOps"` is judged official
  - [3.5 Tags, experimental status and the beaker confirmation](#sec-3-5) — tag colours, the experimental marker and the verbatim confirmation phrase
  - [3.6 The normalised module object and `/api/modules`](#sec-3-6) — the shape the host returns to the frontend, and how manifest fields map onto it
  - [3.7 Multilingual text and JSON conventions](#sec-3-7) — bilingual writing, and the pitfalls of UTF-8 and BOM
- [4. Python extensions: lifecycle and `ctx`](#sec-4) — the entry point of a module's backend code, the hooks, every member of `ctx`, and what it can actually do inside the Flask application.
  - [4.1 Entry convention](#sec-4-1) — `setup(ctx)` in `modules/__init__.py`
  - [4.2 Lifecycle hooks](#sec-4-2) — install / uninstall / enable / disable
  - [4.3 Full table of `ctx` members](#sec-4-3) — attributes, methods and return types
  - [4.4 `ctx.route`: registering module APIs](#sec-4-4) — route prefix, permission code, unified envelope
  - [4.5 `ctx.blueprint`: when you need native Flask power](#sec-4-5) — the cost of taking a Blueprint directly
  - [4.6 `ctx.on` / `ctx.emit`: events](#sec-4-6) — subscribing to host events, broadcasting your own
  - [4.7 Settings and the private data directory](#sec-4-7) — `ctx.get_setting`, `ctx.data_dir`
  - [4.8 Background threads and resource cleanup](#sec-4-8) — don't leak threads or file handles
- [5. The `autoops.bastion` SDK](#sec-5) — the official facade over host capabilities: assets, accounts, credentials, sessions, audit, policy, SSH, and why they are all `async`.
  - [5.1 First principle: anything touching the database or the network is `async def`](#sec-5-1) — the design motivation and its consequences
  - [5.2 Full API table](#sec-5-2) — every method, listed by namespace
  - [5.3 Return values and data structures](#sec-5-3) — the fields of the dataclasses and dicts
  - [5.4 Calling async correctly from a synchronous Flask view](#sec-5-4) — three bridging approaches and how to choose
  - [5.5 Credentials: complete example and security notes](#sec-5-5) — how to use `credentials.get` and the masking convention
  - [5.6 Exceptions and the unified envelope](#sec-5-6) — `ModuleApiError` and error codes
  - [5.7 Audit](#sec-5-7) — writing key actions into the audit chain
  - [5.8 Event broadcasting](#sec-5-8) — letting the frontend know what happened in the background
- [6. The two Web UI modes: `iframe` and `js`](#sec-6) — the two ways to build a module page, their respective URL rules, and why all the official examples use `js` mode.
  - [6.1 Choosing between them](#sec-6-1) — when to use iframe, when you must use js
  - [6.2 Static assets and URL rules](#sec-6-2) — how `/api/modules/<id>/asset/...` maps to files
  - [6.3 Complete `iframe` mode example](#sec-6-3) — a minimal viable skeleton
  - [6.4 Complete `js` mode example](#sec-6-4) — getting the runtime from `window.AutoOpsSDK`
  - [6.5 The shared runtime and build advice](#sec-6-5) — writing modern code without bundling React/antd
  - [6.6 UI and security conventions](#sec-6-6) — use only the host component library, never do a full page reload
- [7. Adding configuration to "Parameters"](#sec-7) — making a module's switches show up in the host's "System settings → Parameters", and reading and writing them correctly.
  - [7.1 Declaration](#sec-7-1) — how to write `settings[]`
  - [7.2 Key names, types and validation](#sec-7-2) — `mod.<id>.<key>` and the boundaries of bool/int/string
  - [7.3 Reading and writing back](#sec-7-3) — `ctx.get_setting` and when a write-back takes effect
  - [7.4 Defaults, uninstall and caveats](#sec-7-4) — where defaults come from, and whether uninstalling clears them

**Part 3 · Debugging and release**

- [8. Debugging and troubleshooting](#sec-8) — where to look first when something breaks: logs, the data directory, the API, a blank page, and a table of common errors.
  - [8.1 Where the logs are](#sec-8-1) — the `module.<id>` logger and the 200-line ring buffer
  - [8.2 The module's private data directory](#sec-8-2) — the place you can write to freely
  - [8.3 Testing the API directly with curl / PowerShell](#sec-8-3) — bypass the frontend to verify the backend
  - [8.4 Troubleshooting an iframe page](#sec-8-4) — blank page, 404, permissions
  - [8.5 Troubleshooting js mode](#sec-8-5) — SDK not injected, mount not registered
  - [8.6 Table of common errors](#sec-8-6) — symptom → cause → fix
- [9. Packaging, installing and uninstalling](#sec-9) — the whole chain from a source directory to a `.mod.ops_ext` and on into an installed instance, plus the checks behind every step.
  - [9.1 Checks before packaging](#sec-9-1) — the files that are pruned automatically
  - [9.2 Using `pack_module.py`](#sec-9-2) — `--check`, `-o`, and how the artifact is named
  - [9.3 The install page flow and the experimental confirmation](#sec-9-3) — the package picker, the preview page and the verbatim confirmation
  - [9.4 Calling the install API directly (automation scenarios)](#sec-9-4) — `upload-check` and installing from a preview session
  - [9.5 What install/enable/disable/uninstall do internally](#sec-9-5) — what gets written and what gets deleted
  - [9.6 Supply chain: signatures and hashes](#sec-9-6) — what can be verified today and what is still missing
- [10. Versions and compatibility](#sec-10) — version number rules, the host version gate, and what happens on upgrade/downgrade/uninstall.
  - [10.1 `version` must be strict semver](#sec-10-1) — both packaging and upgrading depend on it
  - [10.2 `minBastionVersion`](#sec-10-2) — what happens when the host is too old
  - [10.3 Upgrade, downgrade and uninstall](#sec-10-3) — re-uploading is the upgrade; there is no "click to upgrade" entry point
  - [10.4 Advice for writing compatible code](#sec-10-4) — coding for future hosts

**Part 4 · Reference**

- [11. FAQ and known limitations](#sec-11) — frequently asked questions, and the things this version explicitly cannot do.
- [12. hello-world end-to-end walkthrough](#sec-12) — a complete record of taking `hello-bastion` from source to installation, exercising the API, opening the page and uninstalling.
  - [12.1 Source inventory](#sec-12-1) — a source tree you can check against, item by item
  - [12.2 From validation to installation](#sec-12-2) — which step blocks you when validation fails
  - [12.3 Exercising the API](#sec-12-3) — driving the module API with curl
  - [12.4 Page and settings](#sec-12-4) — how the page, navigation and parameters work together
  - [12.5 Troubleshooting and uninstall](#sec-12-5) — removing everything, including the data directory
  - [12.6 Extending onwards from this example](#sec-12-6) — what you can change next
- [Appendix A: Contract clauses this whitepaper relies on](#sec-a) — the `CONTRACT.md` section behind each rule.
- [Appendix B: Pre-release checklist](#sec-b) — tick each item off before submitting/publishing.

---

<a id="sec-1"></a>
## 1. What you can and cannot do

> **Distribution model: the system ships no built-in modules.** A freshly installed instance has **0 modules**; every capability (including the three official examples `hello-bastion`, `js-demo` and `rdp-gateway`) must be installed by an administrator uploading a `.mod.ops_ext`. The official example sources live in `modules-src/`, which is only input for development and packaging — it is never scanned or auto-installed at runtime; the packaged artifacts sit in the **repository root** and are distributed with the repository. Uninstalling really uninstalls; reinstalling = uploading the same package again.

<a id="sec-1-1"></a>
### 1.1 What you can do

| Capability | How | Contract basis |
| --- | --- | --- |
| Add an entry to the sidebar | `description.json.nav[]`, with `type` set to `iframe` or `js` | §2, §7 |
| Deliver a fully custom page | Prefer `js` mode: put an ES Module into `web/` and use the host-injected React/antd; use `iframe` when you need full isolation or a non-React stack | §1, §6 |
| Add configuration items to "Parameters" | Declare `bool`/`int`/`string` in `settings[]`; they land automatically under `mod.<id>.<key>` | §2, §3 |
| Reach internal bastion data | The hosts (assets) / accounts / sessions / audit / policy / settings facades of `autoops.bastion` | §5 |
| Fetch host credentials asynchronously | `await bastion.credentials.get(host_id, account_id=None)` | §5 |
| Register the module's own HTTP API | `@ctx.route(...)` or `ctx.blueprint()` | §4 |
| Write private module files | `ctx.data_dir` (`instance/modules/<id>/data`) | §4 |
| Write audit records yourself | `bastion.audit.log()` / `log_command()` | §5 |
| Broadcast events to the browser | `ctx.emit()` / `bastion.events.emit()` (Socket.IO) | §4, §5 |
| Ship executables | `bin/`; at runtime they are under `ctx.package_dir / "bin"` | §1 |
| Declare dependencies on other modules | `requires: []` (a declaration only; the semantics are in §11) | §2 |

<a id="sec-1-2"></a>
### 1.2 What you cannot do / must not do

- **You cannot change the host layout beyond navigation**. You cannot push pages outside the host's left-hand menu, change the top bar or change the login page; all you can add are the entries in `nav[]`, and the final URL is fixed at `/m/<id><path>`.
- **You cannot add host core permission codes**. Core permission codes are defined in `ALL_PERMISSIONS` in `bastion-backend/app/security.py`, and modules cannot add to them. You may declare module-level permissions in `description.json.permissions` (usually `module:<id>:use`), but whether the role configuration page shows them, and how they are granted, is up to the host implementation.
- **You cannot execute Python during the install-page preview**. The upload preview goes through `POST /api/modules/upload-check`: it only parses `description.json`, writes nothing to disk and imports no module code. Code is loaded only after a successful install.
- **There is no in-process sandbox**. A Python extension can read the database, secrets and other modules' files, and access the network. The host only validates the package's "paths / size / structure"; it does not isolate capabilities.
- **You cannot add database migrations**. The module system has no schema migration mechanism; do not alter host table structures. If you need persistence, use JSON/SQLite files under `ctx.data_dir`.
- **You cannot assume the host environment can `pip install`**. Installing a package never triggers dependency installation. Third-party libraries must be vendored with the package (pure Python goes under `modules/`) or use dependencies the host already has; anything involving platform binaries or external programs goes into `bin/` and you must probe for it yourself.
- **You cannot override host core routes/APIs**. Technically, as trusted code, you could monkeypatch them, but that is unsupported behaviour and it will break on every upgrade; use the dedicated prefix that `ctx.route` gives you.
- **You cannot guarantee that a crash will not affect the host at all**. What the contract provides is "load-failure isolation" and "single-request exception isolation"; malicious code, `os._exit`, exhausting memory/GIL or blocking the main thread can still drag the whole backend down.
- **You cannot put secrets in static assets**. `/api/modules/<id>/asset/**` needs no login, so `web/`, `images/` and `README.md` can be read by any logged-in user, or even by someone who never logged in.
- **You cannot rely on the install page executing your code to do initialisation**. Put initialisation logic in `setup(ctx)`; it runs only in the load phase after a successful install.

<a id="sec-1-3"></a>
### 1.3 Security model and responsibility boundary

**Runtime identity**: a module's Python code runs inside the bastion backend process, using the backend's database connections, application configuration and secrets. It is equivalent to you committing the code straight into `bastion-backend/app/` — it is not a "client-side plugin", and it is not a "restricted DSL".

A few hard rules follow from this:

1. **Install only trusted sources**. The module system currently has no built-in code-signature verification; `author: "AutoOps"` is only metadata (the official badge), not cryptographic proof. Before installing a third-party package you should verify its source, hash and release channel. The host can only guarantee that the ZIP's paths/size/structure are legal, not that the code is harmless.
2. **Module API permissions are module-level**. By default `ctx.route` only checks `module:<id>:use`, which answers "may this user use this module" and **does not** answer "may this user see this host". If an endpoint returns sensitive data keyed by `host_id`, the module must check the current user's grant on that asset itself (see §11 Q11 and §5.5), otherwise you create a privilege-escalation surface.
3. **The SDK executes with the process identity, which is not the current user's identity**. `credentials.get()`, `hosts.list()` and the like do not automatically filter by the JWT user's Grants; the permission model lives on the module side. In the current implementation `hosts.list()`/`hosts.get()`, `accounts.list()`, `credentials.*` and `sessions.list_active()` all return the full data set; only `sessions.open()`, `ssh.run()` and `policy.check()` decide according to the current user's grants. Any asset-sensitive endpoint must do its own authorisation check (see §11 Q10).
4. **Static assets need no login**. Do not embed JWTs, internal URLs, host passwords or debug dumps in them.
5. **Audit only covers the operations that go through the SDK**. Changing the database directly with SQLAlchemy, or bypassing `bastion.audit`, produces no audit record automatically; that makes the bastion's promise of "audit everything" untrue. Route sensitive operations through the SDK's audit facade.
6. **The network is unrestricted**. A module can reach any address; the risk of data leaving the premises rests with the module author and the installer. In enterprise environments, back this up at the code-review / network-policy level.

---

<a id="sec-2"></a>
## 2. Package layout: what is inside `.mod.ops_ext`

<a id="sec-2-1"></a>
### 2.1 ZIP top-level layout

A `.mod.ops_ext` is a **ZIP (deflate)** container. The file name convention is `<id>-<version>.mod.ops_ext`, but **installation trusts the `id`/`version` in `description.json`**, and the file name plays no part in validation.

```text
my-module-1.0.0.mod.ops_ext
├── description.json        # required, must be in the package root
├── modules/                # optional, root of the Python extension
│   ├── __init__.py         # required when modules/ exists, and must define setup(ctx)
│   └── helpers.py          # optional, relative imports
├── images/                 # optional, demo images for the install page
│   └── demo-1.png
├── web/                    # optional, frontend static assets (HTML/JS/CSS/fonts…)
│   ├── index.html
│   └── app.js
├── data/                   # optional, initial data; copied to instance/modules/<id>/data at install
├── bin/                    # optional, executables / external programs
│   └── my-helper.exe
└── README.md               # optional, readable from the install page; the asset API serves it
```

Requirements and limits (contract §1):

- `description.json` must be a **regular file in the package root**, ≤ 256 KB.
- When `modules/` exists, `modules/__init__.py` must exist and must `import` to a callable `setup(ctx)`.
- `data/` is **initial data**, copied into the module's private data directory at install time; it is not a runtime write location (write to `ctx.data_dir` at runtime).
- Only regular files and directories are allowed; **symlinks, device files, absolute paths, `..`, backslashes, drive letters, NUL and illegal file names are all rejected**.
- The whole package must total ≤ 256 MB after extraction, with ≤ 20000 entries.
- **Module code is never imported** during installation; code loading happens after a successful install, inside its own `try/except`.

<a id="sec-2-2"></a>
### 2.2 On-disk layout after installation

```text
bastion-backend/instance/modules/<id>/
├── package/                # contents after ZIP extraction (module code/assets; read-only semantics)
│   ├── description.json
│   ├── modules/__init__.py
│   ├── web/...
│   └── bin/...
├── state.json              # written by the host: enabled, installedAt and other install state
└── data/                   # module-private writable directory (ctx.data_dir)
    └── ...                 # your runtime state
```

From the module's point of view:

| Property | Points to | Purpose |
| --- | --- | --- |
| `ctx.package_dir` | `instance/modules/<id>/package` | Read read-only resources inside the package (`web/`, `bin/`, `README.md`) |
| `ctx.data_dir` | `instance/modules/<id>/data` | The **only** writable directory at runtime |
| `bin/` runtime path | `ctx.package_dir / "bin" / <file>` | External executables (such as the RDP gateway) |
| Static asset URL | `/api/modules/<id>/asset/<relative-path>` | Only applies to `web/`, `images/`, `README.md` |

> This directory only appears after an administrator uploads and installs a package; on a fresh instance `instance/modules/` is empty. The system builds in no modules and pre-installs none, and it will not top itself up from `modules-src/` at startup.

**Do not write to `package_dir`**. Upgrading or reinstalling may replace the whole `package/`; files you write there are lost, and you may corrupt the package contents. Write all state to `data_dir`.

<a id="sec-2-3"></a>
### 2.3 Install-time hard checks and size limits

| Item | Limit / rule | Failure behaviour |
| --- | --- | --- |
| Container | ZIP, deflate | Install fails, rolls back to not-installed |
| `description.json` | Package root, ≤256 KB, valid JSON | `MODULE_INVALID_PACKAGE` |
| Total size after extracting one package | ≤256 MB | Install fails |
| Entry count | ≤20000 | Install fails |
| Paths | Absolute paths, `..`, backslashes, drive letters and NUL are forbidden | Install fails |
| Entry type | Only regular files/directories; symlinks/device files are rejected | Install fails |
| File names | Illegal characters fail immediately | Install fails |
| `modules/` | If present, must have `__init__.py` + `setup` | Install succeeds but `loaded=false`, with the reason in `errors[]` |
| `experimental` | Requires `ack` to match the original text | `EXPERIMENTAL_NOT_ACKED` (400) |

> Recommendation: keep `__pycache__`, `.git`, `node_modules`, `*.pyc` and temporary files out of the package. The packaging tool excludes them by default, but if you write the ZIP by hand you must clean them up yourself.

<a id="sec-2-4"></a>
### 2.4 What each directory is for, and its no-go rules

| Directory | Purpose | Writable at runtime | Accessible as static assets | Notes |
| --- | --- | --- | --- | --- |
| Root `description.json` | Declares metadata / navigation / settings | No | No (only the preview API parses it) | Must be UTF-8 JSON |
| `modules/` | Python extension | No | No | May contain submodules; use relative imports |
| `images/` | Demo images for the install page | No | Yes | PNG/JPG/WebP recommended; don't ship huge images |
| `web/` | Frontend pages and assets | No | Yes | The entry comes from `entry.web` or `nav[].entry` |
| `data/` | Initial data | Copied to `data_dir` after install | No | Copied at install time; read and write `ctx.data_dir` at runtime |
| `bin/` | Executables | No (but the programs may write files themselves) | No | Mind the Linux/Windows platform and execute permissions |
| `README.md` | Documentation | No | Yes | Read without login, so never write secrets there |

<a id="sec-2-5"></a>
### 2.5 Minimal skeleton

A module with "a page only, no Python":

```text
my-module/
├── description.json
└── web/
    └── index.html
```

A module "with a Python API":

```text
my-module/
├── description.json
├── modules/
│   ├── __init__.py
│   └── helpers.py
├── web/
│   └── index.html
└── data/
    └── seed.json
```

The matching minimal working `description.json`:

```json
{
  "id": "my-module",
  "name": { "zh": "我的模块", "en": "My Module" },
  "version": "1.0.0",
  "author": "Your Name",
  "entry": { "web": "web/index.html" },
  "nav": [
    {
      "key": "home",
      "title": { "zh": "我的模块", "en": "My Module" },
      "path": "/",
      "type": "iframe"
    }
  ]
}
```

---

<a id="sec-3"></a>
## 3. `description.json` full field reference

<a id="sec-3-1"></a>
### 3.1 Complete example

```json
{
  "id": "my-module",
  "name": { "zh": "我的模块", "en": "My Module" },
  "version": "1.2.0",
  "author": "Your Name",
  "description": {
    "zh": "一句话说明这个模块做什么。",
    "en": "One line about what this module does."
  },
  "tags": ["experimental", "monitor"],
  "experimental": true,
  "icon": "AppstoreOutlined",
  "homepage": "https://example.com/my-module",
  "license": "MIT",
  "minBastionVersion": "1.0.0",
  "images": ["images/demo-1.png"],
  "entry": { "web": "web/index.html" },
  "nav": [
    {
      "key": "dashboard",
      "title": { "zh": "监控面板", "en": "Dashboard" },
      "path": "/dashboard",
      "entry": "web/dashboard.html",
      "type": "iframe",
      "icon": "DashboardOutlined",
      "permission": "module:my-module:use"
    },
    {
      "key": "settings-ui",
      "title": { "zh": "可视化配置", "en": "Visual Config" },
      "path": "/config",
      "entry": "web/config.js",
      "type": "js",
      "icon": "SettingOutlined"
    }
  ],
  "settings": [
    {
      "key": "enabled",
      "label": { "zh": "启用", "en": "Enabled" },
      "type": "bool",
      "default": "1",
      "description": { "zh": "总开关", "en": "Master switch" }
    },
    {
      "key": "refresh_seconds",
      "label": { "zh": "刷新间隔（秒）", "en": "Refresh interval (s)" },
      "type": "int",
      "default": "30",
      "min": 5,
      "max": 600
    },
    {
      "key": "api_base",
      "label": { "zh": "外部 API 地址", "en": "External API base" },
      "type": "string",
      "default": ""
    }
  ],
  "permissions": ["module:my-module:use"],
  "requires": []
}
```

<a id="sec-3-2"></a>
### 3.2 Field table

| Field | Required | Type | Default | Validation/Notes |
| --- | --- | --- | --- | --- |
| `id` | yes | string | — | `^[a-z][a-z0-9-]{2,39}$`; must not be a reserved word; cannot be changed after installation |
| `name` | yes | string \| `{zh, en}` | — | always normalised to a bilingual object; writing both languages is recommended |
| `version` | yes | string | — | strict semver: `MAJOR.MINOR.PATCH[-pre][+build]`, without a `v` prefix |
| `author` | yes | string | — | `AutoOps` (case/leading and trailing whitespace ignored) = official; the frontend shows a gold "Official" badge |
| `description` | no | string \| `{zh, en}` | empty | displayed on the install page |
| `tags` | no | string[] | `[]` | fixed palette mapping; `experimental` triggers the beaker; `official` is derived from `author`, no need to write it |
| `experimental` | no | bool | `false` | takes the **union** with `experimental` in `tags` |
| `icon` | no | string | `AppstoreOutlined` | antd icon name; the frontend falls back to `AppstoreOutlined` when it cannot be found |
| `homepage` | no | string | `""` | for display only |
| `license` | no | string | `""` | for display only |
| `minBastionVersion` | no | string | `""` | compared with the host `APP_VERSION`; strict semver recommended |
| `images` | no | string[] | `[]` | package-relative paths; displayed through `/api/modules/<id>/asset/<path>`; putting them under `images/` is recommended |
| `entry` | no | object | `{"web":"web/index.html"}` | `entry.web`: default web entry, must be a package-relative path |
| `nav` | no | object[] | `[]` | sidebar entries; at least one item is needed for a menu to appear |
| `nav[].key` | yes | string | — | unique within the module; used as the frontend key/route identifier |
| `nav[].title` | yes | string \| `{zh, en}` | — | menu text |
| `nav[].path` | no | string | `/` | relative path, final URL = `/m/<id><path>`; must start with `/` |
| `nav[].entry` | no | string | `entry.web` | the page/script of this entry, package-relative path; must live under `web/` to be reachable through the static endpoint |
| `nav[].type` | no | `"iframe"` \| `"js"` | `"iframe"` | page loading mode |
| `nav[].icon` | no | string | inherits `icon` | antd icon name |
| `nav[].permission` | no | string | `module:<id>:use` | permission code for accessing this entry / the module API |
| `settings` | no | object[] | `[]` | appears automatically under "Parameter Settings" |
| `settings[].key` | yes | string | — | `^[a-zA-Z][a-zA-Z0-9_]{0,39}$`; the final key name is `mod.<id>.<key>` |
| `settings[].label` | no | string \| `{zh, en}` | falls back to key | display name of the setting |
| `settings[].type` | no | `"bool"` \| `"int"` \| `"string"` | `"string"` | other values get no extra type support |
| `settings[].default` | no | string \| number \| bool | `""` | the host stores it as a string; writing strings uniformly is recommended; bool is normalised to `"1"`/`"0"` |
| `settings[].min` | no | int | `null` | only meaningful for `int` |
| `settings[].max` | no | int | `null` | only meaningful for `int` |
| `settings[].description` | no | string \| `{zh, en}` | empty | helper text on the parameter settings page |
| `permissions` | no | string[] | `[]` | declares module permissions, used by the role configuration page; including `module:<id>:use` is recommended |
| `requires` | no | string[] | `[]` | list of module ids this module depends on; currently declarative only (see §11) |

> Unknown fields: the contract does not promise to "ignore unknown fields", so do not write keys the host does not know; on upgrade they may be dropped or trigger validation failures.
>
> `name`/`description`/`label` also accept a plain string; after normalisation it is a bilingual object. To make both the Chinese and the English environment work, always write `{zh, en}`.

The current implementation additionally enforces the following length/default validations (the contract does not list them one by one; packaging/installation rejects violations):

- `name` ≤64 characters per language; `description` ≤1000 characters per language;
- `nav[].key` must match `^[A-Za-z0-9_-]{1,64}$` and be unique within the module; `nav[].title` ≤64 characters per language;
- `nav[].type` only allows `iframe`/`js`; `nav[].path` must be a package-relative frontend path (no `..`, no backslashes, no absolute paths);
- `settings[].label` ≤64 characters, `settings[].description` ≤256 characters;
- `settings[].min/max` must be integers, and `min <= max`;
- `settings[].default`: `bool` accepts truthy/falsy values and normalises them to `"1"`/`"0"`; `int` must be an integer within `min`/`max`; `string` ≤1024 characters;
- `images[]`, `entry.web`, `nav[].entry` must be safe relative paths (backslashes/`..`/absolute paths/drive letters are rejected);
- `permissions[]` may only hold syntactically valid permission codes; `requires[]` must not contain the module itself.

<a id="sec-3-3"></a>
### 3.3 id rules and reserved ids

```text
^[a-z][a-z0-9-]{2,39}$
```

- must start with a lowercase letter;
- only lowercase letters, digits and hyphens are allowed;
- length 3–40;
- must not be a reserved id: `core`, `system`, `admin`, `modules`, `_builtin`;
- the id determines the URL (`/m/<id>...`, `/api/modules/<id>/...`), the data directory and the settings key prefix; **do not change it once installed**.

<a id="sec-3-4"></a>
### 3.4 Official releases and `author`

- when `author` is compared with `"AutoOps"`, case and leading/trailing whitespace are ignored; if equal, the normalised object gets `official: true` and the frontend shows a gold official badge.
- the official badge is **metadata** only, not a security signature. A third-party package can also write `"AutoOps"` and the host performs no cryptographic verification; do not use it to judge trustworthiness.
- no need to write `official` in `tags`; the frontend adds it automatically from the `official` field.

<a id="sec-3-5"></a>
### 3.5 Tags, experimental status and beaker confirmation

`tags` and `experimental` are in a union relationship: as long as `experimental: true` **or** `tags` contains `"experimental"`, the module is experimental.

Tag colours come from a fixed frontend mapping (the backend only sends the tag string):

| tag | Colour | Icon/Behaviour |
| --- | --- | --- |
| `experimental` | magenta | beaker; a second confirmation is required before installation |
| `official` | gold | added automatically from `author` |
| `security` | red | |
| `network` / `rdp` / `ssh` | blue | |
| `monitor` / `audit` | cyan | |
| `storage` / `file` | green | |
| `ai` | purple | |
| other | default (grey) | |

**Experimental module install confirmation**:

- the frontend pops up a Modal and requires the user to **type by hand** the confirmation phrase below into an input box;
- **pasting is not accepted**;
- the comparison is an **exact match of the whole string after trimming leading and trailing whitespace**;
- the backend validates the `ack` field of the multipart form a second time and returns `400 EXPERIMENTAL_NOT_ACKED` on a mismatch.

The confirmation phrase must be, character for character:

```text
我知道这是实验性功能，使用不当的话也许会造成系统损毁
```

> Note: this sentence has **no** full stop, no exclamation mark and no extra spaces. One missing character means a 400.

<a id="sec-3-6"></a>
### 3.6 Normalised module object and `/api/modules`

`GET /api/modules` and `GET /api/modules/<id>` return the object after the backend's `normalize_description()`, not the raw JSON you wrote. Commonly used fields:

```jsonc
{
  "id": "my-module",
  "name": { "zh": "我的模块", "en": "My Module" },
  "version": "1.2.0",
  "author": "Your Name",
  "official": false,
  "description": { "zh": "", "en": "" },
  "tags": ["experimental", "monitor"],
  "experimental": true,
  "icon": "AppstoreOutlined",
  "images": [
    {
      "path": "images/demo-1.png",
      "url": "/api/modules/my-module/asset/images/demo-1.png"
    }
  ],
  "nav": [
    {
      "key": "dashboard",
      "title": { "zh": "监控面板", "en": "Dashboard" },
      "path": "/m/my-module/dashboard",
      "entry": "web/dashboard.html",
      "icon": "DashboardOutlined",
      "type": "iframe",
      "permission": "module:my-module:use"
    }
  ],
  "settings": [
    {
      "key": "mod.my-module.enabled",
      "rawKey": "enabled",
      "label": { "zh": "启用", "en": "Enabled" },
      "type": "bool",
      "default": "1",
      "min": null,
      "max": null,
      "group": "我的模块"
    }
  ],
  "permissions": ["module:my-module:use"],
  "requires": [],
  "reserved": false,
  "enabled": true,
  "loaded": true,
  "installedAt": "2026-10-23T15:04:05Z",
  "updatedAt": "2026-10-23T15:04:05Z",
  "size": 14745600,
  "errors": []
}
```

When troubleshooting, focus on:

- `enabled`: whether an administrator has enabled it;
- `loaded`: whether the Python extension loaded successfully;
- `errors[]`: the reason loading failed (`setup` raised an exception, a dependency is missing, etc.);
- `nav[].path`: after normalisation this is a **complete URL**, not the original relative path;
- `nav[].entry`: the frontend needs it to load the page/script; the normalisation example in contract §2 omits this field, but the current implementation keeps it. The host implementation is authoritative, so always write `nav[].entry` explicitly;
- `settings[].key`: after normalisation it carries the `mod.<id>.` prefix.

<a id="sec-3-7"></a>
### 3.7 Multilingual and JSON authoring conventions

- the file must be **UTF-8**; no BOM is safest. The current implementation reads with `utf-8-sig` and tolerates a BOM, but do not rely on that.
- use LF line endings; do not hand-write duplicate keys; do not write JSONC comments (the `//` in the contract example is illustrative only).
- write `{zh, en}` for `name`/`description`/`label`/`nav[].title`; a plain string is accepted, but the English environment would only get that same text.
- do not put secrets in `description.json` (both the install page and the API display it).

---

<a id="sec-4"></a>
## 4. Python extension: lifecycle and `ctx`

<a id="sec-4-1"></a>
### 4.1 Entry conventions

- file: `package/modules/__init__.py`;
- a callable `setup(ctx)` must exist;
- loading is performed by the host `app/modules/loader.py` and happens after the installation succeeds; nothing is imported during installation;
- `setup` raising an exception = that module failed to load: `loaded=false`, the reason is recorded in `errors[]`, and **other modules and backend startup are unaffected**;
- inside the module you may split code into several files, using relative imports:

```python
# package/modules/__init__.py
from .helpers import normalize
```

Do not assume the current working directory; derive every path from `ctx.package_dir` / `ctx.data_dir` and use `pathlib.Path`.

<a id="sec-4-2"></a>
### 4.2 Lifecycle hooks

```python
def setup(ctx): ...
def teardown(ctx): ...
def on_enable(ctx): ...
def on_disable(ctx): ...
```

| Hook | When it is called | Required | Notes |
| --- | --- | --- | --- |
| `setup(ctx)` | once when the module is loaded (backend start, enable, load after install) | **required** | register routes/hooks, initialise resources; raising = load failure |
| `teardown(ctx)` | before disable/uninstall (of a loaded module) | optional | stop threads, close connections, flush state; must be idempotent |
| `on_enable(ctx)` | when an administrator enables the module | optional | may happen before or after `setup`; do not initialise twice |
| `on_disable(ctx)` | when an administrator disables the module | optional | real cleanup should still happen in `teardown` |

Additional conventions (inferred from the contract; the implementation is authoritative):

- `setup` may run again on every backend start; do not assume that "it runs once and is then valid forever".
- routes registered in `setup` should be cleaned up by the loader when the module is unloaded; if you manually attached global objects to `ctx.app`, `teardown` must remove them yourself.
- when `ctx.on("install", handler)` / `ctx.on("uninstall", handler)` fire depends on the host's load order; modules are not imported during installation, so **do not rely on the install hook to do first-time initialisation**; put idempotent initialisation in `setup`.
- no hook should call `sys.exit` / `os._exit`, and none should block for a long time.

<a id="sec-4-3"></a>
### 4.3 Full `ctx` member table

| Member | Type/signature | Description |
| --- | --- | --- |
| `ctx.id` | `str` | module id (read-only) |
| `ctx.version` | `str` | module version (read-only) |
| `ctx.author` | `str` | author (read-only) |
| `ctx.official` | `bool` | whether official (derived from author, read-only) |
| `ctx.app` | `Flask` | host app; **use with care**, prefer `ctx.route` |
| `ctx.package_dir` | `Path` | package root directory (read-only) |
| `ctx.data_dir` | `Path` | private writable directory (`instance/modules/<id>/data`) |
| `ctx.logger` | `logging.Logger` | logger name `module.<id>` |
| `ctx.get_setting(key, default=None)` | `str \| default` | read a module setting, automatically prefixes `mod.<id>.` |
| `ctx.set_setting(key, value)` | `str` (current implementation) | write a module setting (persist + commit); may only write declared raw keys |
| `ctx.settings` | `dict[str, str]` | settings snapshot (read-only semantics, may be stale) |
| `ctx.route(path, methods=("GET",), permission=None)` | decorator | attaches the function to `/api/modules/<id>/api<path>`; returns the unified envelope |
| `ctx.blueprint(name="api")` | `Blueprint` | returns a Flask blueprint; the loader collects the routes after `setup` finishes and finally mounts them under `/api/modules/<id>/api` |
| `ctx.on(event, handler)` | `None` | registers a hook; `event ∈ {"install","uninstall","enable","disable"}`; the handler accepts `ctx` or no arguments |
| `ctx.emit(topic, payload)` | `None` | Socket.IO broadcast `module:<id>:<topic>`; `topic` must not contain a colon and must be ≤64 characters |

<a id="sec-4-4"></a>
### 4.4 `ctx.route`: registering module APIs

Final URL:

```text
/api/modules/<id>/api<path>
```

Complete example:

```python
from flask import request
from autoops import ModuleApiError, bastion


def setup(ctx):
    @ctx.route("/stats", methods=("GET",))
    def stats():
        # return a dict → the host wraps it as {"success": true, "message": "ok", "data": {...}}
        return {"module": ctx.id, "version": ctx.version}

    @ctx.route("/echo", methods=("POST",))
    def echo():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            # raise ModuleApiError → the host wraps it as {"success": false, "code": "ERROR", "message": ...}
            raise ModuleApiError("缺少 name", 400)
        return {"hello": name}

    @ctx.route("/missing")
    def missing():
        raise ModuleApiError("对象不存在", 404)
```

Calling it:

```bash
TOKEN="<登录后拿到的 JWT>"
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/my-module/api/stats
```

Success envelope:

```json
{ "success": true, "message": "ok", "data": { "module": "my-module", "version": "1.0.0" } }
```

Failure envelope:

```json
{ "success": false, "code": "ERROR", "message": "对象不存在", "data": null }
```

Rules and pitfalls:

- a function returning `dict` / `list` / `None` → `api_ok(data)`; do not assume other return types are handled correctly (`None` becomes `data: null`).
- only raising `ModuleApiError(message, status)` lets you control the HTTP status code; returning a dict is always `200`. There is currently no official entry point for customising `message`/extra fields through `ctx.route`; if you need a custom response, use `ctx.blueprint()` (see below).
- `methods` defaults to `("GET",)`; POST/PUT/DELETE must be declared explicitly.
- default `permission` rule: the module's first `nav[].permission`, otherwise `module:<id>:use`. Do not assume routes are public by default.
- **do not rely on `async def` views**. The contract does not promise that `ctx.route` supports async views; every example in this whitepaper uses synchronous views plus the bridging approach in §5.4 to call the async SDK. Even if the host implementation supports async views, the synchronous form still works.
- do not put `/api` in the path and do not put the module id in it; the host has already assembled them. Do not register `/<path:...>` to capture the host's other routes.
- do not swallow every exception in a route function and return `{"success": false}`; that gets wrapped in a success envelope again. Either return normally or raise `ModuleApiError`.

<a id="sec-4-5"></a>
### 4.5 `ctx.blueprint`: when you need native Flask capabilities

When you need custom status codes, file responses, `send_file`, streaming responses or native Flask decorators, use a blueprint:

```python
from flask import send_file


def setup(ctx):
    # name is only used as an internal identifier; the host prefixes the blueprint name with the module, so no need to worry about colliding with other modules
    bp = ctx.blueprint("raw")

    @bp.get("/raw")
    def raw():
        return {"ok": True}

    @bp.get("/download")
    def download():
        return send_file(ctx.package_dir / "web" / "demo.zip", as_attachment=True)

    # do not call ctx.app.register_blueprint(bp):
    # the loader collects blueprints centrally after setup finishes; manual registration causes duplicate routes / a lost prefix.
```

Conventions:

- `ctx.blueprint(name)` returns a **Flask Blueprint**; the routes you attach to it are eventually collected by the loader and mounted under `/api/modules/<id>/api`, so `@bp.get("/raw")` ends up at `/api/modules/<id>/api/raw`;
- do not `register_blueprint` yourself and do not add a `url_prefix` to the blueprint; the prefix is managed centrally by the host;
- the host prefixes the blueprint name with `module_<id>_`, so the default `"api"` does not collide with other modules;
- exceptions raised inside a blueprint are **not** automatically turned into the unified envelope (`ModuleApiError` is only converted inside the `ctx.route` dispatcher); if you need the unified envelope, return `{"success": false, ...}` yourself and set the status code;
- a blueprint can also use `before_request`/`after_request`/`teardown_request`; they only take effect for the module's own routes.

<a id="sec-4-6"></a>
### 4.6 `ctx.on` / `ctx.emit`: events

Register lifecycle events (note: `ctx.on` is an ordinary method, **not a decorator**):

```python
def _on_enable(ctx):
    ctx.logger.info("enabled")


def _on_disable(ctx):
    ctx.logger.info("disabled")


def setup(ctx):
    ctx.on("enable", _on_enable)
    ctx.on("disable", _on_disable)
```

A handler may accept `ctx` or take no arguments at all; the host is compatible with both. The table in contract §4 only writes `ctx.on(event)`; the current implementation signature is `ctx.on(event, handler)` and it is not a decorator either, so do not write it wrong.

Broadcasting events:

```python
def setup(ctx):
    @ctx.route("/ping", methods=("POST",))
    def ping():
        ctx.emit("pong", {"at": bastion.util.now().isoformat()})
        return {"sent": True}
```

- the Socket.IO topic actually broadcast by `ctx.emit(topic, payload)` is `module:<id>:<topic>`;
- `topic` must not be empty, must not contain a colon and is at most 64 characters; use `.` or `-` to express hierarchy;
- how the frontend subscribes is decided by the host page; module pages have no standard SDK subscription interface (see §11);
- when `ctx.on("install", handler)`/`"uninstall"` fire depends on the loader, so do not put critical initialisation only in these two hooks.

<a id="sec-4-7"></a>
### 4.7 Settings and the private data directory

```python
import json
import os
import tempfile
from pathlib import Path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def setup(ctx):
    interval = ctx.get_setting("refresh_seconds", "30")
    ctx.logger.info("interval=%s", interval)

    state_path = ctx.data_dir / "state.json"
    if not state_path.exists():
        _atomic_write(state_path, json.dumps({"installed": True}, ensure_ascii=False))
```

- reading a setting is a **synchronous DB call**, so keep it in synchronous views/`setup`; do not put it in `async def` (it blocks the event loop).
- `ctx.set_setting()` commits, so do not write frequently on request hot paths or in loops; check whether the value really changed first. It can only write raw keys declared in `description.json.settings[]`; undeclared keys raise `KeyError` (current implementation); the return value is the normalised string, but the contract does not promise a return type, so do not rely on it.
- `ctx.settings` is a snapshot and may be stale across requests; use `ctx.get_setting()` for the latest value.
- for `get_setting`, passing the raw key (`refresh_seconds`) is recommended; the host adds the `mod.<id>.` prefix automatically; the full key is also recognised in some implementations, but the raw key is the most stable across versions.
- `data_dir` is deleted on uninstall; data that must be kept has to be exported to external storage.

<a id="sec-4-8"></a>
### 4.8 Background threads and resource release

The contract provides no scheduler. When you need background tasks:

```python
import threading

# do not assume you can attach arbitrary attributes to ctx; keep runtime state in a module-level dict.
_WORKERS: dict[str, tuple[threading.Thread, threading.Event]] = {}


def setup(ctx):
    stop = threading.Event()
    thread = threading.Thread(target=_loop, args=(ctx, stop), name="my-module-worker", daemon=True)
    thread.start()
    _WORKERS[ctx.id] = (thread, stop)


def teardown(ctx):
    worker = _WORKERS.pop(ctx.id, None)
    if worker is None:
        return
    thread, stop = worker
    stop.set()
    thread.join(timeout=5)
```

Notes:

- a thread must either be a daemon or be reliably stoppable in `teardown`; otherwise it keeps running after disable/uninstall.
- in a multi-process deployment every worker runs `setup`, so background tasks may execute more than once; de-duplicate with a `data_dir` file lock / host lock, or avoid background tasks.
- to access the database/request context from a background thread, go through `bastion.util.run_blocking` or a thread-safe facade provided by the host; do not take the request `g` directly.
- do not perform long network waits in `setup`; it slows down startup.

---

<a id="sec-5"></a>
## 5. `autoops.bastion` SDK

<a id="sec-5-1"></a>
### 5.1 First principle: anything touching the DB/network is `async def`

```python
from autoops import bastion


async def example():
    # Correct
    hosts = await bastion.hosts.list()

    # Wrong: returns a coroutine, nothing happens, and you get "coroutine was never awaited"
    # hosts = bastion.hosts.list()
```

The original wording of contract §5 is: **every method that touches the database/network is `async def`**; synchronous versions with the `_sync` suffix are also provided for simple scripts.

- `async def` methods must be `await`ed;
- `_sync` versions may only be called where there is **no running event loop**; calling them inside `async def` deadlocks/errors;
- for how to bridge synchronous Flask views, see §5.4;
- do not run synchronous DB queries inside async functions (for example calling `Host.query` directly) — it holds up the event loop; if you really need blocking code use `await bastion.util.run_blocking(...)`.

<a id="sec-5-2"></a>
### 5.2 Full API table

The following signatures correspond item by item to contract §5. Everything after `->` is the return type; every method not marked `_sync` is `async def`.

| Facade | Signature | Description |
| --- | --- | --- |
| Credentials | `await bastion.credentials.get(host_id, account_id=None) -> Credentials` | Decrypts the credential for the specified host/account; automatically writes a `module_credential` audit entry; the plaintext exists only in the return value |
| Credentials | `await bastion.credentials.list(host_id=None) -> list[CredentialMeta]` | Lists credential metadata, **without keys/passwords** |
| Assets | `await bastion.hosts.list(protocols=None, keyword=None) -> list[dict]` | Host list; `protocols` filters by protocol, `keyword` filters by keyword |
| Assets | `await bastion.hosts.get(host_id) -> dict` | Details of a single host |
| Accounts | `await bastion.accounts.list(host_id=None) -> list[dict]` | List of asset accounts |
| Accounts | `await bastion.accounts.reveal_password(account_id) -> str` | Decrypts the account password; **writes an audit entry**, returns the plaintext `str` |
| Accounts | `await bastion.accounts.reveal_private_key(account_id) -> str` | Decrypts the account private key; **writes an audit entry**, returns the plaintext `str` |
| Sessions | `await bastion.sessions.list_active() -> list[dict]` | Currently online sessions |
| Sessions | `await bastion.sessions.open(host_id, account_id, protocol="ssh", source="module:<id>") -> dict` | Registers/opens a session; `source` identifies the origin as `module:<id>` |
| Audit | `await bastion.audit.log(action, target_type=None, target_id=None, message="", detail=None) -> None` | Writes one operation audit entry; do not put plaintext in `detail` |
| Audit | `await bastion.audit.log_command(session_id, command, output=None, allowed=True, reason="") -> None` | Writes one command audit entry |
| Settings | `await bastion.settings.get(key) -> str` | Reads a system setting (string) |
| Settings | `await bastion.settings.set(key, value) -> None` | Writes a system setting (**use with care**, see §7.4) |
| Users | `await bastion.users.list() -> list[dict]` | User list |
| Policy | `await bastion.policy.check(account_id, command) -> dict` | Command policy verdict: `{allowed, matched_rule, action}` |
| SSH | `await bastion.ssh.run(host_id, command, account_id=None, timeout=30) -> dict` | Remote execution; always goes through the command policy; throws `bastion.ModuleError` on failure; returns `{code, stdout, stderr, elapsed}` |
| Events | `await bastion.events.emit(topic, payload) -> None` | Socket.IO broadcast |
| Common | `bastion.util.now() -> datetime` | Current aware UTC time (**synchronous**) |
| Common | `await bastion.util.run_blocking(fn, *a, **kw) -> Any` | Runs blocking code on a thread with app context (**async wrapper**) |

Synchronous versions:

- the naming rule is method name + `_sync`, e.g. `bastion.hosts.list_sync()`, `bastion.credentials.get_sync(host_id)`;
- the contract only promises "synchronous versions with the `_sync` suffix" and does not enumerate the coverage item by item; go by the target host version;
- they may only be called from a thread with no running event loop.

<a id="sec-5-3"></a>
### 5.3 Return values and data structures

#### `Credentials`

The object returned by `credentials.get()` has at least the following fields (contract §5):

| Field | Type | Description |
| --- | --- | --- |
| `host_id` | int | Asset id |
| `account_id` | int | Account id |
| `host_name` | str | Asset name |
| `address` | str | Address |
| `protocol` | str | `ssh` / `rdp` / `winrm`, etc. |
| `port` | int | Port |
| `username` | str | Login username |
| `password` | str | Plaintext password after decryption; may be empty for key-based login |
| `private_key` | str | Plaintext private key after decryption; may be empty for password-based login |
| `passphrase` | str | Private key passphrase; may be empty |

**Return only `username` and the length of `password` to the frontend**; do not serialize the whole object.

#### `CredentialMeta`

`credentials.list()` explicitly promises "no keys"; the contract says it returns `list[CredentialMeta]`, while the current implementation returns `list[dict]` (account metadata + host info). Do not rely on undocumented fields; when you need the account id/username, prefer `accounts.list()` or `credentials.get()`.

#### `hosts.list()` / `accounts.list()` / `sessions.list_active()`

They return `list[dict]`; the exact fields are whatever the SDK implementation provides. The contract does not pin the schema down field by field, so:

- depend only on the fields you actually need, and read them defensively (`item.get("id")`);
- do not hand the whole dict to the frontend as-is (it may contain sensitive/internal fields);
- the `hello-bastion` example only uses `len(...)`, deliberately avoiding binding to fields;
- **the current implementation of these read APIs does no per-user filtering**: an account with module permission can see every host/account/online session. If your module targets ordinary users, you must do the asset-level authorization check yourself, or restrict the API to administrator permission.

#### `ssh.run()`

```python
from autoops import bastion


async def run_uptime(host_id):
    result = await bastion.ssh.run(host_id, "uptime", timeout=15)
    # {"code": 0, "stdout": "...", "stderr": "", "elapsed": 0.42}
    return result
```

- it enforces the command policy; a policy denial/execution failure throws `bastion.ModuleError`;
- error messages may contain output from the target machine; trim/redact them before returning to the frontend;
- this is a high-risk API; always verify the user's authorization on the asset first.

#### `policy.check()`

```python
from autoops import bastion


async def check_command(account_id, command):
    verdict = await bastion.policy.check(account_id, command)
    # {"allowed": False, "matched_rule": "...", "action": "deny"}
    return verdict
```

`ssh.run()` also goes through the policy internally; do not "check first, then skip the policy" inside your module.

<a id="sec-5-4"></a>
### 5.4 Calling async correctly from Flask synchronous views

This is the pitfall module developers hit most often.

**Background**: this project's Flask-SocketIO uses `async_mode="threading"` (`bastion-backend/app/__init__.py`), and ordinary Flask views run on a worker thread that has **no running event loop**. Therefore `asyncio.run(...)` is safe inside an ordinary synchronous view.

But if you put the same code into an `async def` view, or the host switches to ASGI/event-loop mode in the future, `asyncio.run()` throws:

```text
RuntimeError: asyncio.run() cannot be called from a running event loop
```

#### Pattern A: synchronous view + `asyncio.run` (the default scenario in this project)

```python
import asyncio
from autoops import bastion


async def _collect():
    hosts = await bastion.hosts.list()
    return {"hostTotal": len(hosts)}


def setup(ctx):
    @ctx.route("/count")
    def count():
        return asyncio.run(_collect())   # no event loop on the view thread, safe
```

Key points:

- `asyncio.run()` creates/closes a fresh event loop every time, which suits low-frequency module APIs; for high-concurrency APIs use a persistent thread pool instead (see pattern C);
- the coroutine you pass in must be created on the current thread (calling `asyncio.run(_collect())` directly does that);
- do not use `asyncio.run` inside an `async def` that is already `await`ing.

#### Pattern B: prefer the `_sync` versions

```python
from autoops import bastion


@ctx.route("/count")
def count():
    hosts = bastion.hosts.list_sync()   # the synchronous version promised by the contract
    return {"hostTotal": len(hosts)}
```

Only valid in synchronous views; do not call it inside `async def`.

#### Pattern C: separate-thread bridge (the safe choice when the context is uncertain)

When you are unsure which thread the view runs on / whether there is an event loop, use a coroutine factory + a separate thread:

```python
import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any


def run_async(factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Run a coroutine from a synchronous function. factory must return the coroutine, not a coroutine object."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # no event loop on this thread: run it directly
        return asyncio.run(factory())

    # this thread already has an event loop: use another one to avoid nesting asyncio.run
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # re-raise as-is on the calling thread
            box["error"] = exc

    thread = threading.Thread(target=target, name="module-api-bridge", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
```

Usage:

```python
@ctx.route("/count")
def count():
    return run_async(_collect)
```

Notes:

- you must pass a **factory**: if you create the coroutine first and then hand it to another thread, it may bind to the wrong event loop;
- one thread per request is not cheap; for high-QPS scenarios reuse a `ThreadPoolExecutor(max_workers=4)`;
- `thread.join()` adds timeout risk to the request; the SDK itself should guarantee a return, add your own timeout/circuit breaker when necessary.

#### Pattern D: use `run_blocking` for blocking code called from async

```python
def _blocking_read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


async def _load():
    data = await bastion.util.run_blocking(_blocking_read, "/tmp/x.bin")
    return len(data)
```

`run_blocking` runs `fn` on the `module-io` thread pool with a Flask app context. It is **not** for wrapping coroutines: `fn` must be an ordinary synchronous function.

#### Pattern E: parallel SDK calls

```python
import asyncio
from autoops import bastion


async def _stats():
    hosts, sessions = await asyncio.gather(
        bastion.hosts.list(),
        bastion.sessions.list_active(),
    )
    return len(hosts), len(sessions)
```

#### Anti-pattern checklist

| Anti-pattern | Consequence |
| --- | --- |
| Forgetting `await` on `bastion.hosts.list()` | You get a coroutine, the logic never runs, and the log shows `coroutine was never awaited` |
| `asyncio.run(...)` inside an `async def` view | `RuntimeError: asyncio.run() cannot be called from a running event loop` |
| Creating the coroutine first, then handing it to another thread's `asyncio.run` | The coroutine may bind to the wrong loop, `RuntimeError: ... attached to a different loop` |
| Calling `Host.query...` directly inside `async def` | Blocks the event loop; every module/request on the host slows down |
| `time.sleep()` in asynchronous code | Same as above; use `await asyncio.sleep()` or `run_blocking` |
| Wrapping a `_sync` version inside `async def` | Blocks/deadlocks |

<a id="sec-5-5"></a>
### 5.5 Credentials: full example and security reminders

```python
import asyncio
from autoops import ModuleApiError, bastion


async def _credential_summary(host_id: int, account_id: int | None):
    # 1) confirm the asset exists first, so "asset not found" can be a readable 404
    host = await bastion.hosts.get(host_id)
    if not host:
        raise ModuleApiError(f"Asset not found: {host_id}", 404)

    # 2) fetch the credential asynchronously; the SDK writes a module_credential audit entry automatically
    credentials = await bastion.credentials.get(host_id, account_id=account_id)
    try:
        # 3) return only metadata that is safe to expose
        return {
            "hostId": host_id,
            "accountId": credentials.account_id,
            "username": credentials.username,
            "passwordLength": len(credentials.password or ""),
        }
    finally:
        # drop the reference explicitly; Python strings cannot really be wiped from memory, but this shortens how long the plaintext stays resident
        credentials = None


def setup(ctx):
    @ctx.route("/credential-sample", methods=("GET",))
    def credential_sample():
        from flask import request
        raw = (request.args.get("host_id") or "").strip()
        try:
            host_id = int(raw)
        except ValueError:
            raise ModuleApiError("host_id must be a positive integer", 400) from None
        return asyncio.run(_credential_summary(host_id, None))
```

**Security reminders**:

1. **Plaintext stays in memory only.** Do not write it to logs, do not `print` it, do not return it to the frontend, do not write it to `data_dir`, do not put it in exception messages. In log/audit detail put only `host_id`, `account_id`, the username and the length.
2. **Do not cache.** `credentials.get()` decrypts and writes an audit entry every time; caching the plaintext in a global variable expands the exposure surface for a long time and also bypasses the "every use is auditable" semantics.
3. **Python strings are not under your control.** You cannot reliably zeroize them; what you can do is shorten their lifetime, avoid copies, and keep them out of logs/serialization.
4. **Only people who need it should fetch it.** The `permission` on `ctx.route` only controls "can this module be used", not "can this host be viewed". Asset-sensitive APIs must check the current user's authorization on `host_id` themselves (see §1.3, §11 Q11).
5. **Do not display passwords on the page.** Even a password field means the plaintext entered the DOM/browser memory; the example only shows the length.
6. **`password` may be empty for key-based login.** Do not use `len(password) > 0` to decide whether an account can log in; use the `auth_type`/`private_key` semantics.
7. **`accounts.reveal_password` / `reveal_private_key` are just as dangerous**, and they also write an audit entry. Avoid them when you can; when you must use them, follow the rules above.
8. **Do not put sensitive information in audit detail**:

```python
from autoops import bastion


async def log_credential_read(host_id, account_id):
    await bastion.audit.log(
        "module_read_credential",
        target_type="host",
        target_id=host_id,
        message="module requested credential metadata",
        detail={"accountId": account_id, "purpose": "diagnose"},  # no username/password
    )
```

<a id="sec-5-6"></a>
### 5.6 Exceptions and the unified envelope

- Under the `ctx.route` wrapper: returning `dict/list/None` → success envelope; throwing `ModuleApiError(message, status)` → failure envelope + the given status code.
- `bastion.ModuleError` is the SDK's general exception (the contract states that `ssh.run` throws it). The contract does not fix its fields or its `status`; do not rely on undefined attributes such as `exc.status_code` — read defensively with `getattr(exc, "status", None)` first, and fall back to 502/400 when it is absent.
- When catching SDK exceptions in a route, do not return the raw underlying exception text to the frontend (it may contain host/path/credential hints); record the exception **type** to `ctx.logger` and return a readable business error.
- The failure envelope `code` for module APIs defaults to `ERROR`; if you need a more precise code, return the `api_error` structure yourself via `ctx.blueprint()`.

<a id="sec-5-7"></a>
### 5.7 Audit

```python
from autoops import bastion


async def audit_patrol():
    await bastion.audit.log(
        "module_action",
        target_type="host",
        target_id=42,
        message="a user triggered a patrol from the module",
        detail={"module": "my-module", "step": "collect"},
    )
```

- `detail` must be a JSON-serializable dict; do not put plaintext credentials, full command output, or tokens in it.
- `audit.log_command(...)` is for command-level audit; the current implementation looks `session_id` up first as a `sid` string and then as a numeric `id`, and both work, but the contract does not fix the convention; the most cross-version-safe option is to pass the `sid` returned by `/api/sessions` and verify it on the target host version.
- Audit writes should never make the business operation fail; the SDK handles this internally, but your own exception handling should not roll back business work just because an audit write failed.

<a id="sec-5-8"></a>
### 5.8 Event broadcasting

```python
from autoops import bastion


async def broadcast_refresh():
    await bastion.events.emit(
        "my-module:refresh",
        {"at": bastion.util.now().isoformat()},
    )
```

- This is a Socket.IO broadcast; like `ctx.emit`, the current implementation automatically adds the `module:<id>:` prefix and rejects topics that contain a colon or exceed 64 characters. Pass only the event name (e.g. `refresh`); do not build the module prefix yourself.
- There is no standard SDK for how a module page subscribes; for v1, prefer having the page poll its own API, and use event broadcasting for host-side notifications.

---

<a id="sec-6"></a>
## 6. The two Web UI modes: `iframe` and `js`

The official examples [`hello-bastion`](../../modules-src/hello-bastion), [`rdp-gateway`](../../modules-src/rdp-gateway) and [`js-demo`](../../modules-src/js-demo) **all use `js` mode**. `hello-bastion` and `js-demo` are minimal teaching examples, while `rdp-gateway` is a complete business module with a Python backend. None of the three ships pre-installed with the host; all are uploaded and installed as `.mod.ops_ext`, and `modules-src/` is only source code and packaging input.

<a id="sec-6-1"></a>
### 6.1 Comparing the options

**Choose `js` mode by default**: it shares the same React/antd/theme as the host, the page looks like part of the bastion host, and it can reuse the host's `request`, `message` and `history` directly. Use `iframe` only when you need complete isolation (third-party scripts, independent routing, a non-React stack).

| Dimension | `iframe` | `js` (preferred) |
| --- | --- | --- |
| Tech stack | Anything: plain HTML, Vue, React, Svelte, build output | Must be an ES Module; using the host-shared React/antd is recommended |
| Visual consistency | Separate document, fonts/spacing/theme all have to be aligned by hand, easy to diverge from the host | Reuses the host antd and theme directly, consistent by nature |
| Page isolation | Separate window/document, CSS does not pollute each other | Same DOM as the host, CSS may affect each other |
| Getting the JWT | `localStorage.getItem('bastion_token')` (same origin) | `sdk.getToken()`, or attached automatically by the host `request` |
| Routing/URL | Standalone page with a fixed entry | Mounted in the host page container, usually a single page |
| Build requirements | None; just put the output in `web/` | Also optional (`React.createElement`); pre-building is recommended for complex UI |
| Suited for | Non-React stacks, a full standalone console, third-party pages that need hard isolation | Official modules, small panels, embedded cards, anything that must match the host visually |
| Risks | XSS inside the iframe is still same-origin; the token can be read | Style/global variable pollution; duplicate copies of React crash |
| Official examples | None (the minimal skeleton in whitepaper §6.3 is only a syntax reference) | `hello-bastion`, `rdp-gateway`, `js-demo` |

<a id="sec-6-2"></a>
### 6.2 Static assets and URL rules

After a module is installed, `web/`, `images/` and `README.md` are reachable without login through the following endpoint:

```text
GET /api/modules/<id>/asset/<path relative to the package>
```

For example:

```text
description.json's entry.web = "web/index.html"
→ page URL: /api/modules/my-module/asset/web/index.html

description.json's nav[].entry = "web/dashboard.html"
→ page URL: /api/modules/my-module/asset/web/dashboard.html
```

Relative paths inside a page are resolved against the **current asset directory**. Suppose the page address is
`/api/modules/my-module/asset/web/index.html`:

| Written in the page | Actual request | Notes |
| --- | --- | --- |
| `./app.js` | `/api/modules/my-module/asset/web/app.js` | ✅ Asset in the same directory |
| `../images/logo.png` | `/api/modules/my-module/asset/images/logo.png` | ✅ Can cross over into `images/` |
| `/api/modules/my-module/api/stats` | Module API | ✅ Recommended: write it as an absolute path |
| `../api/stats` | `/api/modules/my-module/asset/api/stats` | ❌ Treated as a static file, 404 |

Limits:

- **Only `web/`, `images/` and `README.md` are accessible**; `data/`, `modules/`, `bin/` and `description.json` are not exposed.
- The response header is `Cache-Control: no-cache`; during development just change the file and refresh.
- The static asset endpoint **requires no login**; only the APIs called from the page go through JWT.
- The entry must live under `web/`; `entry: "index.html"` (missing `web/`) returns 404.
- Do not pass the JWT via `?token=`; the token would land in browser history, proxy logs and the Referer. Use the `Authorization` header.

<a id="sec-6-3"></a>
### 6.3 Complete `iframe` mode example

Pick `iframe` only when complete isolation is truly required or you are using Vue/Svelte/plain pages. Below is a **minimal skeleton for a third-party module** (not an official example; replace `my-module` with your own id): save it as `web/index.html` and point `entry.web` in `description.json` at it:

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>我的模块</title>
  <style>
    body { margin: 0; padding: 24px; font: 14px/1.6 "Microsoft YaHei", sans-serif; background: #f5f8fc; color: #1c2b3a; }
    .card { max-width: 720px; padding: 20px; background: #fff; border: 1px solid #dbe6f4; border-radius: 12px; }
    button { padding: 6px 14px; cursor: pointer; }
    pre { padding: 12px; overflow: auto; background: #f3f6fa; border-radius: 8px; }
    .error { color: #b42318; }
    .muted { color: #68788c; }
  </style>
</head>
<body>
  <section class="card">
    <h1>我的模块</h1>
    <p class="muted">当前页在宿主同源 iframe 中运行。</p>
    <button id="load" type="button">加载 /stats</button>
    <p id="status" class="error" role="alert"></p>
    <pre id="out">尚未加载</pre>
  </section>

  <script>
    (() => {
      const MODULE_ID = 'my-module';
      const API_BASE = `/api/modules/${MODULE_ID}/api`;
      const TOKEN_KEY = 'bastion_token';
      const statusEl = document.getElementById('status');
      const outEl = document.getElementById('out');

      const getToken = () => localStorage.getItem(TOKEN_KEY) || '';

      function goLogin() {
        const redirect = encodeURIComponent(location.pathname + location.search);
        // the iframe is same-origin with the host, so a top-level redirect is allowed; do not open the login page only inside this iframe
        window.top.location.href = `/user/login?redirect=${redirect}`;
      }

      function setError(message, withLogin) {
        statusEl.textContent = '';
        const text = document.createElement('span');
        text.textContent = message;
        statusEl.appendChild(text);
        if (withLogin) {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = '重新登录';
          button.style.marginLeft = '8px';
          button.addEventListener('click', goLogin);
          statusEl.appendChild(button);
        }
      }

      async function api(path, options = {}) {
        const token = getToken();
        if (!token) {
          const error = new Error('本地没有 bastion_token，可能尚未登录。');
          error.status = 401;
          throw error;
        }

        const response = await fetch(API_BASE + path, {
          ...options,
          headers: {
            Accept: 'application/json',
            ...(options.headers || {}),
            Authorization: `Bearer ${token}`,
          },
          credentials: 'same-origin',
          cache: 'no-store',
        });

        let body = null;
        try {
          body = await response.json();
        } catch (_) {
          body = null;
        }

        if (response.status === 401) {
          const error = new Error('登录状态已失效。');
          error.status = 401;
          throw error;
        }
        if (response.status === 403) {
          const error = new Error((body && body.message) || '当前账号没有该权限。');
          error.status = 403;
          throw error;
        }
        if (!response.ok || !body || body.success !== true) {
          const error = new Error((body && body.message) || `请求失败（HTTP ${response.status}）`);
          error.status = response.status;
          throw error;
        }
        return body.data;
      }

      async function load() {
        const button = document.getElementById('load');
        button.disabled = true;
        statusEl.textContent = '';
        try {
          const data = await api('/stats');
          outEl.textContent = JSON.stringify(data, null, 2);
        } catch (error) {
          if (error.status === 401) {
            setError(error.message, true);
          } else if (error.status === 403) {
            setError(error.message, false);
          } else {
            setError(error.message || '请求失败', false);
          }
        } finally {
          button.disabled = false;
        }
      }

      document.getElementById('load').addEventListener('click', load);
      load();
    })();
  </script>
</body>
</html>
```

Key points:

- The iframe is **same-origin**, so it can read the host's `localStorage.bastion_token`; that is also its largest risk surface (the token is visible to module pages).
- A 401 must redirect to the **top-level** login page; do not merely load the login page into the iframe. After a successful login the host returns to the original page.
- Use `textContent` for all dynamic text; never concatenate backend data with `innerHTML`.
- When a module API fails, prefer showing the `message` from the backend envelope instead of only the HTTP status code.

<a id="sec-6-4"></a>
### 6.4 Complete `js` mode example

The entry of `js` mode is an ES Module, for example `web/dashboard.js`. The host will:

1. inject the module's entry script into the page as `<script type="module" src="/api/modules/<id>/asset/web/dashboard.js">`;
2. provide the shared runtime `window.AutoOpsSDK = { React, ReactDOM, antd, request, getToken, message, t, history }`;
3. call `window.AutoOpsModules[<id>].mount(el, sdk)`, or use the ESM default-exported `mount/unmount` where supported;
4. call `unmount()` when leaving the page.

Complete example (no build step, using `React.createElement` directly):

```js
// web/dashboard.js
const MODULE_ID = 'my-module';
const API = `/api/modules/${MODULE_ID}/api`;

const sdk = window.AutoOpsSDK;
if (!sdk || !sdk.React || !sdk.ReactDOM) {
  throw new Error('window.AutoOpsSDK 未注入：当前宿主版本可能不支持 js 模式');
}

const { React, ReactDOM } = sdk;
const { useEffect, useRef, useState, useCallback } = React;
const h = React.createElement;

function describeError(error) {
  const status = error && error.response ? error.response.status : 0;
  const body = (error && error.response && error.response.data) || {};
  if (status === 401) return '登录状态已失效，请重新登录。';
  if (status === 403) return body.message || '当前账号没有使用该模块的权限。';
  if (status) return body.message || `请求失败（HTTP ${status}）`;
  return '网络异常，或后端没有响应。';
}

function App({ sdk: host }) {
  const { antd, request } = host;
  const { Alert, Button, Card, Space, Statistic, Typography } = antd;
  const [stats, setStats] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      // request is injected by the host: it attaches the JWT and goes through the host error handling by default; returns the parsed JSON envelope
      const body = await request(`${API}/stats`, { method: 'GET' });
      setStats((body && body.data) || null);
    } catch (error) {
      setError(describeError(error));
    } finally {
      setLoading(false);
    }
  }, [request]);

  useEffect(() => {
    load();
    timerRef.current = window.setInterval(load, 30000);
    return () => {
      if (timerRef.current) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [load]);

  return h(
    'div',
    { style: { padding: 24 } },
    h(
      Card,
      {
        title: h(Typography.Text, { strong: true }, '我的模块'),
        extra: h(Button, { type: 'primary', loading, onClick: load }, '刷新'),
      },
      error
        ? h(Alert, {
            type: 'error',
            showIcon: true,
            message: error,
            style: { marginBottom: 16 },
          })
        : null,
      h(
        Space,
        { size: 40 },
        h(Statistic, { title: '主机总数', value: (stats && stats.hostTotal) || 0 }),
        h(Statistic, { title: '在线会话', value: (stats && stats.activeSessions) || 0 }),
      ),
      h(
        Button,
        {
          style: { marginTop: 16 },
          onClick: () => {
            if (host.history && typeof host.history.push === 'function') {
              host.history.push('/system/settings');
            }
          },
        },
        '打开参数设置',
      ),
    ),
  );
}

let root = null;

export function mount(el, injectedSdk) {
  root = injectedSdk.ReactDOM.createRoot(el);
  root.render(injectedSdk.React.createElement(App, { sdk: injectedSdk }));
}

export function unmount() {
  if (root) {
    root.unmount();
    root = null;
  }
}

const api = { mount, unmount };
window.AutoOpsModules = window.AutoOpsModules || {};
window.AutoOpsModules[MODULE_ID] = api;
export default api;
```

Key points and pitfalls:

- **Do not bundle React/ReactDOM/antd yourself**. Two copies of React on the same page cause hooks errors and break the antd context. Use `window.AutoOpsSDK`.
- `request` is the host's umi-request-based function: it attaches the JWT automatically and throws on 4xx/5xx; the error object usually has `error.response.status` and `error.response.data`. If the host's global error handling redirects to the login page, your `catch` may only see a brief state before the redirect; handle it according to the actual behaviour.
- `t` is the host i18n function (the host only guarantees that it exists, not that it contains your module's own Chinese/English entries). For module-specific copy it is better to write Chinese directly or ship a small dictionary; do not expect `t('My Module')` to be translated into Chinese.
- Use `getToken()` only when you need to display/debug the token; for normal requests use `request`, and do not hand-build the `Authorization` header and run through the host interceptors again.
- For in-page navigation use `sdk.history.push('/system/settings')` (or `/system/modules`), not a full page reload via `window.location`; `history` is provided by the host router, so do not call it after the module has been unmounted.
- **For the UI use only the antd injected by the host**: no custom CSS, no gradients/Hero sections, no hand-drawn page titles. The title, breadcrumbs and the official/experimental labels are provided by the host's `PageContainer`; the module returns only content cards (`Card`/`Statistic`/`Descriptions`/`Table` etc.), which is what keeps it consistent with the other bastion host pages.
- `unmount()` must clean up timers, event listeners, `AbortController` and the React root; otherwise the page keeps polling/throwing after you switch away.
- `mount` must only touch the `el` passed in; do not `document.body.appendChild` or change the host's global layout.
- If you use JSX/TypeScript, build it into ESM locally first and then put it in `web/`; mark `react`, `react-dom` and `antd` as external and replace them with `window.AutoOpsSDK`, or simply skip the bundler.

<a id="sec-6-5"></a>
### 6.5 Shared runtime and build recommendations

| Dependency | Source | Recommendation |
| --- | --- | --- |
| React / ReactDOM | `window.AutoOpsSDK` | Do not bundle; the version follows the host (currently React 19) |
| antd | `window.AutoOpsSDK` | Do not bundle; use the components and version injected by the host |
| request | `window.AutoOpsSDK` | Use the host request; token attachment and unified error handling are automatic |
| `getToken` | `window.AutoOpsSDK` | Reads `bastion_token`; beware of the same-origin XSS risk |
| `message` | `window.AutoOpsSDK` | Lightweight toast; do not use it to show sensitive data |
| `t` | `window.AutoOpsSDK` | Host entries are limited; provide the module's own copy yourself |
| `history` | `window.AutoOpsSDK` | Host SPA router; navigate with `history.push(path)`, do not reload the whole page |

Build options:

1. **No build** (recommended for small panels): use `React.createElement` + `sdk.antd` directly and drop the `.js` into `web/`. The examples in this document use this approach.
2. **Pre-build**: produce ESM with esbuild/Rollup; mark React/antd as external and, after building, manually rewrite them to be taken from `window.AutoOpsSDK`; do not ship a bare module with `import React from 'react'` (there is no import map in the browser, so it fails immediately).
3. **iframe + any build**: the most freedom. Vue/React/Svelte output all work, just put it in `web/`; note that the base path must be set to a relative path (`./`), otherwise the assets 404.

<a id="sec-6-6"></a>
### 6.6 UI/security conventions

- The page occupies only the host content area; do not try to hide/cover the host sidebar, top bar or login page.
- **In `js` mode use only the antd components injected by the host** (`Card`/`Statistic`/`Descriptions`/`Table`/`Tag`/`Button`/`Space`/`Alert` etc.), ship no CSS, write no gradients or big Hero sections, and do not draw the page title yourself. The title, breadcrumbs and the version/official/experimental labels are all provided by the host's `PageContainer`; the root node the module returns holds only content cards.
- Do not load third-party CDN scripts; the bastion host may run offline on an intranet, and third-party scripts can read the same-origin `bastion_token`.
- Do not put the token into URLs or logs, and do not `postMessage` it to untrusted windows.
- `iframe` mode only: navigate to host pages with `window.top`, and only in explicit cases such as login; do not manipulate the host DOM. In `js` mode use `sdk.history.push()`.
- If small local adjustments are genuinely needed, use antd's `style` prop for limited spacing/width tweaks; do not inject global `<style>` or override `.ant-*` class names.
- Mobile/narrow-screen layout is your own responsibility; the host content area width is not fixed.
- Use `textContent` before any backend-returned string enters the DOM; escape it first when `innerHTML` is unavoidable.

---

<a id="sec-7"></a>
## 7. Adding configuration items to "Parameters"

<a id="sec-7-1"></a>
### 7.1 Declaration

Write `settings[]` in `description.json`:

```json
{
  "settings": [
    {
      "key": "enabled",
      "label": { "zh": "启用采集", "en": "Enable collection" },
      "type": "bool",
      "default": "1",
      "description": { "zh": "关闭后不再采集", "en": "Turn off collection" }
    },
    {
      "key": "interval_seconds",
      "label": { "zh": "采集间隔（秒）", "en": "Interval (seconds)" },
      "type": "int",
      "default": "60",
      "min": 10,
      "max": 3600
    },
    {
      "key": "endpoint",
      "label": { "zh": "上报地址", "en": "Endpoint" },
      "type": "string",
      "default": ""
    }
  ]
}
```

After installing/loading, the host registers them into the settings system under the following key names:

```text
mod.<moduleId>.<key>
# Example: mod.my-module.enabled
#          mod.my-module.interval_seconds
#          mod.my-module.endpoint
```

They appear automatically under "System settings → Parameters", and the group name is the module display name (Chinese preferred).

<a id="sec-7-2"></a>
### 7.2 Keys, types and validation

| Type | Stored as | UI | Validation/notes |
| --- | --- | --- | --- |
| `bool` | `"1"` / `"0"` | Toggle switch | Normalized on write; when reading, do not use `bool("0")` directly (that yields `True`) |
| `int` | Decimal string | Number input | `min`/`max` are validated by the host; the module must still guard itself (old data/direct DB edits) |
| `string` | Verbatim string | Text input | May be empty; length limits are up to the module |

- `key` regex: `^[a-zA-Z][a-zA-Z0-9_]{0,39}$`; do not include the `mod.` prefix.
- `default` should preferably always be written as a string; `bool` may be `"1"`/`"0"` or `true`/`false`, and the host normalizes it.
- `min`/`max` only mean something for `int`; do not write length limits for `string` (the contract does not define them).
- Declaration-time validation (current implementation): the `bool` default is normalized to `"1"`/`"0"`; the `int` default must be an integer falling within `min`/`max`; the `string` default must be ≤1024 characters; `min`/`max` must be integers with `min <= max`. Failing this fails at packaging/install time rather than at runtime.
- Setting values are **not secret storage**: `GET /api/settings` is visible to users with `setting:view`, and the database stores plaintext JSON as well. Do not put API keys or passwords into setting items; when a secret is needed, have the administrator use environment variables/external secret management, or clearly disclose the risk.

<a id="sec-7-3"></a>
### 7.3 Reading and writing back

```python
def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def setup(ctx):
    enabled = _as_bool(ctx.get_setting("enabled", "1"), True)
    interval = _as_int(ctx.get_setting("interval_seconds", "60"), 60, 10, 3600)
    endpoint = str(ctx.get_setting("endpoint", "") or "").strip()

    ctx.logger.info("enabled=%s interval=%s endpoint=%s", enabled, interval, endpoint)

    # Write back: persists to the DB + commit
    if interval < 10:
        ctx.set_setting("interval_seconds", 10)
```

Notes:

- The `key` of `ctx.get_setting(key, default)` is a **raw key** (without `mod.<id>.`); the host adds the prefix automatically. Do not assemble the full key yourself and pass it in, to avoid a double prefix.
- `ctx.set_setting` commits immediately; do not write on every request/loop iteration. Compare the current value first and only write when it changes.
- Reads happen in a synchronous context (`setup`/synchronous views); do not call `ctx.get_setting` directly inside `async def`, because it is a synchronous DB call.
- After a setting is changed the host does not proactively push an event to the module; it takes effect the next time the module reads it. The page has to be refreshed/re-fetched.

<a id="sec-7-4"></a>
### 7.4 Defaults, uninstall and caveats

- Defaults are registered when the module is installed/loaded; existing database values take precedence, and `ensure_defaults()` does not overwrite values the user has changed.
- Uninstalling unregisters the module defaults and metadata; the current host implementation also deletes the `mod.<id>.*` rows in `SystemSetting` (the contract does not state this explicitly). So do not rely on "old settings survive a reinstall"; if you need to keep them, export `data_dir`/the setting values first and restore them yourself after reinstalling.
- Handle settings compatibly across upgrades/reinstalls: give new keys defaults, ignore deleted keys, and migrate renamed keys (read the old key and write the new one in your `setup`).
- The `group` of a setting item is generated by the host from the module display name; the module need not and cannot customise the group order.
- Do not confuse "whether the module is enabled" with the module's internal `enabled` setting: the former is the enabled state in `state.json` (the administrator operates it on the extension modules page), while the latter is just the module's own business switch.

---

<a id="sec-8"></a>
## 8. Debugging and troubleshooting

<a id="sec-8-1"></a>
### 8.1 Where the logs are

**Module log endpoint**:

```text
GET /api/modules/<id>/logs
Permission: setting:view
Returns: the most recent 200 lines (in-memory ring buffer)
```

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/my-module/logs
```

**Writing logs in code**:

```python
def setup(ctx):
    ctx.logger.info("my-module %s loaded", ctx.version)
    ctx.logger.warning("something looks wrong: %s", "detail")
```

- The logger name is `module.<id>`; it propagates to the host root logger and is output to the backend console together with the Flask logs (the `logging.basicConfig` call in `python run.py`).
- The log format is decided by the host (currently it contains the time, the level and the logger name).
- Do not log credentials in plaintext, tokens or full command output; when you need to pin something down, log the id/length/type.

**Loader logs**:

- `app/modules/loader.py` handles the `sys.modules['autoops']` injection, imports the module and calls `setup(ctx)`;
- When loading fails, the exception is funnelled into that module's `errors[]`, and the loader also writes a log entry; the backend does **not** exit because a single module's `setup` raised;
- At startup `init_modules(app)` runs after `seed_data` and before the Socket.IO event registration; it guarantees that it never raises. It only loads modules that are already installed and enabled under `instance/modules/`; a completely empty instance is skipped outright and **does not** scan or install from `modules-src/`.

**Checking module state**:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:5000/api/modules/my-module
```

Key fields: `enabled`, `loaded`, `errors[]`, `installedAt`, `size`.

<a id="sec-8-2"></a>
### 8.2 The module's private data directory

```text
bastion-backend/instance/modules/<id>/data/
```

- This is the **only** directory a module should write to; `ctx.data_dir` is already a `Path` pointing at it.
- On install, the package's `data/` is copied here; all reads and writes at runtime happen here afterwards.
- Uninstalling deletes the whole `data/`; export state you need to keep to external storage/a database, or remind the administrator to make a backup before uninstalling.
- While debugging you can:

```powershell
Get-ChildItem -Recurse bastion-backend\instance\modules\my-module\data
Get-Content bastion-backend\instance\modules\my-module\data\state.json
```

- Write files atomically with "temporary file + `os.replace`" so that a process crash does not leave half a JSON file behind (see §4.7 for an example).
- Do not put executables/large files in `data_dir`; do not assume it has unlimited space.

<a id="sec-8-3"></a>
### 8.3 Testing the API directly with curl / PowerShell

Log in first to get a JWT (example; do not put a real password into your shell history):

```bash
curl -s -X POST http://127.0.0.1:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-password>"}'
```

Take `data.token` from the response (or whatever field the current API actually returns). Then:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/my-module/api/stats
```

PowerShell:

```powershell
$token = "<token>"
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/modules/my-module/api/stats" `
  -Headers @{ Authorization = "Bearer $token" }
```

Debugging order:

1. `GET /api/modules/<id>` to look at `loaded`/`errors`;
2. `GET /api/modules/<id>/logs` to read the module logs;
3. call the module API directly with curl (bypassing the page, to tell whether the problem is on the backend or the frontend);
4. check the permissions: does your account have `module:<id>:use` / the permission the route declares;
5. check the parameters (a 404 may simply mean the `host_id` does not exist).

<a id="sec-8-4"></a>
### 8.4 Troubleshooting iframe pages

| Symptom | Check |
| --- | --- |
| Blank page/404 | Is `nav[].entry`/`entry.web` under `web/`; is the asset URL `/api/modules/<id>/asset/web/...` |
| Static JS/CSS 404 | Are the relative paths resolved against `asset/web/`; do not use `../api/...` |
| API 401 | Does `localStorage.bastion_token` exist and is it unexpired; does the request header carry the `Bearer ` prefix |
| API 403 | Does the current user have `module:<id>:use` or the route's custom permission; is the module disabled |
| API 404 | Is the URL `/api/modules/<id>/api/...`; is the route registered in `setup`; is the module `loaded=true` |
| CORS error in the console | Is the iframe really same-origin (host proxying/port changes can make it a different origin); do not fetch cross-origin |
| Changed a file but nothing happens | The static endpoint sends `Cache-Control: no-cache`, but the browser may still force-cache it; Ctrl+F5 |
| Empty token | Has the host added `sandbox` to the iframe without `allow-same-origin`; that makes localStorage unavailable |

Debugging tip: open the browser DevTools → Network, look at the static asset requests first, then at the API requests; for 401/403 read the `message` in the response body instead of only looking at the status code.

<a id="sec-8-5"></a>
### 8.5 Troubleshooting js mode

| Symptom | Check |
| --- | --- |
| Blank page, console shows `Unexpected token '<'` | `entry` points at HTML or at a 404 page; check the `.js` path and the MIME type |
| `window.AutoOpsSDK` does not exist | The host version does not support js mode, or the script ran before the SDK was injected |
| `window.AutoOpsModules[id]` does not exist | The script did not run/has a syntax error/the module id is wrong; check Network and Console |
| React hooks errors/broken antd styles | You bundled your own copy of React/antd; use `sdk.React`/`sdk.antd` instead |
| `mount` reports that `el` is empty | The host page has no rendered container or this is a timing problem; do not touch the DOM before the module is loaded |
| Requests still fire after switching pages | `unmount` did not clear the timers/AbortController; check the effect cleanup |
| A 401 response is handled globally right away | The host `request` 401 interceptor redirects to login; this is expected behaviour, the module side only needs a fallback |

A js-mode script must be an **ES Module** (`export`/`import` syntax), and its entry path must end in `.js`/`.mjs`. Put the script under `web/` and write `nav[].entry` as `web/dashboard.js`.

<a id="sec-8-6"></a>
### 8.6 Common error reference table

| Error code/symptom | Meaning | Handling |
| --- | --- | --- |
| `MODULE_INVALID_PACKAGE` | Illegal ZIP structure/paths/size/JSON | Re-validate with `pack_module.py --check`; check `description.json` and the package root |
| `MODULE_RESERVED_ID` | The id is a reserved word | Use another id (`core`/`system`/`admin`/`modules`/`_builtin`) |
| `MODULE_EXISTS` | The same id is already installed | Uninstall the old version or go through the upgrade flow (see §10.3) |
| `MODULE_NOT_FOUND` | The module does not exist/was uninstalled | Refresh `/api/modules` |
| `EXPERIMENTAL_NOT_ACKED` | The experimental confirmation phrase does not match | Type the confirmation phrase out character by character; check the spaces |
| `MODULE_LOAD_FAILED` | Install succeeded but loading failed | Look at `errors[]` of `/api/modules/<id>`, `/logs`, and the backend traceback |
| 401 | JWT missing/expired | Log in again; check `Authorization` |
| 403 | Insufficient permission/module disabled | Grant `module:<id>:use`; check `enabled` |
| 404 (module API) | Route not registered/wrong URL | Check the `ctx.route` path and the `/api/modules/<id>/api` prefix |
| 500 (module API) | View exception/SDK exception | `/logs` + backend traceback; do not swallow exceptions |
| `coroutine was never awaited` | Missing `await` | Fix it as described in §5.4 |
| `asyncio.run() cannot be called from a running event loop` | Called `asyncio.run` inside an async context | Use `await` or a thread bridge |
| A setting does not appear | Module not loaded/`settings` not declared | Check `loaded`, `errors`; refresh the parameter settings page after reinstalling |
| `No module named autoops` | The host loader did not inject the facade/version mismatch | Confirm that the host implements the module system and that its version is ≥ `minBastionVersion` |

> `POST /api/modules/upload-check` only parses `description.json` and **does not execute Python**. So "the preview looks fine but loading fails after install" is usually a problem in the code itself; go to `/logs` and read the traceback.

---

<a id="sec-9"></a>
## 9. Packaging, installation and uninstallation

<a id="sec-9-1"></a>
### 9.1 Pre-packaging checklist

- [ ] The `id`/`version` in `description.json` are correct and `version` has been bumped according to semver;
- [ ] `modules/__init__.py` defines `setup(ctx)` and a local `python -m py_compile` passes;
- [ ] The assets in `web/` use relative paths/module API absolute paths, with no host paths such as `/static/...`;
- [ ] `data/` contains only initial data, no runtime temporary files;
- [ ] No `__pycache__`, `.git`, `node_modules`, `*.pyc`, logs, keys or test dumps;
- [ ] `README.md` and `images/` are safe to make public (they are readable without login);
- [ ] You have installed it by hand once on the target host version, with `loaded=true` and `errors=[]`;
- [ ] For an experimental module, the confirmation phrase text is ready.

<a id="sec-9-2"></a>
### 9.2 Using `pack_module.py`

The tool is shipped with the backend: `bastion-backend/tools/pack_module.py`. Command line shape (contract §8):

```text
python bastion-backend/tools/pack_module.py <source-dir> [-o <output-dir>] [--check]
```

Run it from the repository root:

```powershell
cd D:\Desktop\AutoOps

# validate only, do not package
python bastion-backend\tools\pack_module.py modules-src\hello-bastion --check

# package into the repository root (official packages ship with the repository)
python bastion-backend\tools\pack_module.py modules-src\hello-bastion -o .
```

If your current directory is `bastion-backend/`, the equivalent command is:

```powershell
python tools\pack_module.py ..\modules-src\hello-bastion -o ..
```

Output:

```text
<id>-<version>.mod.ops_ext
# e.g. hello-bastion-1.1.0.mod.ops_ext (right in the repository root)
```

Tool behaviour (contract §8):

- The input is **one complete package directory** (containing `description.json`);
- `--check` only validates (the same validation as at install time) and produces no ZIP;
- Packaging excludes `__pycache__`, `.git`, `node_modules`, `*.pyc`, `.DS_Store`;
- After packaging it prints `id`/`version`/`size`/the entry count and runs `spec.validate` once more on the ZIP;
- A validation failure exits non-zero and prints the reason.

If your current branch does not have this tool, the modular backend implementation has not been merged yet; do not work around the install validation with a hand-made ZIP — align with the host version first.

<a id="sec-9-3"></a>
### 9.3 The install page flow and the experimental confirmation

> The system starts with 0 modules by default; there is no "built-in module" or preinstalled entry point. Even the three official examples have to go through the upload flow below.

1. The administrator logs in and goes to "System Settings → Extension Modules";
2. Select/drag in the `.mod.ops_ext`;
3. The frontend calls `POST /api/modules/upload-check` and shows a `description.json` preview (name, version, author, tags, description, demo images);
4. If `experimental` is set (or the tags contain `experimental`), a Modal pops up and requires **typing by hand**:
   ```text
   我知道这是实验性功能，使用不当的话也许会造成系统损毁
   ```
   If the input does not match, the "Install" button is unavailable; pasting does not count;
5. Call `POST /api/modules/install` (multipart `file`, plus `ack` for an experimental module);
6. After a successful install the module appears in the list and is enabled/loaded according to the host policy by default; a `nav[]` entry appears in the sidebar.

Install-time error codes are listed in §8.6. A failed install must roll back to "not installed" and never leave half a module behind.

<a id="sec-9-4"></a>
### 9.4 Calling the install API directly (automation scenarios)

```bash
# upload preview: nothing is written to disk, nothing is executed
curl -s -X POST "http://127.0.0.1:5000/api/modules/upload-check" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./my-module-1.0.0.mod.ops_ext"

# install: multipart file; add ack for an experimental module
curl -s -X POST "http://127.0.0.1:5000/api/modules/install" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./my-module-1.0.0.mod.ops_ext" \
  -F "ack=我知道这是实验性功能，使用不当的话也许会造成系统损毁"
```

- `install` requires administrator permission;
- `ack` is required only for experimental modules; do not send it for non-experimental ones;
- On success it returns the normalised module object (same as §3.6).

<a id="sec-9-5"></a>
### 9.5 Internal behaviour of install/enable/disable/uninstall

| Operation | Host behaviour |
| --- | --- |
| Install | Parse the ZIP → validate paths/size/entries/description → extract atomically into a temporary directory → rename to `instance/modules/<id>/package` → write `state.json` → register setting defaults/navigation → try to load the module |
| Load | Inject the `autoops` facade → import the `modules` package → call `setup(ctx)`; a failure only affects this module (`loaded=false` + `errors[]`) |
| Enable | Set `enabled=true`, load if necessary, call `on_enable` |
| Disable | Set `enabled=false`, call `on_disable` / `teardown`, the module routes are no longer available |
| Uninstall | Disable first → `teardown` → delete `package`/`state.json`/`data` → unregister the defaults/navigation |

- Whether a module is enabled by default after a successful install is decided by the host policy; the administrator can toggle it on the extension modules page.
- Once disabled, the module's `/api/modules/<id>/api/**` should return unavailable; the sidebar entry disappears.
- Uninstalling **deletes `data/`**; remind the user to make a backup first.
- Uninstalling really uninstalls: no tombstone is kept, there is no `restore` API and no "one-click reinstall"; to restore it, upload the same `.mod.ops_ext` again.
- Requests in flight/background threads are not forcibly killed; a module has to stop them itself in `teardown`.

<a id="sec-9-6"></a>
### 9.6 Supply chain: signatures and hashes

The current contract (v1) **does not implement package signature verification**; `author: "AutoOps"` is merely metadata. What you can actually do:

1. Obtain `.mod.ops_ext` files only from trusted repositories/release channels;
2. Have the publisher provide a SHA-256:

```powershell
Get-FileHash .\my-module-1.0.0.mod.ops_ext -Algorithm SHA256
```

3. Compare the hash before installing; inside a corporate network you can additionally do a code review/sandbox scan (the scanning tool itself does not affect how the host runs);
4. Do not treat `experimental` as a security boundary; it is only a hint that "a second confirmation is required".

---

<a id="sec-10"></a>
## 10. Versions and compatibility

<a id="sec-10-1"></a>
### 10.1 `version` must be strict semver

Format:

```text
MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
```

Valid:

```text
1.0.0
1.2.3
2.0.0-rc.1
1.4.0-beta.2+build.7
```

Invalid:

```text
v1.0.0        # do not include the v
1.0           # must have three segments
1.0.0.0       # four segments
1.0.0-        # empty pre-release
01.0.0        # numeric identifiers must not have leading zeros
```

- `id` stays the same and `version` goes up; installation uses `description.json` as the source of truth, the file name is not validated;
- The version string appears on the install page, in the module list and in `/api/modules`; do not stuff it with marketing words beyond a date.

<a id="sec-10-2"></a>
### 10.2 `minBastionVersion`

- Optional; declares the minimum host version your module needs;
- The host compares it against the backend-configured `APP_VERSION` (contract §2);
- Fill in **the lowest version you actually tested**; if unsure, start from `1.0.0` instead of aiming high;
- The exact behaviour when "the minimum version is not met" (refuse the install/load failure/warning only) is not specified in the contract; do not rely only on the host blocking it, the module should still do its own compatibility check:

```python
def setup(ctx):
    min_version = "1.0.0"  # keep in sync with description.json
    ctx.logger.info("module %s requires bastion >= %s", ctx.version, min_version)
```

- The semantics of comparing pre-release versions (`-rc`/`+build`) are not specified in the contract; do not rely on pre-release ordering for a production release.

<a id="sec-10-3"></a>
### 10.3 Upgrading, downgrading and uninstalling

The current contract **does not define a dedicated upgrade API**; installing the same id again hits `MODULE_EXISTS`. Until the host provides an upgrade flow, the safe procedure is:

1. Tell the administrator to back up `bastion-backend/instance/modules/<id>/data/` (or use the module's own export feature);
2. Disable the module;
3. Uninstall (this deletes `data/`, so the backup has to come first);
4. Upload and install the new version (there is no upgrade entry point, and no tombstone/one-click restore);
5. Restore `data/` (if the format is compatible);
6. Enable it and verify `loaded=true`, `errors=[]`.

Things to watch out for:

- Settings land in the `SystemSetting` table; the current implementation deletes the `mod.<id>.*` rows on uninstall, but the contract does not say so explicitly, so you should still back up/migrate the setting values yourself before reinstalling;
- Downgrading has no dedicated flow either; handle it as "back up → uninstall → install the old package → restore", and compatibility of data/settings is not guaranteed;
- Never change `description.json.id` to "upgrade" under any circumstances; that is treated as a different module.

<a id="sec-10-4"></a>
### 10.4 Recommended compatibility practices

- Use only the `ctx`/SDK APIs listed in the contract; do not import the internals of `app.modules.*` (they change between upgrades).
- Read optional fields defensively: `getattr(obj, "field", default)`, `item.get("field")`; do not assume a dict has a given key.
- Put a `schemaVersion` in settings/data files and migrate in `setup`:

```json
{ "schemaVersion": 2, "items": [] }
```

- Do not do heavy work in `setup`; the host loads every enabled module at startup, and a slow module slows down the whole startup.
- In a multi-process deployment every worker loads the modules; background jobs/timers must be de-duplicated or explicitly accept the duplication.
- Before upgrading, regress on the target `APP_VERSION`: install, enable, API, page, settings, disable, uninstall.

---

<a id="sec-11"></a>
## 11. FAQ and known limitations

**Q1: Can a module change the layout beyond the host navigation?**<br>
No. The contract only provides `nav[]` entries and the `/m/<id>/*` content area; you cannot change the login page, the top bar or the sidebar structure. js mode shares the DOM, but changing the host layout is unsupported behaviour.

**Q2: Can I add permission codes?**<br>
You cannot add to the host's core permission catalogue (`ALL_PERMISSIONS`). You can declare module-level permissions in `description.json.permissions` (such as `module:<id>:use`), but whether they show up on the role configuration page and how they are granted is up to the host implementation; before the host wires them up, non-super-administrators may not be able to obtain that permission.

**Q3: Does the install page preview execute Python?**<br>
No. `upload-check` only parses `description.json`; it writes nothing to disk and imports nothing. A module is loaded only after a successful install; errors in `setup` show up as `loaded=false` / `errors[]`.

**Q4: Does a module crash affect the whole bastion?**<br>
The contract provides load-failure isolation and request-exception isolation: an exception raised by `setup` only makes this module `loaded=false`; a route exception only makes that request return 500. But there is no process-level sandbox: an infinite loop, memory exhaustion, `os._exit` or blocking the GIL can still drag the process down. Write robust code, and do not throw uncaught exceptions into background threads.

**Q5: Can I `pip install` dependencies?**<br>
No. An install package does not trigger dependency installation. Pure Python libraries can be vendored into `modules/` (mind the license/size); platform binaries go into `bin/` and must be probed for; prefer to use only the dependencies the host already has.

**Q6: Can I use the host's internal modules/database?**<br>
Technically yes (in-process trusted code), but that is non-contract behaviour and an upgrade may break it. Prefer the SDK; when you really need an internal capability, gather the imports in one place, probe the version, and take the compatibility risk on yourself.

**Q7: Can I create database tables?**<br>
There is no migration mechanism, so do not change the host schema. Write private state to `ctx.data_dir` (JSON/SQLite files are both fine); use external storage when you need to share it across machines.

**Q8: Can I run background jobs/timers?**<br>
The contract has no scheduler API. You can start your own threads, but they must be stopped in `teardown`; multiple workers will run them repeatedly; do not block for long inside `setup`.

**Q9: Why does my route default to `module:<id>:use`?**<br>
The `permission` of `ctx.route` defaults to the module's first nav permission, and then to `module:<id>:use`. To change it, pass `permission` on the decorator, but make sure that permission can actually be granted.

**Q10: Does the SDK automatically filter data by the current user's authorization?**<br>
No. In the current implementation `credentials.get()`, `hosts.list()`/`get()`, `accounts.list()`, `credentials.list()` and `sessions.list_active()` all return the full set; `permission` only controls "can the module be used", not "can this host be seen". Any asset-sensitive endpoint has to verify on the module side that the current user is authorized for the `host_id` (the host does not expose a unified current-user/authorization check in the SDK; see the notes in §1.3, and use the host internals `g.actor`/`find_access` with care, with version compatibility in mind).

**Q11: Why can't I see the module in the sidebar?**<br>
Check in turn: the module has `enabled=true`, `loaded=true`, `errors=[]`; the current account has `nav[].permission`; whether `GET /api/modules/nav` returns that entry; whether the frontend has refreshed `initialState`.

**Q12: Why are my items missing from the parameter settings?**<br>
The module is not loaded or `settings` is not declared; setting defaults are registered when the module is loaded/installed. Look at `/api/modules/<id>` and `/logs`, and refresh the page after reinstalling.

**Q13: Do static assets require login?**<br>
No. `/api/modules/<id>/asset/**` is login-free and only allows `web/`, `images/`, `README.md`. Do not put secrets there.

**Q14: Can I return files/streams/custom status codes?**<br>
`ctx.route` is only suited to returning dict/list/None + `ModuleApiError` status codes. For files/streams/custom responses use `ctx.blueprint()` + native Flask responses, and handle errors yourself according to the unified envelope convention.

**Q15: Can I add i18n?**<br>
iframe pages are entirely up to you. The `t` of js mode is the host dictionary function and is not guaranteed to contain your module's strings; ship your own dictionary or just write Chinese. The `name`/`title`/`label` of `description.json` support `{zh,en}`.

**Q16: Can I access the internet?**<br>
Yes, there is no network sandbox. Handle timeouts, proxies, TLS and retries yourself, and pay attention to compliance when sending data outside.

**Q17: Can I emit Socket.IO events?**<br>
Yes. `ctx.emit(topic, payload)` and `bastion.events.emit(topic, payload)` currently both broadcast `module:<id>:<topic>`; the topic cannot contain a colon and is at most 64 characters long. There is no standard SDK for how a module frontend subscribes; v1 recommends polling your own API.

**Q18: Does `requires` install dependencies automatically?**<br>
The contract only defines the declaration field, not the auto-install/ordering/enable semantics. Do not assume a dependency is present; probe and degrade gracefully in `setup`, or write the dependent modules into your deployment documentation.

**Q19: Can the module id be changed?**<br>
No. The id determines the URL, the data directory, the setting keys and the permission codes; changing it is the same as a new module, and the old data and authorizations are all lost.

**Q20: What does uninstalling delete?**<br>
`package/`, `state.json`, `data/`; the current implementation also deletes `mod.<id>.*` from `SystemSetting`, but the contract does not say so explicitly. Back up `data/` and the setting values you need before uninstalling, and tell the user.

**Q21: Why is `loaded=false` after a successful install?**<br>
`setup` raised an exception, `import` failed, a dependency is missing, or the `autoops` facade version does not match. Look at `errors[]` of `/api/modules/<id>` and at `/logs`.

**Q22: Can I change the behaviour of existing host endpoints?**<br>
Technically yes (monkeypatching), but do not. That would let upgrades, audit and security boundaries all run out of control; use your module's own route prefix.

**Q23: Can the module frontend get the current user's information?**<br>
The contract does not give frontend modules a dedicated SDK; an iframe can call the host's `/api/auth/me` (with the JWT), and js mode can use `sdk.request('/api/auth/me')`. Note that this depends on the stability of the host API.

**Q24: During development the frontend is on 8000 and the backend on 5000 — is that cross-origin?**<br>
In the development environment Umi usually proxies `/api`; in production it is same-origin. If you access the backend port directly, the iframe and the API may be different origins and token/fetch will both fail; follow the host's actual deployment method.

---

<a id="sec-12"></a>
## 12. hello-world end-to-end walkthrough

This chapter uses the real example in the repository, [`modules-src/hello-bastion/`](../../modules-src/hello-bastion/), to walk through "source → validation → packaging → installation → page → uninstall". This example uses **`js` mode**: the page is only responsible for the content cards, while the title, breadcrumb and experimental badge are provided by the host's `PageContainer`.

<a id="sec-12-1"></a>
### 12.1 Source file inventory

```text
modules-src/hello-bastion/
├── description.json        # metadata, nav (type=js), settings, permissions
├── modules/
│   └── __init__.py         # setup(ctx), /stats, /credentials-sample, lifecycle hooks
├── web/
│   └── index.js            # js mode entry: export mount/unmount + React.createElement + antd
├── images/
│   └── demo-1.png          # install page demo image (800×450)
└── README.md
```

| File | Purpose |
| --- | --- |
| [`description.json`](../../modules-src/hello-bastion/description.json) | `version: "1.1.0"`, `entry.web: "web/index.js"`, `nav[0].type: "js"`, 3 settings, experimental flag |
| [`modules/__init__.py`](../../modules-src/hello-bastion/modules/__init__.py) | `setup(ctx)`, `/stats`, `/credentials-sample`, async bridge, lifecycle hooks |
| [`web/index.js`](../../modules-src/hello-bastion/web/index.js) | js page: 4 antd `Card`s assembled with `React.createElement`, no JSX, no imports, no custom CSS |
| [`images/demo-1.png`](../../modules-src/hello-bastion/images/demo-1.png) | 800×450 install page demo image |
| [`README.md`](../../modules-src/hello-bastion/README.md) | Installation, API and troubleshooting notes for this example |

Mappings:

- `nav[]` → the sidebar entry "Hello Bastion"; `type: "js"`, `entry: "web/index.js"`, `path: "/"` (the full URL is `/m/hello-bastion/`); the page title, breadcrumb and "experimental" badge are rendered by the host's `PageContainer`, so the module itself must **not** draw a Hero/title again.
- `settings[]` → `show_greeting` (bool), `refresh_seconds` (int, 5–600), `greeting_text` (string).
- `/stats` → `asyncio.gather(bastion.hosts.list(), bastion.sessions.list_active())` + `ctx.get_setting()`.
- `/credentials-sample` → `await bastion.credentials.get(host_id, account_id=...)`, returning only `username` + `passwordLength`.
- `web/index.js` → uses `React`/`ReactDOM`/`antd`/`request`/`history` from `window.AutoOpsSDK`, calling the two module APIs through the host's `request`; `sdk.history.push('/system/settings')` jumps to the parameter settings, and `unmount()` cleans up the root.

<a id="sec-12-2"></a>
### 12.2 From validation to installation

A fresh instance starts with **0 modules**; `hello-bastion` is the same, it must be packaged from source and then uploaded and installed — it does not appear automatically just because `modules-src/` exists in the repository.

**Step 1: syntax/JSON validation** (no host environment required):

```powershell
cd D:\Desktop\AutoOps
python -m py_compile modules-src\hello-bastion\modules\__init__.py
python -c "import json; json.load(open(r'modules-src/hello-bastion/description.json', encoding='utf-8')); print('json ok')"
node --check modules-src\hello-bastion\web\index.js
```

`node --check` is used to confirm that `web/index.js` has no syntax errors. If an older Node version reports an error treating `export` as CommonJS, temporarily copy the file to `.mjs` and check again: `Copy-Item modules-src\hello-bastion\web\index.js "$env:TEMP\hello-check.mjs"; node --check "$env:TEMP\hello-check.mjs"`. The example has no TypeScript/JSX, so `tsc` is not needed.

**Step 2: packaging validation**:

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion --check
```

**Step 3: packaging**:

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion -o .
```

Expected artifact (repository root):

```text
hello-bastion-1.1.0.mod.ops_ext
```

**Step 4: installation**:

1. Log in to the frontend as an administrator;
2. Under "System Settings → Extension Modules", upload `hello-bastion-1.1.0.mod.ops_ext` from the repository root;
3. You should see the demo image/name/version/tags;
4. Because `experimental: true`, hand-type the confirmation phrase 我知道这是实验性功能，使用不当的话也许会造成系统损毁;
5. Installation succeeds, `hello-bastion` appears in the module list with `loaded=true`.

**Step 5: confirm permissions**:

- The super administrator (`*`) has access by default;
- A normal account needs `module:hello-bastion:use`; if the role configuration page does not yet list that permission item, verify with the super administrator first, or wait for the host to wire module permissions into the role page.

<a id="sec-12-3"></a>
### 12.3 Testing the APIs

Open "Hello Bastion" from the sidebar, or curl directly:

```bash
# total hosts / active sessions / three setting values
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/hello-bastion/api/stats
```

Expected (values vary with the environment):

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "module": { "id": "hello-bastion", "version": "1.1.0" },
    "generatedAt": "2026-10-23T15:04:05.123456+00:00",
    "hostTotal": 12,
    "activeSessions": 3,
    "settings": {
      "showGreeting": true,
      "refreshSeconds": 30,
      "greetingText": "Hello, Bastion!"
    }
  }
}
```

Credential sample (returns only the username and its length):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:5000/api/modules/hello-bastion/api/credentials-sample?host_id=1"
```

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "hostId": 1,
    "accountId": 2,
    "username": "root",
    "passwordLength": 14
  }
}
```

When `host_id=99999`:

```json
{
  "success": false,
  "code": "ERROR",
  "message": "资产不存在或当前账号不可见：host_id=99999",
  "data": null
}
```

> The example code maps "asset not found / credential read failure" to 404; production modules should distinguish 400/403/404/502 according to your own business rules, and record logs that allow troubleshooting.

<a id="sec-12-4"></a>
### 12.4 Page and settings

The host wraps the module content in `PageContainer`, with the title "Hello Bastion" and an `extra` area showing the version, official and experimental badges; `mount()` in `web/index.js` only renders content into the passed-in `el`. The page is four antd `Card`s:

1. **Run overview**: an `Alert` shows the greeting (from `greeting_text`, hidden when `show_greeting=false`), followed by 4 `Statistic`s: total hosts, active sessions, auto-refresh interval, last refresh; `extra` holds the "auto refresh every N seconds" tag and a "Refresh now" button.
2. **Module settings**: `Descriptions` shows the current values of the three settings, and a `Table` lists the types and default values of `mod.hello-bastion.show_greeting` / `refresh_seconds` / `greeting_text`; the "Go to parameter settings" button uses `sdk.history.push('/system/settings')` for in-host navigation.
3. **Async SDK demo**: two `InputNumber`s (asset ID required, account ID optional) plus a "Read credential summary" button; it calls `/credentials-sample` through the host's `request`, and the result is shown with `Descriptions` limited to `hostId`, `accountId`, `username`, `passwordLength`.
4. **About this example**: `Descriptions` shows the module id, version (from `module.version` in `/stats`), page mode, API prefix, author, data time; one button uses `sdk.history.push('/system/modules')` to open extension module management, and another calls `sdk.message.success()`.

Settings behaviour:

- After the page loads it requests `/stats` automatically; afterwards it polls according to `refresh_seconds` (minimum 5 seconds); changing and saving a setting takes effect on the next round, or you can click "Refresh now";
- Change the three values under "Parameter settings" → save → return to the module page and wait for the next polling round, or click "Refresh now";
- On 401, log in again first (the host's `request` may already redirect to login under the global policy); on 403, show "You do not have permission to use this module"; for other errors, display the `message` from the backend envelope;
- The module itself writes no CSS: no gradients, no Hero, no self-made page title; all visuals are provided by antd and the host's `PageContainer`.

<a id="sec-12-5"></a>
### 12.5 Troubleshooting and uninstall

**Check status**:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/hello-bastion
```

**Check logs**:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/hello-bastion/logs
```

**Common problems**:

- Page 401: the login session has expired, log in again; in `js` mode the host's `request` may already redirect to login under the global error policy;
- Page 403: the account lacks `module:hello-bastion:use`;
- `/stats` returns 500: check the logs; it is most likely that the host's `autoops.bastion` facade is not implemented/injected;
- `credentials-sample` 404: `host_id` does not exist, or the asset is not visible to the module;
- `python -m py_compile` passes but loading fails: check the traceback in `errors[]`; a common cause is that the export location of `from autoops import ModuleApiError` differs from the host implementation (see the open contract items in Appendix A).

**Uninstall**:

1. Extension modules page → disable → uninstall;
2. The host deletes `package/`, `state.json`, `data/`;
3. Confirm that the sidebar entry disappears;
4. To reinstall, simply upload the package again.

<a id="sec-12-6"></a>
### 12.6 Extending from this example

- Add an endpoint: add `@ctx.route("/my-endpoint")` inside `_register_routes(ctx)` in `modules/__init__.py`, and bridge the async SDK as described in §5.4;
- Add a setting: add an entry to `description.json.settings[]`, then use `ctx.get_setting("new_key", default)` in `setup`;
- Change the page: edit the `React.createElement` tree in `web/index.js` directly, adding a card following the structure of the four `Card`s; the entry stays `entry.web: "web/index.js"` and `nav[].type: "js"`, no backend change needed;
- Add a new card: first destructure the needed components from `antd`, then add `e(Card, ...)` inside `Space`; do not write CSS in the module, leave titles/layout to the host;
- Add asset-level authorization: in sensitive routes, verify the current user's authorization for `host_id` (see §1.3/Q10); do not rely only on the module-level permission;
- Add auditing: call `bastion.audit.log(...)` after key operations, and keep plaintext out of `detail`.

---

<a id="sec-a"></a>
## Appendix A: Contract clauses this white paper depends on

This white paper is written against [`CONTRACT.md`](../modules/CONTRACT.md) v1. The main dependencies are:

| Contract clause | Where this white paper uses it |
| --- | --- |
| §0 Design principles | Filesystem as the source of truth, declarative first, trusted code, static resources exempt from login, failures must be loud |
| §1 Package format | ZIP layout, directory responsibilities, install-time path/size/entry validation, no import at install time |
| §2 `description.json` | All fields, id regex/reserved words, author official determination, tags/experimental, beaker confirmation phrase, normalized object, tag colour palette |
| §3 Backend runtime | `instance/modules/<id>/{package,state.json,data}`, `init_modules` ordering and never throwing, setting key `mod.<id>.<key>`, group/type validation of `GET/PUT /api/settings` |
| §4 Python entry point | `setup`/`teardown`/`on_enable`/`on_disable`, `ctx` members, `ctx.route` wrapping and permission defaults, `ctx.blueprint`, `ctx.on`/`ctx.emit` |
| §5 SDK | All method signatures/return types, fully async, `_sync` variants, `credentials.get` auditing, `run_blocking`, `ModuleError` |
| §6 REST API | `/api/modules*` routes, permissions, error codes, static resource whitelist, logs/upload preview |
| §7 Frontend contract | iframe/js modes, `window.AutoOpsSDK`, `window.AutoOpsModules`, `/m/:moduleId/*`, install page flow |
| §8 Packaging tool | CLI, `--check`, output directory, exclusion rules, post-pack re-validation |
| §9 Official modules | The positioning of the first official module, `modules-src/rdp-gateway/`; `hello-bastion`, `rdp-gateway` and `js-demo` all use `js` mode |

**Known contract gaps** (align with the host implementation during development; this white paper makes no promises about undefined behaviour):

1. The import path of `ModuleApiError` (the current implementation exports it from the `autoops` root, consistent with the examples; the contract only states the class name, so it should be pinned down).
2. The source of the host's `APP_VERSION`, and the exact behaviour when `minBastionVersion` is not satisfied.
3. The `CredentialMeta` fields of `credentials.list()`, and the dict schema of `hosts.list()`/`accounts.list()`/`sessions.list_active()`.
4. Whether `_sync` variants cover every method, and whether the naming is strictly `<method>_sync`.
5. That `ctx.blueprint()` is collected uniformly by the loader after `setup` (the current implementation), and the rule that blueprint names are automatically prefixed with `module_<id>_`; the contract should say explicitly "do not register manually".
6. Whether `ctx.route` supports `async def` views (this white paper assumes synchronous views).
7. When `ctx.on("install")` fires, given the premise of "no import at install time".
8. How `permissions` declarations are merged into the role configuration page (how non-super administrators obtain `module:<id>:use`).
9. Whether and how upgrades (a new version with the same id) are supported; the alternative flow for `MODULE_EXISTS`.
10. Whether the `SystemSetting` row is deleted on uninstall; the current implementation deletes it, but the contract does not state this explicitly.
11. Whether `session_id` in `audit.log_command` is `SessionRecord.id` or the `sid` string (the current implementation accepts both; the contract should fix one).
12. Whether `events.emit` automatically adds the `module:<id>:` prefix (the current implementation does, consistent with `ctx.emit`, and restricts the topic characters).
13. The "currently logged-in user" context for module APIs and the ability to check authorization per asset (not currently exposed by the SDK).
14. The scope of `t` translation entries in js mode, and whether the global error/401 behaviour of `request` matches that of host pages.
15. The `data` structure of the module logs API response (an array or `{lines: [...]}`).
16. The parameter form of `ctx.on(event, handler)` (the current implementation requires passing a handler and does not support using it as a decorator), plus the return type of `ctx.set_setting` and its behaviour for undeclared keys.
17. The topic restrictions of `ctx.emit` (the current implementation forbids colons and limits the length to 64); the contract should state this.
18. The normalized nav example in contract §2 is missing `entry`; the frontend needs it to load iframe/js pages, and the current implementation keeps it, so it should be added to the contract.

---

<a id="sec-b"></a>
## Appendix B: Pre-release self-check checklist

**Package and metadata**

- [ ] `description.json` parses with `json.load`, is UTF-8, and has no BOM;
- [ ] `id` matches `^[a-z][a-z0-9-]{2,39}$` and is not a reserved word;
- [ ] `version` is strict semver and is greater than the previous version;
- [ ] `name`/`nav[].title`/`settings[].label` have `{zh, en}`;
- [ ] `entry.web` / `nav[].entry` are both under `web/`;
- [ ] `images` are all under `images/` and the files exist;
- [ ] The experimental flag matches reality, and the confirmation phrase text is correct;
- [ ] There are no typos in `permissions`/`requires`.

**Code**

- [ ] `python -m py_compile modules/__init__.py` passes;
- [ ] Every SDK call is `await`ed, or the `_sync` variant is used in a synchronous context;
- [ ] No synchronous DB/network/`time.sleep` inside `async def`;
- [ ] `setup` is fast and idempotent; `teardown` stops threads and closes connections;
- [ ] Route permissions are explicit, and sensitive endpoints have asset-level authorization checks;
- [ ] Credential plaintext is not written to logs, returned to the frontend, or persisted to disk;
- [ ] Sensitive operations are audited, and `detail` contains no secrets;
- [ ] Background threads can be stopped on disable/uninstall.

**Frontend**

- [ ] **`js` mode (recommended) uses only the host-injected antd**: no custom CSS, no gradients/Hero, no self-made page title; the title, breadcrumb, official and experimental badges are provided by the host's `PageContainer`;
- [ ] In js mode, `mount/unmount` clean up timers/events/root, and `unmount()` can be called repeatedly;
- [ ] In js mode the module does not bundle React/ReactDOM/antd; all runtime pieces come from `window.AutoOpsSDK`;
- [ ] In-page navigation uses `sdk.history.push()`, not a full-page refresh via `window.location` (except for the login redirect);
- [ ] 401 shows a readable hint or is left to the global policy of the host's `request`; 403 shows a readable reason;
- [ ] Dynamic text uses `textContent`, with no `innerHTML` injection;
- [ ] There are no tokens, secrets or internal addresses under `web/`.
- [ ] **`iframe` mode only**: relative resource paths are correct, APIs use absolute `/api/modules/<id>/api/...`; on 401 redirect the top-level page to login instead of loading the login page only inside the iframe.

**Release**

- [ ] `pack_module.py --check` passes;
- [ ] The packaged artifact installs, enables, is used, is disabled and is uninstalled successfully on the target host version;
- [ ] `GET /api/modules/<id>` returns `loaded=true, errors=[]`;
- [ ] The release SHA-256 and installation instructions are provided;
- [ ] A note that uninstall deletes `data/` is written in the README.
