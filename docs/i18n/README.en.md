<h1 align="center">AutoOps Bastion</h1>

<p align="center">An open-source bastion host that governs who can reach which machine, with which account, and which commands</p>

<p align="center">
  <a href="../../LICENSE"><img src="https://img.shields.io/github/license/sywdn-001/AutoOps?color=blue" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/tests-705%20passed-brightgreen" alt="Backend tests: 705 passed">
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white" alt="Flask 3.1">
  <img src="https://img.shields.io/badge/React%2019%20%2B%20Ant%20Design%206-61DAFB?logo=react&logoColor=white" alt="React 19 + Ant Design 6">
  <img src="https://img.shields.io/github/last-commit/sywdn-001/AutoOps" alt="Last commit">
</p>

<p align="center">
  <a href="../../README.md">中文</a> · <b>English</b>
</p>

> **About this project**: this repository is built **by a human and an AI working together** — the human
> sets the requirements, makes the trade-offs and accepts the result, while the code is written, gated
> and verified on real machines by both. You will find hand-written implementations next to
> AI-assisted ones, refactors and debugging. So that either of us can pick up a file quickly, every
> source file starts with an **AI-generated file summary** describing what the file is responsible for,
> which modules it talks to, and which traps it already knows about — you can understand any file before
> deciding to change it. Coverage is a measured 100% across the 147 files we build together
> (`bastion-backend/app`, `bastion-backend/tools`, `bastion-backend/tests`,
> `ant-design-pro/src/pages/bastion`, `ant-design-pro/src/components/Bastion`,
> `ant-design-pro/src/services/bastion`, plus the shell files adapted here: `src/app.tsx`,
> `src/app.test.tsx`). The leftover upstream `ant-design-pro` template pages and locale bundles are
> untouched.

## Features

- **Identity and access audit** — users, roles, hosts, host accounts and grants are all stored per row: who may reach which machine, with which account, and whether they can open a web terminal or only review recordings afterwards.
- **Command-level policy control** — describe what may and may not be typed with regexes and risk levels; high-risk commands can require an administrator to confirm on the spot, and both allows and denials are audited.
- **SSH gateway** — `ssh -p 2222 <bastion-account>@<host>` drops you into an audited shell that lists only the machines you are allowed to reach, and records every command and its output.
- **Web terminal (Linux / Windows)** — SSH and WinRM straight from the browser, no local client; `cls` clears locally inside a WinRM session and early keystrokes are not lost.
- **Windows Remote Desktop (WebRDP)** — open RDP in the browser, including the forced-SSL fallback for legacy systems (Server 2003 / XP); recordings stream while they happen, so a forcibly closed window loses nothing.
- **SFTP file manager** — browse, upload, download, rename, delete, chmod and archive, all under the same grants and policies as the terminal.
- **AI operations assistant** — describe a task in natural language; the channel is picked per host endpoint (Linux → SSH, Windows → WinRM), sensitive actions require the administrator password, and every tool call is audited.

## Screenshots

> Nothing here is mocked up: `bastion-backend/tools/ui_shots.py` drives a real Chrome (over CDP), signs in to the local service, actually connects to the demo target and to `win-75`, and shoots each page. Re-run `python -u tools/ui_shots.py` after a change to refresh every image. Click an image for the full-size file.

<table align="center">
  <tr>
    <td align="center" width="50%">
      <a href="../screenshots/dashboard.png"><img src="../screenshots/dashboard.png" width="100%" alt="Dashboard" /></a>
      <br /><sub><b>Dashboard</b> — assets, live sessions, risk and recent audit at a glance</sub>
    </td>
    <td align="center" width="50%">
      <a href="../screenshots/multiproto-launcher.png"><img src="../screenshots/multiproto-launcher.png" width="100%" alt="Web terminal launcher" /></a>
      <br /><sub><b>Web terminal launcher</b> — one row per host, one button per permitted protocol</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="../screenshots/multiproto-hosts-list.png"><img src="../screenshots/multiproto-hosts-list.png" width="100%" alt="Host list" /></a>
      <br /><sub><b>Host list</b> — one machine with several protocol endpoints; same-address records merge in one click</sub>
    </td>
    <td align="center" width="50%">
      <a href="../screenshots/web-rdp-win75.png"><img src="../screenshots/web-rdp-win75.png" width="100%" alt="Windows Remote Desktop" /></a>
      <br /><sub><b>Windows Remote Desktop</b> — a real WebRDP session to <code>win-75</code>, recording in progress</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="../screenshots/file-manager.png"><img src="../screenshots/file-manager.png" width="100%" alt="SFTP file manager" /></a>
      <br /><sub><b>SFTP file manager</b> — browse, upload, download, chmod; same grants and policies as the terminal</sub>
    </td>
    <td align="center" width="50%">
      <a href="../screenshots/ai-console.png"><img src="../screenshots/ai-console.png" width="100%" alt="AI operations assistant" /></a>
      <br /><sub><b>AI operations assistant</b> — plain-language requests, with an admin confirmation for sensitive tools</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="../screenshots/ssh-gateway-menu.png"><img src="../screenshots/ssh-gateway-menu.png" width="100%" alt="SSH gateway menu" /></a>
      <br /><sub><b>SSH gateway menu</b> — after <code>ssh -p 2222</code>, only the hosts you may reach</sub>
    </td>
    <td align="center" width="50%">
      <a href="../screenshots/audit-chain-detail.png"><img src="../screenshots/audit-chain-detail.png" width="100%" alt="Tamper-evident audit" /></a>
      <br /><sub><b>Tamper-evident audit</b> — a hash chain over command logs; editing one entry shows up immediately</sub>
    </td>
  </tr>
