# AutoOps Bastion — User Manual

> 中文版：[使用手册](../%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8C.md)

> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if any part of this user manual reads awkwardly, please bear
> with me (and tell me if you can).
>
> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份使用手册的英文版里要是有哪一段读着别扭、或者和实际界面不一致的地方，请大家多多谅解，
> 也欢迎直接指正。

---

# II·V. The admission exception for the "super administrator"

The bastion lets traffic through strictly according to the grant table by default. But a **super administrator** (`is_superuser`, i.e. the built-in `admin` role) can also see and connect to "enabled hosts" in the web terminal and the SSH gateway **without any grant record at all** — otherwise, on a freshly deployed environment, the very first thing an administrator would do is get locked out by their own product.

The exception is **controlled**: it loosens only "whether you can get in", not "what you can do":

- It applies to `is_superuser` only; `ops`/`viewer` still cannot see any host without a grant (`tests/test_access.py::test_regular_user_still_requires_grant` guards this line).
- Sessions are **still persisted** as usual (`source=web`/`gateway`), every command and its output is **still recorded**, and command policies are **still enforced** according to the system default policy.
- The temporary grant is not persisted (it never pollutes the grant table; the session record's `grant_id` is empty), and it never lends another account to the administrator (`test_superuser_never_borrows_account_from_another_host`).
- If a host has no usable asset account, the admission check rejects outright with "该主机下没有可用账号，请联系管理员配置资产账号" ("this host has no usable account — ask an administrator to configure an asset account"), so nobody is sent off to connect for nothing.

---

# III. Usage flows

## Administrator: first configure "who may touch what"

1. **Asset management** → host list: add a host (name / address / port / group / OS type), then add **asset accounts** for that host (password or private key, encrypted as soon as it is stored), and use the one-click "connectivity test" to really run `uname -a` once.
2. **Identity & permissions** → roles / users: create a user and assign roles (built-in `admin`/`ops`/`auditor`/`viewer`, or define your own permission codes).
3. **Access grants**: configure a "user × host (× account)" grant for a user, tick `登录`/`SFTP`/`上传`/`下载`/**允许改文件**/`网页终端`/`端口转发`, set the time window, weekdays, expiry and concurrency cap, and **bind a command policy and a file policy** (an empty file policy means the system default policy is used; the switches decide "whether you may do it", the policy decides "which paths you may touch"). You can also use the "grant matrix" to see at a glance who has which permission on which machine, or grant in bulk.
4. **Command policies**: maintain policies and rules (regex / prefix / exact / contains, with priority, risk level, allow / block / require confirmation), and use the "command simulator" to check before configuring whether a given command will be allowed or blocked.
5. **File policies**: maintain the policies and rules for "which file operations may be performed on which path" (glob / regex / prefix / contains, first match by priority wins, `*` means all operations, risk level, default allow or default deny); built in are "默认文件策略·敏感路径拦截" ("default file policy · sensitive path blocking" — it allows routine operations and blocks `.ssh`, credential files, writes to system directories and so on) and "只读浏览策略" ("read-only browsing policy"). On the same page, the "file operation simulator" lets you verify before binding whether a given operation on a given path will be allowed or denied (rename / move / copy validate the source path and the **target path** at the same time).
6. **System settings / SSH gateway**: inspect gateway status and host fingerprints, and tune the session timeout, command timeout, login-failure lockout, concurrency cap and other parameters. The page shows the two ends of the handshake side by side: the **server version** (what the gateway announces about itself, `SSH-2.0-BastionGW_1.0`) and the **client version** (what the bastion announces as a client when connecting out to a target machine, `SSH-2.0-paramiko_<version>`); the same "client version" also appears in the **Host → Account → connectivity test** result, which makes "the peer does not accept our client version" easy to diagnose.

## Operations staff: several ways in, one single audit trail

**Option A · web terminal (one popup = one session)**

`网页终端` (Web terminal) is an **asset list page**: after choosing an account you click "Connect", and the browser uses **`window.open()` to pop up a dedicated terminal window** (the third argument carries `popup=yes` plus sizing, centred, resizable, with a shape aligned to JumpServer); one window runs exactly one audited session, and closing the window ends the session. The console was rebuilt after the JumpServer `luna` console shape:

- **One session per popup**: the page no longer stacks several sessions, and no longer streams live audit/command logs (those live in "Audit centre → Session records / Command records"); the asset list page itself embeds no terminal and only selects the asset, selects the account and opens a window. Every click on "Connect" is a new window (the window name carries a timestamp), so it is one connection, one window, one session.
- **Toolbar**: asset list (close this window and return to the list), asset name, status, account, session number, **file management** (pops up a separate SFTP visual file manager window), reconnect, search, font size up/down, clean mode, workspace fullscreen, keyboard-shortcut help, disconnect session.
- **Keyboard**: `Ctrl/Cmd+F` search, `Ctrl/Cmd+Shift+C` copy selection, a bare `Ctrl+C` sends an interrupt, `Ctrl/Cmd+V` paste, `Ctrl/Cmd+Shift+F` fullscreen, `Ctrl/Cmd+Shift+P` clean mode, `Ctrl/Cmd+Shift+A` return to the asset list (closes this window) (plus holding Esc for 800 ms to leave clean mode / fullscreen).
- **Right-click menu inside the terminal**: copy / paste / select all / clear screen / font size up / font size down / fullscreen / disconnect or reconnect.
- **Disconnect closes the window**: after you click "Disconnect" (or the session ends server-side), the terminal shows an **in-place countdown**, `本窗口将在 10 秒后自动关闭…` ("this window will close automatically in 10 seconds…"), refreshed once per second without scrolling the screen; at zero the window closes itself. Browsers only allow self-closing for windows opened by script, so when that is refused a hint tells you to close it manually (`Ctrl/Cmd+W`). Clicking "Reconnect" before the countdown ends cancels the close and keeps using this window; **a network dropout does not count as a disconnect**, does not close the window, and still allows "Reconnect".
- **Bottom status bar**: login-state dot + username ｜ current asset name and protocol ｜ status (connected / connecting / disconnected / connection failed, including the reason for the drop) ｜ **connection duration, refreshed every second**.

**Sibling feature of Option A · file manager (SFTP, visual, audited at every step)**

Clicking "File management" on the terminal toolbar **pops up another dedicated window**, just like the terminal (`/files/console`, the same grant, an independent SFTP session):

- **Visual browsing**: it opens at that grant's home directory; the breadcrumb walks up and down the levels; the table lists name / type / size / permissions / modification time; double-click enters a directory, and refresh and sorting are supported. The window title carries the host name, and the toolbar shows the account, the **currently effective file policy name** and the session number.
- **What you can do**: upload (multiple files, **with a live progress panel**: one row per file showing file name / bytes transferred / percentage / live rate, cancellable at any moment with ✕), download (single file, or packaged as a zip), edit online and save (with mtime conflict detection — if someone else changed the file, overwriting is refused), new file / new directory / rename / move / copy / delete (multi-select), change permissions (4-digit octal).
- **Two gates judged together**: ① the `Grant` switches — `SFTP` decides whether you may enter this window, `上传`/`下载`/`允许改文件` decide the individual actions, and a button that is not enabled is greyed out with the reason stated; ② the **file policy** — path-matching rules (`glob` / regex / prefix / contains, first match by priority wins) decide whether that operation is allowed on that path, and rename / move / copy validate the source path and the **target path** together, rejecting the whole operation if either is blocked. **An already-open window follows grant changes live**: if an administrator turns "允许改文件" or `SFTP` off, the next operation is refused on the spot (no need to reopen the window).
- **Denials are visible**: when blocked, the window states the reason directly (for example `文件操作被策略「默认文件策略·敏感路径拦截」拦截：操作 [read] 源路径 [/.ssh/id_rsa] 命中规则 #1：禁止通过文件管理器访问 .ssh 目录`), not a silent failure.
- **Everything leaves a trace**: every operation — **allowed, denied and failed** alike — writes one file audit record (user / host / session number / operation / path / target path / action / risk level / matched rule number and rule text / reason / result / byte count / duration), and is simultaneously counted into the session audit (`protocol=sftp`); failures leave a trace too (for example packaging a non-existent path → 404 plus one record with `result=failure`). Closing the window ends the SFTP session, and the "File records" page can be searched afterwards.
- **Closing**: clicking "Close" or closing the window ends the session, and the window likewise offers a countdown and a "return to asset list" fallback.

**Option B · SSH gateway**

```bash
ssh -p 2222 <bastion-account>@<bastion-IP>
```

After logging in you enter the audit menu:

```
=== AutoOps 堡垒机 审计网关 ===
 1) web-01        10.0.0.11:22    [生产]  策略:标准运维策略  账号: root, deploy
 2) db-01         10.0.0.21:22    [数据库] 策略:高危需确认    账号: dba
请输入主机编号（l 刷新 / i 我的信息 / h 帮助 / q 退出）:
```

Pick a number → (when there are several accounts) pick an account → you enter that host's shell, and every command goes through policy decision and is recorded.

**Both Option A and Option B can reach Windows hosts (WinRM, a character session)**

As long as a Windows host has its protocol configured as `winrm` (default port 5985, authentication `ntlm`), it appears together with the Linux hosts in the **same asset list used by the web terminal** (the address column is annotated "· WinRM 网页终端", and the status bar shows `WINRM`), and it also appears in the **SSH gateway host menu** — both entries lead to a PowerShell character session: `cd` is preserved, `exit` leaves the session and returns to the menu (`q` quits the gateway). Commands and their output keep flowing one by one into "Command records" and the session transcript (`sessions.protocol='winrm'`).

Three capability limits are **written straight into the terminal when the session is established** (the gateway and the web terminal use the same wording) instead of letting users hit the wall themselves:

- **Every command is executed separately on the target machine** (WSMan is stateless): the bastion remembers `cd` and restores it, but in-process state such as environment variables and custom variables is not preserved;
- **Interactive programs such as `more` / `pause` / `Read-Host` are unavailable** — when you need interaction, use remote desktop instead;
- **File transfer is not supported** (WinRM has no SFTP channel): a WinRM session's toolbar has **no "File management" button**; to move files, use a Linux host or remote desktop.

Evidence screenshots: [winrm-webterm-list.png](../screenshots/winrm-webterm-list.png) (the WinRM row in the list and "Connect"), [winrm-webterm-console.png](../screenshots/winrm-webterm-console.png) (after `cd C:\Windows` the prompt and `Get-Location` change together, and the bottom-right status bar reads `WINRM`), [winrm-cls-and-early-input.png](../screenshots/winrm-cls-and-early-input.png) (typing immediately after the window opens still produces output; after `cls` clears in place only a single prompt line is left).

## I·VIII·supplement. One host, several protocol entry points (A + B)

It is normal for a machine to have both RDP (you want the picture) and WinRM (you want command auditing), but under the "one host record, one protocol" model you had to create the same machine as two records — the user's actual feedback was "都是同一台主机，只是协议不同就要搞两个条目，太麻烦了" ("it is the same host, only the protocol differs, and yet I have to make two entries — too annoying"). So two things were done:

- **A · Data model: one host carries several protocol endpoints**. A new `host_protocols` table was added (`uq_host_protocol(host_id, protocol)` pins down "one row per protocol"), each row being `protocol` + `port` + `winrm_transport` + `status`; the `protocol` / `port` / `winrm_transport` columns that used to live on the `hosts` table retire into **mirror fields** (always pointing at the primary endpoint), so the old code path `host.port` keeps working. Protocol endpoints, accounts and grants all hang off the **same host**, and `sessions.host_id` and the audit accounting are completely unchanged (historical sessions are not rewritten by the move).
- **B · Interface: the same record is one entry**. The "protocol entry points" field on the host form is a **multi-select**, and whichever protocol you tick brings up that protocol's port input (the WinRM port / RDP port / SSH port are independent); the host list's address column lists `address:port` per endpoint and the protocol column tags each endpoint; **the web terminal entry page merges rows by `hostId` and puts one entry button per protocol inside the row** (`连接` / `WinRM` / `远程桌面`), and only an SSH endpoint carries a "file" button; the console it leads to has `?protocol=` appended to its address, the server builds the session for that endpoint (`open_session(protocol=…)`), and the gateway menu likewise passes the endpoint protocol down.

Two backend actions go with it:

| Action | API | Notes |
| --- | --- | --- |
| **Merge two hosts with the same address** | `POST /api/hosts/<id>/merge` (administrator) | Merges "another record of the same machine" into this one: accounts, grants and protocol endpoints are all moved over (an account with the same name **and the same credential** is reused; one with the same name but a different credential is renamed and kept — a credential is never lost to a name clash), then the source record is deleted. A different address is a straight 400, and a source machine that still has online sessions is a 409. The front end has a "Merge" button in the host list's action column, and the modal lists only the other hosts **with the same address** |
| **Backfill endpoints for old databases** | Automatic at start-up | `backfill_host_protocols()` runs after `ensure_schema()`: any host that has no endpoint row yet gets one filled in from its mirror fields; machines that already have endpoints are **skipped** (idempotent, so multi-protocol setups an administrator configured by hand are not overwritten) |

Real-machine evidence (real Chrome driven over CDP, with the Windows host `win-75` configured with both `winrm:5985` and `rdp:3389`): [multiproto-launcher.png](../screenshots/multiproto-launcher.png) (the entry page has **only one row**, `win-75`, with two buttons inside it), [multiproto-winrm-console.png](../screenshots/multiproto-winrm-console.png) (the console reached by clicking "WinRM", status bar `WINRM`), [multiproto-hosts-list.png](../screenshots/multiproto-hosts-list.png) (one machine with two protocol tags in the host list, plus the "Merge" button), [multiproto-host-form.png](../screenshots/multiproto-host-form.png) (the "protocol entry points" multi-select in the edit drawer and each protocol's own port), [multiproto-merge-modal.png](../screenshots/multiproto-merge-modal.png) / [multiproto-merge-options.png](../screenshots/multiproto-merge-options.png) / [multiproto-merge-picked.png](../screenshots/multiproto-merge-picked.png) / [multiproto-merge-done.png](../screenshots/multiproto-merge-done.png) (the merge modal lists same-address hosts only; after submitting through the UI the source record disappears from the list).

**Option C · Windows remote desktop (WebRDP)**

Windows hosts simply sit in the **same asset list as the web terminal** (a platform icon distinguishes them, with no extra annotation): clicking "Connect" uses `window.open()` to pop up a **dedicated console window** (`/rdp/console`, `layout:false`, so it does not enter the menu), and one window runs exactly one audited session. The top bar can be collapsed to give the picture more room, and the picture adapts to the window size — when you enlarge the browser window the controlled host switches to the new resolution too (it is not stretching the picture); **after the session drops a 10-second countdown appears and the window closes itself** (the same behaviour as after a Linux web terminal disconnects), and the recording is uploaded automatically.

- **Who draws it**: `ironrdp-wasm` runs in the browser (a third-party library: the Rust IronRDP compiled to WASM); the whole RDP protocol stack is implemented client-side and the picture goes straight into a `<canvas>`; the bastion only relays RDCleanPath bytes and does not decode the picture.
- **How it is authorised**: exactly the same source as the terminal — the `rdp:use` permission code + that host's `Grant` (accounts within the grant, time window / weekdays / expiry / concurrency cap) + the `canWebterm` switch; the host protocol must be `rdp` and the account must use password authentication.
- **Where the password comes from**: the operator **does not need to know** the target machine's password — the bastion decrypts the asset password with Fernet and hands it to the browser with a 120-second one-time ticket (NLA/CredSSP must be computed on the client), and it **writes a dedicated `rdp_credential_reveal` audit record** answering "who handed which machine's which account password to the browser, and when".
- **Trace**: `rdp_ticket` (ticket requested) → `rdp_credential_reveal` (credential handed down) → `rdp_session_open` (session established, including the negotiation result and the server certificate) → `rdp_session_close` (disconnect reason, upstream and downstream byte counts, duration); all four actions can be looked up in the audit centre's session/operation logs.
- **It can be reviewed**: the whole remote-operation process is recorded in the browser into webm (`canvas.captureStream()` + `MediaRecorder`) and **uploaded while recording** — every 5 seconds a chunk is `POST /api/rdp/recordings/chunk`-ed and appended in order to a server-side staging file; a normal end goes through `finalize`, which atomically renames it into place. When the window is killed forcibly (the tab closed directly / a crash / a network drop) the front end has no time to finalize, so the server **automatically closes out** tasks that have been silent for more than 45 seconds into recordings marked "not ended normally" (`recovered=true`) whenever "someone opens the recordings list / someone starts a new remote desktop", so a whole video never vanishes into nothing; "Audit centre → Remote desktop recordings" lists them by operator / asset account / duration / size / time, and clicking "Review" swaps in a 10-minute ticket for streaming playback (`<video>` cannot carry an `Authorization` header, hence `?ticket=`); every review and every deletion leaves an audit record of its own.

## Auditor

`审计中心` (Audit centre) → Session records (including **forced interruption of online sessions**, and a three-view detail: summary / command records / recording playback), Command records (command, output, allowed or blocked, matched rule, risk, duration), **File records** (every operation in the file manager: user / host / session / operation / path / target path / action / risk / matched rule / result / byte count / duration, filterable by user / host / session number / operation / result / risk), Operation log (logins, asset changes, grant changes, policy changes, session establishment and interruption, and so on).

**Purging audit data (administrators only)**: all four audit pages (sessions / commands / **files** / operation log) support ticking rows and "delete selected", or "clear the filtered results" according to the current filter (with no filter set, that empties everything). Purging sessions **cascades** to that session's command log and recording files, and **in-progress sessions are skipped** (with a hint to interrupt them first); every purge action itself writes an audit trace (`purge_audits` / `purge_commands` / `purge_files` / `purge_sessions`), so "who cleared what, and when" is traceable too. **Purging file records requires `command:view_all`**: the built-in `ops`/`admin` have it, `viewer`/`auditor` do not (`tools/live_e2e_check.py` measured a 403 with `e2e-viewer`).

## AI operations assistant (two entry points, one shared set of permissions and audits)

The AI assistant is not "another administrator": every tool it calls hits the bastion's own API **carrying the identity of the current logged-in user**, so **anything this person cannot do in the web UI, the AI cannot do either**; sensitive operations additionally require an administrator password.

**Entry A · the web "AI 运维" page** (`/ai`, requires `ai:use`)

- **Conversation list on the left + message stream on the right**: create / switch / delete conversations, and review past conversations at any time (model, associated hosts, message count and tool-call count are all recorded on the conversation).
- **Streaming Markdown**: the answer grows character by character (the reasoning process is shown separately, collapsed), and code blocks, tables and lists render normally; the **cards** the model emits are rendered in place as real antd components (host inventory tables, change-result key/value pairs, risk warnings, execution steps).
- **Tool calls are visible**: every tool call leaves an expandable record in the bubble (tool name / input arguments / result / duration / whether it is sensitive), and failures show their reason too.
- **Sensitive operations need a password**: when a `sensitive` tool is hit, a confirmation box for the **administrator account + password** pops up (listing the operation about to run and its parameters); refusing aborts, confirming lets the same conversation turn continue, and a wrong password is treated as a refusal and leaves an audit trace.
- **The "what the AI may use" drawer**: lists, grouped, the tools that **the current account** can actually use (tools you lack permission for simply are not in the list).

**Entry B · `/ask-ai` inside an SSH gateway session** (log in and pick a host under requirement ④ first)

```
root@demo-linux-01:~# /ask-ai 这台机器磁盘满了吗
```

- This line **is not executed on the target machine**: on recognising the `/ask-ai` prefix the gateway sends a `Ctrl-U` to erase the line, **does not forward the carriage return**, and then asks the AI (so the command never ends up in the target machine's shell history).
- The answer is **redrawn as a stream** in the terminal (refreshed in place, without scrolling the screen), and Markdown headings / lists / code / tables are coloured as usual; **cards degrade into ASCII tables/lists**, and no JSON ever appears in the terminal.
- When a sensitive operation is triggered it is confirmed step by step with **plain text + masking** (no interactive widgets are emitted): `管理员账号：` → `管理员密码：` (not echoed), and pressing Enter is a refusal.
- Conversations and tool calls are persisted too (`source=shell`, carrying the associated host and session number), and are just as searchable in the audit centre.

**Auditing**: `审计中心 → AI 对话审计` (`/audit/ai`, requires `ai:view`) — the "AI 对话" (AI conversations) tab (conversation / initiator / associated host / message count / tool count / most recent activity) and the "工具调用" (tool calls) tab (time / caller / tool and sensitive marker / status / confirmer / input arguments / result); clicking "view details" lets you read that conversation's **message-by-message** content (including the reasoning process and the card JSON) and all of its tool calls. Only `ai:view_all` can see **other people's** conversations; otherwise you see only your own.

**Three fixes from third-round real-machine feedback** (the user's actual words: "这里明明是 WinRM 远程终端，AI 跑不了命令因为把 WinRM 当 SSH 使了" — "this is clearly a WinRM remote terminal, and the AI cannot run commands because it uses WinRM as if it were SSH"; and "添加主机这里协议选择 SSH 底下就不应该出现 RDP 的东西，合并 WinRM 和 RDP，两者都是 windows" — "when the protocol selected on the add-host form is SSH, nothing about RDP should appear underneath; merge WinRM and RDP, both are Windows")

- **AI command execution picks the channel by endpoint**: the `POST /api/terminal/exec` that `run_command` lands on now picks the channel by the host's **protocol endpoint** — a Linux endpoint goes over SSH (write Bash/POSIX commands) and a Windows endpoint goes over WinRM (write PowerShell commands) — and the response body gained `channel` / `protocol` to tell you which channel the command actually ran on; before the fix a Windows host was always connected over SSH, which reported `Error reading SSH protocol banner`. The tool's description text is also split per channel by OS (so the model knows which syntax to write), and a `protocol` parameter can force a channel.
- **A named protocol must be honoured**: passing `protocol` explicitly when the host has no such endpoint is now **a straight 400** (`NO_CHARACTER_ENDPOINT`, whose message lists the available endpoints) instead of silently switching to another channel and running the command — otherwise the caller believes the command ran on channel A while it actually ran on channel B, and the audit accounting is wrong. Graphical endpoints such as rdp are explicitly refused as well ("rdp 是图形桌面" — "rdp is a graphical desktop").
- **Host form protocol merge**: the "protocol entry points" are merged into two items, **Linux / Unix（SSH 命令行）** and **Windows（命令行 + 远程桌面）**; after choosing Windows you tick the two channels "命令行（WinRM / PowerShell）" and "远程桌面（RDP）" (the same machine may open just one or both), and **only when the matching channel is ticked do** `RDP 安全层` / `WinRM 认证方式` and their respective port boxes appear — choosing SSH no longer makes anything about RDP pop up underneath.

---
