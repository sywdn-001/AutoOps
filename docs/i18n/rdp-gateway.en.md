# RDP gateway (point mstsc straight at the bastion)

> Use Windows' built-in Remote Desktop client (`mstsc`) against the bastion's **4489** port, sign in
> with your **bastion account**, pick a host you are allowed to reach, and that machine's desktop is
> streamed into the same window — nothing to install, and the operator never sees the target
> credential.
>
> 中文版 / Chinese: [../RDP网关.md](../RDP网关.md)

> **Module note**: the RDP gateway is now an official extension module (`rdp-gateway`, packaged as
> `.mod.ops_ext`). The host no longer ships the `/api/rdp-gateway/*` blueprint or the old
> "System settings → RDP gateway" page. The gateway process is supervised by the module, the module
> API prefix is `/api/modules/rdp-gateway/api`, and the module page is mounted by the host under
> "Extension modules".

---

## 1. What problem it solves

WebRDP runs a wasm RDP client inside the browser; in many intranet scenarios the operator only has
Windows' own `mstsc`, or policy forbids browser extensions. The RDP gateway exists for exactly that:

| Aspect | WebRDP | RDP gateway (this document) |
| --- | --- | --- |
| Client | browser (wasm decoding) | Windows' built-in `mstsc` |
| Target credential | decrypted by the bastion and handed to the browser (with a dedicated audit) | **stays inside the bastion process**; the client never gets it |
| Choosing a host | web page list | a menu **drawn by the gateway itself** inside the session (arrow keys / Enter / mouse) |
| Audit | session + credential reveal + recording | session + credential reveal + login/host selection (recording: see limitations) |

## 2. Architecture

```
mstsc ──TLS (RDP)──► bastion-rdp-gateway:4489 ──RDP (TLS+NLA)──► target host :3389
                          │
                          ├─ sign-in page: bastion credentials → JWT (POST /api/auth/login)
                          ├─ menu:          GET  /api/modules/rdp-gateway/api/hosts (same authorization as the web UI)
                          └─ select:        POST /api/modules/rdp-gateway/api/sessions → address/port/account/password (audited)
```

- **Server side**: `bastion-rdp-gateway/` (Rust, `ironrdp-server` 0.13). To `mstsc` it *is* an RDP
  server: X.224 → MCS → TLS → licensing → graphics channel; every screen is drawn by the process
  itself (`font8x8` ASCII rendered into a bitmap).
- **Client side**: the same process uses `ironrdp-client` (vendored) to connect to the target on 3389,
  blits the incoming frames into its own 1280×800 framebuffer and sends keyboard/mouse back as
  FastPath input events.
- **Same authorization as the web side**: menu data comes from
  `accessible_targets(protocols=("rdp",))` — the very same "grant + time window + quota + interactive
  login allowed" decision used by WebRDP and the SSH gateway.

## 3. Using it (three steps)

1. **Install and start the module** (either way)

   ```powershell
   # (1) Upload modules-dist\rdp-gateway-<version>.mod.ops_ext under
   #     "System settings → Extension modules", tick the experimental confirmation and enable it;
   #     then open "Extension modules → RDP gateway" and click "Start" (recommended: status and logs in the UI)
   # (2) from a shell:
   cd bastion-rdp-gateway
   $env:RDP_GATEWAY_PORT = "4489"
   $env:BASTION_INSTANCE_DIR = "..\bastion-backend\instance"
   $env:BASTION_API_BASE = "http://127.0.0.1:5000"   # change when the backend is elsewhere
   .\target\release\bastion-rdp-gateway.exe
   ```

2. **Connect**

   ```
   mstsc /v:<bastion-host>:4489
   ```

   The first time you will see a certificate warning (the gateway uses a self-signed certificate at
   `bastion-backend/instance/rdp-gateway-cert.pem`); accept it.

3. **Sign in, then pick a host**

   ```
   AutoOps Bastion - RDP Gateway
   Username: <bastion account>    (Tab switches fields, Enter signs in)
   Password: ********
   ```

   After signing in you get the menu: `↑↓` to move, `Enter` to connect, mouse click selects the row
   under the pointer; `Esc` returns from the target desktop to the menu.

## 4. Permissions and accounts

- The account needs the `rdp:use` permission (built-in "Operations engineer" role and above have it),
  otherwise the menu will be empty.
- The host must be configured with the **rdp** protocol (endpoint port defaults to 3389 and can be
  changed per host endpoint), and the account needs a grant with "allow interactive login" checked.
- The target credential is stored on the host account and managed by the bastion; when the gateway
  uses it, an `rdp_credential_reveal` audit entry is written.

## 5. Configuration

