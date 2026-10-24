# rdp-gateway

`rdp-gateway` is an official experimental AutoOps extension. A Windows user can
connect `mstsc` to the bastion, sign in with a bastion account, and select an
authorized RDP target. Every target connection is recorded and audited.

- Version: `1.2.2` | Author: `AutoOps` | Experimental
- Route: `/m/rdp-gateway/rdp-gateway`

The gateway is a separate Rust process managed by the Python extension; the Flask host handles login, ACLs, session records, and audit.

## How a connection works

1. Start the gateway from the module page or enable `autoStart`.
2. On Windows, run `mstsc /v:<bastion-ip>:<port>`.
3. Sign in with your bastion account in the gateway session.
4. Select a target host by its number.

The Targets card is read-only, with no launch button. There is no direct-connect
channel: every session passes through the gateway for recording and audit.

## Requirements

- A Windows host for `bastion-rdp-gateway.exe`; administrator rights are needed
  to install, start, or stop it.
- RDP assets and accounts granted through the normal ACL model.
- A reachable TCP port; the default is `4489`.

## Package layout

```text
rdp-gateway/
|-- description.json       Manifest: nav, settings, permissions
|-- modules/               Python lifecycle, targets, sessions, browser bridge
|-- web/index.js           js-mode page using host React and antd
|-- images/demo-1.png      Installation preview image
|-- bin/bastion-rdp-gateway.exe
`-- README.md, README.en.md
```

## Build and install

From the repository root:

```powershell
python bastion-backend\tools\pack_module.py modules-src\rdp-gateway -o .
```

This creates `rdp-gateway-1.2.2.mod.ops_ext` in the repository root; `--check`
validates without producing a package.

1. Sign in as an administrator and open System Settings > Extension Modules.
2. Click Install Extension Module. A new window opens.
3. Click Choose .mod.ops_ext File, select the package, and review its details.
4. Click Install, type the experimental confirmation phrase, and confirm. A new instance starts with zero modules.

## Settings

Settings live under `mod.rdp-gateway.*`; edit them in System Settings > Parameters or on the module page.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `enabled` | bool | `1` | Off stops the process and blocks starts |
| `port` | int | `4489` | 1 to 65535; restart to apply |
| `autoStart` | bool | `0` | Start when the module loads |

## Page and operations

The host `PageContainer` supplies the title. Five cards cover status, targets,
settings, logs, and quick actions. Administrators can start, stop, and diagnose;
users with `rdp:use` can view status, copy the `mstsc` command, and read logs.
Gateway output goes to `data/gateway.log` and the module log ring buffer.

## Module API

Routes are under `/api/modules/rdp-gateway/api` and require `rdp:use`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status`, `/hosts` | State/connect command; authorized assets |
| GET/POST | `/settings` | Read or update settings |
| POST | `/start`, `/stop`, `/sessions` | Control gateway; audited session |
| GET | `/logs`, `/diagnose` | Log tail and diagnostics |

Start, stop, settings changes, and diagnostics require administrator privileges (`403 ADMIN_ONLY` otherwise).

## Internationalization

The host supports Chinese and English. Wrap user-visible text in
`sdk.t("Chinese source text")`; English resolves it in `ant-design-pro/src/i18n/dict/en-US.json`.

```js
const tr = typeof sdk.t === "function" ? sdk.t : (zh) => zh;
const title = tr("Gateway status");
const message = tr("Loaded {count} targets", { count: 4 });
```

Missing entries fall back to Chinese, so add new strings to the host dictionary.
Use `sdk.isEnglish()` for language-specific formatting; a switch reloads the page.

## Uninstall and limits

Uninstalling stops the process tree, closes browser bridges, and deletes the
module directory, logs, and `mod.rdp-gateway.*` settings. There is no tombstone or
one-click restore; upload the same package again to reinstall.

The gateway is Windows-only; credentials are available only at connection setup,
and the target list is an overview, not a launcher.

Python extensions run with full bastion privileges. Install only trusted packages.
