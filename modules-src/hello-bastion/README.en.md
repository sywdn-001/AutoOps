# hello-bastion

`hello-bastion` is an official AutoOps extension module and the smallest
end-to-end `js`-mode example. It adds a sidebar page, exposes settings, calls the
host SDK asynchronously, and returns a credential summary without leaking secrets.

- Module ID: `hello-bastion`
- Version: `1.1.1`
- Author: `AutoOps`
- Route: `/m/hello-bastion/`
- Experimental: yes

## What it demonstrates

- A sidebar entry registered with `nav[].type: "js"`.
- `bool`, `int`, and `string` settings under System Settings > Parameters.
- Async `autoops.bastion` calls from a Flask module route.
- Credential access reduced to a username and password length.
- A page built only from host antd components and `window.AutoOpsSDK`.

## Package layout

```text
hello-bastion/
|-- description.json       Manifest: nav, settings, permissions
|-- modules/__init__.py    Python extension: setup(ctx) and two GET routes
|-- web/index.js           js-mode entry: export mount/unmount
|-- images/demo-1.png      Installation preview image (800x450)
`-- README.md / README.en.md
```

A `.mod.ops_ext` file is a ZIP archive whose root contains `description.json`.
After installation, the host loads `modules/` in Python and `web/` in the browser.

## Build and install

Run from the repository root:

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion -o .
```

This writes `hello-bastion-1.1.1.mod.ops_ext` to the repository root. Add
`--check` instead of `-o .` to validate without packing. The host never scans
`modules-src/` at runtime; official packages are distributed from the repo root.

1. Sign in as an administrator and open System Settings > Extension Modules.
2. Click Install Extension Module. A new window opens.
3. Click Choose .mod.ops_ext File and select the package.
4. After parsing, review the metadata and README, then click Install.
5. Type the experimental confirmation phrase, confirm, and open Hello Bastion.

A fresh instance starts with zero modules, so official samples also need uploading.

## Page and settings

`PageContainer` provides the title and badge; the module renders four cards:
overview, settings, async SDK demo, and about.

Settings are stored as `mod.hello-bastion.<key>`:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `show_greeting` | bool | `1` | Shows or hides the greeting Alert |
| `refresh_seconds` | int | `30` | Polling interval, 5 to 600 seconds |
| `greeting_text` | string | `Hello, Bastion!` | Greeting shown on the page |

Read values with `ctx.get_setting("refresh_seconds", "30")`; convert the string.

## Module APIs

Routes are mounted under `/api/modules/hello-bastion/api` and require
`module:hello-bastion:use`. `GET /stats` returns counts and settings via SDK calls:

```python
hosts, sessions = await asyncio.gather(
    bastion.hosts.list(),
    bastion.sessions.list_active(),
)
```

`GET /credentials-sample?host_id=1` calls
`await bastion.credentials.get(host_id, account_id=account_id)` and returns only
`username` and `passwordLength`. The password stays in process memory and is
never logged or sent to the browser. Missing or unauthorized hosts produce a
readable 4xx error envelope.

## Internationalization

The host UI supports Chinese and English. Wrap user-visible text in
`sdk.t("Chinese source text")`. Chinese returns the source unchanged; English
looks up the source string in `ant-design-pro/src/i18n/dict/en-US.json`.

```js
const tr = typeof sdk.t === "function" ? sdk.t : (zh) => zh;
const title = tr("Read credential summary");
const label = tr("Processed {count} hosts", { count: 12 });
```

Missing entries fall back to Chinese, so add new strings to the host dictionary.
Use `sdk.isEnglish()` only for real language branches, such as date formatting;
a language switch reloads the page, so `mount()` need not react to it.

## Uninstall

Uninstalling deletes the module directory, its private `data/` directory, and all
`mod.hello-bastion.*` settings. There is no tombstone or one-click restore; upload
the same package again to reinstall.

Python extensions run with full bastion privileges. Install only trusted packages.
