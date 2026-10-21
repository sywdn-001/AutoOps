<!--
  Release 说明模板（中英双语）。本文件里的 {{...}} 占位符由
  `.github/scripts/make_release_notes.py` 在发版时替换成真实值：
  VERSION / BUILD_DATE / COMMIT / COMMIT_SHORT / ASSET_TABLE / ASSET_COUNT /
  TOTAL_SIZE / REPO_URL / WORKFLOW_URL / PYTHON_INSTALLER_VERSION /
  NODE_INSTALLER_VERSION / CHANNEL_BANNER_ZH / CHANNEL_BANNER_EN。
  另外还有条件块，脚本按本次通道只留命中的那一段：
  `<!-- if:stable -->…<!-- endif -->` 正式版、`<!-- if:rc -->…<!-- endif -->` 候选版/夜间构建、
  `<!-- if:dev -->…<!-- endif -->` 仅 Dev 分支夜间构建。
  改这个文件就等于改以后每一次的 Release 说明。
-->

# AutoOps 堡垒机 {{VERSION}}

**中文** · [English](#english)

> 发布日期 / Released: {{BUILD_DATE}} ｜ 提交 / Commit: [`{{COMMIT_SHORT}}`]({{REPO_URL}}/commit/{{COMMIT}}) ｜ [构建日志 / Workflow run]({{WORKFLOW_URL}})

{{CHANNEL_BANNER_ZH}}

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 所以这份发布说明可能还有不周到的地方，请大家多多谅解，也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — so these release notes may still have rough edges,
> please bear with me (and tell me if you can).

把「谁能连哪台机器、能用哪个账号、能敲什么命令」全管住的开源运维堡垒机：
身份与权限审计、命令级策略管控、SSH 网关、网页终端（SSH / WinRM）、
WebRDP 远程桌面与录像、SFTP 文件管理器、防篡改审计链、AI 运维助手。

## ⬇️ 本次发布包含 {{ASSET_COUNT}} 个文件（共 {{TOTAL_SIZE}}）

{{ASSET_TABLE}}

## 📦 源码 / Source code

除了 GitHub 自动生成的 `Source code (zip)` / `Source code (tar.gz)`（它们指向本 Release 的 tag），
这次还额外挂了两份源码包：

- `AutoOps-Bastion-{{VERSION}}-Source-Code.tar.gz`
- `AutoOps-Bastion-{{VERSION}}-Source-Code.zip`

两份内容一样，都是**与上面那些压缩包同一次构建**的源码（`git archive` 自构建提交导出，含全部被跟踪的文件）：
想自己编译，或者想核对「这个包到底是从哪份代码打出来的」，用它们。

## 🖥️ RDP 网关（mstsc 直连堡垒机）

Windows 自带的「远程桌面连接」直接连堡垒机的 **4489** 端口：

```
mstsc /v:<堡垒机地址>:4489
```

用**堡垒机账号**登录后，网关会在会话里画一个「你有权访问的主机」选单（↑↓ / Enter / 鼠标都能选），
选中一台就把它的桌面接进这个窗口 —— 不用装任何客户端，目标机口令只留在堡垒机进程里。

- **随包自带**：解压后的 `rdp-gateway\bastion-rdp-gateway.exe`（也可以在「系统设置 → RDP 网关」里点启动/停止）；
- **单独下载**：`AutoOps-Bastion-RDP-Gateway-{{VERSION}}-windows-x64.exe`，只想升级网关时拿这个就够。

用法、配置、审计与已知限制见 [docs/RDP网关.md](https://github.com/{{REPO_URL}}/blob/{{VERSION}}/docs/RDP%E7%BD%91%E5%85%B3.md)
/ [English](https://github.com/{{REPO_URL}}/blob/{{VERSION}}/docs/i18n/rdp-gateway.en.md)。

Point Windows' built-in Remote Desktop client at the bastion on port **4489**: sign in with a bastion
account, pick a host you may reach, and its desktop is streamed into that window. The gateway ships
inside the zip (`rdp-gateway\bastion-rdp-gateway.exe`) and is also attached separately as
`AutoOps-Bastion-RDP-Gateway-{{VERSION}}-windows-x64.exe`.

## ⚡ 快速开始（Windows / Windows Server，免安装）

1. 下载 **`AutoOps-Bastion-{{VERSION}}-windows-server-x64-py*.zip`**（32 位系统选 `x32` 那个）。
2. 解压到任意目录（路径尽量不要有空格）。
3. 双击 `start-autoops.bat`，浏览器会自动打开 <http://127.0.0.1:5000>。
4. 用 **`admin / admin123`** 登录 —— **登录后第一件事就是改口令**。
5. 用完了在窗口里按 `Ctrl+C` 停服务；关掉窗口服务就停了。

- 目录里**已经带好 Python 运行时与前端页面**，目标机器**不需要**装 Python / Node。
- Web 控制台默认 5000，SSH 网关默认 2222：`ssh -p 2222 <堡垒机账号>@<机器IP>`。
- 换端口：`start-autoops.bat --port 8080 --gateway-port 2222`。
- 数据全在 `instance\`（SQLite + 三把密钥 + 录像）：**备份就是整个目录拷走**，
  密钥丢了库里的资产口令就解不开。

<!-- if:stable -->
## 🧰 附件里的 Python / Node 安装包

Release 里另外挂了 Python 与 Node 的官方安装包，给「想在同一台机器上改源码 / 用源码跑」的人用，
**跑上面那个 zip 不需要它们**：

- **Python {{PYTHON_INSTALLER_VERSION}}**：`x64`（`-amd64.exe`，64 位）与 `x32`（不带后缀的 `.exe`，32 位）。
  取自上表所列镜像，文件名为我们加上位数后缀，方便一眼分辨。
- **Node.js v{{NODE_INSTALLER_VERSION}}**：只有 `x64`。
  **官方本来就没有 32 位的 msi**，所以这里不发 32 位 —— 不是漏发，是上游就没有。
<!-- endif -->

<!-- if:rc -->
## 🧰 附件里的 Python / Node 安装包

这一版不是正式版（候选版或夜间构建），没有再重复挂那 150 MB 的 Python 与 Node 官方安装包 ——
需要的话请到[最新的正式版 Release]({{REPO_URL}}/releases/latest) 里取（**跑上面那个 zip 也不需要它们**）。
<!-- endif -->

## 🔐 校验下载（可选）

```bash
# Linux / macOS
sha256sum -c SHA256SUMS.txt

# Windows（PowerShell / cmd 都可以）
certutil -hashfile <下载的文件名> SHA256
```

`SHA256SUMS.txt` 里是本次所有附件的 SHA256；对不上就别装。

## 📚 文档与教程

- **Wiki 使用手册（推荐从这里开始）**：<{{REPO_URL}}/wiki>
- 图文教程（每步一张截图）：[`docs/图文教程.md`]({{REPO_URL}}/blob/main/docs/图文教程.md) ｜ [English]({{REPO_URL}}/blob/main/docs/i18n/tutorial.en.md)
- 使用手册：[中文]({{REPO_URL}}/blob/main/docs/使用手册.md) ｜ [English]({{REPO_URL}}/blob/main/docs/i18n/manual.en.md)
- 审计与安全：{{REPO_URL}}/blob/main/docs/审计与安全.md
- 验证记录（每条结论怎么测出来的）：{{REPO_URL}}/blob/main/docs/验证记录.md
- 提交 Issue 指南：{{REPO_URL}}/blob/main/docs/提交Issue指南.md ｜ [English]({{REPO_URL}}/blob/main/docs/i18n/issue-guide.en.md)

**语言说明**：本项目的文档与教程已全部英译，但项目的首要语言仍是汉语。

## 🐞 反馈

- 遇到问题先看 [排查清单]({{REPO_URL}}/wiki/Troubleshooting) 与 [FAQ]({{REPO_URL}}/wiki/FAQ)，再按
  [提交 Issue 指南]({{REPO_URL}}/blob/main/docs/提交Issue指南.md) 提 issue（带上版本号、系统、复现步骤、日志/截图）。
- 安全问题请走 [SECURITY.md]({{REPO_URL}}/blob/main/SECURITY.md) 里的私下渠道，不要开公开 issue。

## 📄 许可

MIT，版权归 sywdn-001。

<a id="english"></a>

# AutoOps Bastion {{VERSION}}

[中文](#autoops-堡垒机-version) · **English**

> Released: {{BUILD_DATE}} ｜ Commit: [`{{COMMIT_SHORT}}`]({{REPO_URL}}/commit/{{COMMIT}}) ｜ [Workflow run]({{WORKFLOW_URL}})

{{CHANNEL_BANNER_EN}}

An open-source bastion host that governs who may reach which machine, with
which account, and which commands: identity and permission audit, command-level
policy control, an audited SSH gateway, web terminal (SSH / WinRM), WebRDP with
session recording, an SFTP file manager, a tamper-evident audit chain, and an AI
operations assistant.

## ⬇️ What is in this release ({{ASSET_COUNT}} files, {{TOTAL_SIZE}})

{{ASSET_TABLE}}

## 📦 Source code

Besides the `Source code (zip)` / `Source code (tar.gz)` links GitHub generates for the tag,
this release also carries two explicit source archives:

- `AutoOps-Bastion-{{VERSION}}-Source-Code.tar.gz`
- `AutoOps-Bastion-{{VERSION}}-Source-Code.zip`

Both hold the same thing: the source of **the very same build as the packages above**
(exported with `git archive` from the built commit, every tracked file included) — handy when you
want to compile it yourself, or to check which code a given package came from.

## ⚡ Quick start (Windows / Windows Server, portable)

1. Download **`AutoOps-Bastion-{{VERSION}}-windows-server-x64-py*.zip`** (use the `x32` one on 32-bit systems).
2. Unpack it anywhere (avoid spaces in the path).
3. Double-click `start-autoops.bat`; your browser opens <http://127.0.0.1:5000>.
4. Sign in with **`admin / admin123`** — **change that password immediately**.
5. Press `Ctrl+C` in the console window to stop the service.

- The Python runtime and the built web UI are **inside the zip**: the target machine needs **no Python and no Node**.
- Web console port 5000, SSH gateway port 2222: `ssh -p 2222 <bastion-account>@<host-ip>`.
- Other ports: `start-autoops.bat --port 8080 --gateway-port 2222`.
- All state lives in `instance\` (SQLite + three key files + recordings): back it up by copying that folder.
  Lose the keys and the stored asset passwords can no longer be decrypted.

<!-- if:stable -->
## 🧰 Bundled Python / Node installers

The release also carries the official Python and Node installers, for people who want to
build or run the project from source on the same machine — **the zip above does not need them**:

- **Python {{PYTHON_INSTALLER_VERSION}}**: `x64` (`-amd64.exe`) and `x32` (the bare `.exe`).
  They come from the mirror listed above; we only append the bitness suffix to the file name.
- **Node.js v{{NODE_INSTALLER_VERSION}}**: `x64` only. Upstream publishes **no 32-bit msi**,
  so there is nothing to attach for x32 — that is an upstream gap, not a missing upload.
<!-- endif -->

<!-- if:rc -->
## 🧰 Bundled Python / Node installers

This one is not a stable release (release candidate or nightly build), so it does not repeat the
~150 MB of official Python and Node installers — grab those from the
[latest stable release]({{REPO_URL}}/releases/latest) if you need them
(**the zip above does not need them either**).
<!-- endif -->

## 🔐 Verify the download (optional)

```bash
# Linux / macOS
sha256sum -c SHA256SUMS.txt

# Windows (PowerShell or cmd)
certutil -hashfile <downloaded-file> SHA256
```

`SHA256SUMS.txt` lists the SHA256 of every asset in this release; if it does not match, do not install.

## 📚 Documentation

- **Wiki manual (best starting point)**: <{{REPO_URL}}/wiki>
- Illustrated tutorial: [English]({{REPO_URL}}/blob/main/docs/i18n/tutorial.en.md) ｜ [中文]({{REPO_URL}}/blob/main/docs/图文教程.md)
- User manual: [English]({{REPO_URL}}/blob/main/docs/i18n/manual.en.md) ｜ [中文]({{REPO_URL}}/blob/main/docs/使用手册.md)
- Audit and security notes: {{REPO_URL}}/blob/main/docs/审计与安全.md
- Verification log: {{REPO_URL}}/blob/main/docs/验证记录.md
- Issue guide: [English]({{REPO_URL}}/blob/main/docs/i18n/issue-guide.en.md) ｜ [中文]({{REPO_URL}}/blob/main/docs/提交Issue指南.md)

**A note on language**: all documentation and tutorials have been translated into
English, but Chinese remains the project's primary language.

## 🐞 Feedback

- Check [Troubleshooting]({{REPO_URL}}/wiki/Troubleshooting) and [FAQ]({{REPO_URL}}/wiki/FAQ) first;
  then open an issue following the [issue guide]({{REPO_URL}}/blob/main/docs/i18n/issue-guide.en.md)
  (include version, OS, reproduction steps, logs or screenshots).
- For security problems use the private channel in [SECURITY.md]({{REPO_URL}}/blob/main/SECURITY.md).

## 📄 License

MIT, copyright sywdn-001.
