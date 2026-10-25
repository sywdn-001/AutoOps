<h1 align="center">AutoOps 堡垒机</h1>

<p align="center">把「谁能连哪台机器、能用哪个账号、能敲什么命令」全管住的开源运维堡垒机</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/sywdn-001/AutoOps?color=blue" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/tests-793%20passed-brightgreen" alt="Backend tests: 793 passed">
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white" alt="Flask 3.1">
  <img src="https://img.shields.io/badge/React%2019%20%2B%20Ant%20Design%206-61DAFB?logo=react&logoColor=white" alt="React 19 + Ant Design 6">
  <img src="https://img.shields.io/github/last-commit/sywdn-001/AutoOps" alt="Last commit">
</p>

<p align="center">
  <b>中文</b> · <a href="docs/i18n/README.en.md">English</a>
</p>

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份项目说明里要是有写得不够好的地方，请大家多多谅解，也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this project README reads awkwardly, please bear
> with me (and tell me if you can).

> **关于本项目**：本仓库由**人机协作开发** —— 需求、取舍与验收由人把关，代码由人和 AI **一起写**，
> 一起跑门禁、一起做真机取证；仓库里既有手写的实现，也有 AI 参与的实现、重构与排障。
> 为了让人和 AI 都能快速接手，每个源文件的开头都有一段 **AI 生成的文件简介**，说明这个文件负责什么、
> 与哪些模块交互、有哪些容易踩的坑 —— 打开任何一个文件都能先看懂它，再决定要不要改。
> 覆盖范围（`bastion-backend/app`、`bastion-backend/tools`、`bastion-backend/tests`、
> `ant-design-pro/src/pages/bastion`、`ant-design-pro/src/components/Bastion`、
> `ant-design-pro/src/services/bastion`，以及被本项目改造过的 `src/app.tsx`、`src/app.test.tsx`）
> 实测 147 个文件 100% 覆盖；`ant-design-pro` 上游模板残留页与语言包保持原样，未作改动。

## 功能特性

- **身份与权限审计** — 用户、角色、主机、账号、授权逐条落库：谁能连哪台机器、用哪个账号、能开网页终端还是只能事后看录像，全部配得出来。
- **命令级策略管控** — 用正则与风险等级描述「什么能敲、什么不能敲」，高危命令可要求管理员当场二次确认；放行与拒绝都进审计。
- **SSH 网关** — `ssh -p 2222 <堡垒机账号>@<IP>` 登录后是一个受管控的审计 shell，菜单只列有权限的主机，每条命令与输出都被记录。
- **网页终端（Linux / Windows）** — 浏览器里直连 SSH 与 WinRM，不装本地客户端；WinRM 会话里 `cls` 就地清屏、早期按键不丢。
- **Windows 远程桌面（WebRDP）** — 浏览器里开 RDP，含老系统（Server 2003 / XP）的强制 SSL 兜底；录像边录边传，窗口被强行关掉也不丢。
- **RDP 网关（mstsc 直连堡垒机）** — Windows 自带「远程桌面连接」连堡垒机 **4489**，用堡垒机账号登录后，网关在会话里画一个「你有权访问的主机」选单（↑↓ / Enter / 鼠标都能选）；选中后它自己当 RDP 客户端去连目标机，把目标机画面接进这条会话。目标机口令只留在堡垒机进程里，客户端拿不到，取用会写 `rdp_credential_reveal` 审计。详见 [docs/RDP网关.md](docs/RDP网关.md)。
- **SFTP 文件管理器** — 浏览、上传、下载、重命名、删除、改权限、打包下载，走和终端一样的授权与策略。
- **AI 运维助手** — 自然语言下指令，按主机端点自动选通道（Linux 走 SSH、Windows 走 WinRM），敏感操作要管理员密码确认，工具调用全过程进审计。
- **ComputerUse（让 AI 直接操作 Windows 主机）** — 系统设置里分步部署：列华为云镜像的 Python 安装包并先缓存到堡垒机 → 传包静默安装 → `pip install windows-mcp` → 写开机自启 VBS 并启动 MCP → 开 portproxy 与只放行堡垒机的防火墙 → 探活；**部署成功后**，Windows RDP 控制台右侧才会出现可收起的 ComputerUse 侧边栏，AI 经 MCP 看屏幕、点鼠标、跑 PowerShell，每一步都进审计。

