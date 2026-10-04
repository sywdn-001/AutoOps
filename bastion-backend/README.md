# AutoOps 堡垒机 · 后端（Flask）

面向 Linux 服务器的运维堡垒机后端：**JWT 身份认证 + RBAC 权限 + 资产/授权管理 + 命令级策略引擎 + 文件策略引擎 + SSH 网关 + 网页终端 + SFTP 文件管理器 + 全量操作审计**。

> 目标平台 Linux；开发环境 Windows 亦可直接运行（SSH 相关能力基于 paramiko 纯协议实现，不依赖系统 ssh 命令）。

---

## 一、技术栈与依赖

| 组件 | 用途 | 版本 |
| --- | --- | --- |
| Flask | Web 框架 / REST API | >= 3.0 |
| Flask-SQLAlchemy + SQLAlchemy | ORM（默认 SQLite，可换 MySQL/PostgreSQL） | >= 3.1 / >= 2.0 |
| Flask-JWT-Extended | JWT 签发与校验（Header + Query 双通道） | >= 4.6（**注意：3.x 与 Werkzeug 3.x 不兼容**） |
| Flask-SocketIO + python-socketio + simple-websocket | 网页终端实时通道（`async_mode="threading"`，不依赖 eventlet/gevent） | >= 5.3 |
| paramiko | SSH 客户端（连接受管 Linux 主机，含 **SFTP 文件管理器**）**与 SSH 服务端**（堡垒机网关） | >= 3.5 |
| requests | AI 运维（AIOps）：调 DeepSeek 的 `chat/completions` 并**按行读 SSE 流** | >= 2.31 |
| cryptography | Fernet 对称加密：资产口令/私钥/口令短语落库加密 | >= 42 |
| Werkzeug.security | 用户口令哈希（scrypt，自带随机盐） | 随 Flask |
| pytest | 测试（含真实 SSH 协议栈集成测试） | >= 8.0 |

安装：

```bash
cd bastion-backend
python -m pip install -r requirements.txt
```

---

## 二、快速启动

```bash
python run.py                      # 默认 0.0.0.0:5000，SSH 网关 0.0.0.0:2222
python run.py --port 5001 --gateway-port 2222
python run.py --no-gateway         # 只起 Web，不起 SSH 网关
python run.py --debug              # 调试日志
python run.py --reset-admin        # 重置内置管理员口令
```

启动后输出：

| 项目 | 值 |
| --- | --- |
| 后台地址 | `http://127.0.0.1:5000` |
| 健康检查 | `GET /api/health` |
| SSH 网关 | `0.0.0.0:2222`（主机指纹在 banner 与设置页可见） |
| 数据目录 | `bastion-backend/instance/` |
| 默认管理员 | `admin / admin123`（首次登录后**必须改密**） |
| AI 运维（可选） | 读 `bastion-backend/.env` 的 `DEEPSEEK_API_KEY`；**未配置时明确拒绝**（400 `AI_NOT_CONFIGURED`），不静默失败 |

`instance/` 目录内容（首次启动自动生成，**请纳入备份并设 600 权限**）：

| 文件 | 说明 |
| --- | --- |
| `bastion.db` | SQLite 数据库 |
| `secret.key` | Flask SECRET_KEY 与 JWT_SECRET_KEY（随机生成并持久化，**丢失会导致所有 JWT 失效**） |
| `fernet.key` | 资产口令加密主密钥（**丢失则已存资产口令无法解密**） |
| `gateway_host_rsa.key` | SSH 网关主机私钥（RSA 2048） |
| `transcripts/<sid>.log` | 每会话录像（JSON Lines，含命令、输出、放行/拦截） |

---

## 三、目录结构

```
bastion-backend/
├── run.py                      # 启动入口（HTTP + SSH 网关 + 残留会话对账）
├── requirements.txt
├── pytest.ini
├── instance/                    # 运行时数据（自动创建）
├── app/
│   ├── __init__.py             # create_app / 蓝图注册 / 种子数据
│   ├── config.py               # 配置（含 TestConfig）
│   ├── extensions.py           # db / jwt / cors / socketio
│   ├── models.py               # 21 张表 + to_dict（含 AI 三张 + rdp_recordings / rdp_recording_uploads：ai_conversations / ai_messages / ai_tool_calls；host_protocols = 一台主机多个协议端点）
│   ├── security.py             # 权限码、口令哈希、@admin_required、@permission_required
│   ├── schema_sync.py          # 轻量 schema 同步（无 Alembic：只做 ADD COLUMN 的加法迁移）+ backfill_host_protocols() 给老主机补协议端点
│   ├── access.py               # 授权解析：时间窗、账号绑定、可达主机、会话配额
│   ├── policy.py               # 命令策略引擎（分段 + 规则匹配 + 内置策略模板）
│   ├── file_policy.py          # 文件策略引擎（操作 × 路径匹配 + 内置文件策略模板 + 冻结）
│   ├── audit.py                # 审计写入与会话/命令/文件操作落库
│   ├── session_service.py      # 会话生命周期：open_session / teardown / 对账
│   ├── session_registry.py     # 在线会话注册表（内存）
│   ├── idle_sweeper.py         # 空闲超时清理（session_idle_timeout 真正生效：超时自动断开并收口）
│   ├── settings_store.py       # system_settings 读写（带默认值）
│   ├── session_notes.py        # 会话能力边界文案（网页终端与 SSH 网关共用的单一来源：WinRM 的「每条命令单独执行 / 不支持文件传输」）
│   ├── ssh_client.py           # paramiko 连接（口令/私钥/跳板参数）
│   ├── utils.py                # 分页、时间解析、响应封装
│   ├── crypto.py               # Fernet 加解密
│   ├── api/                    # 16 个 API 蓝图，110 条路径 / 141 个 (路径, 方法) 接口组合
│   │   ├── auth.py users.py roles.py hosts.py grants.py
│   │   ├── policies.py file_policies.py files.py
│   │   ├── sessions.py audits.py settings.py ai.py rdp.py
│   │   └── __init__.py         # register_blueprints（新增蓝图必须登记！）
│   ├── ai/                     # AI 运维：tools.py（113 个声明式工具）+ client.py（DeepSeek 流式）+ prompt.py（含 ai-card 协议）+ service.py（回合编排/审批/审计）+ line_split.py（/ask-ai 行分流状态机，网关与网页终端共用）
│   ├── files/                  # SFTP 文件管理器：service.py（会话/操作/策略双闸）+ __init__.py
│   ├── gateway/                # SSH 网关服务端（paramiko ServerInterface）+ ai_shell.py（会话内 /ask-ai）
│   ├── terminal/               # 会话桥：bridge(命令识别) + recorder(录像)
│   ├── winrm/                  # Windows 字符终端（WinRM）：client.py（WSMan 连接/脚本执行/异常人话化）+ bridge.py（WinrmBridge，公开面与 ShellBridge 一致）+ __init__.py
│   ├── webterm/                # Socket.IO 网页终端事件
│   └── rdp/                    # Windows 远程桌面（WebRDP）：cleanpath.py（RDCleanPath 的 X.224/TLS/凭据封包，X.224 的 length 是小端）+ proxy.py（RdpWebSocketMiddleware 字节中继 + 120 秒一次性票据；中继是单线程 + 非阻塞 TLS（setblocking(False)、recv 只认 SSLWantReadError、send 遇 SSLWantWriteError 就 select 等可写）—— OpenSSL 的 SSL 对象不是线程安全的，两个线程并发读写会出现「sendall() 成功但字节没上线」的中继假死；而「读线程 + select + 锁」又会死锁，所以整个中继只用一个线程、一把锁都不用）+ hooks.py（会话记录与审计收口）
├── tools/
│   ├── demo_ssh_target.py      # 假 Linux 演示目标机（真实 SSH 协议栈 + tty 行规程 + exec 请求 + 转义序列过滤 TtyEscapeFilter + SFTP 子系统，默认 127.0.0.1:2200）
│   ├── sftp_backend.py         # 演示用 SFTP 服务端（文件后端 + 子系统安装）
│   ├── live_e2e_check.py       # 真机端到端联调（97 项断言：HTTP + Socket.IO + SSH 网关 + SFTP 文件管理器 + AI 工具目录/客户端版本，含进站字符画/配色/分隔线随内容；项数随数据状态微变）
│   ├── ui_check.py             # 真实 Chrome(CDP) 逐路由巡检后台 UI（21 项断言：16 个路由 + 登录态守卫 + 品牌痕迹/Logo）
│   ├── gw_ai_check.py          # 真实 SSH 网关里跑一次 /ask-ai（9 项断言：选真机进会话 → 粘贴形态提问 → 无「AI 出错/HTTP 400」→ 有真实答案 → 回合后终端仍可用）；**选跑，会消耗一次真实模型调用**
│   ├── console_check.py        # 真实 Chrome(CDP) 驱动网页终端与文件管理器（33 项断言：window.open 弹窗建连/状态条/搜索/右键/全屏往返/断开倒计时与自动关窗/「资产列表」关窗/「文件管理」弹独立窗口 SFTP 列目录/清除入口）
│   ├── winrm_gw_check.py       # 真实 SSH 网关里选一台 Windows（WinRM）主机跑 whoami/hostname（9 项断言：菜单里出现 WinRM 主机 → 进会话有 PowerShell 提示符与能力边界提示 → 输出是目标机的 → exit 回菜单）；**选跑，会在目标机上真执行两条只读命令**
│   └── verify_audit_chain.py   # 审计链式哈希离线校验：逐行重算 + 区分「链前遗留/链内空洞」+ 库外锚点（--print-head 抄锚点 / --expect-head TABLE=HASH 复核，对不上 exit 1）
└── tests/                      # 702 个用例（33 个文件，含真实 SSH 协议栈、真实 SFTP 服务端与网页终端 Socket.IO 端到端）
```

---

## 四、身份与权限模型

### 4.1 三层身份审计

| 层 | 载体 | 表 | 回答的问题 |
| --- | --- | --- | --- |
| 角色 | `Role.permissions`（权限码数组） | `roles` | 这类人**能做什么**（功能级） |
| 用户 | `User`（角色 + 开关） | `users` | 这个人是谁、能否用网关/网页终端、是否被停用/锁定 |
| 授权 | `Grant`（用户 × 主机 × 账号） | `grants` | 这个人**能碰哪台机器、用哪个账号、什么时候能碰、能做什么** |
| 命令策略 | `CommandPolicy` + `CommandRule` | `command_policies` / `command_rules` | 在这台机器上**能执行什么命令、不能执行什么命令** |
| 文件策略 | `FilePolicy` + `FileRule` | `file_policies` / `file_rules` | 在这台机器上**哪些路径能做哪些文件操作**（上传/下载/编辑/删除…），与授权开关**同时成立才放行** |

### 4.2 权限码（36 个，`app/security.py`）

`dashboard:view`、`host:view|manage`、`account:view|manage`、`group:view|manage`、`grant:view|manage`、`policy:view|manage`、`user:view|manage`、`role:view|manage`、`session:view`、`session:view_all`、`session:replay`、`session:terminate`、`command:view`、`command:view_all`、`audit:view`、`terminal:use`、**`rdp:use`**、**`file:use`**、**`filepolicy:view`**、**`filepolicy:manage`**、`setting:view|manage`、**AI 一套（7 个，与人类权限分开）**：**`ai:view`、`ai:use`、`ai:view_all`、`ai:tool`、`ai:tool_write`、`ai:tool_exec`、`ai:manage`**。

内置角色：

| 角色 | 权限要点 |
| --- | --- |
| `admin` | `*`（全部） |
| `ops` | 资产/授权/策略/会话查阅，`session:*`、`command:view_all`、`terminal:use`、**`file:use`、`filepolicy:view`**；**不能**管理用户、角色、系统设置，也不能改文件策略 |
| `auditor` | 只读审计：主机、策略、会话、命令、审计日志；无终端、无写权限 |
| `viewer` | 最小可见：概览、主机、自己的会话与命令（**没有 `command:view_all`，因此不能清除审计**） |