| Location | Key / variable | Default | Meaning |
| --- | --- | --- | --- |
| Module page → module settings | `mod.rdp-gateway.enabled` | off | whether the RDP gateway is enabled |
| Module page → module settings | `mod.rdp-gateway.port` | 4489 | listening port |
| Module page → module settings | `mod.rdp-gateway.autoStart` | off | start the gateway process when the module is enabled |
| Gateway process | `RDP_GATEWAY_PORT` | 4489 | same, for command-line starts |
| Gateway process | `BASTION_INSTANCE_DIR` | `../bastion-backend/instance` | where certificate/logs live |
| Gateway process | `BASTION_API_BASE` | `http://127.0.0.1:5000` | backend address (`BASTION_API_PORT` also works) |
| Gateway process | `BASTION_API_PREFIX` | `/api/modules/rdp-gateway/api` | module API prefix (injected by the module) |
| Gateway process | `RUST_LOG` | `info` | log level; use `trace` to see individual key/mouse events |

After changing the port, stop and start the gateway so it re-binds.

## 6. Audit trail

| action | when it is written |
| --- | --- |
| `rdp_gateway_start` / `rdp_gateway_stop` | an administrator starts/stops the gateway from the UI |
| `login` | somebody signs in on the gateway sign-in page (same path as the web login) |
| `rdp_gateway_open` | a host was selected and a session row was opened (`source=gateway, protocol=rdp`) |
| `rdp_credential_reveal` | the gateway uses a target account's password (who, when, which host/account) |
| `rdp_gateway_close` | returning from the target desktop to the menu |

## 7. Reading the logs

- Connection lifecycle: `RDP client connected / disconnected` with peer address, duration and error;
- Session isolation: every connection logs "session state cleared (not signed in)" — **the previous
  user's login state is never inherited**;
- Input: individual key/mouse events are `trace`; `info` only carries a summary line every 30 events;
- Backend calls: `debug`, including method/path/HTTP status/duration/bytes;
- Frame pump: one line every 30 s (keys, mouse events, frames).

Log file: `bastion-backend/instance/modules/rdp-gateway/data/gateway.log` (when started from the
module page; the page's log panel and `GET /api/modules/rdp-gateway/api/logs` read it directly).

## 8. Building it yourself

```powershell
cd bastion-rdp-gateway
cargo build --release          # output: target\release\bastion-rdp-gateway.exe
```

Two environment notes we already hit:

1. `aws-lc-sys` does not compile on this machine (`LNK1104`), so `tokio-rustls` and `ironrdp-client`
   are **vendored** under `bastion-rdp-gateway/vendor/` and wired with `[patch.crates-io]`; the crypto
   provider is pinned to `ring` (the `rustls-ring` feature of `ironrdp-tls`). Do not switch back to the
   default `rustls` provider;
2. `RdpClient::run()`'s future is not `Send`, so the client and the frame pump each run on their own
   **current-thread runtime**; do not move them back into `tokio::spawn`.

## 9. Known limitations

- **Recording is not wired yet**: only the session row and the audit entries exist today; frames and
  input are not yet written into a replayable recording;
- **Single client**: one gateway process serves one `mstsc` connection at a time (concurrent clients
  would share the same session state — being fixed);
- **No clipboard**: copy/paste between local and target is not wired (the client's `clipboard` feature
  is off);
- **Single-session targets**: if the target only allows one session (regular Windows client), connecting
  through the gateway kicks the existing session — that is the target's policy, not gateway behaviour;
- `Esc` belongs to the gateway (returns to the menu) and is not forwarded to the target.

## 10. Acceptance run (measured on this machine)

1. `mstsc /v:127.0.0.1:4489` → accept the self-signed certificate → sign-in page;
2. sign in with a bastion account → the menu lists the hosts you may reach;
3. `Enter` → the target's Windows Server desktop appears, with correct colours (blue wallpaper, orange
   buttons);
4. `Win+R` opens Run **on the target**; typing `notepad` lands the characters in the target's focused
   window;
5. clicking "Cancel" in the target's Run dialog closes it; double-clicking the desktop Recycle Bin
   opens it;
6. disconnecting and reconnecting returns to the sign-in page (no inherited login state);
7. the audit log shows `login`, `rdp_gateway_open` and `rdp_credential_reveal`.

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| The page says "not running" while the process clearly is | press Refresh; the "Diagnostics" card shows the paths/port the backend computed |
| "Cannot read gateway status" | the backend is an older process (restart it), or the account lacks `rdp:use` |
| Sign-in keeps failing | wrong bastion password, or the backend is not on `127.0.0.1:5000` (set `BASTION_API_BASE`) |
| The menu is empty | the account has no grant for an rdp host, or the host has no rdp endpoint |
| Connecting to the target fails | port 3389 unreachable / bad credential / the target allows only one session |
| Colours look yellow or smeared | outdated build — use one with the R/G/B channel fix |