- **中英双语界面（操作界面已全面英文化）**：顶部栏右上角的地球按钮一键切换「简体中文 / English」，
  所有页面文案、菜单、表头、表单校验、提示语、AI 输入建议都已提供英文版本（切换后整页立即生效）；
  英文版截图见 [`docs/screenshots/en`](docs/screenshots/en) 与 [`docs/i18n/README.en.md`](docs/i18n/README.en.md)。

## 界面截图

<table align="center">
  <tr>
    <td align="center" width="50%">
      <a href="docs/screenshots/dashboard.png"><img src="docs/screenshots/dashboard.png" width="100%" alt="概览大盘" /></a>
      <br /><sub><b>概览大盘</b> — 资产、在线会话、风险与最近审计一屏总览</sub>
    </td>
    <td align="center" width="50%">
      <a href="docs/screenshots/multiproto-launcher.png"><img src="docs/screenshots/multiproto-launcher.png" width="100%" alt="网页终端入口" /></a>
      <br /><sub><b>网页终端入口</b> — 一台主机一行，按权限给出口协议按钮</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="docs/screenshots/multiproto-hosts-list.png"><img src="docs/screenshots/multiproto-hosts-list.png" width="100%" alt="主机列表" /></a>
      <br /><sub><b>主机列表</b> — 一台机器多个协议端点；同地址的两条记录可一键合并</sub>
    </td>
    <td align="center" width="50%">
      <a href="docs/screenshots/web-rdp-win75.png"><img src="docs/screenshots/web-rdp-win75.png" width="100%" alt="Windows 远程桌面" /></a>
      <br /><sub><b>Windows 远程桌面</b> — 浏览器里真连 <code>win-75</code>，右上角正在录制</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="docs/screenshots/file-manager.png"><img src="docs/screenshots/file-manager.png" width="100%" alt="SFTP 文件管理器" /></a>
      <br /><sub><b>SFTP 文件管理器</b> — 浏览 / 上传 / 下载 / 改权限，授权与策略和终端同一条链路</sub>
    </td>
    <td align="center" width="50%">
      <a href="docs/screenshots/ai-console.png"><img src="docs/screenshots/ai-console.png" width="100%" alt="AI 运维助手" /></a>
      <br /><sub><b>AI 运维助手</b> — 自然语言下指令，敏感操作要管理员当场确认</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="docs/screenshots/ssh-gateway-menu.png"><img src="docs/screenshots/ssh-gateway-menu.png" width="100%" alt="SSH 网关菜单" /></a>
      <br /><sub><b>SSH 网关菜单</b> — <code>ssh -p 2222</code> 登录后只列有权限的主机</sub>
    </td>
    <td align="center" width="50%">
      <a href="docs/screenshots/audit-chain-detail.png"><img src="docs/screenshots/audit-chain-detail.png" width="100%" alt="防篡改审计" /></a>
      <br /><sub><b>防篡改审计</b> — 命令日志哈希链，改一条就能查出来</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="100%" colspan="2">
      <a href="docs/screenshots/computeruse-mcp-screenshot.png"><img src="docs/screenshots/computeruse-mcp-screenshot.png" width="72%" alt="ComputerUse 通过 MCP 看到的目标机桌面" /></a>
      <br /><sub><b>ComputerUse</b> — AI 经 MCP 直接截到的 <code>win-75</code> 桌面（这一张就是它拿到的原图，1024×768）</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="docs/screenshots/computeruse-panel-welcome.png"><img src="docs/screenshots/computeruse-panel-welcome.png" alt="ComputerUse 侧边栏（空态）" /></a>
      <br /><sub><b>侧边栏（空态）</b> — 部署成功后 RDP 控制台右侧才出现的 AI 操作台：头部报可用工具数与「重启 MCP」，4 条示例点一下就发</sub>
    </td>
    <td align="center" width="50%">
      <a href="docs/screenshots/computeruse-panel.png"><img src="docs/screenshots/computeruse-panel.png" alt="ComputerUse 侧边栏（对话中）" /></a>
      <br /><sub><b>侧边栏（对话中）</b> — 思维链、每轮工具调用与 Markdown（列表 / 表格）都在对话里流式渲染，样式与「AI 运维助手」共用</sub>
    </td>
  </tr>