> **AI 权限默认只给 `admin`**（它持有 `*`）。内置的 `ops`/`auditor`/`viewer` 都不含任何 `ai:*`，因此默认进不去 AI 页、也拿不到工具——「AI 工具单独一套权限、按用户分配」是靠这一套码 + 角色管理页的勾选实现的：给谁开 AI，就在角色里勾 `ai:view` + `ai:use`（只读）或再加 `ai:tool_write`/`ai:tool_exec`（写/执行，仍需管理员密码确认）。`live_e2e_check.py` 会断言「目录里 7 个 `ai:*` 码齐备」与「`ops` 拿不到任何工具」这两件事。

- 超级管理员 `User.is_superuser=True` 等价于 `*`。
- **写操作一律 `@admin_required`**（对应需求「只有管理员可以添加机器、设置访问权限和其他设置」）；读操作按 `permission_required("<x>:view")`。
- 只有 `host:manage` 才能读到资产账号明文口令（`to_dict(with_secret=...)`）。
- **超级管理员准入兜底**（`app/access.py` 的 `_superuser_access()`）：`is_superuser` 在**没有任何 Grant** 时也能对「已启用主机」建会话，避免新部署环境下管理员被自己的授权表挡在门外。兜底**只放宽准入、不放宽管控**：临时 `Grant` 不写库（不设任何 relationship，`grant_id` 为空）、会话与命令照常落库、命令策略仍按系统默认策略强制执行；主机无可用资产账号时仍拒绝。

### 4.3 授权（Grant）能力位

`can_login`、`can_sftp`、`can_upload`、`can_download`、**`can_file_write`**、`can_port_forward`、`can_webterm`、时间窗（`time_start`/`time_end`/`weekdays`/`expire_at`，支持跨零点）、`max_sessions`、`policy_id`（冻结式：会话建立时快照策略，事后改策略不影响已建会话）、**`file_policy_id`（绑定文件策略；留空走系统默认文件策略，判定时实时读取、不冻结 —— 管理员改完授权/策略对**已打开**的文件窗口立即生效）**。

**两层判定（必须同时成立）**：授权开关决定「能不能做这个动作」（`can_sftp` 没开连窗口都进不去、`can_upload`/`can_download`/`can_file_write` 分别管上传、下载与改动），文件策略决定「这条路径上允不允许」。任一不过即拒绝，且拒绝也写审计。

**Windows 远程桌面（WebRDP）的准入**：与终端完全同源 —— `rdp:use` 权限码 + 该主机的授权（账号/时段/星期/过期/并发上限）+ `can_webterm`；另加两条协议约束：主机 `Host.protocol` 必须是 `rdp`、账号必须是口令认证（私钥账号不给远程桌面）。反向地，**字符会话只认 `protocol in ("ssh", "winrm")`**（Linux 走 SSH、Windows 走 WinRM，共用权限码 `terminal:use`），所以 `protocol=rdp` 的 Windows 主机不会出现在网关菜单里 —— 它走 `/rdp` 那条入口。

**账号解析纪律**：未指定账号 → 精确账号授权优先，否则整机授权 + 该主机第一个可用账号；显式指定账号 → 只认绑定该账号的授权或覆盖整机的通配授权，**绝不静默替换成另一个账号**（有专门回归测试）。

---

## 五、命令策略引擎（`app/policy.py`）

1. **分段**：单遍状态机按 `&& || ; |` 与换行拆段，正确处理引号与反斜杠；再抽出 `$(...)`、反引号内的子命令递归判定 —— `ls && rm -rf /`、`echo $(rm -rf /)` 都会被拆开分别判定。
2. **匹配**：规则按 `priority` 升序，首条命中即定论；`match_type` 支持 `regex`（`re.search`，含 normalized 兜底）/ `prefix` / `exact` / `contains`；`action` ∈ `allow|deny|confirm`。
3. **内置模板**（`BUILTIN_POLICY_REV` 版本化，升级后自动刷新内置规则，管理员自建策略不受影响）：

| 策略 | 默认动作 | 关键规则 |
| --- | --- | --- |
| 只读审计策略 | deny（白名单） | 仅放行查看类命令；**拦截输出重定向与 tee/dd/truncate/mkfifo/mknod**（防止 `echo x > /etc/passwd` 借白名单写文件）；**拦截读取 `/etc/shadow`、`/etc/sudoers`、私钥等凭据文件** |
| 标准运维策略 | allow（黑名单+高危拦截） | 拦截 `rm -rf /`、`mkfs`、`dd of=/dev/*`、`shutdown/reboot`、账号变更、`visudo`、`iptables -F`、`history -c` 等；拦截写系统关键路径与关闭审计；**同样拦截读取凭据文件** |
| 高危需确认 | confirm | 高危命令需二次确认 |
| 全审计放行 | allow | 只记录不拦截（用于排障期） |

> 只读白名单按**命令字**放行 `cat`，因此必须叠加「凭据文件黑名单」与「重定向黑名单」，否则 `cat /etc/shadow`、`echo x > /etc/passwd` 会穿透 —— 这两条是真实发现的越权路径，已修复并有回归测试。

---

## 六、SSH 网关（需求④，`app/gateway/server.py`）

```
ssh -p 2222 <堡垒机账号>@<堡垒机IP>
```

- 用 `paramiko` 实现 SSH 服务端（`ServerInterface`），支持口令认证与公钥认证，主机密钥持久化在 `instance/gateway_host_rsa.key`。
- 登录后先进**彩色进站横幅**（实心块盾牌 + `A u t o O p s` 字标 + 副标题），再进入**审计菜单**：列出你有权限的主机（编号/名称/地址/分组/策略/账号），输入编号选择，多账号再选账号，输入 `l` 刷新、`q` 退出、`h` 帮助、`i` 查看本人信息。
- 会话配额、时间窗、授权能力位、`gateway_enabled` 开关、首次登录强制改密，全部在网关内强制校验。
- 选中主机后建立到目标机的 SSH 通道，**用户输入 → 策略判定 → 放行或拦截**；被拦截的命令**不会到达目标机**，用户会即时看到拦截原因。
- 每条命令（命令文本、执行输出、放行/拦截、风险级别、命中规则、耗时）写入 `command_logs`，整段会话写入 `instance/transcripts/<sid>.log`。
- 失败登录累加 `failed_attempts`，达 `login_max_failures` 后锁定 `login_lock_minutes` 分钟，并写审计。
- **终端排版与观感**：所有输出统一走 `to_crlf()`（`\r\n`）—— PTY 里的裸 `\n` 只把光标下移、**不回列**，菜单文字会排成阶梯状；列对齐用 `pad_display()`/`display_width()` 按**显示列宽**（CJK 记 2 列）补齐，中文主机名/账号名不会顶歪后面的列；终端窄于 `COMPACT_MENU_WIDTH`（96 列）时主机信息自动切换为「主机行 + 明细行」两行式，避免 95 列的单行在 80 列终端（PuTTY 默认）被折行。**配色与分隔线**：`display_width()` 先 `strip_ansi()` 再算列宽（否则 `\x1b[96m` 被当成 5 列宽，中英混排立刻歪），颜色只裹**取值**、标签（` 策略: ` / ` 账号: `）保持纯文本；菜单的四条 `=` 分隔线长度各自等于相邻内容块的显示宽度（`_menu_blocks()` + `_menu_rules()`，不再写死 78 列），上限 `终端列数 - 1`、下限 20 列。**进站字符画**（`_render_banner()`）：实心块盾牌 + `A u t o O p s` 字标 + 副标题，整体居中、只有 CRLF，终端宽度 < 图形宽 + 6 时整块退化为空（绝不折行）；**竖笔画一律用文本**——`▀▄` 半块与 `█` 点阵字母在真实截图里都会因字体行高留缝而碎成虚线，块字符只用于横向条块。以上都有回归用例（`tests/test_gateway_menu.py`，19 条）。
- **输出时序与 ONLCR**：目标机欢迎语不得抢在堡垒机横幅之前 —— `open_session(on_ready=...)` 在 `bridge.start()` **之前**回调，横幅与会话号从这里发出；目标机的裸 `\n` 在客户端可见路径上统一由 `LineEndingNormalizer` 补成 `\r\n`（等价于替不做 `ONLCR` 的目标机/网络设备补上转换，**录像仍记录原始字节**）。两条都有真机级回归用例（`tests/test_terminal_output.py`）。
- **会话控制指令不受命令策略限制**：`exit` / `quit` / `logout` / `bye` 由 `policy.is_session_exit()` 在策略评估**之前**放行 —— 否则只读白名单策略会把它们判成「白名单外一律拒绝」，命令发不到目标机、远端 shell 不结束，**用户被困在会话里出不去**（横幅却写着「输入 exit 返回主机菜单」）。放行后仍然完整落审计（`action=allow`、`reason` 标注「不受命令策略限制」）；回归用例见 `tests/test_integration_ssh.py::test_session_exit_is_allowed_under_readonly_policy`。
- **会话内 `/ask-ai <问题>`**：连上主机后随时问 AI，按键照旧转发给目标机、命中时补发 `Ctrl-U` 抹掉该行（不会被目标机当命令执行），Markdown 原地重绘流式渲染、卡片降级 ASCII、敏感操作走掩码口令确认 —— 详见 **七·七**。**行分流状态机是 `app/ai/line_split.py` 的 `LineShadow`**（与网页终端共用同一份实现）：命中那一行的回车不转发，但**同一 chunk 里回车之后的字节必须继续转发**（粘贴收尾的 `ESC[201~` 被吞掉会让远端 readline 卡在 bracketed paste 模式）；CSI/SS3/OSC 转义序列一律不计入用户输入行，粘贴标记 `ESC[200~`/`ESC[201~` 不清空影子行，方向键等非无害序列保守清行，影子行上限 512 字节。回归：`tests/test_ai_line_split.py`（34）与 `tests/test_gateway_ai_shell.py`（46）。
- **连接目标机后自动清屏**：会话就绪先写 `\x1b[2J\x1b[H`，再重画上下文（主机/账号/会话号）与横幅，最后补一个**裸回车**让远端重画提示符 —— 不这么做，终端里会留着目标机上一屏的滚屏（用户反馈「太乱了」）。清屏与重画卷在网关与网页终端**同一措辞**；裸回车走在 `ShellBridge.feed_input()` 的 Enter 分支之前（空行在任何 `_start_command()`/`evaluate_policy()` 之前返回），**不落 `command_logs`、不进策略引擎**。`_write_session_context()` 是两条入口共用的措辞来源，它同样按协议追加能力边界提示（`app/session_notes.py`）。回归：`tests/test_gateway_clear.py`（10）。
- **握手两端版本都可观测**：`ConnectionInfo` 同时带 `server_version`（对端目标机在 `SSH-2.0-...` 里声明的）与 `client_version`（堡垒机作为 SSH 客户端声明的，`SSH-2.0-paramiko_<版本>` —— 与 paramiko 的 `Transport.local_version` 大小写一致；拿不到传输层时用 `default_client_version()` 兜底，兜底真值也按小写 `paramiko`）。`GET /api/settings/gateway` 回传 `serverVersion`（网关自己的 `SSH-2.0-BastionGW_1.0`）+ `clientVersion`，`POST /api/hosts/<id>/accounts/<aid>/test` 回传目标机的两者，前端在「系统设置 → SSH 网关」与「主机 → 账号 → 连接测试」并排显示。回归：`tests/test_client_version.py`。

## 七、网页终端（需求②，`app/webterm/events.py` + `app/terminal/`）