</table>

> All 40+ real-machine screenshots live in [`docs/screenshots/`](../screenshots/); each one maps to an entry under “Verification” below.


## Quick Start

All you need is **Python ≥ 3.11** (3.13 in development) and **Node ≥ 20** (only to build the frontend). Every piece of data stays in local files — no external database required.

```bash
# 1) Backend: first start creates the database, three keys and the admin admin / admin123
cd bastion-backend
python -m pip install -r requirements.txt
python run.py

# 2) Frontend (in a second terminal)
cd ant-design-pro
npm install
npm run dev            # dev server on http://localhost:8000, /api and /socket.io already proxied
```

| Entry point | Address |
| --- | --- |
| Admin UI | `http://127.0.0.1:8000` (production: `http://<server-ip>:5000`) |
| API / health check | `http://127.0.0.1:5000/api/health` |
| SSH gateway | `ssh -p 2222 <bastion-account>@<server-ip>` |
| Default administrator | `admin` / `admin123` (**forced password change on first login**) |

> No Linux box at hand? `python tools/demo_ssh_target.py` starts a fake Linux with a **real SSH protocol stack** (`127.0.0.1:2200`, account `root` / `s3cret`). Add it as an asset to walk through the web terminal, the SSH gateway, command policy denials and audit storage. For production run `npm run build`; the backend serves the resulting `dist/` on its own port.

## Deploy to Production (Linux)

Requirements: **Ubuntu 22.04+ / Debian 12+ / RHEL 9+**, Python ≥ 3.11, Node ≥ 20 (frontend build only), and two open ports: **5000** (web/API) and **2222** (SSH gateway). Create a dedicated system account:

```bash
sudo useradd -r -m -d /opt/autoops -s /bin/bash bastion
sudo -iu bastion git clone git@github.com:sywdn-001/AutoOps.git /opt/autoops/app
```

**1) Backend dependencies (virtualenv)**

```bash
cd /opt/autoops/app/bastion-backend
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

**2) Frontend build** (served by the backend on the same port — no extra web server needed)

```bash
cd /opt/autoops/app/ant-design-pro
npm ci
npm run build          # produces dist/, mounted automatically at backend start
```

**3) Production settings** (environment variables — put them in the systemd unit or `/etc/autoops.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `BASTION_HOST` / `BASTION_PORT` | `0.0.0.0` / `5000` | Web and API listen address and port |
| `BASTION_GATEWAY_HOST` / `BASTION_GATEWAY_PORT` | `0.0.0.0` / `2222` | SSH gateway address and port (`BASTION_GATEWAY_ENABLED=0` disables it) |
| `BASTION_ADMIN_USERNAME` / `BASTION_ADMIN_PASSWORD` | `admin` / `admin123` | Administrator created on first start — **change before going live** (the UI also forces a change) |
| `BASTION_INSTANCE_DIR` | `bastion-backend/instance` | Data directory: SQLite database, the three keys, session recordings |
| `BASTION_DB_URI` | `sqlite:///<INSTANCE_DIR>/bastion.db` | Point at PostgreSQL etc. (bring your own driver) |
| `BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY` | randomly generated on first start | Session signing, JWT, asset-secret encryption; **multi-instance deployments must share one set** |
| `BASTION_SERVE_FRONTEND` / `BASTION_FRONTEND_DIST` | `1` / auto-detected `ant-design-pro/dist` | Disable static serving or point elsewhere |
| `BASTION_TOKEN_HOURS` | `12` | Login token lifetime (hours) |
| `BASTION_LOGIN_MAX_FAILURES` / `BASTION_LOGIN_LOCK_MINUTES` | `5` / `15` | Failed-login lockout policy |
| `BASTION_SESSION_IDLE_TIMEOUT` | `1800` | Session idle limit (seconds) |
| `BASTION_COMMAND_TIMEOUT` / `BASTION_MAX_OUTPUT_BYTES` | `60` / `262144` | Per-command timeout and output truncation |
| `BASTION_GATEWAY_MAX_SESSIONS` | `5` | Concurrent gateway sessions per user |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `AI_MODEL` | empty / `https://api.deepseek.com` / `deepseek-flash` | Model credentials for the AI assistant (`AI_ENABLED=0` turns it off) |
| `BASTION_CORS_ORIGINS` | `*` | Tighten to your frontend origin when deploying separately |