</table>

> 40+ 张实测截图都在 [`docs/screenshots/`](docs/screenshots/)，下面「验证记录」里的每条实测都对应其中一张。


## 快速开始

只需要 **Python ≥ 3.11**（开发用 3.13）与 **Node ≥ 20**（只在构建前端时用）；数据全部落在本地文件，不依赖外部数据库。

```bash
# 1) 起后端：首次启动自动建库、生成三把密钥、创建管理员 admin / admin123
cd bastion-backend
python -m pip install -r requirements.txt
Copy-Item .env.example .env      # 可选：要用 AI 运维就填 DEEPSEEK_API_KEY（Linux/macOS 用 cp）
python run.py

# 2) 起前端（另开一个终端）
cd ant-design-pro
npm install
npm run dev            # 开发模式 http://localhost:8000，/api 与 /socket.io 已代理到后端
```

| 入口 | 地址 |
| --- | --- |
| 后台 Web UI | `http://127.0.0.1:8000`（生产：`http://<服务器IP>:5000`） |
| 后台 API / 健康检查 | `http://127.0.0.1:5000/api/health` |
| SSH 网关 | `ssh -p 2222 <堡垒机账号>@<服务器IP>` |
| 默认管理员 | `admin` / `admin123`（**首次登录强制改密**） |

> 没有 Linux 机器也能跑通全链路：`python tools/demo_ssh_target.py` 会起一个**真实 SSH 协议栈**的假 Linux（`127.0.0.1:2200`，账号 `root` / `s3cret`），把它当资产加进来就能体验网页终端、SSH 网关、命令策略拦截与审计落库。生产构建用 `npm run build`，产物 `dist/` 由后端同端口直服。

### 编译 RDP 网关并打包成扩展模块

系统**不预装任何扩展模块**（全新实例是 0 个模块），「RDP 网关」也要自己上传安装。它的网关进程是独立的 Rust 程序，所以需要先编译再打包：

```bash
# 前置：Rust 工具链（rustup）+ C 工具链（Windows 需 MSVC Build Tools，Linux 需 gcc）
cd bastion-rdp-gateway
cargo build --release        # 必须 --release；debug 产物会在运行时报 D8050

# 把编译好的网关放进模块包，再打包成 .mod.ops_ext
cp target/release/bastion-rdp-gateway.exe ../modules-src/rdp-gateway/bin/   # Linux 去掉 .exe
cd ..

python bastion-backend/tools/pack_module.py modules-src/rdp-gateway -o .
# → 仓库根目录得到 rdp-gateway-<version>.mod.ops_ext
```

然后登录后台 →「系统设置 → 扩展模块 → 安装扩展模块」，上传仓库根目录里的 `.mod.ops_ext`（实验性模块要逐字输入风险确认语）。其它两个官方示例同样打包：`python bastion-backend/tools/pack_module.py modules-src/hello-bastion -o .`、`... modules-src/js-demo -o .`。

> 打包产物与 `modules-src/rdp-gateway/bin/` 都在 `.gitignore` 里，不进仓库。

## 部署到生产（Linux）

前置：**Ubuntu 22.04+ / Debian 12+ / RHEL 9+**、Python ≥ 3.11（开发用 3.13）、Node ≥ 20（只在构建前端时用）、放行 **5000**（Web/API）与 **2222**（SSH 网关）两个端口。建议单独建一个系统账号跑服务：

