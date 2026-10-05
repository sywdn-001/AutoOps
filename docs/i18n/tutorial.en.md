# Illustrated tutorial: walk through AutoOps Bastion end to end

> This page is the illustrated expansion of "Quick start" in the root [README](../../README.md):
> follow it step by step and you will exercise the whole loop — **onboard hosts → create users and
> roles → grant access → use the web terminal / file manager / remote desktop → audit afterwards**.
> 中文版：[图文教程](../%E5%9B%BE%E6%96%87%E6%95%99%E7%A8%8B.md)

> **Where the pictures come from**: every screenshot below was taken by
> `bastion-backend/tools/tutorial_shots.py`, which drives a **real Chrome** (CDP) session: it logs
> into the local service, really connects to the demo Linux target `e2e-demo-01` and the Windows host
> `win-75`, and captures each step. They are not mock-ups. Re-run
> `python -u tools/tutorial_shots.py --admin-password '<admin password>'` after changing the code to
> refresh all of them.

---

## 0. Bring the service up (30 seconds)

Three terminals: backend, demo target, frontend.

```bash
# terminal 1: backend (API + SSH gateway + web terminal)
cd bastion-backend
python -m pip install -r requirements.txt
python run.py
#   API        http://127.0.0.1:5000   (health check: GET /api/health)
#   SSH gateway 0.0.0.0:2222
#   default administrator admin / admin123 (password change enforced on first login)

# terminal 2: demo target (a fake Linux with a real SSH stack, handy for practice)
cd bastion-backend
python tools/demo_ssh_target.py
#   listens on 127.0.0.1:2200, account root / s3cret

# terminal 3: frontend
cd ant-design-pro
npm install
npm run dev
#   http://localhost:8000
```

Sanity check once the backend is up: open <http://127.0.0.1:5000/api/health> in a browser —
seeing `{"success": true, ...}` means the API is alive.

---

## 1. Sign in and read the dashboard

### ① Open the bastion and sign in with your bastion account

![Login page](../screenshots/tutorial/01-login.png)

Go to <http://localhost:8000>. On the very first login use `admin / admin123`; the system will
**force you to change it** (initial passwords that are never rotated are not allowed in — see
[审计与安全](../%E5%AE%A1%E8%AE%A1%E4%B8%8E%E5%AE%89%E5%85%A8.md)). Note that this is a
**bastion account**, not a target-machine account: target credentials are configured later under
"Accounts" and ordinary users never see their passwords.

### ② Dashboard: the whole picture on one screen

![Dashboard](../screenshots/tutorial/02-dashboard.png)

Asset count, live sessions, risk hints and recent audit entries. Day to day you start in the audit
centre to look for anomalies, then go to asset management to onboard machines.

---

## 2. Onboard your machines

### ③ Assets → Hosts

![Host list](../screenshots/tutorial/03-hosts.png)

One machine per row, and **a row can carry several protocol endpoints**: a Linux box gets `ssh:22`,
a Windows box can carry both `winrm:5985` and `rdp:3389` — that is, the same Windows machine offers a
character shell (every command audited) *and* a graphical desktop (fully recorded). Two records that
share an address and differ only by protocol can be folded into one machine with the row's "Merge"
action, which re-parents both accounts and endpoints.

### ④ Edit a host: protocol endpoints and per-protocol ports

![Edit host](../screenshots/tutorial/04-host-form.png)

Click "Edit" in a row to open the drawer:

- **Protocol entry**: pick `Linux / Unix (SSH command line)` for Linux, or `Windows (command line +
  remote desktop)` for Windows and tick the channels you want (WinRM command line / RDP desktop).
- **A port per protocol**: WinRM defaults to 5985 (use 5986 when the target enables HTTPS — TLS is
  detected automatically), RDP defaults to 3389, SSH to 22.
- **RDP security layer**: "Auto" (includes NLA) by default; for legacy systems (Windows Server 2003 /
  XP class) that get disconnected during the NLA phase, switch to "Force SSL".
- **Accounts**: use "Accounts" to add target credentials (username + password, stored encrypted).

---