**4) Run under systemd** (`/etc/systemd/system/autoops.service`)

```ini
[Unit]
Description=AutoOps Bastion
After=network-online.target

[Service]
User=bastion
WorkingDirectory=/opt/autoops/app/bastion-backend
EnvironmentFile=/etc/autoops.env
ExecStart=/opt/autoops/app/bastion-backend/.venv/bin/python run.py --host 0.0.0.0 --port 5000 --gateway-port 2222
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now autoops
sudo systemctl status autoops
curl -fsS http://127.0.0.1:5000/api/health      # {"success":true,...}
```

**5) Three things to do right after going live**

1. Open `http://<server-ip>:5000`, sign in as the initial administrator and **change the password immediately** (forced).
2. Tighten concurrency, idle timeout and command timeout under *System settings → System parameters*, and create roles and grants for your operators under *Identity & access* — do not use the super-admin account for daily work.
3. Back up exactly two things: `$BASTION_INSTANCE_DIR` (SQLite + three keys + recordings) and the `BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY` values in `.env`. **Lose the keys and the stored asset credentials can never be decrypted again.**

**6) Upgrade**

```bash
cd /opt/autoops/app
git pull
cd bastion-backend && .venv/bin/python -m pip install -r requirements.txt
cd ../ant-design-pro && npm ci && npm run build
sudo systemctl restart autoops
```

The schema is aligned additively by `ensure_schema()` at startup (including the multi-protocol endpoint backfill), so no migration script is needed and no data is dropped.

**7) HTTPS reverse proxy** (optional but recommended; **WebSocket passthrough is mandatory** — the web terminal depends on it)

```nginx
server {
    listen 443 ssl http2;
    server_name bastion.example.com;
    ssl_certificate     /etc/letsencrypt/live/bastion.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bastion.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 3600s;      # the web terminal is a long-lived connection
    }
}
```

The SSH gateway is not HTTP: users connect with `ssh -p 2222 <account>@<server-ip>`. To use the standard port 22, forward it to 2222 with your firewall.

## Usage

**1) Open an audited terminal in the browser** — *Web terminal* → pick a host and account → *Connect*; every command and its output lands in the audit trail.

```console
$ whoami
opsadmin
$ cat /etc/shadow
⛔ Blocked by command policy: matched the “no reading password files” rule (risk level high)
```

**2) Use the SSH gateway from the command line** — log in with your **bastion account**; the menu lists only the hosts you may reach:

```bash
ssh -p 2222 opsadmin@127.0.0.1
# Welcome opsadmin, available hosts:
#   1) e2e-demo-01   127.0.0.1:2200   Linux
# Choose host > 1
```

**3) Let the AI assistant run a command** — describe the task in *AI operations*; when the model calls `run_command` the channel is chosen per host endpoint, and sensitive actions ask for the administrator password:

```console
> show me the hostname and OS version of win-75
[Bastion] The AI requested 1 sensitive action; administrator confirmation required: run_command {"hostId": 5, ...}
Administrator password: ********
WIN-930NKGJCOED
Microsoft Windows Server 2025 Datacenter
```

**4) Verify the audit chain** — command, file and AI tool-call streams all carry `prev_hash` / `entry_hash`; recomputing offline exposes tampering:

```bash
cd bastion-backend
python tools/verify_audit_chain.py
# all three streams recomputed entry by entry → chain intact
```

## Tech Stack

| Layer | Choices |
| --- | --- |
| Backend | Python 3.13 · Flask 3.1 · Flask-SQLAlchemy / SQLAlchemy 2.0 · Flask-JWT-Extended · Flask-SocketIO (threading + simple-websocket) |
| Connectivity | paramiko (SSH / SFTP) · pywinrm (WinRM) · in-house RDP gateway (WebRDP, decoded in the browser) |
| Frontend | React 19 · Ant Design 6 · Ant Design Pro 6 / Umi Max 4 · Biome · TypeScript strict · vitest |
| Storage | SQLite by default (`BASTION_DB_URI` swaps in PostgreSQL etc.) · Fernet-encrypted asset secrets · file-based session recordings |

## Project Structure