```bash
sudo useradd -r -m -d /opt/autoops -s /bin/bash bastion
sudo -iu bastion git clone git@github.com:sywdn-001/AutoOps.git /opt/autoops/app
```

**① 后端依赖（虚拟环境）**

```bash
cd /opt/autoops/app/bastion-backend
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

**② 前端构建**（产物由后端同端口直服，不需要额外的 Web 服务器）

```bash
cd /opt/autoops/app/ant-design-pro
npm ci
npm run build          # 产物 dist/，后端启动时自动挂载
```

**③ 生产配置**（环境变量，写进 systemd 单元或 `/etc/autoops.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BASTION_HOST` / `BASTION_PORT` | `0.0.0.0` / `5000` | Web 与 API 监听地址、端口 |
| `BASTION_GATEWAY_HOST` / `BASTION_GATEWAY_PORT` | `0.0.0.0` / `2222` | SSH 网关监听地址、端口（`BASTION_GATEWAY_ENABLED=0` 关掉整条网关） |
| `BASTION_ADMIN_USERNAME` / `BASTION_ADMIN_PASSWORD` | `admin` / `admin123` | 首次启动创建的管理员，**上线前必须改**（登录后也会强制改密） |
| `BASTION_INSTANCE_DIR` | `bastion-backend/instance` | 数据目录：SQLite 库、三把密钥、会话录像都落这里 |
| `BASTION_DB_URI` | `sqlite:///<INSTANCE_DIR>/bastion.db` | 换成 PostgreSQL 等外部库时改这里（需自备驱动） |
| `BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY` | 首次启动随机生成并落盘 | 会话签名、JWT、资产口令加密；**多实例部署必须显式给同一组值** |
| `BASTION_SERVE_FRONTEND` / `BASTION_FRONTEND_DIST` | `1` / 自动找 `ant-design-pro/dist` | 关掉前端直服，或指定别处的构建产物 |
| `BASTION_TOKEN_HOURS` | `12` | 登录令牌有效期（小时） |
| `BASTION_LOGIN_MAX_FAILURES` / `BASTION_LOGIN_LOCK_MINUTES` | `5` / `15` | 连续登录失败锁定策略 |
| `BASTION_SESSION_IDLE_TIMEOUT` | `1800` | 会话空闲上限（秒） |
| `BASTION_COMMAND_TIMEOUT` / `BASTION_MAX_OUTPUT_BYTES` | `60` / `262144` | 单条命令超时与输出截断 |
| `BASTION_GATEWAY_MAX_SESSIONS` | `5` | 单用户网关并发会话上限 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `AI_MODEL` | 空 / `https://api.deepseek.com` / `deepseek-flash` | AI 运维助手的模型凭据（`AI_ENABLED=0` 整体关闭） |
| `BASTION_CORS_ORIGINS` | `*` | 前后端分离部署时收紧到前端域名 |

**④ systemd 常驻**（`/etc/systemd/system/autoops.service`）

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

**⑤ 上线后必做的三件事**

1. 浏览器打开 `http://<服务器IP>:5000`，用初始管理员登录 → **立刻改密**（登录后强制）。
2. 「系统设置 → 系统参数」按需收紧并发数、空闲超时、命令超时；「身份与权限」给运维建角色与授权，**不要日常拿超管账号干活**。
3. 备份只备份两样：`$BASTION_INSTANCE_DIR`（SQLite + 三把密钥 + 录像）与 `.env` 里的 `BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`。**密钥丢了，已存的资产口令就解不开。**

**⑥ 升级**

```bash
cd /opt/autoops/app
git pull
cd bastion-backend && .venv/bin/python -m pip install -r requirements.txt
cd ../ant-design-pro && npm ci && npm run build
sudo systemctl restart autoops
```

数据库表结构由 `ensure_schema()` 在启动时**只加不减**地对齐（含「一台主机多协议」的端点回填），不写迁移脚本也不会丢数据。