## 3. Identity and permissions: who may do what

### ⑤ Users: who may sign in to the bastion

![User management](../screenshots/tutorial/05-identity-users.png)

One bastion account per operator (again: *not* a target account). A user's role decides which menus
appear and which machines they may touch — but the front end is only a convenience layer; every API
re-checks the permission codes on the server side.

### ⑥ Roles: permissions are ticked, not assumed

![Role management](../screenshots/tutorial/06-identity-roles.png)

Permission codes are grouped by module; typical ones are "view assets", "open the web terminal",
"transfer files", "watch recordings", "edit command policies". **Grant the minimum**: an
audit-only operator should not get write access to asset management.

### ⑦ Access grants: who → which host → which account

![Access grants](../screenshots/tutorial/07-grants.png)

This is the heart of the bastion. One grant = a user (or role) × a host × an account, plus whether
they **may open the web terminal**, **may use the file manager**, and any time-window or duration
limits. Machines without a grant do not appear in the web-terminal list or the SSH gateway menu at
all; even a hand-crafted `hostId` is rejected by the backend, and that rejection is written to the
audit log.

---

## 4. Command and file policies

### ⑧ Command policies: what may and may not be typed

![Command policies](../screenshots/tutorial/08-policies.png)

Rules are regular expressions plus a risk level. When a "deny" rule matches, the command **never
reaches the target**: the user sees a refusal and the audit log records `denied`. A high-risk command
can instead be routed for **on-the-spot approval** by an administrator. Policies apply per
user/role and per host scope, and both allowances and refusals are recorded.

> File policies (which paths may be touched, whether upload/download is allowed) live under "File
> policies" and follow the same idea, applied to the SFTP file manager.

---

## 5. The four places where work actually happens

### ⑨ Web terminal: one row per host, one button per protocol

![Web terminal launcher](../screenshots/tutorial/09-launcher.png)

The "Web terminal" page lists every host **you are granted**, with a button per protocol: SSH for
Linux, WinRM / remote desktop for Windows. Clicking "Connect" opens a **new browser tab** — one tab,
one session.

### ⑩ Reach a Linux host straight from the browser

![Linux web terminal](../screenshots/tutorial/10-console-linux.png)

No client to install: it is a real terminal in the browser. This one is connected to the demo target
`e2e-demo-01`, where `whoami` returned `opsadmin`. The top bar shows the host, the login account and
the **session id**; on the right you can change the font size, search the scrollback, or disconnect;
the bottom bar shows `SSH · connected · elapsed`.

> Every command and every line of output in this terminal is recorded. Want AI help while working?
> Type `/ask-ai <question>`.

### ⑪ File manager: SFTP without leaving the browser

![File manager](../screenshots/tutorial/11-file-manager.png)

Click "File manager" in the terminal toolbar to open another tab: directory tree on the left, file
list on the right, with upload, download, mkdir, rename, chmod and delete. **It runs through the same
grants and policies**: users without file permission never see the button, and paths outside the
allowed scope are refused and audited.

### ⑫ Windows hosts get a character session too (WinRM)

![WinRM console](../screenshots/tutorial/12-winrm-console.png)

Once a Windows host carries a `winrm:5985` endpoint, the same web-terminal entry point opens a
PowerShell session (here `hostname` returned `WIN-930NKGJCOED`). Its semantics differ from Linux, and
the page says so up front:

- each command runs **separately** on the target; `cd` is remembered (the session keeps the current
  directory), but in-process state such as environment variables is **not**;
- interactive programs such as `more` / `pause` / `Read-Host` are unavailable, and this session does
  not transfer files;
- when you need those, use the remote desktop instead.

### ⑬ Windows Remote Desktop: driven from the browser, recorded throughout

![WebRDP remote desktop](../screenshots/tutorial/13-rdp-console.png)

Click "Remote desktop" on a Windows host and the Windows desktop appears in the browser (here
`win-75`, Windows Server 2025). The top-right shows "**Recording**" and the elapsed time, with
reconnect / disconnect / close-window controls. The recording is stored server-side and can be
replayed later under "Audit centre → Remote desktop recordings".

