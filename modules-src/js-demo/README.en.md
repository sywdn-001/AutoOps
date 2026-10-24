# js-demo

`js-demo` is an official AutoOps extension module and the smallest possible
`js`-mode page. It proves that a module can reuse the host React, ReactDOM,
antd, request, token, message, and router runtime without a build step.

- Module ID: `js-demo`
- Version: `1.0.1`
- Author: `AutoOps`
- Route: `/m/js-demo/ui`
- Experimental: no

## What it demonstrates

- An ES module with no `import` statements and no bundler.
- Registration through `window.AutoOpsModules['js-demo']`.
- `mount(el, sdk)` / `unmount()` lifecycle and idempotent cleanup.
- A health request through the host `request` wrapper.
- An antd Switch-controlled timer and host `message.success()` call.

## Package layout

```text
js-demo/
|-- description.json       Manifest: id, nav, js entry, permissions
|-- web/index.js           No-import ESM with mount() and unmount()
|-- README.md              Chinese README
`-- README.en.md           This file
```

The browser entry is a single ES module. The host injects it with a
`<script type="module">` tag when the route opens and removes it on leave.

## Runtime contract

The host builds `window.AutoOpsSDK` first, loads the module script, then looks up
the module handle by ID. The entry must export the lifecycle functions and also
register the same object globally:

```js
window.AutoOpsModules = window.AutoOpsModules || {};
window.AutoOpsModules["js-demo"] = { mount, unmount };
```

`mount(el, sdk)` may be async and may await `sdk.antd`; the host does not await
it. Render only inside the supplied `el`. `unmount()` must tolerate being called
twice and must clear timers and the React root.

## Build and install

From the repository root:

```powershell
python bastion-backend\tools\pack_module.py modules-src\js-demo -o .
```

This creates `js-demo-1.0.1.mod.ops_ext` in the repository root. Use `--check`
instead of `-o .` to validate the package only.

1. Sign in as an administrator.
2. Open System Settings > Extension Modules.
3. Click Install Extension Module. A new window opens.
4. In that window, click Choose .mod.ops_ext File and select the package.
5. Review the parsed metadata and README, then click Install.
6. Open JS Mode Demo from the sidebar.

The host starts with zero modules. This sample is not experimental, so no typed
confirmation is required, but it must still be uploaded before use.

## Page behavior

- It calls `GET /api/health` and shows `service`, `gateway`, and `port`.
- It displays only whether `sdk.getToken()` is present, never the token.
- A Switch starts and stops a counter that increments once per second.
- A button calls the host `message.success()` to prove the App context is shared.
- All content stays inside the mount element and uses antd cards and controls.

## Internationalization

The host UI supports Chinese and English. Put every user-visible string through
`sdk.t("Chinese source text")`. Chinese returns the source; English resolves the
source string through `ant-design-pro/src/i18n/dict/en-US.json`.

```js
const tr = typeof sdk.t === "function" ? sdk.t : (zh) => zh;
const title = tr("Host runtime demo");
const label = tr("Requests: {count}", { count: 1 });
```

Missing entries fall back to Chinese, so add new strings to the host dictionary.
Use `sdk.isEnglish()` for language-specific formatting; a switch remounts the module.

## Extend the sample

Add another antd `Card` inside the existing `Space` and call `sdk.history.push()`
for in-app navigation. Do not add custom CSS, gradients, or a hero section; the
host `PageContainer` owns the page title and visual frame. Reuse `sdk.request`
for host APIs so authentication and error handling stay consistent.

## Validate and uninstall

```powershell
node --check modules-src\js-demo\web\index.js
python bastion-backend\tools\pack_module.py modules-src\js-demo --check
```

Uninstalling removes the module directory and its settings. There is no tombstone
or one-click restore; upload the same `.mod.ops_ext` again to reinstall.

Python extensions, when present, run with full bastion privileges. Install only trusted packages.