- Socket.IO 通道，`connect` 阶段校验 JWT（`auth.token` / `Authorization` / `?token=`，含登出黑名单）、账号启用状态、`webterm_enabled` 开关与 `terminal:use` 权限。
- 事件：`terminal:open` / `input` / `resize` / `close` / `ping` ↔ `terminal:opened` / `output` / `command` / `notice` / `closed` / `error`。
- `OutputPump` 用增量 UTF-8 解码器解决多字节字符被拆包乱码，并按时间片合批推送；积压超阈值丢弃最旧数据并提示，避免内存膨胀。它带一个**闸门**（`pause()`/`release()`）：`terminal:opened` 只会在服务端真正存好会话之后发出，而目标机输出先被攒着 —— 既不抢在会话头之前到达，也不会出现「客户端按 opened 发键时服务端还没就绪」的竞态。
- 会话就绪前到达的按键会**先缓冲、就绪后按序回放**（`ctx["early_input"]`），绝不静默丢弃；客户端每次都会收到 `terminal:opened`（含 `segmented` 字段，前端据此决定是否提示降级）。
- 网页终端与 SSH 网关共用同一套 `open_session`、策略引擎与录像存储 —— **两条入口的每条命令与输出都会被记录**。
- **网页终端里同样能用 `/ask-ai <问题>`**：`on_input` 先把按键喂进 `app/ai/line_split.py` 的 `LineShadow`，命中就补发 `Ctrl-U` 抹行（**该行不会被目标机执行**），随后起一条独立线程跑 `ai_shell.run_ask_ai`，流式 Markdown 走该会话的 `OutputPump` 原地重绘；AI 等输入（掩码口令确认）时按键进队列、**不会漏给目标机**，超时/中止会复位状态。回归：`tests/test_webterm_ai.py`（6，含真实 Socket.IO 链路）。
- **连接目标机后自动清屏**（与网关同一措辞）：会话就绪先清屏再重画上下文，随后补一个裸回车；裸回车不落库、不进策略（`CommandLog` 计数为 0）。用户反馈的「连上 Linux 客户机之后清一下屏，要不然太乱了」即为此项。重画的三行上下文之后还会按协议追加**能力边界提示**（`app/session_notes.py`：WinRM 会话多两行，说明「每条命令单独执行、cd 保留 / 进程内状态不保留 / 交互式程序与文件传输不可用」）—— 桥接层自己的连接横幅紧跟着就被这句清屏擦掉了，所以提示必须在这里重打（详见七·十）。

命令识别采用「提示符标记（marker）」方案：会话建立时把目标机 `PS1` 改写为带随机 marker 的提示符，据此精确切分每条命令的边界与输出归属；若目标机不允许改写 `PS1`，则**自动降级为原始录制模式**并明确提示用户。

---

## 七·五、文件管理器与文件策略（需求⑤，`app/files/` + `app/file_policy.py`）

终端弹窗工具条的「文件管理」按钮会 `window.open()` **再弹一个独立窗口**（前端路由 `/files/console`，`layout:false`、不进菜单），窗口内用 **paramiko SFTP** 可视化浏览与操作被管主机的文件；每个操作都过访问控制，并**全部留痕**。

- **服务层** `app/files/service.py`：`FileSession` 持有 `sftp` 通道与 `grant_id`，`open_file_session()` 建会话（同时写 `session_records`，`protocol=sftp`），`FileError(code, status, message)` 统一成业务错误。操作集：`list` `read` `download` `archive` `upload` `write` `mkdir` `rename` `move` `copy` `delete` `chmod`；分集常量 `WRITE_OPERATIONS`（上传/编辑/新建/改名/移动/复制/删除/改权限）、`DOWNLOAD_OPERATIONS`（download/archive）、`UPLOAD_OPERATIONS`。每用户在线文件会话上限 `MAX_SESSIONS_PER_USER=8`。
- **两道闸（AND）**：
  1. `Grant` 开关 —— `can_sftp` 决定能不能开/留这个窗口，`can_upload`/`can_download`/`can_file_write` 分别管上传、下载、改动；未开放的按钮在前端直接置灰并给出原因。
  2. `FilePolicy` 规则 —— `evaluate_file_policy(冻结策略, 操作, 路径, target_path=…)`，规则按 `priority` 升序**首条命中即定论**，否则用 `default_action`；`match_type` 支持 `glob` / `regex` / `prefix` / `contains`；`operation` 只接受单个操作或 `*`。**`rename`/`move`/`copy` 同时校验源路径与目标路径，任一被拦即整条拒绝**（否则可以把合法文件复制进 `/etc`）。
- **授权变更实时生效**：`FileSession.rights()` 每次判定都按 `grant_id` 从库里重读四个开关（授权行被删 → 全 False），所以管理员**撤销 SFTP 或收回改文件权限对已经打开的窗口立即生效**，不需要用户重开窗口（`capabilities()` / `to_dict()` 也走它）。
- **内置文件策略**（`BUILTIN_FILE_POLICY_REV` 版本化，升级后对老库重新播种）：
  - `默认文件策略·敏感路径拦截`：默认放行常规运维操作，叠加 14 条拦截规则 —— `.ssh` 目录、私钥与凭据文件（`id_rsa`/`*.pem`/`.netrc`/`.pgpass` 等）、系统目录写入（`/etc`、`/boot`、`/sys`…）、**复制到系统目录**（按目标路径）等。
  - `只读浏览策略`：`default_action=deny`，只放行 `list` 导航（`^/.*$`）与白名单目录下的 `archive`（打包下载）；用于「能看不能碰」的岗位。
- **留痕**：每次操作写一条 `file_logs`（用户/主机/会话号/操作/路径/目标路径/动作/风险级别/命中规则号与规则文本/原因/结果/字节数/文件数/耗时），同时累加会话记录的 `command_count`、`bytes_out`、`max_risk_level`。三种结果都记：`success`、`denied`（授权级拒绝 `matchedRuleId` 为空、策略级拒绝带命中规则）、`failure`（例如打包不存在的路径 → 404 且留一条失败审计）。`check_operation`（试算）只判定、不写审计。**失败文案统一中文框**：`_audit()` 里对非成功结果调 `_human_error(operation, message)` —— 已含中文或空串原样返回，否则框成「修改文件权限失败：Permission denied」（`operation_label()` 给操作起中文名），**异常原文一字不改**（收敛全部失败分支，回归 `test_failure_audit_message_is_framed_in_chinese_but_keeps_the_raw_error`）。
- **其它实现细节**：在线编辑保存做 **mtime 冲突检测**（文件被别人改过 → 409 `FILE_CONFLICT`）；下载带 `Content-Disposition`（含 UTF-8 文件名）；打包下载流式返回 `application/zip`（临时文件用完即删）；`rename` 先试 `posix_rename`，遇到 `Operation unsupported` 回退 `sftp.rename`；读二进制文件直接 415 `FILE_BINARY`。
- **改权限如实回读**：`chmod` 返回 `{"path","before","after","requested"}` —— `after` 是**改完后重新 `stat` 读到的真实权限**（读不到才退回 `requested`），审计 message 也用真实值。原因：Windows 上的目标机（含演示目标机）`os.chmod` 只能切只读位，若 `after` 直接回请求值，就会在审计里留下「声称改了、其实没改」的假成功（真机联调抓到的缺陷）。
- **时间字段口径（踩过坑）**：`list` / `stat` 返回的 `mtime` 是 **ISO8601 字符串**（`_iso()` 产出，UTC 带 `Z`），而 `read` 返回的 `mtime` 是 **epoch 秒（数字）** —— 前端曾把前者当数字乘 1000，整列显示成 `NaN-NaN-NaN NaN:NaN`（浏览器实测揪出的缺陷）。改前端时按 `FileEntry.mtime: string` / `FileReadResult.mtime: number` 区分。
- **窗口被遗弃也会收口**：关掉文件管理器标签页不会触发前端卸载清理，会话会留在注册表里；`app/idle_sweeper.py` 按 `session_idle_timeout`（默认 1800 秒）扫 `last_active`，超时自动断开并写 `session_records.end_reason = "空闲超时自动断开"`（详见「七·六」）。

### 文件域接口

| 接口 | 语义 |
| --- | --- |
| `POST /api/files/sessions` | 开会话，返回 `{sid, homeDir, canSftp, canFileWrite, policyName, operations…}` 能力字典（`file:use`） |
| `GET /api/files/sessions` / `GET|DELETE /api/files/sessions/<sid>` | 在线文件会话列表 / 单个会话详情与关闭 |
| `GET /api/files/sessions/<sid>/list` `/stat` `/read` `/download` | 浏览、属性、读文本、下载（`action=download`） |
| `POST /api/files/sessions/<sid>/write` `/mkdir` `/rename` `/copy` `/delete` `/chmod` `/archive` `/upload` | 写入类操作（要求 `can_file_write` / `can_upload` 且策略放行） |
| `POST /api/files/check` | 操作试算：只回 `{allowed, action, riskLevel, ruleId, rulePattern, reason, policyName,…}`，不落审计 |
| `/api/file-policies`（CRUD + `options`/`operations`/`reset-builtin`/`test`/`evaluate`）、`/api/file-rules` | 文件策略与规则管理（`filepolicy:view` / `filepolicy:manage`） |
| `GET /api/audits/files`、`POST /api/audits/files/delete`、`GET /api/audits/files/options` | 文件审计查询 / 清除（`command:view_all`）/ 筛选候选项 |

---

## 七·六、会话空闲超时（`app/idle_sweeper.py`）

`session_idle_timeout`（参数设置里可改，默认 1800 秒）由守护线程真正执行：`create_app()` 启动 `bastion-idle-sweeper`（`TESTING` 或 `IDLE_SWEEPER_DISABLED` 时不启动），每 30 秒扫一次在线会话注册表，把 `now - last_active >= timeout` 的会话 `registry.close(sid, reason="空闲超时自动断开")` 并收口会话记录 —— 网页终端、SSH 网关、SFTP 文件管理器三条路径共用同一份 `last_active`：网页终端/网关由 `app/terminal/bridge.py` 在**收到目标机输出**与**收到用户按键**时 `registry_touch(sid)`，文件管理器由 `FileSession.touch()` 同步更新。`timeout <= 0` 视为不清理。

> 为什么必须有它：关掉浏览器标签页不会触发前端卸载清理（也不该信任客户端），会话会一直挂在注册表里占配额 —— 真机实测泄漏过 8 条文件会话，正好撞满 `MAX_SESSIONS_PER_USER=8`，后续 `POST /api/files/sessions` 全部报 `QUOTA_EXCEEDED`。回归：`tests/test_idle_sweeper.py`（3 条）+ `tests/test_files_service.py::test_abandoned_file_session_is_reaped_by_idle_sweeper`。
>
> 真机验证（`session_idle_timeout=20`）：以 `e2e-ops` 开一条文件会话 → 75 秒后 `/api/files/sessions` 为空，会话记录 `status=terminated`、`endReason="空闲超时自动断开"`。

---

## 七·七、AI 运维（AIOps，需求⑥~⑬，`app/ai/` + `app/gateway/ai_shell.py`）

把 DeepSeek 接成「会用堡垒机全部功能的助手」：它在**调用者本人的权限**下操作堡垒机，所有对话、工具调用、确认动作全部入审计。

### 配置（`bastion-backend/.env`，已被 `.gitignore` 忽略）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `AI_ENABLED` | `1` | 关掉后所有 AI 接口 403 `AI_DISABLED`，菜单也不显示 |
| `DEEPSEEK_API_KEY` | 空 | **必填**；缺失时 `/api/ai/chat` 直接 400 `AI_NOT_CONFIGURED`（不是先 200 再在流里报错） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 兼容 OpenAI 协议的中转地址也能用 |
| `AI_MODEL` | `deepseek-flash` | |
| `AI_REQUEST_TIMEOUT` / `AI_MAX_TOKENS` | `120` / `8192` | |
| `AI_MAX_TOOL_ROUNDS` | `8` | 一个回合里最多几次「模型要工具 → 执行 → 回灌」 |
| `AI_HISTORY_LIMIT` | `40` | 回灌的历史消息条数 |
| `AI_CONFIRM_TTL` | `300` | 敏感操作确认的窗口（秒） |

`app/config.py` 用 `load_dotenv()` 读它；模板见 `.env.example`。

### 五个模块