**⑦ HTTPS 反代**（可选，推荐；**必须透传 WebSocket**，网页终端靠它）

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
        proxy_read_timeout 3600s;      # 网页终端是长连接
    }
}
```

SSH 网关不是 HTTP，用户直接 `ssh -p 2222 <账号>@<服务器IP>`；想走标准 22 端口，就用防火墙/端口转发把 22 转给 2222。

**⑧ Windows 免安装包**（目标机不想装 Python / Node 时）

推一个 `v*` tag，GitHub Actions 会自动发正式 Release；**推 `Dev` 分支也会自动出一版「Dev 分支夜间构建（Nightly Build）」预发布**——版本号在候选版号后带 RC 与日期/运行号（形如 `v1.1.1-RC-nightly.20261016.42`，每次都是新 tag），Release 标题、说明横幅与包内 `README-FIRST.txt` 都写明「开发分支夜间构建，仅供体验新功能，请勿用于生产环境」。

两种发布都产出 Windows(Server) **x64 / x32** 各一份 onedir 免安装包（PyInstaller 打包、前端 `dist/` 已内置、双击 `start-autoops.bat` 即用），并附上**源码包** `*-Source-Code.tar.gz` 与 `*-Source-Code.zip`（按本次构建的提交导出）与 `SHA256SUMS.txt`；正式版还另附华为云镜像的 Python 3.15.0 x64/x32 安装包、阿里云镜像的 Node v26.11.1 x64 msi（夜间构建不重复挂这 150 MB，说明里指向最新正式版）。Release 说明中英双语（`.github/workflows/release.yml`）。

想在本机打一份：**先建一个干净的 venv**，不要在本机全局 Python 里打 —— PyInstaller 会给「当前环境里能导入的每个包」找 hook，全局环境能慢到十几分钟：

```powershell
pwsh -File bastion-backend/packaging/build-local.ps1 -SkipFrontend -SmokeTest
```

产物在 `bastion-backend/build/dist/AutoOps-Bastion/`；`-Zip` 顺手压成与 CI 同名的 zip，`-SmokeTest` 用 5080 端口 + 不开网关起一次服务打 `/api/health`（不碰你正在跑的 5000/2222）。

---

## 使用示例

**1）在浏览器里开一个被审计的终端** — 「网页终端」→ 选主机与账号 → 「连接」：窗口里敲的每条命令与输出都会落进审计。

```console
$ whoami
opsadmin
$ cat /etc/shadow
⛔ 已按命令策略拦截：命中「禁止读取口令文件」规则（风险等级 high）
```

**2）从命令行走 SSH 网关** — 用**堡垒机账号**登录，菜单只列你有权限的主机：

```bash
ssh -p 2222 opsadmin@127.0.0.1
# 欢迎 opsadmin，可访问的主机：
#   1) e2e-demo-01   127.0.0.1:2200   Linux
# 选择主机 > 1
```

**3）让 AI 助手代跑命令** — 「AI 运维」里用自然语言下指令；模型调 `run_command` 时按主机端点自动选通道，敏感操作要管理员密码确认：

```console
> 看一下 win-75 的主机名和系统版本
[堡垒机] AI 请求执行 1 个敏感操作，需要管理员确认：run_command {"hostId": 5, ...}
管理员密码：********
WIN-930NKGJCOED
Microsoft Windows Server 2025 Datacenter
```

**4）校验审计链有没有被动过** — 命令、文件、AI 工具调用三类流水都带 `prev_hash`／`entry_hash`，离线重算即可发现篡改：

```bash
cd bastion-backend
python tools/verify_audit_chain.py
# 三类流水的哈希链逐条重算一致 → 校验通过
```

## 技术栈

| 层次 | 选型 |
| --- | --- |
| 后端 | Python 3.13 · Flask 3.1 · Flask-SQLAlchemy / SQLAlchemy 2.0 · Flask-JWT-Extended · Flask-SocketIO（threading + simple-websocket） |
| 连接层 | paramiko（SSH / SFTP）· pywinrm（WinRM）· 自研 RDP 网关（WebRDP，浏览器侧解码） |
| 前端 | React 19 · Ant Design 6 · Ant Design Pro 6 / Umi Max 4 · Biome · TypeScript strict · vitest |
| 存储 | SQLite（默认；`BASTION_DB_URI` 可换 PostgreSQL 等）· Fernet 加密资产口令 · 文件型会话录像 |

## 项目结构

```text
AutoOps/
├── bastion-backend/            # 后端：API + SSH 网关 + 网页终端 + AI 运维
│   ├── app/
│   │   ├── api/                # REST 蓝图（认证、资产、授权、策略、审计、终端、RDP…）
│   │   ├── gateway/            # SSH 网关（端口 2222 的审计 shell）
│   │   ├── webterm/ terminal/  # 网页终端（Socket.IO 事件 + 会话）
│   │   ├── files/              # SFTP 文件管理器（服务层 + 策略）
│   │   ├── rdp/                # WebRDP 网关与录像
│   │   ├── winrm/              # WinRM 字符会话（Windows 网页终端）
│   │   ├── ai/                 # AI 运维助手（工具注册 + 模型客户端）
│   │   ├── modules/            # 扩展模块系统：.mod.ops_ext 安装、注册、ctx 与 autoops.bastion SDK
│   │   ├── models.py access.py session_service.py policy.py audit.py
│   │   └── schema_sync.py      # 启动时「只加不减」地补齐表结构
│   ├── tests/                  # 839 个用例 / 42 个文件
│   ├── tools/                  # 演示目标机、四套联调脚本、审计链校验、README 配图生成器
│   └── instance/               # 运行时数据（SQLite、密钥、录像）——已在 .gitignore
├── ant-design-pro/             # 前端：Ant Design Pro 6 + Umi Max 4，业务页全部重写
│   └── src/pages/bastion/      # 堡垒机业务页（资产、终端、文件、远程桌面、审计…）
├── modules-src/                # 扩展模块源码：hello-bastion / js-demo / rdp-gateway
│                               # 打包后是仓库根的 *.mod.ops_ext，宿主不预装任何模块
├── docs/
│   ├── 图文教程.md              # 逐步配图的操作教程（中）
│   ├── 使用手册.md 审计与安全.md 需求对照.md 验证记录.md
│   ├── modules/                # 扩展模块开发白皮书 + 模块契约（第三方开发看这里）
│   ├── i18n/                   # 英文：README.en.md、manual.en.md、tutorial.en.md
│   └── screenshots/            # 60+ 张实测截图（含 tutorial/ 教程配图）
├── LICENSE  README.md
```

## 配置说明

**命令行参数**（`python run.py --help`）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` / `--port` | `0.0.0.0` / `5000` | Web 与 API 监听地址、端口 |
| `--gateway-host` / `--gateway-port` | `0.0.0.0` / `2222` | SSH 网关监听地址、端口 |
| `--no-gateway` | 关 | 本次不启动 SSH 网关 |
| `--debug` / `--log-level` | 关 / `info` | 调试模式与日志级别 |
| `--reset-admin` | — | 忘记管理员口令时重设（`--reset-admin` 后按提示输入） |