> RDP is a graphical protocol, so **command-level policies cannot see inside it**; the compensation
> is "record everything + control who may replay the recordings".

---

## 6. Audit afterwards: who did what, when

### ⑭ Remote desktop recordings

![Remote desktop recordings](../screenshots/tutorial/14-rdp-recordings.png)

One recording per RDP session, filterable by host, account and time. Auditors with the permission see
everyone's recordings; ordinary users see only their own — filtered in the backend, not merely hidden
in the UI.

### ⑮ Command log: every command and its output

![Command log](../screenshots/tutorial/15-audit-commands.png)

This is the hardest requirement of the lot: **every command and its output is recorded**. The list
shows who, in which session, at what time, typed what, what came back, which policy matched, and
whether it was allowed or refused. Open a row for the full output.

### ⑯ Session log

![Session log](../screenshots/tutorial/16-audit-sessions.png)

Who connected from where, to which machine, with which account, over which protocol (SSH / WinRM /
RDP / SFTP), for how long, and whether it is still live. Administrators can **force-terminate** a
live session.

### ⑰ Operation log and hash chain

![Operation log and hash chain](../screenshots/tutorial/17-audit-chain.png)

Beyond "who connected where", this records "who changed the configuration": added a host, edited a
grant, changed a policy, deleted a user. Each entry is linked into a **hash chain** (the previous
entry's hash feeds the next one), so tampering with any historical row breaks the chain — verify it
with `python tools/verify_audit_chain.py`.

---

## 7. AI operations assistant

### ⑱ Natural language, human-approved for sensitive work

![AI operations assistant](../screenshots/tutorial/18-ai-console.png)

Under "AI operations" you can simply talk, e.g. "how much disk is left on win-75?". Every tool the AI
calls (run a command, read the audit log, open a terminal) is subject to the **same permissions and
policies**. An action that hits a high-risk policy does not proceed on its own: the confirmation
request is pushed to an administrator who approves or denies it on the spot. The conversation, the
tool calls and the approval decisions all land in "Audit centre → AI conversation audit".

---

## 8. The SSH gateway: the command-line way in

You do not have to use the browser — from any terminal:

```bash
ssh -p 2222 <bastion-account>@<bastion-ip>
```

What you get is an **audited shell**: first a menu of the hosts you are granted, then the host you
pick, and from there every command and its output is recorded as usual. Menu, selection and echo are
implemented by the bastion itself, so **hosts you have no grant for never even appear**.

Administrators can check the port and the on/off switch under "System settings → SSH gateway":

![SSH gateway settings](../screenshots/ssh-gateway-menu.png)

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Login page shows 404 / 502 | Backend down or wrong port; `config/proxy.ts` proxies to `127.0.0.1:5000`, override with `BASTION_API=... npm run dev` |
| Web terminal stuck on "connecting" | WebSocket not passed through; `/socket.io/` needs the `Upgrade` and `Connection` headers (see the Nginx notes in the root README) |
| Pages look stale | Browser cache — hard-refresh with `Ctrl + F5` |
| A host is missing from the list | No grant for that host (or the grant lacks web-terminal permission); the audit log shows the matching refusal |
| A command was refused | It matched a command policy; find the rule under "Command policies" — the reason is in the command log |
| A Windows host will not connect | WinRM not enabled / wrong port (5985 vs 5986), or RDP security layer must be "Force SSL"; use "Test connection" per protocol in the host row |

## Related documents

- [使用手册](../%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8C.md) — field-by-field and page-by-page guide (Chinese)
- [User manual (English)](manual.en.md) — the English counterpart of the above
- [审计与安全](../%E5%AE%A1%E8%AE%A1%E4%B8%8E%E5%AE%89%E5%85%A8.md) — audit data flow, hash chain, credential handling
- [需求对照](../%E9%9C%80%E6%B1%82%E5%AF%B9%E7%85%A7.md) — each requirement mapped to its implementation and evidence
- [验证记录](../%E9%AA%8C%E8%AF%81%E8%AE%B0%E5%BD%95.md) — gate and real-machine verification log, with screenshots