| 文件 | 职责 |
| --- | --- |
| `app/ai/tools.py` | **113 个声明式工具**（覆盖人类能做的全部功能：资产/账号/授权/策略/用户/角色/会话/审计/终端/文件/系统设置），工厂 `T(name, description, method, path, args="", *, permission=READ, category="其他", sensitive=False, query=(), card="")`；权限三档 `ai:tool`（只读）/`ai:tool_write`（写，默认敏感）/`ai:tool_exec`（执行，默认敏感）；`tools_for_permission(permissions, *, is_admin=False)` 按调用者权限裁剪；`build_request()` 拼请求；**`invoke(app, tool, args, token)` 用 `app.test_client()` 带 `Authorization: Bearer <调用者自己的 JWT>` 打自己的 REST** —— 所以 AI 能做的事永远不超过这个人本人能做的事，并且天然复用命令策略、文件策略、授权与会话配额 |
| `app/ai/client.py` | `create_client(config)` → `DeepSeekClient`，`stream_chat()` 按行读 SSE。**真实事件位移字段是 `text`**（`{"type":"reasoning"\|"content","text":...}`、`{"type":"tool_call","index":..,"id":..,"name":..,"arguments":..}`、`usage`、`finish`），`service.py` 再转成对前端友好的 `{"type":"content","delta":...}` |
| `app/ai/prompt.py` | 系统提示词 + **````ai-card` 卡片协议**（`table` / `keyvalue` / `alert` / `steps`）：模型把结构化结果放进围栏代码块，网页端渲染成 antd 卡片、SSH 终端降级成 ASCII |
| `app/ai/service.py` | 回合编排：`Caller.from_user(user, *, token, source, ip, user_agent)`（携带**用户自己的 JWT**，工具执行时用它）、`create_conversation`、`add_message`、`run_turn(app, conversation, caller, client, *, user_text="", resume=False)`、`execute_tool`、`pending_tool_calls`、`approve_tool_call` / `reject_tool_call`、`extract_cards` / `strip_cards` |
| `app/ai/line_split.py` | **`/ask-ai` 行分流状态机**（`AI_PREFIXES`、`extract_question`、`is_ai_command`、`class LineShadow.feed(raw) -> (forward_bytes, question\|None)`）：SSH 网关（`app/gateway/server.py`）与网页终端（`app/webterm/events.py`）**共用同一份实现**。命中那一行的回车不转发，其余字节照常转发；CSI/SS3/OSC 跨 chunk 保持状态、粘贴标记 `ESC[200~`/`ESC[201~` 不清行、非无害序列保守清行、上限 512 字节 |

### 流式事件契约（`POST /api/ai/chat`，SSE）

`start{conversationId,messageId,model,tools}` → `reasoning{delta}` / `content{delta}` → `tool_call{callId,name,args,sensitive,permission,status,description}` → `tool_result{callId,name,ok,status,summary,preview,card,durationMs,confirmedBy?}` → `cards{cards}` → `message_end{messageId,content,reasoning,cards,usage,finishReason,elapsedMs,status}`，异常走 `error{message,status?}`；外层另有 `open`/`close`。

**敏感操作**：命中 `sensitive=True`（写配置或在目标机执行动作）时，服务层不执行，先发 `confirm_required{conversationId,toolCalls[],toolCallId,name,permission,count,args,reason}`；前端弹管理员账号密码框、SSH 里走掩码文本提示，通过 `POST /api/ai/confirm` 由**管理员**验证口令后才 `approve_tool_call` 执行（写 `audit_logs(action="ai_tool_confirm")`），拒绝则 `reject_tool_call`。没有待确认记录时返回 400 `NOTHING_TO_APPROVE`。**确认请求里只有密码是必填的**：`adminUsername` 留空即回退到发起人自己的账号（前端标签写的就是「留空则用当前账号」）—— 早期要求两者都非空，照着标签填（账号留空）必然 400，页面会同时弹出「请输入管理员账号与密码」与 axios 的 `Request failed with status code 400`；安全口径一步未放松：仍要求 `is_admin()` 且口令校验通过，账号留空 + 口令错或发起人不是管理员依旧是 403、工具保持 `pending` 且不写库（回归：`tests/test_ai_api.py` 两条）。

### 历史协议：`tool_calls` 必须配得上 `tool` 响应

上游是 OpenAI 兼容协议：assistant 声明了 `tool_calls`，后面就**必须**紧跟同 `tool_call_id` 的 `role="tool"` 消息。历史一旦在这里破了，`POST /api/ai/chat` 会一直回 `DeepSeek 返回 HTTP 400`（上游响应体为空，终端只看到一句状态码），而且**这个对话之后每次请求都 400** —— 历史是累积的。

**破口在「拒绝」**：`reject_tool_call()` 早期只把 `ai_tool_calls.status` 改成 `rejected`，没有写回 tool 消息，于是那条 `tool_calls` 永久悬空；而 `pending_tool_calls()` 只查 `pending`/`approved`，对 rejected 记录是隐形的，下一轮就带着坏历史直接去找模型。

现在的两层防护：

1. **写侧** —— `reject_tool_call()` 拒绝时补一条 tool 响应（`_append_unexecuted_tool_message()`，内容形如 `[工具 x] 未执行：管理员 y 拒绝（或取消）了该操作：…`），同一 `call_id` 已有响应则不重复写。
2. **读侧** —— `build_messages()` 还原历史时就地做**协议修复**：没等到结果的 `tool_calls` 补一条「未执行」的 tool 响应；`limit` 把窗口切在中间产生的**孤儿 tool 消息直接丢弃**；`status="error"` 的 assistant（`（调用模型失败：…）`是堡垒机自己的占位，不是模型的话）不回灌。**因此修复前产生的坏对话不用清库，下一轮自动自愈**（真机验证：把线上库拷一份，两条坏对话各续问一句 → HTTP 200、零 `error` 事件）。

另外 `_http_message()` 在上游响应体为空时返回「…（上游响应体为空，无错误详情）」，避免只丢一个状态码、没法排查。回归：`tests/test_ai_api.py` 的 `_protocol_problems()` 校验器 + 5 条用例（补齐缺失响应 / 丢窗口孤儿 / 跳过 error 占位 / 拒绝后下一轮报文合法 / 历史遗留悬空自愈）。

### 数据表与审计口径

| 表 | 关键列 |
| --- | --- |
| `ai_conversations` | `title`、`user_id`/`username`、`source`（`web`/`shell`）、`model`、`host_id`/`host_name`/`host_address`/`sid`、`message_count`/`tool_count`/`token_count` |
| `ai_messages` | `role`、`content`、`reasoning`、`tool_calls`(JSON)、`tool_call_id`、`cards`(JSON)、`status`、`model`、`prompt_tokens`/`completion_tokens`/`reasoning_tokens`、`finish_reason`、`elapsed_ms` |
| `ai_tool_calls` | `call_id`、`tool_name`、`arguments`(JSON)、`required_permission`、`sensitive`、`status`（`pending`/`approved`/`rejected`/`running`/`success`/`failure`）、`confirmed_by`/`confirmed_at`、`result_summary`/`result_preview`、`error`、`duration_ms` |

「对话入审计」是三层：**用户说了什么、AI 回了什么**落 `ai_messages`；**调用了什么工具、参数、结果**落 `ai_tool_calls`；工具真正打到 REST 后，命令/文件/配置变更继续落 `command_logs`/`file_logs`/`audit_logs`。审计中心新增「AI 对话审计」页（`/audit/ai`）同时看这三层。

**审计文案口径（给人看的用中文直白描写，给系统看的用英文）**：写库时就分两层 —— 人读的 `message`/`result_summary` 输出中文（工具摘要经 `_human_message()` 归一：`ok`→`成功`、`OK（共 N 条）`→`成功（共 N 条）`；文件域失败经 `files/service.py` 的 `_human_error()` 框成「修改文件权限失败：Permission denied」，**异常原文一字不改**），机器读的字段（`action` 如 `ai_tool_call`/`ai_chat`、权限码、工具名、`reason`/`status` 枚举）保持英文。**审计记录不可回改**，所以历史里那批老文案（`OK`/`ok`）由前端展示层 `humanAuditMessage()`（`src/services/bastion/constants.ts`）兜底翻译 —— 只归一开头的机器词，括号说明与异常原文原样保留。

### 接口（9 条，`app/api/ai.py`）

| 方法 | 路径 | 守卫 |
| --- | --- | --- |
| GET | `/api/ai/status` | 登录即可（返回 `canUse`/`canViewAll`/`canManage`/`toolCount`/`permissions` 等，前端据此渲染） |
| GET | `/api/ai/tools` | 登录即可，但 `items` 按调用者权限裁剪（没 `ai:tool*` 就是空） |
| POST | `/api/ai/chat` | `ai:use` |
| POST | `/api/ai/confirm` | `ai:use`（口令由管理员账号校验） |
| GET | `/api/ai/conversations` | `ai:view`（他人对话需 `ai:view_all`/`ai:manage`） |
| GET | `/api/ai/conversations/<id>` | `ai:view` |
| DELETE | `/api/ai/conversations/<id>` | `ai:view` + 本人或 `ai:manage` |
| GET | `/api/ai/tool-calls` | `ai:view` |
| GET | `/api/ai/settings` | `ai:manage` |

另有 `POST /api/terminal/exec`（给 AI 的远程执行通道）：需 `terminal:use` **且** `ai:tool_exec`，复用命令策略与审计，不能被普通终端权限顺带拿到。

### 会话里的 `/ask-ai`（SSH 网关与网页终端共用，`app/gateway/ai_shell.py` + `app/ai/line_split.py`）

在网关 shell 里连上主机后随时可以问 AI：`/ask-ai 这台机器磁盘满了吗`。

三条设计决定（都写在模块 docstring 里）：

1. **用户按键照旧逐字节转发给目标机**（回显、补全、`Ctrl-C` 都是目标机 bash 的行为）；网关与网页终端都维护一份「影子行缓冲」（`app/ai/line_split.py` 的 `LineShadow`，两条入口共用同一实现、行为完全一致）判断这一行是不是 `/ask-ai`，命中时补发 `Ctrl-U`（readline 的 `unix-line-discard`）把该行抹掉、**不转发回车** —— 所以 `/ask-ai ...` 绝不会被目标机当命令执行。**粘贴也要能拦住**：转义序列（CSI/SS3/OSC）跨 chunk 保持状态且不计入输入行，粘贴标记 `ESC[200~`/`ESC[201~` 不清空影子行，命中后**同一 chunk 里回车之后的字节继续转发**（否则粘贴收尾标记被吞、远端 readline 卡在 bracketed paste）。
2. **不输出可交互控件**：卡片在终端降级成 ASCII 表/键值对（`render_cards_text`），正文用 `TerminalMarkdown` 逐行原地重绘（`\r\x1b[K` + 渲染后的行）实现 Markdown 流式渲染；````ai-card`` 块整块丢弃、绝不让 JSON 漏到终端。
3. **审批与审计与网页端同一套**：敏感操作在这里走「管理员账号 / 管理员密码（掩码）」两步文本提示，通过后仍调 `approve_tool_call` 并写同一条审计；网关线程没有浏览器 JWT，`_mint_token(user, hours=2)` 在内存里临时签发 2 小时 JWT（不落盘不外发，只给 `app.test_client()` 的同机请求用），保证 AI 在 Shell 里能做的事不超过这个人在网页端能做的事。

配套回归：`tests/test_gateway_ai_shell.py`（46 条，覆盖命令识别、影子行缓冲、Ctrl-U/Ctrl-C/Ctrl-D/ESC、流式渲染、卡片降级、`.env` 缺失、无权限、口令审批/拒绝、`source="shell"` 落库）、`tests/test_ai_line_split.py`（34 条，`LineShadow` 状态机本身：跨 chunk 转义、粘贴标记、命中行尾部字节、别名与上限）、`tests/test_webterm_ai.py`（6 条，网页终端入口含真实 Socket.IO 链路）。

---

## 七·八、审计链式哈希（防篡改，`app/models.py` + `app/audit.py` + `tools/verify_audit_chain.py`）

三张流水表 `audit_logs` / `command_logs` / `file_logs` 每行挂两个哈希（`prev_hash`、`entry_hash`，64 位小写 hex），**按 `id` 升序单向成链**：

- `entry_hash = HMAC-SHA256(key, table + prev_hash + 规范化字段)`，`key = sha256(b"bastion-audit-chain-v1" + SECRET_KEY)`（`app/models.py` 的 `EntryHashMixin` / `compute_entry_hash()`）；密钥**只从 `SECRET_KEY` 派生、不落库** —— 拿到库也伪造不出新哈希。
- 字段规范化 `_norm()`：`None`→`""`、`datetime`→`isoformat(timespec="microseconds")`、`bool`→`"1"/"0"`、`dict`/`list`→`json.dumps(ensure_ascii=False, sort_keys=True, separators=(",",":"))`；`name\x02value` 之间用 `\x01` 连接（改成长度前缀更稳，列为后续加固项）。
- 写库时序（`log_event` / `log_command` / `log_file_op`）：`add()` → `flush()`（拿自增 id）→ `_last_entry_hash(model, before_id=id)` 取**上一条**（`id <` 当前 id）→ 赋两列 → `commit()`。**不能** flush 完直接读「上一行」：自己刚 flush 出来的空 `entry_hash` 会被当先驱，导致第 2 条起 `prev_hash` 全空（有回归用例钉着）。
- 校验 `verify_table_chain(model, *, secret_key=None, expected_head=None)` 逐行重算，返回 `{ok, total, verified, prefix, pending_inside, first_bad, head, anchor_checked, errors[:32]}`：

| 情况 | 判定 |
| --- | --- |
| **链起步前**的空哈希行（老库升级遗留） | 合法：计入 `prefix`，不报错，也不推进 `prev_hash` |
| **链起步后**再出现空哈希行 | **异常**：计入 `pending_inside`、`first_bad` 指向该行、报「链内出现空 entry_hash（疑似清空哈希冒充升级前遗留）」—— 把哈希清空同样是「改哈希」 |
| 字段被改 / 连接关系被改 | `entry_hash` 重算不符 或 `prev_hash` 与上一行不符，`first_bad` 点名到行 |
| 整段删尾行 | **库内自洽、抓不出来**（链只能证明「手上这串连续」）→ 必须比对**库外锚点**：`expected_head` 与 `head` 不符即报「链尾与库外锚点不符（链被截断或表被替换）」 |

- `GET /api/audits/chain`（`audit:view`）给界面用：`counts[表] = {total, hashed, pending, pendingPrefix, pendingInside}`、`heads[表] = {lastId, lastEntryHash, verifiedHead}`（`lastEntryHash` 是最后一行**存储值**，`verifiedHead` 是最后一个**非空**哈希 —— 抄锚点用它），`healthy` **只看 `pendingInside`**（外加最新样本 `prev_hash` 自洽）：老库有遗留行不会让徽标一上线就全红。
- `POST /api/audits/chain/verify?table=<表>&expected_head=<哈希>`（`audit:view`，只读校验，审计员也该能自证）：`expected_head` 必须与 `table` 搭配，否则 400 `ANCHOR_NEEDS_TABLE`。
- CLI（离线，不依赖 Flask 会话）：

```bash
python tools/verify_audit_chain.py                 # 逐表逐行校验，坏链 exit 1；打印「遗留前缀 N, 链内空洞 N」
python tools/verify_audit_chain.py --print-head    # 打印三张表链尾锚点，抄到库外（异地日志/工单）
python tools/verify_audit_chain.py --expect-head audit_logs=<哈希>   # 复核：对不上 exit 1（可重复传）
```

真实库实测：`[OK] audit_logs: 总行 87, 已哈希 87, 遗留前缀 0, 链内空洞 0, 坏首行 None`（`command_logs` 6/6、`file_logs` 0/0）；`--expect-head audit_logs=deadbeef` → `[FAIL]` + 「库外锚点不符：期望 deadbeef… 实际 99252fe4902d…（链被截断或表被替换）」+ **exit 1**。审计页 `/audit/logs` 顶部有「链完整性」徽标（`✓ CHAIN INTEGRITY · 链式哈希 · 完整可信` + 三表统计 + 「刷新状态」/「逐行校验哈希」），**徽标红 ⇔ `verify.ok=False`，界面与 CLI 必须同声**（对抗测试把两者一起断言）。

配套回归：`tests/test_audit_chain.py`（9 条：新记录两哈希非空且首条 `prev_hash` 为空、长链自洽、改字段/改 `prev_hash` 检出、三表都挂链、`compute_entry_hash` 确定性、遗留前缀语义）、`tests/test_audit_chain_tamper.py`（3 条对抗用例：**清空链尾哈希必须判红且点名行 id**、**老库遗留前缀不得把完好的链判红**（`pendingPrefix`/`pendingInside` 契约）、**截断只对库外锚点可见**）。

---

## 七·九、Windows 远程桌面（WebRDP，`app/rdp/` + `app/api/rdp.py`）

前七节的网页终端只给 Linux 机器开 shell。这一节让**浏览器直接连 Windows 机器的 3389**，而身份、授权、审计一样不少：**浏览器侧**跑第三方库 `ironrdp-wasm`（Rust 的 IronRDP 编成 WASM，RDP 协议栈全在客户端，画面画进 `<canvas>`），**堡垒机侧**只做 RDCleanPath 字节中继 —— 口令、票据、授权、留痕都留在堡垒机。

### 模块（`app/rdp/`）

| 文件 | 行 | 干什么 |
| --- | --- | --- |
| `cleanpath.py` | 369 | RDCleanPath 的 X.224 / TLS / 凭据封包与解包：`perform_handshake()` 按协商结果决定是否起 TLS，解析服务器证书与 `selectedProtocol`。**坑：X.224 的 length 字段是小端**，写反了包就是废包 |
| `proxy.py` | 689 | `RdpWebSocketMiddleware`（在 `app.wsgi_app` 外再包一层，**必须在 `socketio.init_app` 之后**）接住 `/api/rdp/ws` 的升级请求，做浏览器 ↔ 网关 ↔ 目标机 3389 的字节中继；`RdpTicketStore`（`TICKET_TTL_SECONDS = 120`，用一次即作废）；`build_hooks()` 把会话记录与审计挂到同一套 `session_service` / 审计上；**RDP 会话也会登记进 `session_registry`**（`kind="rdp"`、`stop=_StopRequest.request`），于是 `POST /api/sessions/<id>/terminate` 与空闲清理都能真的停掉隧道；断线原因经 `humanize_socket_error()` 翻成人话 |
| `hooks.py` | 150 | `_open_session()` / `_close_session()`：**跨 app context 的收口**。开会话时只把记录 **id** 交给网关，绝不交出 ORM 实例 —— `_run()` 结束会 `db.session.remove()`，实例随即 detach，收口时再碰它必抛 `DetachedInstanceError`（线上就这样丢过一条 `rdp_session_close`）；本轮改成交回**值字典 `{"id", "sid"}`**（同样跨 context 安全，`sid` 供在线表登记），`_close_session()` 兼容字典与裸 id，被强制中断时收口为 `terminated` |
| `__init__.py` | 48 | 导出 |

### 接口（`rdp` 蓝图，7 条；`/api/rdp/ws` 是 WebSocket，走中间件不进 `url_map`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/rdp/targets` | 我能远程桌面的主机：`accessible_targets(actor, protocols=("rdp",))` |
| `POST` | `/api/rdp/sessions` | `{hostId, accountId?}` → `{ticket, wsPath, expiresIn, host, account, credential}`；准入 = `rdp:use` + 授权（账号/时段/星期/过期/并发上限）+ `can_webterm` + 主机 `protocol=rdp` + 口令认证 |

**为什么口令要下发给浏览器**：NLA/CredSSP 必须在 RDP 客户端一侧算 NTLM 应答，而客户端就是浏览器里的 ironrdp-wasm，所以接口把该账号的资产口令随一次性票据一起下发，并**专门写一条 `rdp_credential_reveal` 审计**留痕。这是「口令集中托管、操作员不知道目标机口令」这个模型的必然代价；要口令绝不出服务器，得改成服务端 RDP 客户端（guacd / FreeRDP 把位图流回浏览器），本项目当前不做。

### 会话录像与回看（需求⑤）

「谁登录了哪台 Windows 机器、远程操作全过程」要能事后回看。录像**在浏览器侧录**：控制台把 `<canvas>` 交给 `canvas.captureStream(12)`，再用 `MediaRecorder` 按 webm 分片，会话结束时一次性上传；后端落盘 + 建 `rdp_recordings` 行 + 写审计。为什么不在服务端录？网关只是字节中继、看不到画面 —— 画面是 WASM 在浏览器里解出来的。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/rdp/recordings` | multipart：`file` + `hostId`（必需，且必须是当前账号可远程桌面的主机，否则 403）+ `sessionId?` / `durationSeconds` / `startedAt` / `width` / `height` / `accountUsername?`；容器只收 `video/webm`、`video/x-matroska`（或 `application/octet-stream`）；文件名由服务端 uuid 生成，请求里的任何字符串都不参与拼路径；超过 `RDP_RECORDING_MAX_MB` 回 413 |
| `GET` | `/api/rdp/recordings` | 分页列表；有 `session:view_all` 或 `audit:view` 看全部，否则只看自己上传的；支持 `hostId` / `sessionId` / `keyword` |
| `GET` | `/api/rdp/recordings/<id>/file` | 录像本体，支持 `Range`（206 / 416）与 `Content-Disposition: inline`；**`<video>` 带不了 `Authorization` 头，所以这个接口同时接受 `?ticket=`** |
| `POST` | `/api/rdp/recordings/<id>/ticket` | 签一张 10 分钟的回放票据（JWT，`scope=rdp-recording` 且绑定这一条录像），**并在这一步写 `rdp_recording_viewed`** —— 播放器一次回看会发很多次 Range 请求，审计只留一条 |
| `DELETE` | `/api/rdp/recordings/<id>` | 仅管理员：先删盘上文件再删行，写 `rdp_recording_deleted` |

审计三段：`rdp_recording_saved`（谁存了哪台机器的录像、体积、时长）→ `rdp_recording_viewed`（谁回看了哪条）→ `rdp_recording_deleted`。配置项：`RDP_RECORDING_DIR`（默认 `instance/rdp_recordings`）、`RDP_RECORDING_MAX_MB`（默认 512；实测码率约 16 KB/s，够 9 小时）。回看入口在「审计中心 · 远程桌面录像」（`/audit/recordings`）。

### 审计四段（都可查、可回放定位）

`rdp_ticket`（申请票据）→ `rdp_credential_reveal`（口令下发）→ `rdp_session_open`（会话建立，`detail` 里带协商结果与服务器证书）→ `rdp_session_close`（断开原因、`bytesFromClient` / `bytesFromServer`、时长）。会话行落 `sessions` 表（`protocol='rdp'`、`source='web'`），`idle_sweeper` 对 rdp 会话同样生效：空闲超时或堡垒机重启都会收口，不会留挂着不闭的会话。

### 协议隔离

主机表 `Host.protocol` 取 `ssh` / `rdp` / `winrm`（默认 `ssh`）。`app/access.py` 的 `target_protocol()` 判协议；`accessible_targets(user, protocols=("ssh", "winrm"))` 是 **SSH 网关/TUI 菜单**唯一的主机来源 —— 菜单里同时列 Linux（ssh）与 Windows 字符终端（winrm），只有图形桌面（rdp）不在菜单里（它走浏览器端的 `/rdp` 入口）。**网页终端入口页把三类主机合在一起**（需求⑥）：`GET /api/terminal/targets` 按权限码决定包含哪些协议 —— `terminal:use` 覆盖 `ssh` 与 `winrm`（两者都是「字符 shell」，只是桥接层不同），`rdp:use` 覆盖 `rdp`，任一即可进这个页面；条目回传 `protocol`，前端据此决定弹终端窗口还是远程桌面窗口。前端按 `protocol` 渲染操作系统图标与协议注记（`src/components/Bastion/osMeta.tsx`、`src/pages/bastion/terminal/index.tsx` 的 `protocolHint()`），主机列表与终端选择列表看到的都是真实系统与真实入口。

### 回归

`tests/test_rdp_gateway.py`（57 例）：RDCleanPath 封包/解包（含 X.224 小端 length）、`X224_CC_HYBRID_EX` 协商、票据 TTL 与一次性、WebSocket 中继、`_open_session` / `_close_session` 跨 app context 的会话记录与审计收口（断言 `status=closed`、字节数、`end_reason`、`ended_at` 与 open/close 两条审计都在）、关闭原因的人话化（NLA/CredSSP 阶段零字节关闭 → 提示「强制 SSL」，服务端回过数据则不加这句），以及网页终端列表「三类主机合并 + 按权限码分协议」。隧道用例带 **30 秒看门狗**：卡住时先 `faulthandler.dump_traceback(all_threads=True)` 打出所有线程的栈和现场（`server.received` / `ws.sent`）再 `fail`，绝不无限挂住测试会话。另覆盖**会话中断链路**（通用 `stop` 真能停隧道、`forced` 收口成 `terminated`）与 `humanize_socket_error()` 的人话映射。

`tests/test_rdp_recording.py`（49 例）：录像上传的 mime/体积/权限/主机准入校验、列表可见性（`session:view_all` / `audit:view` 看全部，否则只看自己）、`Range` 206 与 416、`Content-Disposition: inline`、三段审计，以及回放票据 —— 10 分钟有效、绑定单条录像（拿 A 的票拉 B 必 401）、过期/换 `scope`/拿登录 token 冒充一律 401、签票只写一条 `rdp_recording_viewed`。

---

## 七·十、Windows 网页终端（WinRM，`app/winrm/` + `app/session_notes.py`）

七·九 让浏览器连 Windows 的**图形桌面**；这一节给 Windows 机器开**字符 shell** —— 浏览器里一个 PowerShell 提示符，敲的每条命令与目标机的每段输出都进审计，身份/授权/命令策略/录像与 Linux 会话共用同一套。两条路各有位置：RDP 需要图形栈与 WASM 客户端、带宽也重，适合「必须看到桌面」的场合；运维日常的看服务、查日志、重启进程走 WinRM 更轻，且在**同一张网页终端列表**里就能连（`terminal:use` 一个权限码管到底）。

### 模块（`app/winrm/` + 一处共享文案）

| 文件 | 行 | 干什么 |
| --- | --- | --- |
| `client.py` | 297 | `WinrmTarget` / `ScriptResult` / `WinrmConnection` 三个数据类 + `build_target()`（解资产口令、`5986` 自动 `use_ssl`、key 账号与空凭据直接报中文错）、`connect()`（`winrm.Protocol` + `open_shell(codepage=65001)`）、`run_script()`（脚本编成 UTF-16LE + base64 走 `-EncodedCommand`，后台线程收 `get_command_output`，超时/取消先请求中断）、`decode_bytes()`（UTF-8 → GBK → 兜底）、`_friendly()`（把 `timed out` / `refused` / `401` / 证书校验这些英文异常翻成人话）、`PROBE_SCRIPT` 与 `test_connection()`（资产「连接测试」用，回 `whoami / COMPUTERNAME / PowerShellVersion / OSVersion`） |
| `bridge.py` | 792 | `WinrmBridge`：把「一个持久 WSMan shell + 每条输入一条自己会退出的短命令」包装成与 `ShellBridge` **完全一致**的公开面（`start/stop/feed_input/resize/stats/closed/at_prompt`），于是网关与网页终端两条通路都不用为 WinRM 改一行；本地做行编辑（光标/退格/Ctrl-W/历史/Ctrl-C）与 `\x1b[…` 序列解析，远端只负责执行；`build_script()` 在命令尾部追加落款 `Write-Output "<marker>{退出码}|{当前目录}<marker>"`，`split_trailer()` 从输出里剥出退出码与 cwd ⇒ **`cd` 能在下一条命令生效**；`decode_clixml()` 把 PowerShell 往 stderr 写的 CLIXML 包（`#< CLIXML … <S S="Error">…_x000D__x000A_</S>`）还原成可读中文；`cls` / `clear` / `Clear-Host` 在 `LOCAL_CLEAR_COMMANDS` 里**就地清屏**（见下面「两条本地兜底」） |
| `__init__.py` | 38 | 导出（`WinrmBridge` / `WinrmError` / `build_target` / `connect` / `run_script` / `close` / `test_connection`） |
| `app/session_notes.py` | 27 | **单一文案来源**：`session_note_lines(protocol)` 给 WinRM 会话返回那两行能力边界提示。网页终端与 SSH 网关各写一遍必然漂移，所以抽到这里 |

### 三条硬约束（为什么这么设计）

1. **每条命令单独执行，`cd` 保留、进程内状态不保留**：WSMan 的 shell 是无状态的，`$env:FOO`、自定义变量、`Set-Location` 之外的会话状态都不会活到下一条命令 —— 这是协议本身的性质，不是实现偷懒。`cd` 之所以保留，是桥接层自己在服务端记 cwd 并在每条命令前 `Set-Location -LiteralPath …`。两行提示（`app/session_notes.py`）就是把这个边界**说在用户敲命令之前**。
2. **交互式程序不可用**：`more` / `pause` / `Read-Host` 这类要等 stdin 的程序拿不到真正的 TTY。实测 `cmd.exe /K` 上的 `get_command_output()` 会阻塞 150 秒以上，所以实现选择的不是「假装能交互」，而是「超时可中断 + 明确告知」。
3. **没有文件传输**：WinRM 没有 SFTP 通道。网页终端的「文件管理」按钮对 WinRM 会话**直接不渲染**（不是留一颗永远点不动的灰按钮），`file:use` 权限仍然在，只是这个协议用不上。

### 两条本地兜底（用户实测反馈后补的）

1. **`cls` / `clear` / `Clear-Host` 就地清屏**：WSMan 会话没有真实控制台，PowerShell 的 `Clear-Host` 依赖 `$Host.UI.RawUI`，在 `-NonInteractive` + 重定向输出下会直接报错、屏幕根本不干净（用户实测反馈）。现在这几个等价写法命中 `LOCAL_CLEAR_COMMANDS` 后**不发给目标机**，由桥接层自己写清屏序列（`\x1b[2J\x1b[H`）并重画提示符。判定点放在命令策略**之后** —— 策略里若把 `cls` 列进白名单/黑名单，拦截结论照样先生效；走本地清屏同样记一条 `command_logs`（`reason='本地清屏指令（WinRM 会话没有真实控制台，不发给目标机）'`、`duration_ms=0`）。
2. **会话还没就绪时敲的键不丢**：服务端在 `open_session()` 返回前把收到的按键攒进 `early_input`、就绪后按序回放（`app/webterm/events.py`），但前端原先 `inputEnabled` 只认 `connected`，把这段时间的按键**静默丢在浏览器里**（WinRM 建连更慢，用户表现为「有概率敲命令没输出」）。现在 `inputEnabled` 只排除 `failed` / `disconnected`，会话连上后还会自动把焦点收回终端。

### 协议隔离与两处入口

`accessible_targets(user, protocols=("ssh", "winrm"))` 是 SSH 网关菜单的主机来源，所以 Windows 字符终端在 `ssh <堡垒机IP>` 的 TUI 菜单里**和 Linux 主机并列出现**（`app/gateway/server.py:994-1000`），选中后走 `open_session()` → `WinrmBridge`，`exit` 回菜单、`q` 退网关，与 Linux 会话完全同构。网页终端入口页 `GET /api/terminal/targets` 同样包含 winrm（`terminal:use` 覆盖 ssh 与 winrm），列表里该行标注 `192.168.0.75:5985 · WinRM 网页终端`，只给「连接」按钮。资产侧 `Host.protocol` 保存 `winrm`、`Host.winrm_transport` 保存 `ntlm`（默认）/ `basic`（需要目标机开 `AllowUnencrypted`），新增资产时端口默认 `5985`。

### 审计口径

会话行落 `sessions` 表且 `protocol='winrm'`（`source='web'` 或 `'gateway'`），命令/输出与 Linux 会话走同一张 `command_logs`、同一份转录（`instance/transcripts/<sid>.log`）：`input`/`output` 是逐键逐段留痕，`command` 事件带 `action`（`allow` / `deny` / `error` / `timeout`）、退出码与 cwd。被策略拒绝的命令**不占执行序号**（`stats()["commands"]` 只数真正发到目标机的），并且**根本不会发到目标机**。

### 回归

`tests/test_winrm_gateway.py`（49 例）：客户端层 11 例（解口令、5986→https、key/空凭据报错、非法 transport 回落、`decode_bytes`、`_friendly` 六种英文异常、`-EncodedCommand` 的 UTF-16LE+base64 还原、超时请求中断、`connect()` 的超时参数与 codepage、`test_connection` 的成功/超时/空输出）；桥接层 17 例（`split_trailer` 取**第一个**标记的回归用例、半个落款、`decode_clixml`、逐行执行并剥落款、`cd` 跨命令带目录、非零退出码、CLIXML stderr、策略拒绝且不发目标机、`exit` 记会话控制并回调、**`cls`/`clear` 就地清屏且一条都不发目标机**、**白名单策略下 `cls` 照样被拒且不清屏**、超时 `action=timeout`、单条失败 `action=error` 且会话可继续、行编辑/历史/中文/Ctrl-W、Ctrl-C 中断、`stats`/`resize`、连接横幅）；会话分发 2 例（winrm 主机 → `WinrmBridge` 且 `SessionRecord.protocol == "winrm"`、rdp 主机 → `SessionError`「只有 ssh / winrm 能进网页终端」）；资产接口 5 例（默认端口 5985 与 `winrmTransport` 默认 ntlm、PUT 改 basic、PUT 不传 port 不改端口、非法 transport 400、测试连接成功/失败）；网页终端入口 2 例（winrm 主机出现在 `/api/terminal/targets` 且 `/check` 放行、只有 `terminal:use` 的角色看得到 winrm 但看不到 rdp）。

真机两条（都要求后端在跑）：
```bash
python tools/winrm_gw_check.py --admin-password <当前管理员口令>   # SSH 网关里选 Windows 主机跑命令（9 项断言）
python %TEMP%\_winrm_cdp.py 1 && python %TEMP%\_winrm_cdp.py 2    # 浏览器 CDP 取证：列表页 + 控制台（一次性探针）
```

实测截图（根 `docs/screenshots/`）：`winrm-webterm-list.png`、`winrm-webterm-console.png`、`winrm-cls-and-early-input.png`（`cls` 之后就只剩一行提示符，验证就地清屏与「刚开窗就敲」的按键有输出）。

---

## 七·十一、一台主机多个协议端点（`host_protocols`，A+B）

同一台机器常常既要 RDP（看画面）又要 WinRM 或 SSH（要命令审计）。按「一条主机记录一个协议」的建模就得把同一台机器建成几条记录，用户实测反馈「都是同一台主机，只是协议不同就要搞两个条目，太麻烦了」，于是把「协议」从主机的一个字段升级成主机的一组**端点**：

- **表**：`host_protocols(id, host_id, protocol, port, winrm_transport, status)`，`uq_host_protocol(host_id, protocol)` 钉住「同一协议只有一条」；
- **镜像字段**：`hosts.protocol` / `hosts.port` / `hosts.winrm_transport` 保留并**始终指向主端点**（`Host.mirror_primary_endpoint()` 维护）。老代码路径读 `host.port` 依然正确，`sessions.host_id` 与历史审计口径一个字都不改；
- **兜底**：`Host.protocol_endpoints()` 在该主机**没有任何端点行**时按镜像字段造一条（`persisted=False`），所以老库、老测试、`make_host()` 夹具全都不受影响；
- **回填**：老库升级时 `backfill_host_protocols()` 在 `ensure_schema()` 之后跑一遍，给每台还没有端点行的主机补一条（已有端点的**跳过**，管理员手工配过多协议的不被覆盖）。

### 接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/hosts` / `PUT /api/hosts/<id>`（管理员） | 都接受 `protocols: [{protocol, port?, winrmTransport?}]`（也可以简写成 `["ssh","winrm"]`）。显式给的端口必须是 1-65535，协议必须 `ssh`/`rdp`/`winrm` 且不重复，WinRM 的 `winrmTransport` 只能是 `ntlm`/`basic`，否则 400；`_apply_endpoints()` 按请求对齐端点表（增/删/改），然后把主端点写回镜像字段。**不传 `protocols` 就走老的单协议路径**，行为与以前完全一致 |
| `POST /api/hosts/<id>/merge`（管理员） | 把**同地址**的另一条主机记录并进本机（`{"sourceId": n}`）：账号、授权、协议端点全部搬过来，随后删除来源记录。同名**且同一把凭据**的账号复用（`_same_credential()` 解密后比对，Fernet 密文带随机 IV 不能比密文），同名不同凭据的**改名保留**（绝不因为重名丢掉来源机的登录凭据）；地址不同 400，来源机还有在线会话 409。会话记录不改写（那是审计数据） |
| `GET /api/terminal/targets` / `POST /api/terminal/targets/<id>/check` | 前者按**端点**展开（一台主机两条端点就两条记录，每条带 `protocol`/`port`/`endpoints`），前端按 `hostId` 合并成一行；后者接受 `{"protocol": "winrm"}` 精确点名端点，该机没有这个字符端点时回「该主机没有「winrm」这个字符端点」 |

### 会话层与两处入口

`open_session(..., protocol=None)` 先解析端点点名：显式要 `winrm` 而这台机器没有该端点，就报「该主机没有「winrm」这个端点（可用：ssh、rdp）」；不指定则取该机指定的那个（否则退回第一个 ssh/winrm 端点）。端口与 WinRM 认证方式都取自**端点**（`endpoint.port` / `endpoint.winrm_transport`），两个 `build_target()` 都新增了 `port=` 覆盖参数。网页终端的 `terminal:open` 事件与 SSH 网关菜单都把端点协议传下去（网关菜单来自 `accessible_targets(user, protocols=("ssh","winrm"))` 的展开结果），rdp 票据与它的审计目的地也用 **rdp 端点**的端口。

### 回归

`tests/test_host_protocols.py`（23 例）：模型兜底/端点优先/停用端点/镜像写回、`protocols[]` 的增删改与六类非法输入、入口列表按端点展开与按权限裁剪、rdp 票据用端点端口、合并（搬家完整性、同名同凭据复用、同名不同凭据改名且凭据不丢、地址不同 400、在线会话 409）、回填幂等、老主机无端点行仍可用。全量 `python -m pytest -q` → **702 passed（33 个文件）**。

真机取证（CDP 驱动真实 Chrome，`win-75` 同时配 `winrm:5985` + `rdp:3389`）：`multiproto-launcher.png`（入口页一行两颗按钮）、`multiproto-winrm-console.png`、`multiproto-hosts-list.png`、`multiproto-host-form.png`、`multiproto-merge-modal.png` / `-options` / `-picked` / `-done`（**在真实 UI 上把一台同地址主机合并进来**）。

---

## 八、REST API 约定

- 成功：`{"success": true, "message": "...", "data": ...}`
- 列表：`{"success": true, "message": "ok", "data": [...], "total": n, "page": 1, "pageSize": 20}`
- 失败：`{"success": false, "code": "...", "message": "...", "data": null}` + 4xx/5xx
- 认证：`Authorization: Bearer <JWT>`（也支持 `?token=`，供 EventSource/WebSocket 使用）
- 兼容 ant-design-pro 模板：`POST /api/login/account`、`GET /api/currentUser`、`POST /api/login/outLogin`、`GET /api/notices`

主要接口分组（共 99 条路径 / 129 个接口组合）：`/api/auth/*`、`/api/users`、`/api/roles`、`/api/hosts`、`/api/host-groups`、`/api/grants`、`/api/policies`、`/api/policy-rules`、**`/api/file-policies`、`/api/file-rules`、`/api/files/*`**、`/api/sessions`、`/api/commands`、`/api/audits`（含 **`/api/audits/files`**）、**`/api/ai/*`（9 条，见 七·七）**、`/api/dashboard/*`、`/api/settings/*`、`/api/terminal/*`、`/api/health`。

审计清除（管理员专属，均写自身留痕）：

| 接口 | 语义 |
| --- | --- |
| `POST /api/audits/delete` | `{"ids":[...]}` 删勾选行；`{"all":true, <筛选键>, "before":ISO}` 按筛选清空（`all` 必须显式 true） |
| `POST /api/commands/delete` | 同口径清命令记录（`sessionId`/`username`/`hostId`/`action`/`riskLevel`/`keyword`） |
| `POST /api/sessions/delete` | 清会话记录并**级联**删命令日志与录像文件；`status='active'` 的会话跳过并在 `skippedActive` 里计数 |
| `POST /api/audits/files/delete` | 清文件操作记录（`operation`/`action`/`result`/`riskLevel`/`username`/`hostName`/`sid`/`keyword`），留痕 `purge_files`；权限口径与命令记录一致（`command:view_all`） |

---

## 九、测试

```bash
python -m pytest -q                       # 702 passed（33 个文件）
python -m pytest tests/test_audit_chain.py tests/test_audit_chain_tamper.py tests/test_password_change_effective.py -q   # 审计链式哈希（含 3 条对抗用例：清哈希/老库前缀/截断锚点）+ 改口令真的生效
python -m pytest tests/test_policy.py -q  # 策略引擎
python -m pytest tests/test_files_service.py tests/test_files_api.py -q   # SFTP 文件管理器（真 SFTP 服务端）
python -m pytest tests/test_idle_sweeper.py -q   # 空闲超时清理
python -m pytest tests/test_integration_ssh.py -q   # 真实 SSH 协议栈端到端
python -m pytest tests/test_ai_tools.py tests/test_ai_api.py -q   # AI 工具目录 / 对话接口与敏感操作审批
python -m pytest tests/test_gateway_ai_shell.py -q   # 网关里 /ask-ai 的行缓冲、流式渲染与口令确认
python -m pytest tests/test_ai_line_split.py tests/test_webterm_ai.py -q   # /ask-ai 行分流状态机（网关与网页终端共用）+ 网页终端入口
python -m pytest tests/test_rdp_gateway.py -q   # Windows 远程桌面（WebRDP）：RDCleanPath 封包/解包、票据 TTL 与一次性、WebSocket 中继、会话记录与审计收口
python -m pytest tests/test_rdp_recording.py -q # 远程桌面录像：上传/列表/Range 回放/三段审计 + 回放票据（10 分钟、绑定单条录像、各类伪造 401）
python -m pytest tests/test_winrm_gateway.py -q # Windows 网页终端（WinRM）：客户端/桥接层/会话分发/资产接口/网页终端入口（49 例，全部不碰真机）
python tools/winrm_gw_check.py --admin-password <当前管理员口令>   # 真机：SSH 网关菜单里选 Windows 主机跑 whoami/hostname（9 项断言，选跑）
python -m pytest tests/test_gateway_clear.py -q   # 连接目标机后自动清屏（且裸回车不落库）
python -m pytest tests/test_client_version.py -q   # SSH 握手两端版本（网关页 + 账号连接测试）

python tools/verify_audit_chain.py                # 审计链式哈希离线校验：逐表逐行重算，坏链 exit 1（打印「遗留前缀 N, 链内空洞 N」）
python tools/verify_audit_chain.py --print-head   # 抄链尾锚点：三张表的真链尾哈希，写进异地日志/工单
python tools/verify_audit_chain.py --expect-head audit_logs=<哈希>   # 复核库外锚点：对不上 exit 1（链被截断或表被替换）

python tools/live_e2e_check.py            # 真机联调：97 项断言（HTTP + Socket.IO + SSH 网关 + SFTP 文件管理器 + AI 工具目录/客户端版本，含网关进站字符画/配色/分隔线随内容）
python tools/live_e2e_check.py --admin-password <当前管理员口令>   # 改过默认口令后这样跑
python tools/ui_check.py                  # 真实 Chrome(CDP) 逐路由巡检后台 UI：21 项断言（16 个路由 + 登录态守卫 + 品牌痕迹/Logo）
python tools/console_check.py             # 真实 Chrome(CDP) 驱动网页终端与文件管理器：33 项断言（一个 window.open 弹窗 = 一条会话，含断开倒计时与自动关窗；工具条「文件管理」再弹一个独立文件窗口）
python tools/console_check.py --admin-password <当前管理员口令>   # 默认 admin/admin123 已失效时
python tools/gw_ai_check.py --question "你好"                    # 真实 SSH 网关里跑一次 /ask-ai：9 项断言（选跑，需要 Key，会消耗一次真实模型调用）
python tools/gw_ai_check.py --admin-password <当前管理员口令>     # 网关登录口令不是 admin123 时
```

> 管理员首次登录 SSH 网关会被强制改密（产品行为），`admin123` 随即失效。要么用 `--admin-password`
> （或 `BASTION_ADMIN_PASSWORD` 环境变量）传入当前口令，要么 `python run.py --reset-admin` 恢复默认值。

`tools/live_e2e_check.py` / `tools/ui_check.py` 是**打真机的联调脚本**（不是 pytest 用例）：前者要求后端已 `python run.py` 且 `tools/demo_ssh_target.py` 在 2200 监听，会自建/复用 `e2e-ops`、`e2e-demo-01`（幂等，支持 `--base`/`--gateway-port`/`--target-port`）；后者要求后端托管 `dist` 且存在浏览器调试端点，会把 JWT 注入 localStorage 后逐路由截图取证，支持 `--token` / `--admin-user` / `--admin-password`（只认命名参数），并且**先做登录态守卫**——注入令牌后访问 `/dashboard`，若被弹回登录页或页面为空立刻判 FAIL 并提示，避免「令牌无效 → 巡检了 14 次登录页 → 15/15 全绿」这种**假通过**；每个路由也会校验 `location.pathname` 未落到 `/user/login`。它的「渲染完成」判据是**先给 3 秒下限、再等正文连续 3 次不变**（早期用固定 `time.sleep(5)`：和 pytest 并行跑时 `/api/policies` 慢过 5 秒就误报红；改成「正文 >120 字即算渲染完」又会读到表格还没数据的中间态），另在登录页与主页各做两条品牌/痕迹断言（模板外链零容忍 + Logo 必须是已加载的本地 SVG）。**别和 `python -m pytest` 并行跑**，两者抢同一个后端与 SQLite。

`tools/console_check.py` 用 CDP 真点「连接」复现用户操作路径，除了建连/输入/状态条/搜索/右键菜单，还会验证**全屏往返**（工具条两次真进真退 + `Ctrl+Shift+F`；注意进全屏会改变视口尺寸，脚本每次点击前重新取按钮坐标）、**断开后的 10 秒关窗倒计时**（逐秒递减 → 归零后弹窗自动关闭）以及**工具条「资产列表」关窗回到列表**；文件管理器部分（第 5.5 节）会真点工具条「文件管理」，断言**再弹一个独立窗口** `/files/console`、窗口内用 SFTP 真列出远端 `readme.txt`/`docs`/`data`、路径条在顶层 `/`、工具条有「上传 / 新建目录」入口、带出该授权绑定的文件策略名，且该窗口全程无未捕获 JS 异常。

Windows 控制台若出现中文乱码，先 `$env:PYTHONIOENCODING='utf-8'`。

`tests/test_integration_ssh.py` **不 mock paramiko**：文件内自建一个假 Linux 目标机（真实 SSH 服务端 + 行式 shell），因此以下都是真实协议栈验证：

| 用例 | 覆盖需求 |
| --- | --- |
| `test_web_session_records_command_and_output` | ② 网页端连接 Linux 并进 shell；命令与输出落库、录像落盘 |
| `test_denied_command_never_reaches_target` | ① 不允许执行的命令**根本没到目标机**，用户收到拦截通知 |
| `test_gateway_end_to_end_menu_session_audit` | ④ `ssh` 登录网关 → 菜单选主机 → 执行命令 → 会话与命令落库 + `gateway_login` 审计 |
| `test_gateway_rejects_bad_password` | 认证失败锁定 + 审计留痕 |
| `test_transcript_endpoint_replays_recorded_events` | ⑤ 录像回放接口分页契约（`events/nextOffset/eof/size/stats`） |
| `test_webterm_socket_pushes_command_events` | ② 网页终端的 `terminal:command` 实时推送（命令落库之外的第二条通道） |
| `test_gateway_forced_password_change_reaches_menu` | ④ 首次登录强制改密后能进入主机菜单 |
| `test_session_exit_is_allowed_under_readonly_policy` | ① 会话控制指令（`exit`/`quit`/`logout`/`bye`）**不受白名单策略限制**：只读策略下 `exit` 必须 `allow`、真的发到目标机、会话确实结束（用户不会被困在会话里），且仍完整留痕 |

`tests/test_demo_target_echo.py` 钉住演示目标机的 **tty 行规程（ECHO）**：真实 Linux 的逐字符回显由 pty 驱动做、shell 不做，早期演示目标机只在 Enter 后整行回显，导致经堡垒机敲键看不到字符（用户实测反馈「输入不回显」；A/B 探针证明直连 2200 与经网关 2222 都是同样空回显，即堡垒机是忠实透传）：

| 用例 | 不变量 |
| --- | --- |
| `test_every_keystroke_is_echoed_immediately` | 敲 `w`/`h`/`o` 立即回显 `w`/`h`/`o`，**不需要按 Enter** |
| `test_type_command_then_enter_only_adds_newline` | Enter 后 shell 只补换行（不重复整行） |
| `test_backspace_erases_one_character` / `test_ctrl_u_erases_the_whole_line` | 退格 `\b \b`；Ctrl-U 按缓冲长度整行擦除；空行上按退格不回显 |
| `test_ctrl_c_rewrites_prompt_and_clears_buffer` | Ctrl-C 回显 `^C` + 重画提示符 + 清行缓冲 |
| `test_tab_is_buffered_and_echoed` / `test_other_control_bytes_are_silent_and_not_buffered` | TAB 正常入缓冲；ESC 等控制字节静默且不入缓冲 |
| `test_echo_follows_buffer_state_across_many_keystrokes` | 敲/退/改混合操作后回显与行缓冲始终一致 |

`tests/test_terminal_output.py` 专门钉住会话输出流的**三条硬不变量**（都是真机联调暴露、pytest 全绿却用户一眼可见的缺陷）：

| 用例 | 不变量 |
| --- | --- |
| `test_normalizer_*`（5 条） | 客户端可见输出里的裸 LF 一律补成 CRLF；已有的 CRLF 不重复补；`\r` 与 `\n` 被拆包时不产生 `\r\r\n` |
| `test_webterm_ready_callback_runs_before_target_output` | `on_ready` 是**第一个**回调（横幅/会话号在目标机输出之前），且输出流里无裸 LF |
| `test_gateway_banner_precedes_target_output_and_stream_has_no_bare_lf` | 真实 paramiko 客户端 + 裸 LF 目标机：横幅下标 < 目标机提示符下标、无裸 LF/孤立 CR |
| `test_webterm_keeps_input_sent_before_session_is_ready` | 会话就绪前到达的按键**不丢**：先发按键后开会话，命令仍要执行、`terminal:command` 仍要推送 |

其余测试文件：`test_policy.py`（分段/匹配/高危拦截/凭据保护/会话控制指令）、`test_access.py`（时间窗、账号绑定、配额、超级管理员兜底）、`test_api_auth.py`（登录/锁定/改密/登出黑名单）、`test_api_permissions.py`（**需求③验收**：33 个写接口对非管理员一律 403）、`test_api_grants.py`（管理员授权 CRUD 正常路径）、`test_security.py`（口令哈希往返与「新建凭据必须能立刻登录」）、`test_recorder.py`（录像读写与分页）、`test_gateway_menu.py`（网关菜单排版与观感，19 条：CRLF 归一、无裸 LF/CR、CJK 显示列宽对齐、`display_width()` 必须剥掉 ANSI、颜色只裹取值、**分隔线长度跟随内容宽度**、窄终端两行式布局、进站字符画等宽/居中/窄终端退化）、`test_idle_sweeper.py`（空闲超时：只断开超时会话、`timeout<=0` 不清理、`timeout_seconds()` 真读设置）、`test_settings_api.py`（参数设置：`default_policy_id` 允许留空、非法值仍 400、**改设置必须写审计且同值提交提示「设置无变化」**）、`test_terminal_output.py`（会话输出流：横幅时序、ONLCR 归一化、就绪前按键不丢）、`test_demo_target_echo.py`（演示目标机 tty 行规程/逐字符回显）、`test_files_service.py`（**需求⑤**：真 paramiko SFTP 服务端上的文件服务全操作 —— 列目录/读/写+mtime 冲突 409/上传/下载/归档/新建/重命名/移动/复制/删除/改权限（**断言 `after` 与随后 `stat` 一致，不假设平台一定改得动**）、双路径校验（`copy` 到系统目录按目标路径拒绝）、只读授权与默认拒绝策略、`.ssh` 与凭据拒绝、被拒落 `file_logs`、**授权变更对已开会话立即生效**、**归档缺路径退化成 404 且留一条失败审计**、**被遗弃会话被空闲清理收口**）、`test_files_api.py`（**需求⑤**：`/api/files/*` 与 `/api/file-policies|file-rules` 的 HTTP 全链路与权限，含未授权用户 403 与拒绝原因文案）、`test_seed_upgrade.py`（**升级回归**：对已存在的旧库再跑 `create_app()`，角色权限/内置命令策略/内置文件策略必须补齐并提交）、`test_routes.py`（蓝图注册、每个 app 的 Socket.IO 处理器注册、5xx 冒烟守卫）、`test_gateway_fingerprint.py`（网关主机指纹）、**`test_ai_tools.py`（18 条：113 个工具无重名、每个工具的 `path`+`method` 都能在 `app.url_map` 里匹配到、敏感工具必须带 `permission`、`tools_for_permission()` 按权限裁剪、`build_request()` 的参数落位、**工具结果摘要里的机器词归一到中文**（`_human_message()`：`ok`→`成功`、`OK（共 N 条）`→`成功（共 N 条）`）**、**`test_ai_api.py`（31 条：`/api/ai/*` 的权限守卫与信封、缺 Key 直接 400 `AI_NOT_CONFIGURED`、`AI_ENABLED=0` → 403、流式事件契约、敏感操作 `confirm_required` → 管理员口令批准/拒绝两条路、审批落 `audit_logs(ai_tool_confirm)`、`DetachedInstanceError` 回归（流式期间请求上下文已结束）、**确认时账号留空回退当前账号（口令对即通过；非管理员或口令错仍 403 且工具保持 pending）**、**历史协议修复（悬空的 `tool_calls` 一定配上「未执行」的 `tool` 响应；老的坏对话下一轮自愈，不再每次 HTTP 400）**）**、**`test_ai_line_split.py`（34 条：`/ask-ai` 行分流状态机本身 —— 别名与识别、跨 chunk 的 CSI/SS3/OSC 转义、粘贴标记 `ESC[200~`/`ESC[201~` 不清行、非无害序列保守清行、命中行之后的字节继续转发、多行粘贴、影子行上限、退格/Ctrl-U/Ctrl-C）**、**`test_webterm_ai.py`（6 条：网页终端入口的 `/ask-ai` 分流、独立 AI 线程、掩码口令输入、AI 等输入时按键不漏给目标机、连接后清屏，含一条真 Socket.IO + 假目标机链路）**、**`test_gateway_clear.py`（10 条：连接目标机后清屏 + 重画上下文、裸回车不落 `command_logs` 也不进策略引擎）**、**`test_client_version.py`（7 条：`default_client_version()` 真值、握手上报两端版本、拿不到传输层时的兜底、网关页与账号连接测试都要回传；含连接测试命令失败必须回**结构化 400 + 两端版本 + 失败审计**，而不是 500）。

---

## 十、运维提示

- **必改默认口令**：`admin/admin123` 仅用于首次进入，请立即修改。
- 修改 `gateway_host`/`gateway_port` 后需**重启后端进程**才能生效（运行中的网关监听不会热切换）。
- 生产部署建议 Gunicorn + gevent/eventlet 反代（需相应替换 `socketio` 的 `async_mode`）或直接 `python run.py` 配合 systemd/NSSM；务必用 HTTPS/WSS 终结前端流量，并把 `CORS_ORIGINS` 收窄到你的域名。
- 备份 `instance/`（数据库 + 三把密钥 + 录像）；`secret.key`/`fernet.key` 丢失将分别导致 JWT 全失效与资产口令不可解密。
- **老库升级**：项目**没有 Alembic**，靠 `app/schema_sync.py:ensure_schema()` 在 `create_app()` 里 `db.create_all()` 之后做一轮**只加字段**的 `ALTER TABLE … ADD COLUMN`（按 `PRAGMA table_info` 判断列是否存在）；本轮新增的 `grants.file_policy_id`、`grants.can_file_write` 就是靠它平滑升级的（否则报 `sqlalchemy.exc.OperationalError: no such column: grants.file_policy_id`）。
- **升级后必须确认种子数据真的落库**：`seed_data()` 曾有一个提交缺陷 —— `changed` 只在「本次新建了角色」时为 True，老库里角色都在，于是**角色权限的更新永远不 commit**；而 `seed_policies()`/`seed_file_policies()` 只 `flush()`，靠上面那次 commit 兜底。后果是升级后 `ops` 拿不到 `file:use`（文件接口一律 403）、`file_policies` 表为空 —— 而**文件策略为空会被试算器当成「未绑定策略，按审计放行」**（策略缺口静默变放行）。现已改为无条件 commit，并有 `tests/test_seed_upgrade.py` 对「旧库再跑一次 `create_app()`」做回归。排查命令：`python -c "from app import create_app; from app.extensions import db; from app.models import FilePolicy, SystemSetting; a=create_app(); [print(p.name, len(p.rules)) for p in FilePolicy.query.all()]; print(db.session.get(SystemSetting, 'builtin_file_policy_rev').value if db.session.get(SystemSetting, 'builtin_file_policy_rev') else None)"`。
- 审计记录**只增不改**：审计中心提供检索、回放与**带二次确认的清除**（`POST /api/audits/delete`、`/api/commands/delete`、`/api/sessions/delete`、`/api/audits/files/delete`，按筛选条件或选中项，且清除动作本身也写审计、只对 `command:view_all` 开放）；业务数据（会话/命令/文件记录）不会因为删除对话或关闭窗口而消失。
- **`.env` 与 AI 密钥**：`bastion-backend/.env` 存 `DEEPSEEK_API_KEY`（模板 `.env.example`），已被 `.gitignore` 忽略 —— **别把真 Key 提交进仓库**；换 Key 后重启进程生效。AI 工具只调用本项目自己的 REST（带调用者自己的 JWT），因此它的能力上限就是该用户人类权限的上限，命令/文件/配置变更照常受策略与审计约束。
- **账号「连接测试」的失败形态**：目标机若只允许交互 shell、拒绝 `exec`（例如某些网络设备或受限账户），接口回 **400 `SSH_TEST_COMMAND_FAILED`** + 已经握手成功的 `serverVersion`/`clientVersion`/`fingerprint`，并写一条失败审计；不会再出现裸 500 与内部英文异常。演示目标机（`tools/demo_ssh_target.py`）本轮补上了 `check_channel_exec_request` 才能被测试，且应答后必须**先 `shutdown_write()` 发 EOF 再关通道** —— 服务端直接 `channel.close()` 会让客户端 `Channel._close_internal()` 清空接收缓冲，客户端侧看起来仍是「exec 被拒/Channel closed.」。
- **`/ask-ai` 报 `DeepSeek 返回 HTTP 400` 时先别怀疑 Key**：多数是**历史报文**坏了（详见「七·七 → 历史协议」）。判别方法：看该对话的 `ai_messages` 是否连续出现多条 `status='error'`、内容为「（调用模型失败：DeepSeek 返回 HTTP 400）」的 assistant 消息，同时 `ai_tool_calls` 里有 `rejected` 记录 —— 那就是「拒绝敏感操作留下的悬空 `tool_calls`」。现在写侧与读侧都补齐了，**老对话下一轮自动自愈，不需要清库、也不需要新建对话**。