```text
AutoOps/
├── bastion-backend/            # Backend: API + SSH gateway + web terminal + AI ops
│   ├── app/
│   │   ├── api/                # REST blueprints (auth, assets, grants, policies, audit, terminal, RDP…)
│   │   ├── gateway/            # SSH gateway (audited shell on port 2222)
│   │   ├── webterm/ terminal/  # Web terminal (Socket.IO events + sessions)
│   │   ├── files/              # SFTP file manager (service layer + policy)
│   │   ├── rdp/                # WebRDP gateway and recordings
│   │   ├── winrm/              # WinRM character sessions (Windows web terminal)
│   │   ├── ai/                 # AI assistant (tool registry + model client)
│   │   ├── models.py access.py session_service.py policy.py audit.py
│   │   └── schema_sync.py      # additive-only schema alignment at startup
│   ├── tests/                  # 705 test cases across 33 files
│   ├── tools/                  # demo target, three E2E suites, audit-chain verifier, screenshot generator
│   └── instance/               # runtime data (SQLite, keys, recordings) — gitignored
├── ant-design-pro/             # Frontend: Ant Design Pro 6 + Umi Max 4, all business pages rewritten
│   └── src/pages/bastion/      # Bastion pages (assets, terminals, files, RDP, audit…)
├── docs/
│   ├── 需求对照.md 使用手册.md 审计与安全.md 验证记录.md   (Chinese deep-dive pages)
│   ├── i18n/README.en.md       # this file
│   └── screenshots/            # 40+ real-machine screenshots
├── LICENSE  README.md
```

## Configuration

**Command-line flags** (`python run.py --help`)

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` / `--port` | `0.0.0.0` / `5000` | Web and API listen address and port |
| `--gateway-host` / `--gateway-port` | `0.0.0.0` / `2222` | SSH gateway address and port |
| `--no-gateway` | off | Skip the SSH gateway for this run |
| `--debug` / `--log-level` | off / `info` | Debug mode and log level |
| `--reset-admin` | — | Reset the administrator password when locked out |

**Environment variables**: the full production set (listen addresses, keys, lockout policy, timeouts, AI credentials — 20+ entries) is in the table under [Deploy to Production §3](#deploy-to-production-linux); names and defaults come straight from `bastion-backend/app/config.py`.

**Frontend dev proxy**: `config/proxy.ts` proxies `/api/` and `/socket.io/` (WebSocket included) to `http://127.0.0.1:5000`; override with `BASTION_API` when the backend runs elsewhere:

```bash
set BASTION_API=http://192.168.1.10:5000        # Windows
export BASTION_API=http://192.168.1.10:5000     # Linux / macOS
```

> Use `npm run dev` (`MOCK=none`). The template's `npm start` turns on Pro's mock server, which hijacks `/api/currentUser`, `/api/login/account` and friends.

## Security Notes

- Change the initial administrator password before going live and create dedicated roles for operators; never do daily work as the super administrator.
- The three keys (`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`) plus the `instance/` directory are the whole estate: **lose the keys and stored asset credentials can never be decrypted**; multi-instance deployments must share one set.
- Asset passwords, private keys and passphrases are stored Fernet-encrypted and only echoed back to callers holding `host:manage`.
- Web terminals, the file manager and AI tool calls are all audited, and the audit streams carry a hash chain verifiable offline with `tools/verify_audit_chain.py`.
- Deeper threat-model notes and the “super administrator admission exception” are documented in `docs/审计与安全.md` and `docs/使用手册.md` (Chinese).

## Verification

705 backend test cases, 85 frontend unit tests, plus three real-machine E2E suites:

```bash
cd bastion-backend && python -m pytest -q          # 705 passed (33 files)
python tools/live_e2e_check.py                     # 97 assertions: HTTP + Socket.IO + SSH gateway + SFTP + AI
python tools/ui_check.py                           # 21 assertions: real Chrome walkthrough of the admin UI
python tools/console_check.py                      # 33 assertions: real Chrome driving the terminal and file manager
cd ../ant-design-pro && npx biome check && npx tsc --noEmit && npx vitest run
```

The entry-by-entry verification log (criteria, commands, results and screenshots) is in
[docs/验证记录.md](../验证记录.md); requirement-by-requirement mapping in
[docs/需求对照.md](../需求对照.md); walkthroughs in [docs/使用手册.md](../使用手册.md);
audit pipeline and security notes in [docs/审计与安全.md](../审计与安全.md) (all Chinese).

## Contributing

1. Fork, then branch off `main`; use `feat(scope): summary` / `fix(scope): summary` commit messages.
2. Before pushing: `cd bastion-backend && python -m pytest -q`; for frontend changes also run `npx biome check && npx tsc --noEmit && npm run build`.
3. New behaviour needs tests or E2E assertions, and the PR description must quote real output — “should be fine” is not accepted in this repository.
4. Report security issues privately rather than in a public issue.

## License

MIT © 2026 [sywdn-001](https://github.com/sywdn-001). See [LICENSE](../../LICENSE) for details.