**环境变量**：完整清单（**68 个键，每个都带默认值**）在 [`bastion-backend/.env.example`](bastion-backend/.env.example)，覆盖后端、RDP 扩展模块、前端代理与本地校验脚本；生产上最常用的十几项另见下方 [部署到生产 §③](#部署到生产linux) 的表格。

```powershell
cd bastion-backend
Copy-Item .env.example .env      # Linux / macOS / Git Bash：cp .env.example .env
notepad .env                     # 取消注释要改的行，或直接新增 KEY=VALUE
python run.py                    # 重启生效（.env 只在启动时读一次）
```

- **只用改一项也能跑**：不建 `.env` 就是全部默认值；唯一真需要填的是 `DEEPSEEK_API_KEY`（不填时 `/api/ai/chat` 直接返回 400 `AI_NOT_CONFIGURED`，其它功能不受影响）。
- **优先级**：真实环境变量 > `.env` > 代码默认值。命令行、systemd `EnvironmentFile`、容器 env 里设置的值不会被 `.env` 覆盖，所以生产上可以只用 systemd 管理。
- **密钥留空最省事**：`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY` / `BASTION_GATEWAY_HOST_KEY` 不填时首次启动自动生成到 `instance/`；改这几个值会让已有登录态失效、已存的资产口令解不开。
- **别提交 `.env`**：`.gitignore` 已忽略它，`.env.example` 只是模板，保持注释状态、不含真实凭据。
- 想确认读到了哪些：启动日志会打「已从 .env 载入 N 个变量」，`GET /api/health` 里有网关开关与端口。

**前端开发代理**：`config/proxy.ts` 把 `/api/` 与 `/socket.io/`（含 WebSocket）代理到 `http://127.0.0.1:5000`，后端不在本机时用 `BASTION_API` 覆盖：

```bash
set BASTION_API=http://192.168.1.10:5000        # Windows
export BASTION_API=http://192.168.1.10:5000     # Linux / macOS
```

> 请用 `npm run dev`（`MOCK=none`）。模板自带的 `npm start` 会打开 Pro 的 mock 服务，把 `/api/currentUser`、`/api/login/account` 一类请求交给假数据。

## 安全须知

- 上线前**必须**改掉初始管理员口令，并给运维人员建独立角色；日常不要拿超级管理员账号干活。
- 三把密钥（`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`）与 `instance/` 目录就是全部家底：**密钥丢了，已存的资产口令解不开**；多实例部署必须显式给同一组值。
- 资产口令、私钥、口令短语都用 Fernet 加密落库，接口只在具备 `host:manage` 权限时回显。
- 网页终端、文件管理器、AI 工具调用三类操作全部进审计；审计流水带哈希链，可用 `tools/verify_audit_chain.py` 离线校验。
- 更多威胁模型与「超级管理员准入例外」的说明见 [docs/审计与安全.md](docs/审计与安全.md) 与 [docs/使用手册.md](docs/使用手册.md)。

## 验证记录

后端 793 个用例、前端 13 个文件 195 个单测，加上四套真机联调脚本：

```bash
cd bastion-backend && python -m pytest -q          # 793 passed（35 个文件）
python tools/live_e2e_check.py                     # 97 项：HTTP + Socket.IO + SSH 网关 + SFTP + AI 端到端
python tools/ui_check.py                           # 21 项：真实 Chrome 逐路由巡检后台
python tools/console_check.py                      # 33 项：真实 Chrome 驱动网页终端与文件管理器
python tools/computeruse_check.py --host 5         # 9 项：ComputerUse 部署记录 + 侧边栏四个接口（加 --host 5 --account 5 --deploy 走完整 7 阶段）
cd ../ant-design-pro && npx biome check && npx tsc --noEmit && npx vitest run
```

> ComputerUse 的侧边栏能不能用起来，取决于目标机上的 MCP 进程与那次登录会话：MCP 进程会随登录
> 会话注销而消失（此时探活如实报「连不上目标机的 MCP 服务」，脚本把工具清单/截图/PowerShell 三项标成
> SKIP 并以非 0 退出）；而进程活着但跑在**没有桌面的会话**里时，探活与 PowerShell 依然可用，只有截图与
> 界面自动化会失败（如实报 `screen grab failed`）。要拿桌面画面就用 RDP 登录目标机 —— StartUp 里的自启
> 脚本会把 MCP 接管到你的桌面会话，端口与侧边栏地址都不变。

逐条的验证记录（每条实测的判据、命令、结果与截图）见 [docs/验证记录.md](docs/验证记录.md)，
需求与扩展需求逐条对照见 [docs/需求对照.md](docs/需求对照.md)，使用流程见
[docs/使用手册.md](docs/使用手册.md)，审计数据流与安全须知见
[docs/审计与安全.md](docs/审计与安全.md)。

## 文档

**从零上手看这篇**：[图文教程](docs/图文教程.md) —— 从起服务到纳管机器、授权、开终端、
看审计，每一步都配一张操作截图。

| 文档 | English | 内容 |
| --- | --- | --- |
| [docs/图文教程.md](docs/图文教程.md) | [tutorial.en.md](docs/i18n/tutorial.en.md) | 逐步配图的操作教程 |
| [docs/使用手册.md](docs/使用手册.md) | [manual.en.md](docs/i18n/manual.en.md) | 每个页面的字段与操作说明 |
| [docs/审计与安全.md](docs/审计与安全.md) | — | 审计数据流、哈希链、凭据与密钥处理 |
| [docs/需求对照.md](docs/需求对照.md) | — | 需求逐条对应到实现与验证 |
| [docs/验证记录.md](docs/验证记录.md) | — | 门禁与真机联调的实测记录（含截图取证） |
| [docs/modules/WHITEPAPER.md](docs/modules/WHITEPAPER.md) | [whitepaper.en.md](docs/i18n/whitepaper.en.md) | **扩展模块开发白皮书**：包结构、`description.json` 全字段、Python 生命周期与 `ctx`、`autoops.bastion` SDK、两种 Web UI 模式、打包安装卸载、版本兼容、排障与一份全流程走查 |
| [docs/modules/CONTRACT.md](docs/modules/CONTRACT.md) | [contract.en.md](docs/i18n/contract.en.md) | 模块系统契约（白皮书的权威依据）：包格式、REST 接口、归一化字段、错误码 |
| [docs/提交PR指南.md](docs/提交PR指南.md) | [pr-guide.en.md](docs/i18n/pr-guide.en.md) | 提 PR 之前看这篇：分支与提交信息、门禁怎么跑、说明怎么写、自检清单 |
| [docs/提交Issue指南.md](docs/提交Issue指南.md) | [issue-guide.en.md](docs/i18n/issue-guide.en.md) | 提 issue 之前看这篇：模板怎么填、环境与复现步骤写到什么程度 |
| **[Wiki 使用手册](https://github.com/sywdn-001/AutoOps/wiki)** | 中英同页 | 按主题拆开的 15 篇：安装部署、快速上手、资产与账号、身份与授权、策略、网页终端与文件、远程桌面与录像、SSH 网关、审计中心、AI 助手、系统设置、安全维护、升级备份、FAQ、故障排查 |

全部文档的索引见 [docs/README.md](docs/README.md)；仓库英文介绍见
[docs/i18n/README.en.md](docs/i18n/README.en.md)。

## 贡献指南

1. Fork 后从 `main` 切分支，提交信息用 `feat(scope): 说明` / `fix(scope): 说明`。
2. 提交前跑一遍：`cd bastion-backend && python -m pytest -q`；改前端再跑 `npx biome check && npx tsc --noEmit && npm run build`。
3. 新增行为请同时补测试或联调脚本断言，PR 说明里附上实际输出（本仓库不接受「应该没问题」这类结论）。
4. 准备提 PR 时先看 [提交 PR 指南](docs/提交PR指南.md)（中英双语：[PR guide](docs/i18n/pr-guide.en.md)）—— 分支与提交信息、门禁怎么跑、说明四段式、哪些改动要先开 issue 商量，都在里面。
5. 发现问题、想提建议或提问，先看 [提交 Issue 指南](docs/提交Issue指南.md)（三个 issue 模板、环境与复现步骤该写什么都在里面）。
6. 安全相关问题请走私下渠道，不要直接开公开 issue —— 支持版本、范围、处理时限与安全港见 [SECURITY.md](SECURITY.md)。

## 许可证

MIT © 2026 [sywdn-001](https://github.com/sywdn-001). See [LICENSE](LICENSE) for details.
