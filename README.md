# AutoOps 堡垒机

面向 Linux 服务器的运维堡垒机：**身份与权限审计 + 资产/授权管理 + 命令级策略管控 + SSH 网关 + 网页终端（Linux）+ Windows 网页终端（WinRM）+ Windows 远程桌面（WebRDP）+ SFTP 文件管理器 + AI 运维助手 + 全量操作审计**。

- 后端：`bastion-backend/`（Python 3.13 + Flask 3.1 + SQLAlchemy + paramiko + Socket.IO），详见 [后端说明](bastion-backend/README.md)
- 前端：`ant-design-pro/`（基于 [ant-design-pro v6](https://github.com/ant-design/ant-design-pro)，Umi Max 4 + React 19 + antd 6 + Biome），登录页与后台框架复用 Pro，业务页全部重写

---

## 一、需求对照

| # | 需求 | 实现 | 验证证据 |
| --- | --- | --- | --- |
| 1 | **身份审计**：每个人能干什么/不能干什么、能执行哪些命令/不能执行哪些命令、能碰哪台机器/不能碰哪台机器，全部管好 | 三层模型：`Role`（36 个权限码）+ `User`（角色/启用状态/网关与终端开关）+ `Grant`（用户×主机×账号，含 `can_login`/`can_sftp`/`can_upload`/`can_download`/`can_file_write`/`can_port_forward`/`can_webterm`、时间窗、星期窗、过期时间、并发上限、命令策略 + **文件策略**）+ `CommandPolicy`/`CommandRule`（`allow`/`deny`/`confirm` 规则，按优先级首条命中即定论）+ `FilePolicy`/`FileRule`（哪条路径上能做哪些文件操作） | `tests/test_access.py`（23）、`tests/test_policy.py`（57）、`tests/test_files_service.py`、`tests/test_files_api.py`、`tests/test_integration_ssh.py::test_denied_command_never_reaches_target`（**被拦截的命令根本没到达目标机**，且用户即时收到拦截原因） |
| 2 | **调用第三方库实现网页端连接 Linux 及网页端 shell** | `paramiko` 作为 SSH 客户端连接受管主机；`Flask-SocketIO` + `xterm.js` 提供浏览器内交互式 shell；`OutputPump` 增量 UTF-8 解码解决中文乱码 | `tests/test_integration_ssh.py::test_web_session_records_command_and_output`（真实 SSH 协议栈：建会话 → 执行命令 → 命令/输出落库 + 录像落盘）。**Windows 主机的网页 shell 也补齐了**（第三方库换成正调 WSMan 的 `pywinrm`，入口与审计同源），见一·八 |
| 3 | **只有管理员可以添加机器、设置访问权限和其他设置** | 所有写操作一律 `@admin_required`；普通角色即使伪造请求也拿不到写权限；资产口令仅 `host:manage` 可见（Fernet 加密存储） | `tests/test_api_permissions.py`（**33 个写接口对 ops/viewer/匿名逐一断言 403/401**）+ 前端 `access.ts` 菜单级隐藏（双保险） |
| 4 | **SSH 网关**：`ssh 堡垒机IP` 用堡垒机账号登录 → 进入有权限的审计 shell → 选择有权限的 Linux 主机执行命令 → 每条命令与输出都被记录 | `paramiko` 实现 SSH 服务端（主机密钥持久化、口令+公钥认证、失败锁定），登录后进入中文审计菜单（**彩色进站字符画 + `=` 分隔线跟随内容宽度**，列主机/选账号/刷新/退出），选中后建立到目标机的通道，逐条命令策略判定，全部落库。**菜单里的 Windows 主机（WinRM）走同一套流程、同一张命令表与同一份转录**，见一·八 | `tests/test_integration_ssh.py::test_gateway_end_to_end_menu_session_audit`（`SSHClient` 真实登录 → 菜单选主机 → 执行命令 → 会话与命令落库 + `gateway_login` 审计）、`::test_gateway_rejects_bad_password`（失败锁定 + 审计留痕）、`tools/winrm_gw_check.py`（真机 9 项：菜单里选 Windows 主机跑 `whoami`/`hostname` 并核对落库） |
| 5 | **文件管理器**：终端弹窗工具条里加可视化文件管理器，点开后像终端一样弹出独立窗口，用 SFTP 可视化浏览远端文件，**每一个操作都进审计与访问控制** | 终端工具条「文件管理」→ `window.open()` 独立窗口（`/files/console`，`layout:false` 不进菜单）；窗口内用 paramiko SFTP 列目录/面包屑/上传/下载/在线编辑/新建/重命名/移动/复制/删除/改权限/打包下载。**两道闸**：`Grant` 开关（`can_sftp` 能不能进、`can_upload`/`can_download`/`can_file_write` 能不能改）+ `FilePolicy`（哪条路径上能做哪些操作，glob/regex/prefix/contains，优先级首条命中），任一不过即拦；**放行、拒绝、失败三类结果都写 `file_logs`**（操作/路径/目标路径/命中规则/风险/结果/耗时/字节数），拒绝时界面直接显示「为什么不让」 | `tests/test_files_service.py`（14，真 paramiko SFTP 服务端端到端）、`tests/test_files_api.py`（13，HTTP 全链路 + 权限）、`tools/live_e2e_check.py` 文件域 40 项、`tools/console_check.py` 的 6 项浏览器断言 |

### 一·五、扩展需求：AIOps 智能运维（AI 运维助手）

在原有五条需求之外追加的一组需求（`m08822/m08824`）：在堡垒机里内置一个能真正**干活**的 AI 运维助手 —— 但它必须活在堡垒机既有的权限与审计体系里，不能开一个绕过管控的后门。

| # | 需求 | 实现 | 验证证据 |
| --- | --- | --- | --- |
| 6 | 用 **DeepSeek** 模型，API Key 存 `.env`（不进仓库） | `app/ai/client.py` 的 `DeepSeekClient`（OpenAI 兼容 `/chat/completions`，`requests` + `stream=True` 逐行解析 SSE）；配置全部走环境变量：`AI_ENABLED` / `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `AI_MODEL`（默认 `deepseek-flash`）/ `AI_REQUEST_TIMEOUT` / `AI_MAX_TOKENS` / `AI_HISTORY_LIMIT` / `AI_MAX_TOOL_ROUNDS` / `AI_CONFIRM_TTL`；`.env` 已在 `.gitignore`，仓库只留 `.env.example` | `tests/test_ai_api.py`（31）、`tests/test_ai_tools.py`（18）；未配置 Key 时接口直接返回 400 而不是伪装成功 |
| 7 | **与 AI 的对话也要入审计**（用户说了什么 / AI 输出了什么 / 调用了什么工具） | 三张表：`AiConversation`（发起人/来源/模型/关联主机与会话/标题）、`AiMessage`（`role`/`content`/`reasoning`/`model`/`usage`/耗时）、`AiToolCall`（工具名/入参/结果/状态/敏感标记/确认人）；工具真正执行时**复用同一套 REST 与审计链路**，所以命令与文件操作照旧落 `command_logs`/`file_logs`，确认动作另落 `audit_logs`（`ai_tool_confirm`） | 审计中心 → **AI 对话审计**（`/audit/ai`：对话列表 + 工具调用列表 + 详情抽屉）；`tests/test_ai_api.py` 断言会话/消息/工具调用落库与 `ai:view_all` 可见范围 |
| 8 | AI 对话要**流式渲染 Markdown** | 后端 `app/ai/service.py:run_turn()` 产出事件流（`start`/`content`/`reasoning`/`tool_call`/`tool_result`/`confirm_required`/`cards`/`message_end`/`error`），`app/api/ai.py` 转成 SSE；前端 `services/bastion/ai.ts:streamAiChat()` 用 `fetch` + `getReader()` 增量解析，页面用 `@ant-design/x-markdown` 的 `XMarkdown` 渲染（思考过程单独折叠） | `tests/test_ai_api.py`（SSE 事件顺序与增量）、浏览器实测（真流式长出来的答案） |
| 9 | AI 可以**发卡片** | 系统提示词约定 ```ai-card 围栏 + JSON（`table`/`keyvalue`/`alert`/`steps` 四种），`service.py:extract_cards()` 抽取、`strip_cards()` 从正文剔除；前端把该围栏就地渲染成 antd `Table`/`Descriptions`/`Alert`/`Steps`；SSH 里没有浏览器组件，卡片降级成 ASCII 表格/列表（`ai_shell.render_cards_text()`），**绝不在终端里漏 JSON** | `tests/test_gateway_ai_shell.py` 的卡片降级与「流式过程不漏卡片 JSON」用例；浏览器实测（主机清单卡片） |
| 10 | 给 AI 的**工具 ≥50 个**，覆盖人类能做的全部功能 | `app/ai/tools.py` 声明式注册 **113 个工具**（资产/账号/授权/用户/角色/命令策略/文件策略/会话/终端执行/审计/设置等），按 `group` 分组；`invoke()` 用 `app.test_client()` 带调用者自己的 JWT 打自身 REST —— 所以 AI 的权限**天然等于这个人的权限**（越权拿不到数据） | `tests/test_ai_tools.py`：每个工具的目标路径都用 `app.url_map` 校验存在、按权限过滤、参数校验 |
| 11 | **敏感操作要输入管理员密码** | 工具声明 `sensitive=True` 时，执行前先落一条 `pending` 工具调用并回 `confirm_required` 事件；网页端弹管理员账号+密码 Modal，SSH 里用掩码提示逐步读入；`approve_tool_call()` 校验密码与管理员身份（口令错误即 `rejected`，不执行）、写 `audit_logs`（`ai_tool_confirm`），再 `resume` 续跑同一轮对话 | `tests/test_ai_api.py`（确认/拒绝/口令错误三分支）、`tests/test_gateway_ai_shell.py`、真机实测（改主机描述：签批后才执行） |
| 12 | AI 工具**单独一套权限**，可按用户分配 | 追加 7 个权限码：`ai:view` / `ai:use` / `ai:view_all` / `ai:tool` / `ai:tool_write` / `ai:tool_exec` / `ai:manage`；没有 `ai:use` 连对话都不给开，没有 `ai:tool_exec` 则所有「在目标机执行命令」的工具被过滤；`tools_for_permission()` 按权限裁剪工具清单，前端「AI 能用什么」抽屉展示的就是**这个人**实际可用的工具 | `tests/test_ai_tools.py`（`tools_for_permission` 裁剪）、`tests/test_gateway_ai_shell.py`（无 `ai:use` 直接拒绝）、`tests/test_api_permissions.py` |
| 13 | SSH 里连上机器后可随时 `/ask-ai <问题>`，**不输出可交互控件**，但 Shell 中 Markdown 要流式渲染 | `app/gateway/ai_shell.py`：网关维护「影子行缓冲」判断当前行是否 `/ask-ai`，命中时补发 `Ctrl-U` 抹掉该行且**不转发回车**（目标机永远不会把它当命令执行）；`TerminalMarkdown` 把 Markdown 就地重绘（`\r\x1b[K` 原地刷新、标题/列表/代码/表格上色、围栏转暗色横线、卡片降级 ASCII）；确认环节是纯文本提示 + 掩码输入；网关进程内 `_mint_token()` 临时签发 2 小时 JWT 供工具调用 | `tests/test_gateway_ai_shell.py`（**46 例**：命令识别、影子行/Ctrl-U 不误判、粘贴标记与跨 chunk 转义、Markdown 渲染、卡片降级、确认与拒绝、上下文落库 `source="shell"`）；行分流状态机已抽成 `app/ai/line_split.py`（`LineShadow`），**网页终端同样支持 `/ask-ai`**（`tests/test_ai_line_split.py` 34 例 + `tests/test_webterm_ai.py` 6 例）；真机 paramiko 实测（`/ask-ai` 前后按键转发、工具调用、敏感操作签批、原始输出 9991 字节原地重绘） |

### 一·六、扩展需求：审计防篡改（三张流水表链式哈希）

审计记录本身也要能被证明「没被动过」：三张流水表（`audit_logs` / `command_logs` / `file_logs`）逐行挂链，密钥由 `SECRET_KEY` 派生、不落库；校验既能逐行走接口/CLI，也能在审计页一眼看到徽标。

| # | 需求 | 实现 | 验证证据 |
| --- | --- | --- | --- |
| 14 | 审计记录**防篡改**：改字段、改哈希、清哈希、删记录都要能发现 | `app/models.py` 的 `EntryHashMixin` / `compute_entry_hash()`：`entry_hash = HMAC-SHA256(key, table + prev_hash + 规范化字段)`，`prev_hash` 指向上一条的 `entry_hash`（创世为空串）；`app/audit.py` 的 `log_event` / `log_command` / `log_file_op` 在 `flush()` 之后、`commit()` 之前算哈希，`_last_entry_hash(before_id)` 按 `id <` 取上一条（否则会把自己 flush 出来的空哈希当成前驱、第 2 条起 `prev_hash` 全空）；`verify_table_chain()` 逐行重算，并区分 `prefix`（链起步前的合法遗留）与 `pending_inside`（链内空洞）；`GET /api/audits/chain` 暴露 `pendingPrefix`/`pendingInside`/`verifiedHead`，`healthy` 只看链内空洞 | 审计页「链完整性」徽标（徽标红 ⇔ `verify.ok=False` 必须同声）+ `POST /api/audits/chain/verify` + `python tools/verify_audit_chain.py`；`tests/test_audit_chain.py`（9）、`tests/test_audit_chain_tamper.py`（3）；截图 [audit-chain-badge.png](docs/screenshots/audit-chain-badge.png) / [audit-chain-detail.png](docs/screenshots/audit-chain-detail.png)。**观感口径**：徽标与详情抽屉统一蓝白（`colorPrimary #1677ff` 一系：卡片白底 + `#e6f0ff` 描边 + 淡蓝渐变头、正常态用 `Badge status="processing"` 与蓝色数字，只有「链异常/危险」才出红——状态色仍按语义走，不为好看牺牲告警） |

### 一·七、扩展需求：Windows 远程桌面（WebRDP）

需求④ 的网页终端解的是 Linux 的 shell，但运维现场同样有 Windows 机器（本项目被管的真机就是 `win-75` / `192.168.0.75:3389`）。这一节把「在浏览器里开 Windows 桌面」也纳入同一套身份、授权与审计：**浏览器侧**跑第三方库 **`ironrdp-wasm`**（Rust 的 IronRDP 编译成 WASM，在浏览器里实现 RDP 客户端协议栈并画到 `<canvas>`），**堡垒机侧**做一个 RDCleanPath 字节中继，口令仍由堡垒机集中托管。

| # | 需求 | 实现 | 验证证据 |
| --- | --- | --- | --- |
| 15 | **浏览器里连 Windows 远程桌面（WebRDP）**：权限、准入与审计与 Linux 终端完全同源，操作员不需要知道目标机口令 | 前端 `src/pages/bastion/rdp/`：**入口已并入「网页终端」** —— Windows 主机与 Linux 主机同列一张资产表（按主机的 `protocol` 决定点「连接」时弹哪种窗口），独立的「远程桌面」列表页与菜单已删除；点「连接」用 `window.open()` 弹出独立控制台窗口 `/rdp/console`（`layout:false`，同终端一样「一窗口一会话」）。`console.tsx` 用 `ironrdp-wasm@1.1.0` 在浏览器里跑 RDP 协议栈并渲染到 `<canvas>`：**顶栏可收起**（给画面让位）、**画面随窗口自适应**（`ResizeObserver` 去抖后 `Session.resize()`，让被控主机真的换分辨率而不是把画面拉伸；画布后备缓冲完全交给 wasm、**不再手工改 `canvas.width/height`**（手工改会清空画布，而 RDP 只补增量 ⇒ 右下黑带），画布 CSS 盒按位图宽高比等比 contain，工具条显示的是**真正生效**的分辨率 —— 连接首帧那次 resize 排在 `Start RDP session` 之前会被目标机忽略，所以 1.2 秒后补发、最多 3 次）、键鼠经 `input.ts` 翻成 RDP 输入 PDU（**全屏下的坐标按 `object-fit: contain` 的真实内容框与居中偏移换算**，`src/pages/bastion/rdp/input.test.ts` 5 例钉住）、错误一律经 `describeRdpError()` 翻成人话；会话断开后弹 **10 秒倒计时自动关窗**（与 Linux 网页终端断开后的行为一致），录像自动上传。后端 `app/rdp/`：`proxy.py` 的 `RdpWebSocketMiddleware` 在 WSGI 层接住 `/api/rdp/ws?ticket=…` 的升级请求，做**浏览器 ↔ 网关 ↔ 目标机 3389 的字节中继**（补 RDCleanPath/分片长度、解析协商结果与服务器证书写进会话记录）；`cleanpath.py` 负责 RDCleanPath 的 X.224/TLS/凭据封包解包；`hooks.py` 负责跨 app context 的会话记录与审计收口。**准入全部复用终端那一套**：`@permission_required("rdp:use")` + `accessible_targets(actor, protocols=("rdp",))`（授权/时段/星期/过期/配额都在里面）+ `canWebterm`（该授权允许在这台机器上开交互式会话）+ 账号必须在授权范围内 + 必须是口令认证账号。`POST /api/rdp/sessions` 只发一张 **120 秒一次性票据**（`TICKETS.issue()`，WebSocket 只带票据 id、不带 token），口令随票据下发给浏览器（CredSSP/NLA 必须在客户端侧算，这是模型代价，已在模块 docstring 里写明），同时写 `rdp_ticket` 与 `rdp_credential_reveal` 两条审计 | `tests/test_rdp_gateway.py`（57 例：RDCleanPath 封包/解包与 **X.224 length 小端**、票据 TTL 与一次性、`X224_CC_HYBRID_EX` 协商、WebSocket 中继（进程内起假 TCP + 真 TLS 目标机，断言 `echo:PING` 回显与上下行字节数）、会话记录与审计收口；中继里**同一个 `SSLSocket` 只由单线程读写**（曾试过「读线程 + `select` + `io_lock` 串行化」，但那会死锁 —— `select()` 说可读 ≠ `recv()` 不阻塞），并配 30 秒看门狗 + 全线程栈 dump）；**真机联调**（Chrome CDP 驱动真浏览器连 `win-75`）：ironrdp 日志 `Server confirmed connection selected_protocol=SecurityProtocol(HYBRID_EX)` → `Connected with success`，画布像素取证 `nonBlackRatio=0.9997` / `distinct=218`（不是黑屏），见 [web-rdp-win75.png](docs/screenshots/web-rdp-win75.png)；**审计留痕**（实机库）：`sessions` id=14 `protocol=rdp`、`source=web`、`status=closed`、`end_reason=客户端已断开`、`bytes_in=2457`、`bytes_out=1951034`、`duration_seconds=19`，`audit_logs` 里 `rdp_ticket` → `rdp_credential_reveal` → `rdp_session_open`（detail 含 `negotiation.selectedProtocol=8` 与服务器自签证书 `CN=WIN-930NKGJCOED`）→ `rdp_session_close`（`{"sid":"57f020d94e244f0aaf2bbee80e642747","bytesFromClient":2457,"bytesFromServer":1951034}`）四段齐全 |；**本轮 8 项改造的真机复核**（Chrome CDP 驱动真浏览器）：菜单里已无「远程桌面」（`hasRdpMenu=false`）、主机列表里 rdp 行的「远程桌面」按钮已删（`rdpButtons=0`）、连接日志面板已删（`steps=0`）、顶栏可收起（`collapsed=true` 且画布随之变大）、视口改成 1120×720 后被控机收到 `1120×652`、断开会弹 10 秒倒计时（10→9→8）、录像自动保存（`#2，8 秒`）并在「审计中心 · 远程桌面录像」票据流式回看（`readyState=4`、`currentTime=4.8`） |
| 16 | **远程桌面录像（视频审计）**：谁登录了哪台 Windows 机器、远程操作全过程都能回看 | 录像在**浏览器侧**录：`canvas.captureStream(12fps)` + `MediaRecorder`（优先 VP9）分片攒成 webm，每 5 秒把分片 `POST /api/rdp/recordings/chunk` **边录边传**（服务端按 `uploadId` 顺序追加，静默超 45 秒的任务由下一次「看列表 / 开新会话」自动收口成 `recovered` 录像），前端兜底才整包 `POST /api/rdp/recordings`；后端 `app/models.py` 新增 `rdp_recordings` 表（第 19 张表），文件名一律服务端 uuid 生成（`RDP_RECORDING_DIR` 默认 `instance/rdp_recordings`），审计写 `rdp_recording_saved` / `rdp_recording_viewed` / `rdp_recording_deleted` 三段；列表与删除在「审计中心 → 远程桌面录像」（`/audit/recordings`，`canRdpRecordings` = `audit:view` 或 `session:view_all` 或 `rdp:use`），非审计员只看得到自己上传的那些。回看走**票据**：`POST /api/rdp/recordings/<id>/ticket` 换一张 10 分钟、绑定「用户 + 具体录像」的 JWT，`GET …/file?ticket=…` 支持 Range —— `<video>` 带不了 `Authorization` 头，录像又动辄几百 MB、不能整包拉成 blob 再播；签票那一步就写 `rdp_recording_viewed`，票路径不再重复记。**为什么不在服务端录**：堡垒机只做字节中继、不解码画面，服务端转码会给中继进程带来与并发数成正比的 CPU 开销，而浏览器侧正好握着 `captureStream` 这个零拷贝入口 | `tests/test_rdp_recording.py`（**39 例**：上传校验与服务端命名、列表可见范围、Range 206 分片、删除仅管理员，以及票据 7 例 —— 签票只记一条审计、无 `Authorization` 头也能 200/206、票据绑定单条录像、过期/错 scope/拿登录 token 当票一律 401、他人录像签票 403、文件丢失 404）；**真机实测**：断开弹窗写「录像已保存（#2，8 秒，可在『审计中心 · 远程桌面录像』回看）」，回看改在**独立弹出窗口**里放（`/rdp/play`：`window.open()` + 终端同款 `consoleWindowFeatures()`），弹窗 `<video>` 取证 `readyState=4`、`currentTime` 持续推进（2.35 → 5.35）、`videoWidth×videoHeight=1600×910`、`paused=false`、`error=null`，且列表页 `.ant-modal` 数量为 **0**（Modal 已删），见 [rdp-playback-popup.png](docs/screenshots/rdp-playback-popup.png) 与 [rdp-recordings.png](docs/screenshots/rdp-recordings.png) |

**协议隔离与观感**：主机表 `protocol` 列取 `ssh` / `rdp` / `winrm`（默认 `ssh`）。`app/access.py:target_protocol()` 判定协议，**字符入口（网页终端与 SSH 网关菜单）按 `protocols=("ssh", "winrm")` 取主机** —— ssh 与 winrm 都是「一条命令一段输出」的字符 shell，只是桥不同，所以共用 `terminal:use` 权限码；**远程桌面（rdp）不进字符菜单**（`/api/terminal/targets/<id>/check` 对 rdp 主机明确拒绝并提示「请回资产列表点『连接』」）。**网页终端入口页是唯一把三类主机合在一起的地方**：它按权限码决定列哪些协议（有 `terminal:use` 列 ssh 与 winrm、有 `rdp:use` 列 rdp，都有就合并成一张表），并把 `protocol` 回传给前端决定弹终端窗口还是远程桌面窗口。主机与账号选择列表按操作系统显示图标（`src/components/Bastion/osMeta.tsx`：Windows / Linux 各自的图标与措辞，`ProtocolTag` 对 winrm 单独标注），一眼能分清这台机器该走哪条通道。

**老目标机（Windows Server 2003 / XP）与主机级「RDP 安全层」**：老系统的 RDP 只支持 TLS 1.0 与标准 RDP 安全层，Python 3.13 的默认 TLS 上下文（最低 TLS 1.2）会被对端直接重置 —— 实测 `192.168.0.105:3389`（Server 2003）协商回 `PROTOCOL_HYBRID` 后 `ConnectionResetError 10054`，把上下文放宽到 `TLSv1 … TLSv1_2` + `ALL:@SECLEVEL=0` 即可握上（对端证书 `CN=WIN-EM5GV59V0PA`）。所以 `app/rdp/proxy.py` 的握手改成**两段式**：先按现代 TLS 握，失败（`ssl.SSLError` 或 `OSError`）就换一条新连接、按兼容模式再握一次；两次都失败才抛可读的 `RdpProxyError`。另外主机表新增 `rdp_security` 列（UI 里叫「RDP 安全层」，取值 `auto` / `ssl`）：`auto` 按客户端协商（含 NLA），`ssl` 则改写 X.224 请求里客户端声明支持的安全层、只报 `PROTOCOL_SSL`，绕开 NLA —— 老系统在 NLA 阶段被直接掐断时用得上。**能力边界要如实说**：Server 2003 这一类 RDP 5.x 目标机即使把会话建起来（实测服务端已回 59646 字节），浏览器里的 `ironrdp-wasm` 仍以 `decode error` 收场，**结论是这类老系统不支持 WebRDP，请改用 Windows 自带的「远程桌面连接」**（用户已用系统自带 `mstsc` 的同一 Administrator 账号实测可连，确认卡点在浏览器客户端的 RDP 协议版本，而不是网络或账号）；堡垒机现在保证的是「失败一定说得清」：控制台告警、会话记录结束原因与审计里都会写明原因和出路（前端 `describeRdpError()` 的 `/decode error/` 与 `/websocket connection failed/` 两个分支 + 后端 `_with_security_hint()`），不再只丢一句「客户端内部错误（WebSocket connection failed）」（见 [rdp-legacy-2003-message.png](docs/screenshots/rdp-legacy-2003-message.png)、[rdp-security-field.png](docs/screenshots/rdp-security-field.png)）。

### 一·八、扩展需求：Windows 网页终端（WinRM）

需求②「浏览器里的网页 shell」与需求④「`ssh 堡垒机IP` 后的审计菜单」原先只覆盖 Linux 主机（Windows 那边只有一·七的远程桌面）。可远程桌面只给画面，**命令级审计拿不到** —— 而运维现场对 Windows 同样需要「敲命令、看输出、每条都留痕」。这一节用 **WinRM（WSMan）** 把 Windows 也接进那两条字符入口：`app/winrm/client.py` 管连接与脚本执行，`app/winrm/bridge.py` 的 `WinrmBridge` 把「一个伪终端」重新实现了一遍，**公开面与 Linux 的 `ShellBridge` 完全一致**，所以 `session_service.open_session()` 只按 `host.protocol` 选桥，SSH 网关与网页终端两条入口一行都不用改。

| # | 需求 | 实现 | 验证证据 |
| --- | --- | --- | --- |
| 17 | **Windows 主机也能开字符会话**：网页终端列表与 SSH 网关菜单都能连，命令与输出同样逐条审计 | 主机表 `protocol` 取值扩到 `ssh` / `rdp` / `winrm`（默认端口 5985），新增 `winrm_transport` 列（`ntlm` 默认 / `basic`，后者需目标机 `AllowUnencrypted`）；`accessible_targets(protocols=…)` 与网关菜单改取 `("ssh", "winrm")`，**两类字符 shell 共用 `terminal:use` 权限码**（rdp 仍单独 `rdp:use`）；网页终端列表把三类主机合在一张表里，WinRM 行地址列标注「· WinRM 网页终端」；WinRM 没有 SFTP 通道 ⇒ 会话窗口**直接不渲染**「文件管理」按钮（而不是留一颗永远点不动的灰按钮）。桥接层的难点在 `WinrmBridge`：WSMan 无状态，所以提示符下每敲一条命令都被包成一条「自己会退出」的 PowerShell 脚本（`-NoLogo -NoProfile -NonInteractive -EncodedCommand`，UTF-16LE + base64）在**同一个持久 shell** 里跑，`cd` 由桥接层记住并在下一条命令前 `Set-Location` 复原（环境变量等进程内状态不保留）；脚本尾部回显 `退出码\|cwd` 的随机 marker 用于切分输出（`split_trailer()` 取**第一个** marker —— 取最后一个会把落款当成正文），PowerShell 的错误流是 CLIXML 包、`decode_clixml()` 还原成人话。`more` / `pause` / `Read-Host` 这类交互式程序不可用（实测 `cmd.exe /K` 的 `get_command_output()` 会阻塞 150 秒以上），会话建立时由 `app/session_notes.py` 这个**单一文案来源**把两条能力边界写给用户；桥接层自己的连接横幅紧跟着就被「连上自动清屏」擦掉，所以提示在清屏后重画上下文时补上（网关与网页终端同一措辞）。`cls` / `clear` / `Clear-Host` 由堡垒机**就地清屏**（WSMan 会话没有真实控制台，PowerShell 的 `Clear-Host` 依赖 `$Host.UI.RawUI`、在 `-NonInteractive` + 重定向输出下会报错），只写清屏序列并重画提示符 —— 不发给目标机，但照常过命令策略、照常留痕；会话还没就绪时敲下的按键由服务端 `early_input` 缓冲、就绪后按序回放（前端 `inputEnabled` 不再只认 `connected`，修掉「窗口一打开就敲、命令静默消失」）。被拒的命令照旧不进目标机，且**不占用执行序号** | `tests/test_winrm_gateway.py`（**49 例**，不碰真机：客户端层的口令解密/`-EncodedCommand` 编码/异常人话化、桥接层的 marker 切分与 `cd` 保持/退出码/CLIXML/策略拒绝不发目标机/`cls` 就地清屏且一条都不发目标机/超时与中断、会话分发选桥、资产接口的协议与传输方式校验、网页终端入口按权限码列 winrm 主机）；`tools/winrm_gw_check.py`（**真机 9 项**：网关菜单里出现 WinRM 主机 → 进会话有 PowerShell 提示符与能力边界提示 → whoami/hostname 是目标机的 → exit 回菜单）；浏览器真机取证 [winrm-webterm-list.png](docs/screenshots/winrm-webterm-list.png)、[winrm-webterm-console.png](docs/screenshots/winrm-webterm-console.png)（状态栏 `WINRM`、`cd C:\Windows` 后在浏览器里提示符与 `Get-Location` 同步变化）与 [winrm-cls-and-early-input.png](docs/screenshots/winrm-cls-and-early-input.png)（`cls` 之后只剩一行提示符） |
| 18 | 网关里选 Windows 主机进会话，**每条命令与输出都被记录**（需求④ 在 Windows 上同样成立） | 网关整条通路只依赖桥接层公开面与 `bridge.segmented=False` 兜底，因此不需要为 Windows 写第二套菜单；`exit`（或 Ctrl-D）会退出 Windows 会话并回到主机菜单（记一条 `session control`），`q` 才退出网关。会话落 `sessions.protocol='winrm'`、命令进同一张 `command_logs`、输出进同一份转录 | 真机 paramiko 实测：`sessions` 表 id 88 / sid `1e614bbad133401c949d4faf48c95c4b` / `source=gateway` / `protocol=winrm` / `command_count=3` / `end_reason=用户在终端里退出了 Windows 会话`；转录 `instance/transcripts/1e614bbad133401c949d4faf48c95c4b.log` 里 `CMD 1 'whoami' allow out='win-930nkgjcoed\administrator\r\n'`、`CMD 2 'hostname' allow out='WIN-930NKGJCOED\r\n'` 逐条在案 |

**为什么用 WinRM 而不是在 Windows 上装 SSH**：WinRM 是 Windows 原生自带的远程管理通道（5985/5986，`Enable-PSRemoting -Force` 即开），不需要在被管机器上装额外服务；代价是它**不是终端会话**——没有伪终端、没有交互式程序、没有文件传输，这三条都在会话建立时明确告知用户（见上表）。需要画面或传文件时仍然走一·七的远程桌面。

---

## 二、快速开始

### 1) 启动后端

```bash
cd bastion-backend
python -m pip install -r requirements.txt
python run.py
```

| 项目 | 值 |
| --- | --- |
| 后台 API | `http://127.0.0.1:5000` |
| 健康检查 | `GET /api/health` |
| SSH 网关 | `0.0.0.0:2222` |
| 默认管理员 | **`admin` / `admin123`**（首次登录会强制改密） |

数据落在 `bastion-backend/instance/`（SQLite + 三把密钥 + 会话录像），首次启动自动生成。

### 2) 启动前端

```bash
cd ant-design-pro
npm install
npm run dev          # UMI_ENV=dev MOCK=none，http://localhost:8000
```

> ⚠️ 请用 **`npm run dev`**（`MOCK=none`）。模板的 `npm start` 会开启 Pro 自带的 mock 服务，其中 `/api/currentUser`、`/api/login/account` 等会被假数据接管（本项目已删除这两个冲突的 mock 文件，但仍建议用 `npm run dev` 以走 `config/proxy.ts` 的 dev 代理）。

`config/proxy.ts` 的 dev 段已把 `/api/` 与 `/socket.io/`（含 WebSocket）代理到 `http://127.0.0.1:5000`；如后端不在本机 5000，用环境变量覆盖：

```bash
set BASTION_API=http://192.168.1.10:5000   # Windows
export BASTION_API=http://192.168.1.10:5000  # Linux/macOS
```

### 3) 生产构建

```bash
cd ant-design-pro
npm run build        # 产物在 dist/
```

两种托管方式，任选其一：

**A. 后端直接托管（最省事）** —— 构建产物就在 `ant-design-pro/dist/`，`python run.py` 会自动把它挂到同一端口，浏览器直接开 `http://127.0.0.1:5000` 即可（`/api/*`、`/socket.io/*` 保持 JSON 与长连接，不会被前端路由吞掉）。可用 `BASTION_SERVE_FRONTEND=0` 关闭，或用 `BASTION_FRONTEND_DIST=/path/to/dist` 指向别处。

**B. Nginx 托管** —— 把 `dist/` 交给 Nginx（或任意静态服务器），并将 `/api/` 与 `/socket.io/`（需 `Upgrade`/`Connection` 头透传）反代到 Flask 进程。

### 4) 没有 Linux 机器？用演示目标机先跑通全链路

```bash
cd bastion-backend
python tools/demo_ssh_target.py            # 127.0.0.1:2200, root / s3cret
```

它是一个**真实 SSH 协议栈**的假 Linux（不是真机，只对 `whoami`、`uname -a`、`df -h`、`ps aux`、`cat /etc/shadow` 等常见命令给出确定性应答，未知命令回 `command not found`）。它实现了真实 tty 的行规程：**敲键即回显**、退格 `\b \b`、Ctrl-U 擦除整行、Ctrl-C 重画提示符（`tests/test_demo_target_echo.py` 固化）。把它当成一台被管主机加到「资产管理」里（地址 `127.0.0.1`、端口 `2200`、账号 `root` / `s3cret`），就能完整体验网页终端、SSH 网关、命令策略拦截与审计落库。

### 5) 三个联调脚本（自证交付，可反复运行）

```bash
cd bastion-backend
python tools/live_e2e_check.py      # 真实 HTTP + Socket.IO + SSH 网关 + SFTP 文件管理器 + AI 运维端到端，97 项断言
python tools/ui_check.py            # 真实 Chrome(CDP) 逐路由巡检后台 UI，21 项断言（16 个路由 + 登录态守卫 + 品牌痕迹/Logo）
python tools/console_check.py       # 真实 Chrome(CDP) 驱动网页终端与文件管理器，33 项断言
```

- `live_e2e_check.py` 会**自建/复用** `e2e-ops` 账号与 `e2e-demo-01` 主机（幂等），并重置该账号口令（因此网关必然先走一次强制改密，脚本主动走完这一步）；支持 `--base` / `--gateway-port` / `--target-port` / `--admin-user` / `--admin-password`（管理员若在 SSH 网关首次登录时被强制改密，`admin123` 会失效，用这两个参数或 `python run.py --reset-admin` 传当前口令）。
- `ui_check.py` 需要先起后端（托管 `dist`）与一个已登录的浏览器调试端点；脚本会把 JWT 注入 localStorage 后逐路由（含 `文件策略` 与 `文件记录` 两个文件域页面）截图与抓取文本，判据是「**仍在登录态** + 已渲染出正文 + 无 JS 错误 + 无异常标记」，并在登录页与主页各做两条**品牌/痕迹断言**（正文与所有 `img`/`a` 里不得出现 `alipayobjects.com`、`ant.design`、`umijs.org`、`utoo.land`、`github.com/ant-design`、`procomponents`；Logo 必须是本地 `.svg` 且已加载出 `naturalWidth`）。
  「已渲染出正文」的判据本身修过两轮：早期用固定 `time.sleep(5)`，后端同时在跑 pytest（CPU + 同一个 SQLite）时 `/api/policies` 慢过 5 秒 → `/policies` 只剩页头，误红；改成「正文 >120 字即算渲染完」后又走另一个极端 —— 页面骨架一到就返回，读到的是**表格还没有数据**的中间态（`/dashboard` 只读到 245 字），既弱化证据又可能漏掉「数据回来之后才崩」。现在先给懒加载与接口 3 秒下限，再等正文**连续 3 次不变**才算渲染完。**注意：不要和 `python -m pytest` 并行跑**，两者会抢同一个后端与数据库。
  也支持 `--admin-user` / `--admin-password` / `--token`（只认命名参数）；带令牌后会先做**登录态守卫**（访问 `/dashboard` 被弹回登录页即立刻判 FAIL 并提示），避免「巡检了 N 次登录页却全绿」这类假通过。
- `console_check.py` 用 CDP 在资产列表页**真点「连接」**（必须真实鼠标事件：`el.click()` 没有用户激活，`window.open` 会被当弹窗拦掉；按钮还得先滚进视口，否则点到视口外）→ 断言**弹出了独立终端窗口**（给 `window.open` 埋点，校验第三参数含 `popup=yes` + 尺寸，而不是不带 features 的普通新标签页；两者在 CDP 里都是 `type=page`，只看 target 分不出来）且窗口内 `window.opener` 仍在（弹窗里的「资产列表」靠它聚焦回原窗口）→ 在弹窗里逐键输入 `whoami` 断言回显 `opsadmin` → 校验状态条（资产/SSH/已连接/连接时长）与**没有多标签栏、没有实时审计面板** → `Ctrl+F` 搜索浮层 → 右键菜单 → `Ctrl+Shift+F` 全屏往返（真进 `document.fullscreenElement`、再点一次真退出；进全屏会改变视口尺寸，所以每次点击前都重新定位按钮坐标，不复用旧坐标）→ 点工具条「断开」断言终端内出现**关窗倒计时**（读 `本窗口将在 N 秒后自动关闭`）→ 隔 2.5 秒断言**逐秒递减**且状态条变「已断开」→ 等倒计时归零断言**弹窗真的自动关闭**（`window.open` 出来的窗口才允许脚本自关）→ 重开一个弹窗点工具条「资产列表」断言**关窗回到列表** → 审计页「删除选中 / 清除筛选结果」二次确认（不真的确认删除）。主机名/期望回显可用 `BASTION_CONSOLE_HOST` / `BASTION_CONSOLE_WHOAMI` 覆盖；参数同 `ui_check.py`。文件管理器部分（第 5.5 节）：真点终端工具条的「文件管理」（同样必须真实鼠标事件，`window.open` 才不被拦）→ 断言弹出了**独立窗口** `/files/console?hostId=…&accountId=…` → 窗口内正文出现 `readme.txt`/`docs`/`data`（**真的用 SFTP 列了远端目录**）→ 路径条在顶层 `/`（面包屑可跳转）→ 工具条有「上传」「新建目录」等入口 → 窗口带出该授权绑定的文件策略名 → 该窗口全程无未捕获 JS 异常。

---

## 二·五、关于「超级管理员」的准入例外

堡垒机默认严格按授权表放行。但**超级管理员**（`is_superuser`，即内置 `admin` 角色）在**没有任何授权记录**时，也能在网页终端与 SSH 网关看到并连上「已启用的主机」——否则新部署的环境里，管理员第一件事就是被自己的产品挡在门外。

例外是**受控**的，只放宽「能不能进」，不放宽「能干什么」：

- 仅 `is_superuser` 生效；`ops`/`viewer` 没有授权依旧看不到任何主机（`tests/test_access.py::test_regular_user_still_requires_grant` 守着这条线）。
- 会话**照常落库**（`source=web`/`gateway`），每条命令与输出**照常记录**，命令策略按系统默认策略**照常强制执行**。
- 临时授权不落库（不会污染授权表，会话记录的 `grant_id` 为空），也不会把别的账号借给管理员（`test_superuser_never_borrows_account_from_another_host`）。
- 主机若没有可用资产账号，准入校验会直接拒绝并提示「该主机下没有可用账号，请联系管理员配置资产账号」，不让人白连一次。

---

## 三、使用流程

### 管理员：先把「谁能碰什么」配好

1. **资产管理** → 主机列表：新增主机（名称/地址/端口/分组/系统类型），再为该主机添加**资产账号**（口令或私钥，落库即加密），并可一键「连通性测试」真实跑一次 `uname -a`。
2. **身份与权限** → 角色/用户：新建用户并指派角色（内置 `admin`/`ops`/`auditor`/`viewer`，也可自定义权限码）。
3. **访问授权**：给用户配置「用户 × 主机（× 账号）」授权，勾选 `登录`/`SFTP`/`上传`/`下载`/**允许改文件**/`网页终端`/`端口转发`，设置时间窗、星期、过期时间、并发上限，并**绑定命令策略与文件策略**（文件策略留空表示用系统默认策略；开关决定「能不能用」，策略决定「能碰哪些路径」）；也可用「授权矩阵」一眼看清谁对哪台机器有什么权限，或批量授权。
4. **命令策略**：维护策略与规则（正则/前缀/精确/包含，优先级、风险级别、放行/拦截/需确认），用「命令试算器」在配置前先验证某条命令会被放行还是拦截。
5. **文件策略**：维护「哪条路径上能做哪些文件操作」的策略与规则（glob/正则/前缀/包含，优先级首条命中，`*` 表示全部操作，风险级别，默认放行或默认拒绝），内置「默认文件策略·敏感路径拦截」（放行常规运维操作，拦截 `.ssh`、凭据文件、系统目录写入等）与「只读浏览策略」；同页可用「文件操作试算器」在绑定前先验证某条路径上的某个操作会被放行还是拦下（重命名/移动/复制会同时校验源路径与目标路径）。
6. **系统设置 / SSH 网关**：查看网关运行状态与主机指纹，调整会话超时、命令超时、登录失败锁定、并发上限等参数。页面上并排显示握手两端的版本号：**服务器版本**（网关自己对外声明的 `SSH-2.0-BastionGW_1.0`）与**客户端版本**（堡垒机作为客户端去连目标机时声明的 `SSH-2.0-paramiko_<版本>`）；同一个「客户端版本」也会出现在**主机 → 账号 → 连通性测试**的结果里，方便排查「对端不认我们的客户端版本」这类问题。

### 运维人员：几种接入方式，同一套审计

**方式 A · 网页终端（一个弹窗 = 一条会话）**

`网页终端` 是一个**资产列表页**：选好账号后点「连接」，浏览器会用 **`window.open()` 弹出一个独立终端窗口**（第三参数带 `popup=yes` 与尺寸，居中、可缩放，形态对齐 JumpServer），一个窗口只跑一条审计会话；关掉窗口即结束会话。控制台按 JumpServer `luna` 的控制台形态重做：

- **一条会话一个弹窗**：页面里不再堆多个会话，也不再实时铺审计/命令日志（那些去「审计中心 → 会话记录 / 命令记录」看）；资产列表页本身不内嵌终端，只负责选资产、选账号、开窗口。每次点「连接」都是一个新窗口（窗口名带时间戳），所以「一连接一窗口一会话」。
- **工具条**：资产列表（关掉本窗口回到列表）、资产名、状态、账号、会话号、**文件管理**（弹出 SFTP 可视化文件管理器独立窗口）、重新连接、搜索、字号增减、纯净模式、工作区全屏、快捷键说明、断开会话。
- **键盘**：`Ctrl/Cmd+F` 搜索、`Ctrl/Cmd+Shift+C` 复制选中、纯 `Ctrl+C` 发中断、`Ctrl/Cmd+V` 粘贴、`Ctrl/Cmd+Shift+F` 全屏、`Ctrl/Cmd+Shift+P` 纯净模式、`Ctrl/Cmd+Shift+A` 回到资产列表（关闭本窗口）（还有 Esc 长按 800ms 退出纯净/全屏）。
- **终端内右键菜单**：复制 / 粘贴 / 全选 / 清空屏幕 / 放大字号 / 缩小字号 / 全屏 / 断开或重新连接。
- **断开即关窗**：点「断开」（或会话在服务端结束）后，终端里出现 `本窗口将在 10 秒后自动关闭…` 的**原地倒计时**（逐秒刷新，不刷屏），数到 0 自动关掉窗口；浏览器只允许脚本打开的窗口自关，被拒时提示手动关闭（`Ctrl/Cmd+W`）。倒计时结束前点「重新连接」可以取消关闭继续用这个窗口；**网络掉线不算断开**，不会关窗，仍可「重新连接」。
- **底部状态条**：登录态圆点 + 用户名｜当前资产名与协议｜状态（已连接/连接中/已断开/连接失败，含断线原因）｜**连接时长每秒刷新**。

**方式 A 之姊妹功能 · 文件管理器（SFTP，可视化，每步都审计）**

终端工具条上点「文件管理」，会像终端一样**再弹一个独立窗口**（`/files/console`，同一条授权、独立 SFTP 会话）：

- **可视化浏览**：进入即为该授权的主目录，面包屑可逐级跳转，表格列出名称/类型/大小/权限/修改时间，双击目录进入、支持刷新与排序；窗口标题带主机名，工具条显示账号、**当前生效的文件策略名**与会话号。
- **能做的操作**：上传（多文件，**带实时进度面板**：每个文件一行，显示文件名 / 已传字节 / 百分比 / 实时速率，可随时点 ✕ 取消）、下载（单文件 + 打包下载 zip）、在线编辑并保存（带 mtime 冲突检测，文件被别人改过则拒绝覆盖）、新建文件/新建目录/重命名/移动/复制/删除（多选）、改权限（4 位八进制）。
- **两道闸一起判**：① `Grant` 授权开关——`SFTP` 决定能不能进这个窗口，`上传`/`下载`/`允许改文件` 决定具体动作，没开的按钮直接置灰并说明原因；② **文件策略**——按路径匹配规则（`glob`/正则/前缀/包含，优先级首条命中）判定该操作在该路径上是否允许，重命名/移动/复制会同时校验源路径与**目标路径**，任一被拦即整条拒绝。**已经打开的窗口也实时跟随授权变更**：管理员把「允许改文件」或 `SFTP` 关掉，下一次操作当场被拒（不需要重开窗口）。
- **拒绝可见**：被拦时窗口内直接给出原因（如 `文件操作被策略「默认文件策略·敏感路径拦截」拦截：操作 [read] 源路径 [/.ssh/id_rsa] 命中规则 #1：禁止通过文件管理器访问 .ssh 目录`），不是静默失败。
- **全量留痕**：每一次操作——**放行、被拒、失败**三种结果都写一条文件审计（用户/主机/会话号/操作/路径/目标路径/动作/风险级别/命中规则号与规则文本/原因/结果/字节数/耗时），并同时计入会话审计（`protocol=sftp`）；失败也会留痕（例如打包一个不存在的路径 → 404 并记一条 `result=failure`）。窗口关掉即结束 SFTP 会话，「文件记录」页可查。
- **关闭**：点「关闭」或关掉窗口结束会话，窗口内同样有倒计时与「返回资产列表」兜底。

**方式 B · SSH 网关**

```bash
ssh -p 2222 <堡垒机账号>@<堡垒机IP>
```

登录后进入审计菜单：

```
=== AutoOps 堡垒机 审计网关 ===
 1) web-01        10.0.0.11:22    [生产]  策略:标准运维策略  账号: root, deploy
 2) db-01         10.0.0.21:22    [数据库] 策略:高危需确认    账号: dba
请输入主机编号（l 刷新 / i 我的信息 / h 帮助 / q 退出）:
```

选择编号 → （多账号时）选择账号 → 进入该主机的 shell，每条命令都会经过策略判定并被记录。

**方式 A 与方式 B 都能连 Windows 主机（WinRM，字符会话）**

Windows 主机只要协议配成 `winrm`（默认端口 5985，认证方式 `ntlm`），就会和 Linux 主机一起出现在**网页终端那张资产列表**里（地址列标注「· WinRM 网页终端」，状态栏显示 `WINRM`），也会出现在**SSH 网关的主机菜单**里 —— 两条入口进的都是 PowerShell 字符会话：`cd` 会保留，`exit` 退出会话回到菜单（`q` 退出网关）。命令与输出照旧逐条进「命令记录」与会话转录（`sessions.protocol='winrm'`）。

三条能力边界会**在会话建立时直接写在终端里**（网关与网页终端同一措辞），而不是让用户自己撞墙：

- **每条命令在目标机上单独执行**（WSMan 是无状态的）：`cd` 由堡垒机记住并复原，但环境变量、自定义变量等进程内状态不保留；
- **`more` / `pause` / `Read-Host` 这类交互式程序不可用** —— 需要交互的活儿请改用远程桌面；
- **不支持文件传输**（WinRM 没有 SFTP 通道）：WinRM 会话的工具条上**没有「文件管理」按钮**，传文件请改用 Linux 主机或远程桌面。

取证截图：[winrm-webterm-list.png](docs/screenshots/winrm-webterm-list.png)（列表里的 WinRM 行与「连接」）、[winrm-webterm-console.png](docs/screenshots/winrm-webterm-console.png)（`cd C:\Windows` 后提示符与 `Get-Location` 同步变化、右下状态栏 `WINRM`）、[winrm-cls-and-early-input.png](docs/screenshots/winrm-cls-and-early-input.png)（刚开窗就敲也有输出；`cls` 就地清屏后只剩一行提示符）。

### 一·八·补、一台主机、多个协议入口（A + B）

一台机器同时有 RDP（要画面）和 WinRM（要命令审计）是常态，但按「一条主机记录一个协议」的建模，就得把同一台机器建成两条记录 —— 用户实测反馈的原话是「都是同一台主机，只是协议不同就要搞两个条目，太麻烦了」。所以做了两件事：

- **A · 数据模型：一台主机挂多个协议端点**。新增 `host_protocols` 表（`uq_host_protocol(host_id, protocol)` 钉住「同协议只有一条」），每行是 `protocol` + `port` + `winrm_transport` + `status`；`hosts` 表原有的 `protocol` / `port` / `winrm_transport` 退役成**镜像字段**（始终指向主端点），老代码路径 `host.port` 照常可用。协议端点、账号、授权都挂在**同一台主机**上，`sessions.host_id` 与审计口径完全不变（历史会话不会被搬家改写）。
- **B · 界面：同一条记录就是一个条目**。主机表单的「协议入口」是**多选**，选中哪个协议就出现哪个协议的端口输入（WinRM 端口 / RDP 端口 / SSH 端口各自独立）；主机列表的地址列逐端点列出 `地址:端口`、协议列逐端点打标签；**网页终端入口页按 `hostId` 合并成一行，行内每个协议一颗入口按钮**（`连接` / `WinRM` / `远程桌面`），只有 SSH 端点才带「文件」按钮；点进去的控制台地址带上 `?protocol=`，服务端按该端点建会话（`open_session(protocol=…)`），网关菜单同样把端点协议传下去。

配套两个后端动作：

| 动作 | 接口 | 说明 |
| --- | --- | --- |
| **合并两台同地址主机** | `POST /api/hosts/<id>/merge`（管理员） | 把「同一台机器的另一条记录」并进本机：账号、授权、协议端点全部搬过来（同名**且同一把凭据**的账号复用，同名不同凭据的改名保留 —— 绝不因为重名丢掉凭据），随后删除来源记录。地址不同直接 400，来源机还有在线会话则 409。前端在主机列表的操作列有「合并」按钮，弹窗只列出**同地址**的其它主机 |
| **老库回填端点** | 启动时自动 | `backfill_host_protocols()` 在 `ensure_schema()` 之后跑：哪台主机还没有端点行，就按它的镜像字段补一条；已有端点的机器**跳过**（幂等，管理员手工配过多协议的不被覆盖） |

真机取证（CDP 驱动真实 Chrome，Windows 主机 `win-75` 同时配了 `winrm:5985` 与 `rdp:3389`）：[multiproto-launcher.png](docs/screenshots/multiproto-launcher.png)（入口页**只有一行** `win-75`，行内两颗按钮）、[multiproto-winrm-console.png](docs/screenshots/multiproto-winrm-console.png)（点「WinRM」进的控制台，状态条 `WINRM`）、[multiproto-hosts-list.png](docs/screenshots/multiproto-hosts-list.png)（主机列表一台机两个协议标签 + 「合并」按钮）、[multiproto-host-form.png](docs/screenshots/multiproto-host-form.png)（编辑抽屉里「协议入口」多选 + 每个协议各自的端口）、[multiproto-merge-modal.png](docs/screenshots/multiproto-merge-modal.png) / [multiproto-merge-options.png](docs/screenshots/multiproto-merge-options.png) / [multiproto-merge-picked.png](docs/screenshots/multiproto-merge-picked.png) / [multiproto-merge-done.png](docs/screenshots/multiproto-merge-done.png)（合并弹窗只列同地址主机；走完 UI 提交后来源记录从列表消失）。

**方式 C · Windows 远程桌面（WebRDP）**

Windows 主机就排在**「网页终端」同一张资产列表**里（系统图标区分平台，不额外加标注）：点「连接」用 `window.open()` 弹出**独立控制台窗口**（`/rdp/console`，`layout:false` 不进菜单），一个窗口只跑一条审计会话。顶栏可以收起给画面让位；画面随窗口大小自适应 —— 拉大浏览器窗口时被控主机也会换到新分辨率（不是把画面拉伸）；**会话断开后弹 10 秒倒计时自动关窗**（和 Linux 网页终端断开后的行为一致），录像自动上传。

- **谁在画**：浏览器里跑 `ironrdp-wasm`（第三方库，Rust 版 IronRDP 编译成 WASM），RDP 协议栈完全在客户端侧实现，画面直接进 `<canvas>`；堡垒机只做 RDCleanPath 字节中继，不解码画面。
- **怎么授权**：与终端完全同源 —— `rdp:use` 权限码 + 该主机的 `Grant`（授权范围内的账号、时段/星期/过期/并发上限）+ `canWebterm` 开关；主机协议必须是 `rdp`，账号必须是口令认证。
- **口令从哪来**：操作员**不需要知道**目标机口令 —— 堡垒机用 Fernet 解出资产口令，随 120 秒一次性票据下发给浏览器（NLA/CredSSP 必须在客户端算），并**专门写一条 `rdp_credential_reveal` 审计**：「谁在什么时候把哪台机器的哪个账号口令交给了浏览器」。
- **留痕**：`rdp_ticket`（申请票据）→ `rdp_credential_reveal`（口令下发）→ `rdp_session_open`（会话建立，含协商结果与服务器证书）→ `rdp_session_close`（断开原因、上下行字节数、时长），四个动作都在审计中心的会话/操作日志里可查。
- **能回看**：远程操作全过程在浏览器侧录成 webm（`canvas.captureStream()` + `MediaRecorder`），**边录边传** —— 每 5 秒把分片 `POST /api/rdp/recordings/chunk` 按序追加进服务端暂存文件，正常结束走 `finalize` 原子改名转正；窗口被强行关掉（直接关标签页 / 崩溃 / 断网）时前端来不及 finalize，服务端会在「有人打开录像列表 / 有人开新远程桌面」时把静默超过 45 秒的任务**自动收口**成录像并标成「未正常结束」（`recovered=true`），整段视频不再凭空消失；「审计中心 → 远程桌面录像」按操作人/资产账号/时长/体积/时间列出，点「回看」换一张 10 分钟票据流式播放（`<video>` 带不了 `Authorization` 头，所以走 `?ticket=`），每次回看、每次删除都会另外留一条审计。

### 审计员

`审计中心` → 会话记录（含**在线会话强制中断**、详情三视图：概要 / 命令记录 / 录像回放）、命令记录（命令、输出、放行或拦截、命中规则、风险、耗时）、**文件记录**（文件管理器里的每一次操作：用户/主机/会话/操作/路径/目标路径/动作/风险/命中规则/结果/字节数/耗时，可按用户/主机/会话号/操作/结果/风险筛选）、操作日志（登录、资产变更、授权变更、策略变更、会话建立与中断等）。

**清除审计数据（仅管理员）**：四个审计页（会话 / 命令 / **文件** / 操作日志）都支持勾选后「删除选中」，或按当前筛选条件「清除筛选结果」（未设置筛选即清空全部）。会话清除会**级联删除**该会话的命令日志与录像文件，**进行中的会话会被跳过**（提示先中断再清除）；每一次清除动作本身都会写入一条审计留痕（`purge_audits` / `purge_commands` / `purge_files` / `purge_sessions`），所以「谁在什么时候清掉了什么」也可追溯。**文件记录的清除需要 `command:view_all`**：内置 `ops`/`admin` 有，`viewer`/`auditor` 没有（`tools/live_e2e_check.py` 用 `e2e-viewer` 实测 403）。

### AI 运维助手（两种入口，同一套权限与审计）

AI 助手不是「另一个管理员」：它调用的每一个工具都是**带着当前登录者的身份**去打堡垒机自己的接口，所以**这个人在网页上做不了的事，问 AI 也做不了**；敏感操作还要额外过一道管理员口令。

**入口 A · 网页「AI 运维」页**（`/ai`，需要 `ai:use`）

- **左侧对话列表 + 右侧消息流**：新建/切换/删除会话，历史对话随时回看（模型、关联主机、消息数、工具调用数都记在会话上）。
- **流式 Markdown**：答案逐字长出来（思考过程单独折叠展示），代码块、表格、列表正常渲染；模型给的**卡片**就地渲染成真正的 antd 组件（主机清单表格、修改结果键值、风险告警、执行步骤）。
- **工具调用可见**：每次工具调用都在气泡里留下一张可展开的记录（工具名 / 入参 / 结果 / 耗时 / 是否敏感），失败也会显示原因。
- **敏感操作要口令**：命中 `sensitive` 工具时弹出**管理员账号 + 密码**确认框（列出即将执行的操作与参数），拒绝即中止，确认后同一轮对话继续跑；口令错误直接判为拒绝并留痕。
- **「AI 能用什么」抽屉**：按分组列出**当前账号**实际可用的工具清单（权限不够的工具根本不在列表里）。

**入口 B · SSH 网关会话内 `/ask-ai`**（先按需求④登录并选好主机）

```
root@demo-linux-01:~# /ask-ai 这台机器磁盘满了吗
```

- 这一行**不会被目标机执行**：网关识别到 `/ask-ai` 前缀后会发一个 `Ctrl-U` 把该行擦掉、**不转发回车**，再向 AI 提问（所以目标机的 shell 历史里不会留下这条命令）。
- 答案在终端里**流式重绘**（原地刷新，不刷屏），Markdown 的标题/列表/代码/表格照常上色；**卡片降级为 ASCII 表格/列表**，终端里不会出现 JSON。
- 触发敏感操作时用**纯文本 + 掩码**逐步确认（不输出任何可交互控件）：`管理员账号：` → `管理员密码：`（不回显），回车即拒绝。
- 对话与工具调用同样落库（`source=shell`，带关联主机与会话号），审计中心一样可查。

**审计**：`审计中心 → AI 对话审计`（`/audit/ai`，需要 `ai:view`）——「AI 对话」页签（对话 / 发起人 / 关联主机 / 消息数 / 工具数 / 最近活动）与「工具调用」页签（时间 / 调用人 / 工具与敏感标记 / 状态 / 确认人 / 入参 / 结果），点「查看详情」可读该对话的**逐条消息**（含思考过程与卡片 JSON）与全部工具调用；只有 `ai:view_all` 才能看到**别人**的对话，否则只看得到自己的。

**第三轮实测反馈的三个修复**（用户原话：「这里明明是 WinRM 远程终端，AI 跑不了命令因为把 WinRM 当 SSH 使了」「添加主机这里协议选择 SSH 底下就不应该出现 RDP 的东西，合并 WinRM 和 RDP，两者都是 windows」）

- **AI 执行命令按端点选通道**：`run_command` 落到的 `POST /api/terminal/exec` 现在按主机的**协议端点**挑通道 —— Linux 端点走 SSH（写 Bash/POSIX 命令）、Windows 端点走 WinRM（写 PowerShell 命令），响应体新增 `channel` / `protocol` 告诉你命令实际跑在哪条通道上；修复前 Windows 主机一律被拿去连 SSH，于是报 `Error reading SSH protocol banner`。工具的说明文字也按系统分通道（让模型知道该写哪种语法），并支持 `protocol` 参数强制指定通道。
- **点名协议必须照办**：显式传了 `protocol` 但该主机没有这个端点时**直接 400**（`NO_CHARACTER_ENDPOINT`，文案列出可用端点），不再静默换一条通道把命令跑掉 —— 否则调用方以为命令跑在 A 通道、实际跑在 B 通道，审计口径就错了。rdp 这类图形端点也会被明确拒绝（「rdp 是图形桌面」）。
- **主机表单协议合并**：「协议入口」合并成 **Linux / Unix（SSH 命令行）** 与 **Windows（命令行 + 远程桌面）** 两项；选 Windows 后再勾「命令行（WinRM / PowerShell）」「远程桌面（RDP）」两个通道（同一台机器可以只开一个、也可以都开），**只有勾了对应通道才出现** `RDP 安全层` / `WinRM 认证方式` 与各自的端口框 —— 选 SSH 时下面不会再冒出 RDP 的东西。

---

## 四、审计数据流

```
用户输入命令
   ↓
ShellBridge 按提示符标记切分命令边界
   ↓
policy.evaluate_policy(冻结策略, 命令)  ── 分段 → 逐段匹配规则 → 首条命中定论
   ├── allow  → 命令下发到目标机，输出被捕获
   └── deny   → 命令【不下发】，向目标机发 Ctrl-C，用户收到拦截通知
   ↓
command_logs（命令/输出/动作/风险/命中规则/耗时/退出码）
+ transcripts/<sid>.log（会话级 JSON Lines 录像）
+ audit_logs（会话建立、中断、登录、配置变更）
   ↓
审计中心可按用户 / 主机 / 会话 / 命令 / 动作 / 风险 / 结果检索，支持录像回放
```

文件管理器是另一条并行链路，同样「先判定、再执行、必留痕」：

```
用户在某条路径上发起文件操作（上传 / 下载 / 编辑 / 新建 / 重命名 / 移动 / 复制 / 删除 / 改权限 / 打包）
   ↓
① Grant 授权开关（can_sftp / can_upload / can_download / can_file_write）
     └── 未开放 → 直接拒绝，按钮置灰 + 说明原因（每次判定都重读授权，改完立即生效）
   ↓
② file_policy.evaluate_file_policy(冻结文件策略, 操作, 路径, target_path=目标路径)
     ── 规则按 priority 升序 → 首条命中即定论（重命名/移动/复制同时校验源与目标路径）
     ├── allow  → 下发到 SFTP 执行
     └── deny   → 【不下发】，窗口内直接给出「命中规则 #N：…」
   ↓
file_logs（操作/路径/目标路径/动作/风险/命中规则/原因/结果=success|denied|failure/字节数/耗时）
+ session_records 的 command_count / denied_count / max_risk_level / bytes_out
+ audit_logs（会话建立与中断、策略与授权变更、清除审计等）
   ↓
审计中心 → 文件记录：可按用户 / 主机 / 会话号 / 操作 / 动作 / 风险 / 结果检索
```

AI 运维是第三条链路：**AI 只能走人类已经走过的门**，它的每一步同样「先判定、再执行、必留痕」。

```
用户提问（网页「AI 运维」页，或 SSH 会话里 /ask-ai）
   ↓
service.run_turn()：拼系统提示 + 按 ai:* 权限裁剪出的工具清单 → 调 DeepSeek（流式）
   ↓
事件流（SSE / 终端原地重绘）：start → content / reasoning / tool_call → tool_result → message_end
   ├── 普通工具 → tools.invoke() 带【调用者本人】的 JWT 打堡垒机自身 REST
   │        └── 复用既有权限判定与审计：命令落 command_logs、文件操作落 file_logs
   └── 敏感工具 → 先落 pending 工具调用 → 回 confirm_required（网页弹口令框 / 终端掩码输入）
            ├── admin 口令正确 → approve_tool_call() 写 audit_logs(ai_tool_confirm) → resume 续跑
            └── 口令错误或拒绝 → rejected（不执行），同样留痕
   ↓
ai_conversations / ai_messages / ai_tool_calls（谁在什么时候问了什么、答了什么、调了什么工具、结果如何）
   ↓
审计中心 → AI 对话审计：可按对话 / 工具调用检索，只有 ai:view_all 能看别人的对话
```

---

## 五、验证记录

| 类别 | 结果 |
| --- | --- |
| 后端单元 + 接口 + 权限测试 | `python -m pytest -q` → **705 passed（33 个文件）, exit 0** |
| **Windows 网页终端（WinRM，`app/winrm/`）** | `tests/test_winrm_gateway.py`（**49**）全绿：客户端层（口令解密、`5986` 走 https、私钥账号与空口令给人话报错、非法传输方式回落 `ntlm`、解码 UTF-8/GBK/脏字节、六种网络异常翻译、`-EncodedCommand` 的 UTF-16LE+base64 编码、超时请求中断并清 shell、`test_connection` 的成功/超时/空输出）；桥接层（脚本生成与 `Set-Location`、**落款 marker 取第一个而不是最后一个**（回归用例）、CLIXML 错误流还原、逐行执行与输出归属、`cd` 跨命令保留、非零退出码、策略拒绝**不发目标机**、**`cls`/`clear` 就地清屏且一条都不发目标机**、**白名单策略下 `cls` 照样被拒且不清屏**、`exit` 记会话控制并回调收口、超时/失败各自的事件动作、行编辑/历史/中文/Ctrl-W/Ctrl-C、能力边界横幅）；会话分发（`protocol=winrm` 选 `WinrmBridge` 并落 `sessions.protocol`、`rdp` 主机进网页终端被拒「只有 ssh / winrm 能进网页终端」）；资产接口（新建 winrm 主机默认端口 5985 与 `ntlm`、改 `basic`、非法传输方式 400、测试连接回 `check=winrm`）；网页终端入口（winrm 主机出现在 `/api/terminal/targets` 且 `/check` 放行、只有 `terminal:use` 的角色看得到 winrm 却看不到 rdp 主机）。**钉死的三条产品行为**：被拒命令**不占执行序号**（`stats()["commands"]` 只数真跑过的）、`stop()` 不触发 `on_close`（只有会话正常结束/目标机不可用才触发）、单条命令失败只记 `action=error` 且会话可继续用 |
| **WinRM 真机（SSH 网关里选 Windows 主机）** | `python tools/winrm_gw_check.py --admin-password <口令>` → **共 9 项，通过 9，失败 0，exit 0**：网关菜单里出现 WinRM 主机 → 进会话有 PowerShell 提示符与两行能力边界提示 → `whoami`/`hostname` 输出是目标机的 → `exit` 回主机菜单并给出「会话已关闭」收口提示。同一轮真机里核对落库：`sessions` 表 id 88 / `source=gateway` / `protocol=winrm` / `command_count=3` / `end_reason=用户在终端里退出了 Windows 会话`，转录里 `CMD 1 'whoami' allow`、`CMD 2 'hostname' allow` 逐条在案（需求④ 在 Windows 上成立） |
| **用户实测反馈的两个缺陷（已修，真机复验）** | ①「有概率敲命令没输出」＝会话就绪前敲下的键被前端**静默丢弃**（服务端 `early_input` 本就会缓冲并在就绪后按序回放，前端 `inputEnabled` 却只认 `connected`；WinRM 建连更慢所以更容易踩到）⇒ `inputEnabled` 改为只排除 `failed`/`disconnected`，并在会话连上后把焦点收回终端；②「`cls`/`clear` 清不了屏」＝WSMan 会话没有真实控制台、PowerShell 的 `Clear-Host` 依赖 `$Host.UI.RawUI`、在 `-NonInteractive` + 重定向输出下直接报错 ⇒ `cls` / `clear` / `Clear-Host` 命中 `LOCAL_CLEAR_COMMANDS` 后由堡垒机**就地清屏**（只写清屏序列 + 重画提示符，不发目标机；判定在命令策略之后，白名单策略照样能拦）。真机复验（CDP 驱动真实 Chrome，每轮开窗后**立刻**敲 `echo early-N-ok`）：2 轮 `FAILS=0`、`cls` 之后屏长 28 只剩一行提示符、截图 `winrm-cls-and-early-input.png`；落库核对 `command_logs` id 68/70 = `echo early-1-ok`/`echo early-2-ok`（`action=allow`、`duration_ms=473`、`output` 正确），id 69/71 = `cls`（`reason=本地清屏指令（WinRM 会话没有真实控制台，不发给目标机）`、`duration_ms=0`），`sessions` 111/112 均 `protocol=winrm`/`command_count=2`/`bytes_in=21`（对照修复前两条 `command_count=0`、`bytes_in=1`） |
| **审计链式哈希（三张流水表 · 防篡改）** | `tests/test_audit_chain.py`（9）+ `tests/test_audit_chain_tamper.py`（3）全绿：`audit_logs`/`command_logs`/`file_logs` 每行按 id 单向挂 `prev_hash`/`entry_hash`（HMAC-SHA256，密钥从 `SECRET_KEY` 派生，**不写库**），改字段 / 改哈希 / 动连接关系都能**点名到具体行**；**链起步前的空哈希行是合法遗留**（`pendingPrefix`，老库升级后徽标不会一上线就全红）、**链内的空哈希行是异常**（`pendingInside`，「改完字段再把两列哈希清空」冒充遗留照样判红），`GET /api/audits/chain` 的 `healthy` 只看后者 —— **界面徽标与 CLI 退出码必须同声**（对抗测试里两者一起断言）；链的固有边界写进代码、CLI 与界面文案：整段删尾行在库内自洽，要抓它得比对**库外锚点**（`python tools/verify_audit_chain.py --print-head` 抄锚点 / `--expect-head TABLE=HASH` 复核，对不上 exit 1）。CLI 在真实库上实测：`[OK] audit_logs: 总行 87, 已哈希 87, 遗留前缀 0, 链内空洞 0, 坏首行 None`；`--expect-head audit_logs=deadbeef` → `[FAIL]` + 「库外锚点不符：期望 deadbeef… 实际 99252fe4902d…（链被截断或表被替换）」、**exit 1** |
| 文件管理器单元 + 接口（真 paramiko SFTP 服务端） | `tests/test_files_service.py`（14）与 `tests/test_files_api.py`（13）全绿：列目录/读/写（带 mtime 冲突 409）/上传/下载/归档/新建/重命名/移动/复制/删除/改权限，**双路径校验**（`copy` 到系统目录按目标路径拒绝）、只读授权与默认拒绝策略（`list` 允许但写入拒绝）、`.ssh` 与凭据路径拒绝、被拒操作落 `file_logs`、**授权变更对已开会话立即生效**（`test_grant_changes_apply_to_the_open_session`）、**归档不存在的路径退化成 404 且留一条失败审计**（`test_archive_missing_path_fails_cleanly_and_is_audited`）、**改权限断言 `after` 与随后 `stat` 一致**（不假设平台一定改得动）、**被遗弃的会话会被空闲清理收口**（`test_abandoned_file_session_is_reaped_by_idle_sweeper`）、**失败审计文案统一用中文框住、异常原文一字不改**（`_human_error()`：「修改文件权限失败：Permission denied」，回归 `test_failure_audit_message_is_framed_in_chinese_but_keeps_the_raw_error`） |
| 会话空闲超时（`app/idle_sweeper.py`） | `tests/test_idle_sweeper.py`（3）全绿：只断开 `last_active` 超时的会话、`timeout<=0` 视为不清理、`timeout_seconds()` 真读参数设置；**真机验证**：`PUT session_idle_timeout=20` → 以 `e2e-ops` 开一条文件会话 → 75 秒后 `/api/files/sessions` 为空，会话记录 `status=terminated` / `endReason="空闲超时自动断开"` |
| 参数设置（`/api/settings`） | `tests/test_settings_api.py`（5）全绿：`default_policy_id` 允许留空、指向不存在的策略仍 400、必填整型留空仍 400「必须是整数」、**改设置必须写审计**（`update_settings` 与 `AuditLog.action="update_settings"`）、同值再提交提示「设置无变化」 |
| **AI 工具目录（113 个工具）** | `tests/test_ai_tools.py`（18）全绿：工具名唯一、**每个工具的目标路径都用 `app.url_map` 校验真实存在**（避免注册了一个打不通的接口）、按 `ai:*` 权限与管理员身份裁剪（`tools_for_permission`）、参数校验（必填/类型/枚举）、未授权工具的拒绝、`build_request()` 生成的 path/query/body 与接口签名一致、**工具结果摘要里的机器词归一到中文**（`_human_message()`：`ok`→`成功`、`OK（共 N 条）`→`成功（共 N 条）`，机器可读的结构化字段一个不动） |
| **AI 对话接口与审批链路** | `tests/test_ai_api.py`（31）全绿：会话 CRUD 与可见范围（`ai:view_all`）、SSE 事件顺序（`start → content → message_end`）、**敏感工具先返回 `confirm_required` 且不执行**、管理员口令正确才 `approved` 并写 `audit_logs(ai_tool_confirm)`、口令错误/显式拒绝走 `rejected`、`resume` 续跑同一轮、未配置 Key 时 400、`ai:use` 缺失时 403、消息与工具调用落库、**确认弹窗「管理员账号留空」回退到当前登录账号**（口令对即通过并记 `confirmed_by`，账号留空但口令错/非管理员仍是 403 且工具保持 pending）、**历史协议修复**（被拒/未确认的 `tool_calls` 一定配得上「未执行」的 `tool` 响应，同一对话之后不再 HTTP 400） |
| **SSH 网关内 `/ask-ai`** | `tests/test_gateway_ai_shell.py`（**46**）全绿：/ask-ai 与 /ask 识别与用法提示、影子行缓冲（**命中时回车不转发**，退格/Ctrl-U/Ctrl-C/Ctrl-D/ESC 均不误判）、`TerminalMarkdown`（标题/列表/代码上色、`\r\x1b[K` 原地重绘、**流式过程绝不漏卡片 JSON**、围栏不重复翻转、超宽行落盘不重绘）、卡片降级 ASCII（table/keyvalue/alert/steps + 中文列宽对齐）、`run_ask_ai` 八条路径（空问题给用法、缺 Key 拒绝、无 `ai:use` 拒绝、正文+工具调用+卡片落库且 `source="shell"`、管理员口令批准、口令错误拒绝、空账号拒绝）；**影子行状态机已抽成 `app/ai/line_split.py` 的 `LineShadow`，SSH 网关与网页终端共用同一份实现**（同 chunk 里回车之后的字节必须继续转发，否则多行粘贴的收尾 `ESC[201~` 会被吞掉、远端 readline 卡在 bracketed paste） |
| **`/ask-ai` 行分流状态机（两条入口共用）** | `tests/test_ai_line_split.py`（34）全绿：`extract_question`/`is_ai_command` 识别与别名、`LineShadow.feed()` 跨 chunk 保持状态、CSI/SS3/OSC 转义序列**绝不算进用户输入行**（9 种序列参数化）、粘贴标记 `ESC[200~`/`ESC[201~` 不清空影子行、方向键/Home/Delete 等非无害序列保守清行、退格与 Ctrl-U/Ctrl-C 清行、影子行长度上限、**同一 chunk 内命中行之后的字节仍逐个转发**、bracketed paste 里带换行的多行输入 |
| **连接目标机后自动清屏（网关 + 网页终端）** | `tests/test_gateway_clear.py`（10）与 `tests/test_webterm_ai.py` 的清屏断言全绿：会话建立后先清屏（`\x1b[2J\x1b[H`）再重画上下文与横幅，终端里不再残留上一屏；清屏与重画走的是同一条写通道，**裸回车不落库、不进策略引擎**（`CommandLog` 计数必须为 0） |
| **SSH 握手两端版本（客户端版本）** | `tests/test_client_version.py`（7）全绿：`default_client_version()` 必须是真的 `SSH-2.0-paramiko_<版本>` 标识串（**小写与 paramiko 真实握手标识一致**，否则同一个字段在「拿得到/拿不到传输层」两种情况下会显示两种字样）、`connect()` 同时回传 `server_version`/`client_version`/指纹且 keepalive 仍为 30、传输层拿不到 `local_version` 时兜底、连接已断（无 transport）时客户端版本仍可读、`GET /api/settings/gateway` 与账号连通性测试两个接口都暴露 `clientVersion`（网关页上两者必然不同：网关自己声明的版本 vs paramiko 客户端版本；账号连通性测试回传的是**目标机**声明的服务端版本，真实 OpenSSH 目标上两者不同）、**连接测试里命令执行失败必须回结构化 400 `SSH_TEST_COMMAND_FAILED` + 两端版本 + 一条失败审计**（不再裸 500） |
| **真实浏览器全功能实测（Chrome CDP 驱动真实键盘鼠标，逐阶段脚本）** | 登录流（错误口令提示剩余次数、成功写 token、刷新保持、未登录访问业务页被弹回）、16 个路由逐页渲染（无 JS 异常 / 无模板痕迹 / 无「接口不存在」）、资产管理（主机与分组增删改、启停、账号管理）、身份与权限（角色增删、用户增删改与重置密码并用新口令真登录）、访问授权（新增/编辑/开关/删除）、命令与文件策略（规则抽屉 + 试算器：`rm -rf /` 拦截命中规则 #1、`ls -la` 放行、`/etc/passwd` 写操作拦截命中规则 #10）、审计中心（会话详情三页签、录像回放渲染、在线会话中断、命令「查看输出」、搜索表单展开后筛选生效、四个页面的清除流程）、系统设置（改值回读、恢复默认、清理残留会话）、SSH 网关状态页、个人设置（三个页签、改密后新口令立刻可用且旧口令失效、再改回）、**文件管理器（SFTP 可视化浏览、新建目录、上传、重命名、复制、在线编辑保存、删除、打包下载）**、**非管理员边界 9/9**（4 个管理员专属接口全部 403 `ADMIN_ONLY`、越权路由落到 403 页、菜单从 9 项裁剪到 7 项）。网页终端本身（连接 → 逐键输入 `whoami` → 回显与审计）由上一行的 `tools/console_check.py` 巡检脚本用同一套 CDP 手段覆盖 |
| 升级/重播种回归 | `tests/test_seed_upgrade.py`（2）：对**已存在的旧库**再跑一次 `create_app()`，角色权限、内置命令策略与**内置文件策略**必须补齐并提交（修掉「角色已存在 → 权限更新永不落库」的提交缺陷） |
| 真实 SSH 端到端（不 mock paramiko） | `tests/test_integration_ssh.py` 全绿：网页终端会话录制、**拦截命令零下发**、SSH 网关菜单全流程、失败登录锁定、录像接口分页回放、`terminal:command` 实时推送、网关强制改密 |
| **WebRDP 网关与录像（`tests/test_rdp_gateway.py` 57 + `tests/test_rdp_recording.py` 49（原 39 + 边录边传 10：分片按序拼接、幂等重传、乱序 409、`uploadId` 路径逃逸、静默任务自动收口成「未正常结束」、收口后 finalize 幂等、过短丢弃、abort、主机准入与归属、同任务换主机 409））** | 57 例覆盖 RDCleanPath 封包/解包与 X.224 length 小端、票据 TTL/一次性/绑定主机、`X224_CC_HYBRID_EX` 协商、WebSocket 中继与双向字节、会话记录与审计收口；39 例覆盖录像上传（multipart、mime/体积校验、服务端 uuid 命名、`rdp_recording_saved`）、列表可见范围、Range 206 分片、删除（仅管理员）与**回放票据**（签票只记一条审计、`?ticket=` 不带 `Authorization` 也能 200/206、票据绑定单条录像、过期/错 scope/拿登录 token 当票一律 401、他人录像签票 403、文件丢失 404）。**中继做成单线程 + 非阻塞 TLS**：同一 `SSLSocket` 不能被两个线程同时操作（否则会出现「`sendall()` 成功但字节没上线」的假死），而「读线程 + `select` + `io_lock`」的版本又会死锁（`select` 说可读 ≠ `recv` 不阻塞），所以整个中继只用一个线程：`setblocking(False)` + `recv` 只认 `SSLWantReadError` + `send` 遇 `SSLWantWriteError` 就 `select` 等可写；修后整文件**连跑 8 次全绿**（修复前整文件通过率只有约 1/3）。这条用例另加 30 秒看门狗 + `faulthandler.dump_traceback(all_threads=True)`，把「无限挂住」变成「带现场证据的失败」 |
| 接口可达性守卫 | `tests/test_routes.py`：所有蓝图必须已注册、无重复路由、核心路径不得 404、每个 app 都必须挂上 Socket.IO 事件处理器 |
| 网关菜单排版（真实 SSH 字节流） | `tests/test_gateway_menu.py` **19 条**全绿；用 paramiko 以真实 PTY 抓取菜单字节流复核：**裸 LF / 裸 CR 均为 0**、帮助块说明文字起始列唯一（第 16 显示列）、**120 列与 80 列终端下均无超宽行**（80 列自动切换两行式布局） |
| 网关进站观感（彩色字符画 + 分隔线随内容） | `tests/test_gateway_menu.py` 覆盖：`display_width()` 必须剥掉 ANSI（否则 `\x1b[96m` 被当 5 列宽，中英混排立刻歪）、颜色只裹取值（标签保持纯文本，列对齐仍可断言）、**四条分隔线长度各自等于相邻内容块宽度且互不相等**（旧版是固定 78 列横贯整屏）、字符画等宽/居中/CRLF、窄终端下**整块退化为空**绝不折行；`tools/live_e2e_check.py` 对运行中的真机再验 4 条（字符画排在菜单之前、横幅与菜单带 ANSI、分隔线长度不唯一且 ≤ 120、菜单区无裸 LF） |
| 网关会话输出流（真实 SSH 协议栈 + 裸 LF 目标机） | `tests/test_terminal_output.py` 全绿：横幅必须排在目标机 MOTD 之前、客户端字节流里不得出现裸 LF/孤立 CR（替不做 ONLCR 的目标机补归一化） |
| 演示目标机 tty 行规程（回显）与 exec | `tests/test_demo_target_echo.py`（14）全绿：逐字符回显、退格 `\b \b`、Ctrl-U 整行擦除、Ctrl-C 重画提示符、控制字节静默；**exec 通道**（`check_channel_exec_request`：接受请求并回传命令输出、接受 `str` 形式的命令、客户端中途断连不崩服务端——曾经的实现应答后直接关通道，客户端仍报 `Channel closed.`，因为服务端 CLOSE 会清空接收缓冲，必须**先 `shutdown_write()` 发 EOF 再等 0.2 秒关**）；**转义序列过滤（`TtyEscapeFilter`）**：粘贴标记 `ESC[200~`/`ESC[201~`、方向键、SS3 必须在写进行缓冲之前被吃掉（否则 `-bash: [201~echo: command not found`），截断序列与超长/OSC 序列要安全放弃且**绝不吞掉回车**；并用 paramiko 以真实 PTY 复核——直连 2200 与经网关 2222 都能 **逐字符收到 `w`/`h`/`o`** |
| 会话控制指令不受策略限制 | `tests/test_integration_ssh.py::test_session_exit_is_allowed_under_readonly_policy`：只读（白名单）策略下 `exit` 必须 `allow`、必须真的发到目标机并结束会话（用户不会被困在会话里），且仍完整留痕 |
| **真机联调（真实 HTTP + Socket.IO + SSH 网关 + SFTP 文件管理器）** | `python tools/live_e2e_check.py --admin-password <口令>` → **共 97 项，通过 97，失败 0**（项数随数据状态微变：会话/审计等断点只在库里有对应数据时才跑；跑之前先确认演示目标机在 2200 端口活着，否则文件域与终端域会整片红）（对照需求逐项取证，含被拒命令不泄露 `/etc/shadow` 口令哈希、网关会话命令与输出落库、网页终端「就绪前按键不丢」、**网关进站字符画/配色/分隔线随内容/无裸 LF 4 项**；文件域 40 项：授权开关透出 `canSftp/canUpload/canDownload/canFileWrite/filePolicyName`、开会话拿能力字典（12 个操作）、SFTP 真列目录与目录项字段、读/下载字节一致、`/.ssh/id_rsa` 命中规则 #1 被拦、只读授权下写入被拒、管理员改授权后**同一会话当场生效**、上传/在线编辑保存/回读/重命名/复制/移动/改权限/删除、**打包下载返回真 zip（403 字节 / 3 条目）**、`文件审计覆盖本轮所有操作 missing=[]`、被拦操作同样留痕、`ops` 可清除 / 无 `command:view_all` 的 `e2e-viewer` **403**、关闭会话落到会话审计；**AIOps 12 项**：AI 状态（`enabled/configured/model/totalToolCount`）、工具目录 ≥50 且字段完整无重名、12 个关键工具覆盖（建用户/改主机/删主机/建授权/执行命令/改设置/查审计/列会话/重置口令/建命令策略/建文件策略/中断会话）、敏感工具都带 `permission`、**内置 `ops` 角色无 `ai:*` 时工具目录可见但一个都拿不到（真断言，不是恒真的假绿）**、`/api/roles/permissions` 里 7 个 `ai:*` 码齐备、未登录 401、对话与工具调用列表信封、不存在对话/假对话确认 404；**客户端版本 2 项**：`GET /api/settings/gateway` 的 `clientVersion` 是 paramiko 的真实标识串（`SSH-2.0-paramiko_<版本>`，大小写不敏感校验）且与 `serverVersion` 不同、账号连通性测试回传两端版本与指纹） |
| **后台 UI 逐路由巡检（真实 Chrome CDP）** | `python tools/ui_check.py --admin-password <当前口令>` → **共 21 个路由/断言，通过 21，失败 0，exit 0**（注入令牌长度 397；每个路由都是真实渲染：/dashboard 1091 字、/audit/logs 1276 字、/audit/sessions 971 字、**新增 /file-policies 与 /audit/files**…；无 JS 错误、无「接口不存在」等异常标记，未登录访问审计页被正确弹回登录页；**登录页与主页均「无 Ant Design Pro 痕迹」且「品牌 Logo 为本地 SVG 且已加载」**。脚本内置登录态守卫，令牌无效直接判 FAIL——曾因参数解析 bug 出现过「巡检了 14 次登录页却 15/15 全绿」的假通过，已修） |
| **网页终端 + 文件管理器巡检（真实 Chrome CDP 驱动键盘与鼠标）** | `python tools/console_check.py --admin-password <当前口令>` → **共 33 项，通过 33，失败 0，exit 0**（资产列表页 4 行资产、页面内不内嵌终端 → 真点「连接」弹出独立终端窗口 `/terminal/console?hostId=3&accountId=3`（埋点校验 `window.open` 第三参数含 `popup=yes,width=800,height=520,…`，且弹窗内 `window.opener` 仍在）→ 会话建立 → 无多标签栏 / 无实时审计面板 → 终端内逐键输入 `whoami` 回显 `opsadmin` → 状态条 `e2e-demo-01 SSH 已连接 00:20` 且时长走动 → Ctrl+F 搜索浮层开关 → 右键菜单（复制/清空屏幕/断开）→ **全屏四步**（工具条真进 `document.fullscreenElement`、再点真退出、`Ctrl+Shift+F` 进、再退出）→ **点「断开」后终端出现关窗倒计时并逐秒递减**（实测 `剩余=10` → `10 → 7`）→ 状态条 `已断开` → **倒计时归零后弹窗自动关闭**（`remaining=0`）→ 重开弹窗点工具条「资产列表」**关窗回到列表** → **点工具条「文件管理」弹出 `/files/console` 独立窗口，窗口内 SFTP 真列出 `readme.txt`/`docs`/`data`、路径条在顶层 `/`、工具条有「上传 / 新建目录」、带出文件策略名、该窗口无未捕获 JS 异常** → 审计页「删除选中 / 清除筛选结果」二次确认含留痕说明 → 资产列表页 `infoAlerts=0`） |
| **SSH 网关内 `/ask-ai` 实测（选跑，会消耗真实模型调用）** | `python tools/gw_ai_check.py` → **共 9 项，通过 9，失败 0**（登录网关 → 菜单里选真机 `Ubuntu_Linux`（192.168.0.111）→ 粘贴形态 `/ask-ai 你好`：**粘贴标记被吞、远端 bash 没把它当命令执行、终端里没有「AI 出错」也没有 HTTP 400、出现 `[堡垒机] AI 上下文` 与提问行、流式原地重绘出真实答案正文、AI 回合之后 `echo` 照常可用**）。`tools/live_e2e_check.py` 故意不打真实模型（保证门禁便宜、可反复跑），这条补的正是「连上机器后到底能不能问 AI」——用户实测报的 `DeepSeek 返回 HTTP 400` 就发生在这条路上 |
| 前端门禁 | `npx biome check`（本次改动的文件）→ `No fixes applied.`（exit 0）、`npx biome lint src` → `Checked 257 files`（exit 0，26 条既有 warning，无新增）、`npx tsc --noEmit` → exit 0、`npm run build` → exit 0、`npx vitest run` → **12 files / 85 tests：85 passed, exit 0**（本轮 `import 52.86s`；上一轮同一套用例在 `import 69.16s` 时 `src/app.test.tsx > app getInitialState > should fetch currentUser when not on login page` 会超 15000ms 失败 —— 同一条**环境边界**用例：单跑该文件 6/6 通过、这条用例实测 14959ms，离上限只差 41ms，与改动无关。两轮记录都留着，不粉饰）。单测清单（新增 `src/pages/bastion/rdp/recordingUpload.test.ts` 9 条：分片串行不并发（乱序即坏视频）、失败自动重试两次（`[0,1,1,2]`）、重试用尽只记失败且后续片照发、`drain` 期间新入队的片也等完、`onProgress` 快照、空队列立即返回、`uploadId` 是 8-64 位小写十六进制且不重复、非安全上下文（局域网 IP 没有 `crypto.randomUUID`）时的三级回退（新增 `src/pages/bastion/rdp/types.test.ts` 7 条：老目标机的两类报错（WebSocket 认证阶段被掐断 / 解码失败）都翻成中文并给出「强制 SSL / 改用系统远程桌面」两条出路）（含 `src/components/Bastion/jsonText.test.ts` 9 条：截断 JSON 修复只保留完整条目、完整 JSON 不算截断、解析不了回退纯文本、机器词归一、失败消息里多出的 `HTTP 403` 行不挡载荷起点；新增 `src/pages/bastion/rdp/input.test.ts` 5 条：1:1 / 等比缩放 / 左右黑边 / 上下黑边 / 越界钳制、新增 `src/pages/bastion/files/uploadProgress.test.ts` 7 条：按已发字节算百分比 / **发送阶段封顶 99%**（最后 1% 留给服务端落盘）/ total 为 0 或数据异常返回 0 / 向下取整 / 封顶可调 / 速率文案 / 无速率时不给「0 B/s」）、`npm run build` → exit 0 |

实测截图（`docs/screenshots/`，全部由本次 Chrome CDP 实测现场截取）：

| 截图 | 内容 |
| --- | --- |
| [file-manager.png](docs/screenshots/file-manager.png) | 文件管理器窗口：SFTP 可视化浏览（名称/大小/修改时间/权限/属主）、工具条（上传 / 新建目录 / 新建文件）、底部「已连接 · 共 22 项」与文件策略名；深色底一律用灰阶文字（`#e6edf3` 强调 / `#cbd5e1` 正文 / `#94a3b8` 次要 / `#64748b` 弱化） |
| [terminal-search.png](docs/screenshots/terminal-search.png) | 终端输出搜索浮层：深色输入框 + 灰阶文字（占位符 / 区分大小写 / 全词 / 正则 / 关闭），替代原先继承 antd 浅色主题的黑色文字 |
| [file-policy.png](docs/screenshots/file-policy.png) | 文件策略页：规则抽屉（优先级 / 动作 / 操作 / 匹配方式 / 路径模式 / 风险）+ 操作试算器 |
| [ops-menu.png](docs/screenshots/ops-menu.png) | 非管理员（e2e-ops）登录后的菜单裁剪：只剩概览 / 网页终端 / 资产管理 / 访问授权 / 命令策略 / 文件策略 / 审计中心 |
| [ai-chat.png](docs/screenshots/ai-chat.png) | AI 运维对话页：左侧对话列表**平铺落地**（无圆角、无投影、当前项左侧色条）、右侧正文铺满内容区（右侧 15% 留白已去掉）、**没有卡片套卡片**（工具调用块是平铺区段、结果表格直接内联）、流式 Markdown + 折叠的「思考过程 / 工具调用 N 次」 |
| [ai-sensitive-confirm.png](docs/screenshots/ai-sensitive-confirm.png) | 敏感操作确认弹窗：改主机描述需要**管理员口令**，账号栏可留空（占位符「留空则用当前登录账号」），弹窗内列出待确认工具与入参 |
| [ai-approved.png](docs/screenshots/ai-approved.png) | 口令通过后原地续跑：工具调用状态转「已完成」并写出结果，主页面对应的真实数据已被改（接口回查可见） |
| [ai-audit-tool-calls.png](docs/screenshots/ai-audit-tool-calls.png) | AI 对话审计 ·「工具调用」面：**入参列不再是 `<pre>` 里的一坨 JSON**，而是键值卡片（表格直接内联），结果列在摘要下给出「查看输出」入口 |
| [ai-audit-tool-card.png](docs/screenshots/ai-audit-tool-card.png) | 对话详情里最长的 `role="tool"` 消息（`list_ai_tools`，原始 63909 字符、落库被截到 8048）：现在渲染成「工具名 + 成功」标题 + 键值卡片 + 嵌套 `groups` 表格 + **「后端已截断，这里按完整条目渲染」**标注 + 逐层「原始 JSON」入口——用户报的那堵 JSON 墙消失 |
| [audit-events-zh.png](docs/screenshots/audit-events-zh.png) | 概览「最近审计事件」：**给人看的用中文直白描述**（`AI 调用工具 list_hosts：成功（共 4 条）`、`用户 admin 向 AI 提问：列出所有纳管主机`、`admin 登录成功（console）`），**给系统看的保持英文**（`ai` / `ai_tool_call` / `ai_chat` / `auth` / `login`） |
| [audit-chain-badge.png](docs/screenshots/audit-chain-badge.png) | 审计中心 ·「链完整性」徽标（实拍，**蓝白配色**：`colorPrimary #1677ff` 一系，正常态一律蓝白、只有异常/危险才出红）：`✓ CHAIN INTEGRITY · 链式哈希 · 完整可信` + 三张流水表的总数/已哈希/链尾哈希 + 点「逐行校验哈希」后的「全表校验通过」结论，并**把链的固有边界写在界面上**（整段删尾行要靠库外锚点：`CLI --print-head / --expect-head`）；徽标红 ⇔ `verify.ok=False`，界面与 CLI 同一个结论 |
| [audit-chain-detail.png](docs/screenshots/audit-chain-detail.png) | 审计详情抽屉里的「链式哈希」区块（实拍）：前块哈希 / 本块哈希 + `HMAC-SHA256` 标记 + 计算口径与边界说明，抽屉内同为蓝白底（`#f7faff` 底 + `#e6f0ff` 描边），下方「原始详情数据」仍原样可查 |
| [web-rdp-win75.png](docs/screenshots/web-rdp-win75.png) | Windows 远程桌面（WebRDP）真机联调实拍：浏览器里**真的画出了 Windows 桌面**（Tailscale / 云更新服务管理器）而不是黑屏 —— 判定不看「有没有字节」，看画布像素统计（`nonBlackRatio=0.9997`、`distinct=218`）；上方工具条与状态条是本次真机实测的现场文字（**这张图取自本轮改造之前**：图中下方的「连接日志」面板与工具条里的「资产列表」按钮已按需求删掉，现在只留顶部状态条与错误 Alert） |
| [winrm-webterm-list.png](docs/screenshots/winrm-webterm-list.png) | **网页终端入口页里的 Windows（WinRM）主机**（真机实拍）：三类主机同列一张表 —— `Server2003`（远程桌面）、`UbuntuLinux` / `e2e-demo-01`（SSH，带「文件」按钮）、`win-75`（远程桌面）、**`win-75-winrm` `192.168.0.75:5985 · WinRM 网页终端`**；WinRM 那一行只有「连接」，**没有「文件」按钮**（WinRM 无 SFTP 通道，按钮直接不渲染），命令策略「未绑定策略」 |
| [winrm-webterm-console.png](docs/screenshots/winrm-webterm-console.png) | **WinRM 网页终端控制台**（真机实拍）：首屏就是三行会话上下文 + **两行 WinRM 能力边界提示**（「每条命令在目标机上单独执行，cd 会保留；环境变量、自定义变量等进程内状态不保留」「more / pause / Read-Host 等交互式程序不可用，本会话也不支持文件传输；需要这些能力时请改用远程桌面」）；`whoami` → `win-930nkgjcoed\administrator`、`hostname` → `WIN-930NKGJCOED`、`cd C:\Windows` 后提示符与 `Get-Location` 同步变成 `C:\Windows`（**cd 保持真的生效**）、`Get-Date` 回真实日期；工具条里**没有「文件管理」按钮**，右下状态条 `win-75-winrm \| WINRM \| 已连接 \| 01:26` |
| [winrm-cls-and-early-input.png](docs/screenshots/winrm-cls-and-early-input.png) | **用户实测反馈的两个缺陷修完之后的实拍**：这一轮每开一个 WinRM 控制台窗口就**立刻**敲 `echo early-N-ok`（不等任何提示），输出照常回显、命令照常落库 —— 修掉了「窗口一打开就敲、命令静默消失」；随后敲的 `cls` **就地清屏**（WSMan 会话没有真实控制台，`Clear-Host` 在 `-NonInteractive` 下会报错），屏幕上只剩一行 `PS C:\Users\Administrator>`，右下状态条 `win-75-winrm \| WINRM \| 已连接 \| 00:03` |
| [multiproto-launcher.png](docs/screenshots/multiproto-launcher.png) | **一台主机多协议 · 入口页合并成一行**（真机实拍）：`win-75` **只有一行**，地址列逐端点列出 `192.168.0.75:3389 · 远程桌面` 与 `192.168.0.75:5985 · WinRM 网页终端`，操作列**每协议一颗按钮**（`远程桌面` / `WinRM`），没有「文件」按钮（该机没有 SSH 端点）—— 对照上一轮同一台机器要占两行、各带一颗「连接」 |
| [multiproto-winrm-console.png](docs/screenshots/multiproto-winrm-console.png) | **从合并后的行里点「WinRM」进控制台**（真机实拍）：控制台地址带上 `?protocol=winrm`，首屏 `[堡垒机] 已连接 win-75（192.168.0.75:5985），账号 Administrator，会话号 …` + 三行能力边界提示，敲 `hostname` 回 `WIN-930NKGJCOED`，右下状态条 `WINRM` |
| [multiproto-hosts-list.png](docs/screenshots/multiproto-hosts-list.png) | **主机列表里的多协议主机**（真机实拍）：`win-75` 一行的地址列两个端点（`:5985` / `:3389`）、协议列 `WinRM` + `RDP` + `Windows` 三个标签、操作列多出一颗**「合并」**按钮（把同地址的另一条记录并进来） |
| [multiproto-host-form.png](docs/screenshots/multiproto-host-form.png) | **主机编辑抽屉的「协议入口」多选**（真机实拍）：`win-75` 选中 `WinRM（Windows 命令行 / PowerShell）` + `RDP（Windows 远程桌面）`，下面**按协议各自给一个端口输入**（`WINRM 端口` 5985 / `RDP 端口` 3389），RDP 安全层与 WinRM 认证方式各自独立配置 |
| [hostform-linux.png](docs/screenshots/hostform-linux.png) | **第三轮实测反馈修完的「新增主机」表单**（真机实拍）：协议入口是**多选**，只勾 `Linux / Unix（SSH 命令行）` 时下面**只有 `SSH 端口`** —— 用户报的「选 SSH 底下却出现 RDP 安全层 / WinRM 认证方式」已经不可能出现（这两个字段只在勾了对应 Windows 通道时才渲染） |
| [hostform-windows.png](docs/screenshots/hostform-windows.png) | **同一个表单勾上 Windows 之后**（真机实拍，键盘真实操作下拉）：多出「Windows 通道」勾选框（`命令行（WinRM / PowerShell）` + `远程桌面（RDP）` 都勾着）与 `WINRM 端口` 5985 / `RDP 端口` 3389 / `RDP 安全层` / `WinRM 认证方式` —— WinRM 与 RDP 不再是下拉里的两个独立条目，而是同一个「Windows（命令行 + 远程桌面）」下的两个通道（Linux 还选着，所以 `SSH 端口` 22 也在，多选本来就允许共存） |
| [hostform-win75.png](docs/screenshots/hostform-win75.png) | **编辑已有的 `win-75`**（真机实拍）：协议入口只有 `Windows（命令行 + 远程桌面）`、两个通道都勾着，字段里**没有 `SSH 端口`**（这台机器没有 ssh 端点）—— 表单回填按端点反推，跟库里的事实一致 |
| [multiproto-merge-modal.png](docs/screenshots/multiproto-merge-modal.png) / [multiproto-merge-options.png](docs/screenshots/multiproto-merge-options.png) / [multiproto-merge-done.png](docs/screenshots/multiproto-merge-done.png) | **「合并」弹窗只列同地址主机 + 走完 UI 真的合并**（真机实拍）：弹窗写明「只允许同一地址的主机互相合并」与本机地址，下拉里只有 `192.168.0.75` 那台同地址记录（`multiproto-merge-options.png` 是下拉展开、`multiproto-merge-picked.png` 是选中态）；点「合并」后 toast「已把选中的主机合并进本机」，列表里来源记录消失、只剩合并后的 `win-75`（`multiproto-merge-done.png`） |
| [rdp-recordings.png](docs/screenshots/rdp-recordings.png) | 审计中心 ·「远程桌面录像」列表：按操作人 / 资产账号 / 时长 / 体积 / 录制时间列出（**页面横幅已按用户反馈删除**，可见范围说明降级成表头文字；「回看」的 Tooltip 写明「在独立窗口里回放（会换一张 10 分钟有效的一次性票据，并在审计里记一条查看记录）」） |
| [rdp-recording-recovered.png](docs/screenshots/rdp-recording-recovered.png) | 审计中心 ·「远程桌面录像」列表里的**自动收口录像**实拍：两行带橙色「未正常结束」标签（`#16` 198.4 KB / 00:21、`#15` 143.2 KB / 00:26）—— 这是浏览器窗口被强行关掉（直接关标签页 / 崩溃 / 断网）时，服务端凭边录边传已经收到的分片自动收口的录像；正常结束的 `#14`/`#13` 没有这个标签。鼠标悬停标签会说明「只录到窗口关闭那一刻，不是完整的操作过程」 |
| [rdp-playback-popup.png](docs/screenshots/rdp-playback-popup.png) | **弹出式回放窗口**（`/rdp/play`）：顶部一行元信息（`录像回放 · #9 win-75` + 操作人 / 资产账号 / 时长 / 体积 / 录制时间）+「重新换票」「关闭窗口」，舞台里 `<video>` 正在**边流边放**那段录下来的 Windows 桌面（本次取证 `readyState=4`、`currentTime` 从 2.35 走到 5.35、`1600×910`、`error=null`） |
| [rdp-session-interrupted.png](docs/screenshots/rdp-session-interrupted.png) | **在「会话记录」页点「中断」，RDP 会话真的被停掉**：顶部 toast「已中断该会话」，「当前在线会话」随即变空，下方记录里该会话变成黄标**已被中断**（库里 `status=terminated`、`end_reason=管理员强制中断`，审计里 `terminate_session` 与 `rdp_session_close` 相隔 118 毫秒成对出现） |
| [terminal-rdp-tooltip.png](docs/screenshots/terminal-rdp-tooltip.png) | 网页终端入口页（**横幅已删**）：Linux 与 Windows 同列一张表；打开远程桌面前的口令下发告知挪到了 Windows 行「连接」按钮的 Tooltip 上（「CredSSP/NLA 必须在浏览器侧完成，所以该资产账号的口令会下发到你的浏览器（服务端会单独留一条审计）」） |
| [hosts-no-rdp-button.png](docs/screenshots/hosts-no-rdp-button.png) | 资产管理 · 主机列表实拍：**rdp 主机行不再有「远程桌面」按钮**（操作列只剩 账号管理 / 编辑 / 删除，`rdpButtons=0`）—— 远程桌面入口统一收在「网页终端」页，避免同一个动作有两个入口 |
| [rdp-session-ended.png](docs/screenshots/rdp-session-ended.png) | 远程桌面断开后的落地界面实拍：弹窗写明结束原因（`user initiated disconnect`）、**本窗口将在 10 秒后自动关闭**（与 Linux 网页终端一致）、录像保存结果（`#2，8 秒`）与「留在本窗口 / 立即关闭」两个选择 |
| [file-upload-progress.png](docs/screenshots/file-upload-progress.png) | 文件管理器**上传进度面板**实拍（深色窗口）：每个文件一行 = 文件名 + 进度条 + `已传 / 总大小 · 百分比 · 速率` + ✕ 取消。本轮真机取证（16 MB 演示文件、限速到约 1.7 MB/s）：`0 B / 16.0 MB · 0%` → `16.0 MB / 16.0 MB · 99%`（**发送阶段封顶**）→ `100%`（行变 `is-done`，随后文件出现在目录列表里）；中途点 ✕ 会立刻收起面板且不出现成功提示 |
| [rdp-alert-readable.png](docs/screenshots/rdp-alert-readable.png) | 控制台「连接失败」告警的可读性修复实拍（深色底 + 亮字）：标题「连接失败」`#ffa39e`、描述 `#f0c9c6`、「重试」按钮 `#ffd6d3` + 红边，卡片底 `#2a1215` —— 实测计算色 `rgb(255, 163, 158)` / `rgb(240, 201, 198)` / `rgb(255, 214, 211)`；修复前标题被 antd v6 的浅色主题令牌压成 `rgba(0, 0, 0, 0.88)`，深底上几乎看不见 |
| [rdp-legacy-2003-message.png](docs/screenshots/rdp-legacy-2003-message.png) | Windows Server 2003（`192.168.0.105`）连不上的**人话化报错**实拍：「连接失败 | 与堡垒机的远程桌面通道在认证阶段就断了（目标机常常一个字节都没回就直接关闭连接，Windows Server 2003 / XP 这类只支持老旧 RDP 协议的系统尤其如此）。可在资产管理里把这台主机的「RDP 安全层」设为「强制 SSL」再试；若仍失败，请改用 Windows 自带的「远程桌面连接」访问该主机。原始信息：[read frame by hint] custom error: WebSocket connection failed」——**不再只丢「客户端内部错误」**；同一刻的审计（`rdp_session_close` id 477）写着 `[SSL: TLSV1_ALERT_INTERNAL_ERROR]` + 「目标机在 NLA/CredSSP 认证阶段没回任何数据就关闭了连接…可在资产管理里把这台主机的「RDP 安全层」设成「强制 SSL」再试」 |
| [rdp-security-field.png](docs/screenshots/rdp-security-field.png) | 资产管理 · 主机编辑抽屉里的**「RDP 安全层」**（`auto` / `ssl`）与协议说明实拍：选「自动」按客户端协商（含 NLA）；老系统（Windows Server 2003 / XP 一类）在 NLA 阶段被直接断开时改成「强制 SSL」退回标准 RDP 安全层。该字段由 `Host.rdp_security` 落库，票据与审计都带着它的取值 |

已修复并在测试中固化的真实缺陷（节选，均带回归用例）：

- `verify_password(明文, 哈希)` 参数顺序在调用点传反 → **任何人都无法登录**（新增「新建凭据必须能立刻登录」用例）。
- `naive/aware datetime` 混用导致 `TypeError: can't subtract offset-naive and offset-aware datetimes`（统一 `utcnow()` 为 naive UTC + 归一化函数）。
- 只读白名单策略可被 **输出重定向** 绕过去写系统文件（`echo x > /etc/passwd`）、放行 `tee/dd/truncate`；以及白名单放行 `cat` 导致 **`cat /etc/shadow` 读到口令哈希** —— 均已加黑名单并回归。
- 授权解析会 **静默把用户点名的账号换成另一个账号** → 改为精确匹配，匹配不到就拒绝。
- `host-groups`、`policy-rules` 两组接口因蓝图漏注册而 **静默 404**；`has_any_permission` 签名与调用不一致导致多个接口 500；`CommandLog.created_at` 列名错误导致审计接口 500。
- 会话结束后记录永远停在 `active`（桥接线程异步收口）→ 主动断开时同步收口。
- 策略拒绝后 `at_prompt` 被误置为 False，导致下一条命令被当作「交互输入」**透传且不留 CommandLog** → 修正状态机，保证拒绝后每条命令仍完整审计。
- SSH 网关菜单在真实终端里 **文字错位（阶梯状右移）**：帮助块用的是裸 `\n`（PTY 里只下移不回列），且主机/账号列按**字符数**补齐（中文占 2 列）→ 统一 `to_crlf()` 归一 + 按显示列宽 `pad_display()` 对齐；并顺带修掉**窄终端**（80 列默认宽度）下主机行 95 列被折行的问题（自动切换两行式布局）。共 11 条排版回归用例。
- **SSH 网关进站「太素」+ `=` 号横贯整屏**（用户实测反馈）：菜单分隔线是写死的 `"=" * 78`，文字只占约 60 列，视觉上就是一条与内容无关的长横线；整个菜单只有白字。改为**四条分隔线的长度各自等于相邻内容块的显示宽度**（身份块 68 / 主机块 106 / 帮助块 38 列），并给取值上色（主机名白粗、`地址:端口` 青、分组暗、策略黄、账号绿、分隔线蓝、`>_` 黄），标签与列宽仍按 `strip_ansi()` 后的可见文本计算。进站再加一段**彩色字符画**（实心块盾牌 + `A u t o O p s` 字标 + 副标题），宽度不够时整块退化为空、绝不折行。**踩过的坑**：先用 `▀▄` 上下半块拼 5 行点阵字母，再用全实心 `█` 点阵，两版都在真实截图里**碎成虚线**（块字符在不同字体/行高下上下不一定严丝合缝，Consolas 与 Cascadia Mono 都留缝）→ 最终**竖笔画一律靠文本、块字符只用于横向条块（盾牌）**；字符画宽度 29 列，40 列终端也能完整显示。

- 进入目标机会话时 **目标机欢迎语抢在堡垒机横幅之前**（横幅在 `open_session()` 返回后才写，而读数线程已经启动）→ 新增 `on_ready(sid, info)` 回调，横幅/`terminal:opened` 一律在 `bridge.start()` 之前发出。
- **提示符「没有从头开始」**：演示目标机 / 网络设备 / 部分容器 shell 不做 PTY 的 `ONLCR` 转换，裸 `\n` 直接送到客户端，终端里 LF 只下移不回列 → 客户端可见输出路径统一走 `LineEndingNormalizer`（等价于替目标机补 ONLCR，录像仍存原始字节）。
- 网页终端 **会话就绪前的按键被静默丢弃**：`terminal:opened` 一度在 `ctx["opened"]` 写入之前发出，客户端收到后立刻发 `terminal:input` 时服务端还没存会话 → 第一条命令凭空消失（真机联调表现为 web 端 `whoami` 不回显、实时命令推送为空、命令日志里只剩第二条）→ 输出加**闸门**（`OutputPump.pause()/release()`）、`opened` 改为会话就绪后统一发出，并新增**就绪前按键缓冲 + 回放**，三条不变量各有回归用例。
- **演示目标机敲键不回显**（用户实测反馈）：真实 Linux 的逐字符回显是 **pty 驱动（ECHO 标志）**做的，而演示目标机早期只在收到 Enter 后整行回显 → 经堡垒机连接时敲键期间一个字节都看不到，看起来像堡垒机吞了输入。用 A/B 探针证实「直连 2200 ≡ 经网关 2222」都是空回显（堡垒机忠实透传）→ 给演示目标机补上 tty 行规程（`line_discipline_echo()`），8 条单测固化。
- **白名单策略把 `exit` 也判成拒绝**：只读策略 `default_action=deny` + 兜底规则让 `exit`/`quit`/`logout` 全被拒 → 命令发不到目标机、远端 shell 不结束，**用户被困在会话里出不去**（横幅却写着「输入 exit 返回主机菜单」）→ 新增 `policy.is_session_exit()`，会话控制指令在策略评估前放行，仍完整落审计（`action=allow`、标注「不受命令策略限制」）。
- **控制台右面板地址显示成 `127.0.0.1:2200:2200`**：`terminal:opened` 回来时 `tab.address` 已经是「主机:端口」，面板又拼了一次 `:${tab.port}` → 改为「已含端口就用原值，否则补端口」，同一份数据在状态条、面板、右键菜单里都一致。
- **终端宿主 div 丢了 CSS 类名**：`console.css` 里 `.bastion-terminal-host`（`position:absolute; inset:0`）与 `.bastion-terminal-host .xterm{height:100%}` 一直没有被挂到 DOM 上，终端高度只能靠父容器凑巧撑开（多标签/全屏切换时容易量到 0 尺寸而 `fit()` 失效）→ 给 `TerminalPane` 的宿主节点补上类名。
- **终端页冒出一个「小输入框」+ 框选后整屏被绿色盖住**（用户实测反馈）：`TerminalPane` 从头到尾**没有引入 `@xterm/xterm/css/xterm.css`** —— xterm 的隐藏辅助输入框（IME/按键捕获用的 `.xterm-helper-textarea`）失去 `position:absolute; opacity:0`，直接以 8×18 的 `inline-block` 样式冒在终端左上角；同时 `.xterm-screen{position:relative}` 等定位规则全缺失，行/选中高亮元素失去绝对定位，框选时绿色高亮块盖住整屏文字 → 在 `TerminalPane.tsx` 引入 `@xterm/xterm/css/xterm.css`（xterm 官方要求的样式表），并把选中高亮由品牌绿 `rgba(82,196,26,.35)` 换成中性蓝 `rgba(96,165,250,.32)` + `selectionForeground:#fff`，避免和绿色光标/提示符糊在一起。实测：辅助输入框恢复 `position:absolute; opacity:0`、拖选 3 个选中格背景 `rgb(38,64,103)`、文字清晰可读。
- **「浏览器拦截了新标签页」误报**：资产列表页曾用 `window.open(url, '_blank', 'noopener')` 的返回值判断弹窗是否被拦截，而按规范**带 `noopener` 时它恒返回 `null`** —— 标签页其实开成功了也会弹告警 → 现在的写法是 `window.open(url, 窗口名, 'popup=yes,width=…,height=…')`（不带 `noopener`，因为弹窗里的「资产列表」要靠 `window.opener` 聚焦回原窗口），拿到句柄 `opened.focus()`，只有确实为 `null` 才提示「请允许本站点弹出窗口」。（巡检脚本侧另有两条坑：`el.click()` 不产生用户激活、`window.open` 会被当弹窗拦掉，必须发真实鼠标事件；且按钮要先 `scrollIntoView`，否则坐标落在视口外点空。）
- **Biome 把终端里的 `fit()` 当成「只跑这个测试」**：`components/TerminalPane.tsx` 里一个负责重新量尺寸的本地 `useCallback` 名叫 `fit`，被 `lint/suspicious/noFocusedTests`（jasmine/vitest 的 `fit(` 只跑当前用例）误判成 3 条 warning → 内部实现改名 `fitTerminal`（对外 `TerminalHandle.fit` 属性名保持不变，调用方无感），warning 归零。
- **终端弹窗里「资产列表」和「全屏」点了没反应**（用户实测反馈）：① 「资产列表」只调了 `window.opener.focus()` —— 浏览器对**跨窗口聚焦**基本是忽略的（没有用户激活时更是无效），看起来就是「点了没用」；② 「全屏」只切了一个 CSS class，`document.fullscreenElement` 恒为 `null`，Esc、F11 与浏览器自身的全屏状态互相打架。改成：回到列表 = 关掉本窗口（`window.close()`，失败则 `location.assign('/terminal')` 兜底），全屏 = 优先 `document.documentElement.requestFullscreen()`、被拒才退回 CSS class，`fullscreenchange` 反向同步按钮状态，`Ctrl/Cmd+Shift+F` 与工具条走同一条路径。巡检固化 6 项断言（真进/真退全屏各两次、断开倒计时、自动关窗、「资产列表」关窗）。**巡检脚本侧另有一条同类坑**：进全屏会改变视口尺寸，工具条按钮从 `x=667.7` 位移到 `x=682`，复用进全屏前的坐标会点空 —— 每次点击前重新 `find_rect` 取坐标。
- **断开网页终端会话后窗口留在原地**（用户实测反馈）：原先断开只是把终端写成「已断开」，弹窗还开着，用户得自己找关闭按钮。现在断开后终端内出现 `本窗口将在 10 秒后自动关闭…` 的**原地倒计时**（`\r` + `\x1b[K` 逐秒刷新，不刷屏），数到 0 自动 `window.close()`；浏览器只允许脚本打开的窗口自关，被拒时兜底提示「请手动关闭本窗口（Ctrl/Cmd + W）」。倒计时期间点「重新连接」会取消倒计时继续使用本窗口；**通道掉线（socket 断开）不启动倒计时**，保留「重新连接」。
- **`ui_check.py` 两次「假信号」**（都不是页面坏了，是判据错了）：① 固定 `time.sleep(5)` 判「渲染完成」→ 后端同时跑 pytest 时 `/api/policies` 慢过 5 秒，`/policies` 只剩页头 53 字 → 误红；② 改成「正文 >120 字即算渲染完」→ 读到表格还没有数据的中间态（`/dashboard` 只读到 245 字），弱化证据并可能漏掉「数据回来之后才崩」。现在先给 3 秒下限、再等正文**连续 3 次不变**；并把「无 Ant Design Pro 痕迹 + 品牌 Logo 是本地 SVG 且已加载」固化成 4 条断言。教训：**不要和后端 pytest 并行跑浏览器巡检**（抢同一个后端与 SQLite）。
- **产品里到处是「功能怎么用」的说明性横幅**（用户长期偏好：「以后写代码开发也别加了」）：`审计范围`、`每条命令都同时记录了输入`、`用户的可用主机与命令策略由访问授权决定`、`这些参数对所有会话立即生效`、`规则按优先级从小到大逐条匹配`、仪表盘`审计说明` 共 6 处 info Alert 全部删除。保留的是**功能性**提示：状态告警（强制改密 / 网关未监听 / 缺少权限）、破坏性操作确认、错误与重试、空状态，以及表单里的输入示例（密码规则、正则匹配示例）。
- **交付物里残留 ant-design-pro 模板痕迹**：布局 Logo 指向 `gw.alipayobjects.com` 的外链（内网环境根本取不到）、页脚写着 `Ant Design Pro ©` 并挂着 Umi/Utoo/GitHub 仓库链接、右上角有 Docs 与 v5/v4/v2 版本外链菜单、登录页背景用 `mdn.alipayobjects.com` 外链图、`config/config.ts` 里还留着模板作者的 **Google Analytics 埋点**（会向模板方上报）。现在：`public/logo.svg` 与 `public/favicon.svg` 换成自绘几何盾牌（`#2E7BFF→#12B8C8` 渐变 + 青色 `>_` 提示符，纯 path，无外部字体图片），`defaultSettings.logo = '/logo.svg'`、`favicons: ['/favicon.svg']`，页脚先是改成 `AutoOps 堡垒机 © {年} / 命令级策略管控 · 会话全程留痕`，后来按用户要求**整体删除**（`app.tsx` 里 `footerRender: false`，`src/components/Footer` 组件与登录页底部那处引用一并移除），删掉 Docs 与版本外链菜单，登录页背景改本地 `linear-gradient`，移除 GA 埋点与三个版本宏。构建产物 `dist/*.js|html` 里 `alipayobjects.com|ant.design|umijs.org|utoo.land|github.com/ant-design|G-59NF1VHHPF` **零命中**。
- **升级到带文件管理器的版本后，老库里的角色权限与内置文件策略全都不落库（真缺陷，最危险的一条）**：`seed_data()` 里 `changed` 只在「本次新建了角色」时才置 True，`if changed: _db.session.commit()` —— 老库里 `admin/ops/auditor/viewer` 都已存在，于是**角色权限的更新永远不会提交**；而 `seed_policies()` / `seed_file_policies()` 只 `session.flush()` 不 commit，本来就靠上面那次 commit 兜底。后果：升级后 `ops` 拿不到 `file:use`（文件接口一律 403）、`file_policies` 表为空 —— 而**文件策略为空意味着试算器把 `/etc/passwd` 的写操作判成「未绑定策略，按审计放行处理」**（策略缺口被静默当成放行）。修法：角色循环结束后无条件 commit，并在三个 seed 之后再 commit 一次兜底；新增 `tests/test_seed_upgrade.py` 对「已存在的旧库再跑一次 `create_app()`」做回归（角色权限、内置命令策略、内置文件策略与 `builtin_file_policy_rev` 都必须补齐并落库）。
- **已打开的文件会话不跟随授权变更（真缺陷）**：`FileSession` 在 `open_file_session()` 时把 `can_sftp/can_upload/can_download/can_file_write` 拍平成字段，后续判定只读这份快照 —— 管理员把「允许改文件 / 上传」打开（或**把 SFTP 整个关掉**）对已经开着的窗口毫无影响，撤销高危能力不生效。改为 `FileSession.rights()` 每次判定都按 `grant_id` 重读授权（授权行被删则四个开关全 False，脱离应用上下文则退回快照），`capabilities()`/`to_dict()` 也走它；新增 `test_grant_changes_apply_to_the_open_session`（关 → 改开 → 当场可建目录 → 再改关 → 又拒且远端无目录）。巡检侧用同一个会话复验：管理员 PUT 打开开关后，立即在同一 sid 上上传/建目录/保存/重命名/复制/移动/改权限/删除全部转绿。
- **打包下载遇到不存在的路径直接 500**：`_collect_archive_members()` 里 `session.sftp.stat(target)` 抛 `FileNotFoundError: [Errno 2] No such file`，被 Flask 兜成**未处理 500**（响应是真 JSON 但状态码 500，且这次操作**没有留下任何审计**）。改为 `_wrap_io()` 归一成业务错误（缺文件 → 404 `FILE_NOT_FOUND`、权限 → 403、其余 → 502），并且 `archive_paths()` 采集失败时**也要写一条 `result=failure` 的文件审计**再抛出（打包失败也是一次操作）。回归：`test_archive_missing_path_fails_cleanly_and_is_audited`。
- **默认文件策略漏了「复制到系统目录」**：`copy` 只按**源路径**判定，于是可以把 `/data/notes.txt` 复制进 `/etc`（源路径合法、目标非法）→ 新增一条 `copy` 规则（优先级 28）拦 `SYSTEM_PATH_PATTERN`；`rename/move/copy` 现在同时校验源与**目标**路径，任一被拦即整条拒绝。用例：`test_copy_into_system_dir_is_blocked_on_target_path`。
- **「只读浏览」文件策略把「列目录」也拒了**：白名单式只读策略（`default_action=deny`）下连 `list /` 都不放行 —— 用户进窗口第一步就看不到任何文件，等于策略把功能整体废掉。补两条内置规则：优先级 49 放行 `list`（导航路径 `^/.*$`）、优先级 53 在白名单目录放行 `archive`，并把内置策略版本 `BUILTIN_FILE_POLICY_REV` 提到 2（版本号变化才会对老库重新播种）。
- **paramiko SFTP 子系统协商 EOF**：自建 SFTP 服务端时覆写 `check_channel_subsystem_request` 直接 `return True`，丢掉了父类默认实现里「真的把子系统接起来」的那一步 → 客户端侧 `EOF during negotiation`。必须 `return super().check_channel_subsystem_request(channel, request)`（`tools/sftp_backend.py` 与 `tests/sftp_target.py` 都按这个写）。
- **Windows 上 `os.chmod` 只切换只读位**：本地开发的演示 SFTP 后端拿到 `chmod 0755` 后，`st_mode` 里只有 owner-write 会变 → 改权限用例只断言 `re.fullmatch(r"\d{4}", stat["modeOctal"])`（四位八进制格式正确），不断言具体位。
- **老库没有迁移工具（无 Alembic）**：新增 `app/schema_sync.py:ensure_schema()`，在 `create_app()` 里 `db.create_all()` 之后跑一轮**只做加法**的 `ALTER TABLE … ADD COLUMN`（按 `PRAGMA table_info` 判断列是否已存在），让「加字段」这种升级不需要重建开发库（本轮 `grants.file_policy_id` / `grants.can_file_write` 就是靠它平滑升级的；探针复现过 `sqlalchemy.exc.OperationalError: no such column: grants.file_policy_id`）。
- **`session_idle_timeout` 是个摆设（真缺陷，浏览器实测揪出）**：参数设置里能改、默认 1800 秒，但全代码库**没有任何一处读取它** —— `app/session_registry.py` 的 `last_active`/`touch()` 与 `FileSession.touch()` 一直只在**更新**空闲时间，没人**检查**。后果：关掉浏览器标签页不会触发前端卸载清理（也不该信任客户端），网页终端/SFTP 会话永久留在注册表里占配额 —— 实测泄漏 8 条文件会话，正好撞满 `MAX_SESSIONS_PER_USER=8`，之后 `POST /api/files/sessions` 一律 `QUOTA_EXCEEDED`。修法：新增 `app/idle_sweeper.py`（`create_app()` 启动守护线程，每 30 秒扫一次，`now - last_active >= timeout` 就 `registry.close(sid, reason="空闲超时自动断开")` 并收口会话记录；`timeout<=0` 不清理；`TESTING`/`IDLE_SWEEPER_DISABLED` 不启线程），并让 `ShellBridge.feed_input()`（用户按键）与 `_emit_output()`（目标机输出）都 `registry_touch(sid)`。真机验证：`session_idle_timeout=20` → 75 秒后会话从在线列表消失、记录变 `terminated`/`空闲超时自动断开`。
- **改权限「声称改了、其实没改」（真缺陷，浏览器实测揪出）**：`chmod_path()` 返回的 `after` 直接是**请求值** `f"{mode:04o}"`，审计 message 也跟着写「0666 → 0600」。但演示目标机跑在 Windows 上（`os.chmod` 只能切只读位），文件系统权限**实际没变**，列表回读恒为 `0666` —— 也就是说审计里会留下一条假成功。改为 chmod 之后**重新 `stat` 回读真实权限**作为 `after`（读不到才退回请求值），返回体补 `requested` 字段（`{"path","before","after","requested"}`），审计用实际值；测试相应改成「`after` 与随后 `stat` 一致」而不是「等于请求值」。
- **点「个人设置」页签只改 URL、不切内容（真缺陷，浏览器实测揪出）**：`src/pages/bastion/account/index.tsx:278` 用 `history.location.search` 推导当前页签 —— 这个值不是响应式的，点页签后 URL 变成 `?tab=security` 但内容区仍停在「基本资料」（用户看到的就是「点了没反应」；从右上角用户菜单「修改密码」跳进来反而正常，因为那次是整页路由跳转）。改为 `const [searchParams] = useSearchParams(); const tab = searchParams.get('tab') ?? 'profile';`。
- **文件管理器的「修改时间」整列显示 `NaN-NaN-NaN NaN:NaN`（真缺陷，浏览器实测揪出）**：后端 `list_dir`/`stat_path` 返回的 `mtime` 是 **ISO8601 字符串**（`_iso()` 产出，形如 `2026-10-01T08:03:26.123456Z`），而前端 `types.ts` 把它声明成 `number`（注释还写着「秒级时间戳」），列表渲染又用本地 `formatTime(seconds) => new Date(seconds * 1000)` —— 字符串乘 1000 得 `NaN`，`new Date(NaN)` 再被 `getFullYear()/getMonth()/getDate()` 拼成 `NaN-NaN-NaN NaN:NaN`。修法：`FileEntry.mtime` 类型改成 `string` 并订正注释，删掉本地 `formatTime`，改用仓库既有的 `formatDateTime`（`constants.ts`，专为后端 UTC ISO 串写的，非法值回退原文、空值给 `-`）。顺带核对：编辑器保存用的 mtime 来自 `GET .../read`（那个接口确实返回 epoch 秒），所以 409 冲突检测**没有**被这个类型错误影响。
- **参数设置页「保存」必然报错（真缺陷，浏览器实测揪出）**：`PUT /api/settings` 返回 400 `「默认命令策略」必须是整数` —— `app/api/settings.py` 把 `default_policy_id` 放进 `INT_KEYS` 走严格 `parse_int`，而该键的默认值本来就是空串（`app/settings_store.py` `"default_policy_id": ""`），前端又把空值原样提交，于是**整页设置都存不下去**。修法：新增 `OPTIONAL_INT_KEYS = {"default_policy_id"}`，空/None 直接按「未指定默认策略」落库，其余整型键仍严格校验。
- **改系统设置从不写审计（真缺陷，浏览器实测揪出；违反需求①）**：`update_settings()` 返回的是**被忽略的键**（比如 `default_policy_id` 这类白名单外或格式不符的键），而 `app/api/settings.py` 的 PUT 与 reset 都把它当成「发生变化的键」→ 保存成功也提示「设置无变化」，`if changed:` 永不成立 → **谁改了系统设置、改了哪些项，审计里查不到**。修法：`update_settings()` 先 `all_settings()` 取旧值、逐键比较后返回**真正变化的键**，两处调用方无需改动即恢复正确语义；回归用例断言 `AuditLog.action="update_settings"` 且 message 含改动的键名。

- **连上目标机后终端里还残留上一屏（用户实测反馈：「太乱了」）**：会话建立时只写了横幅与欢迎语，目标机自己的上一屏（登录前的提示符、上一次会话的滚屏）还在视口里。改为**会话就绪后先清屏再重画**（`\x1b[2J\x1b[H` + 上下文 + 横幅），网关与网页终端两条入口同一措辞；清屏后补一个**裸回车**触发远端提示符重绘 —— 裸回走在 `ShellBridge.feed_input()` 的 Enter 分支之前就 `return`（`bridge.py` 里空行在任何 `_start_command()`/`evaluate_policy()` 之前返回），所以**不落 CommandLog、不进策略引擎**，回归用例断言 `CommandLog` 计数为 0。
- **演示目标机把粘贴标记当命令字符（真缺陷，真机揪出）**：终端里粘贴 `/ask-ai 你好` 时客户端会带上 bracketed paste 的 `ESC[200~`/`ESC[201~`，演示目标机把它们当普通字符写进行缓冲 → 下一条命令变成 `-bash: [201~echo: command not found`。给演示目标机新增 `TtyEscapeFilter`（CSI/SS3/OSC 状态机，转义序列先吃掉再写缓冲；截断、超长、不支持的序列安全放弃，**绝不吞掉回车**），3 条用例固化。
- **链式哈希的「空哈希一律跳过」给了篡改者一件隐形斗篷（真缺陷，对抗测试揪出）**：`verify_table_chain()` 把空 `entry_hash` 的行无条件当「老库升级遗留」跳过 → **改完字段再把该行两列哈希一起清空**，校验照样返回 `{'ok': True, 'total': 5, 'verified': 4, 'first_bad': None}`，而界面用的 `GET /api/audits/chain` 又因为 `pending>0` 判它不健康 —— **同一个事实，CLI 说干净、徽标说红**，而运维只看退出码。修法：引入 `chain_started`，空哈希**只在链起步前**合法（计入 `prefix`）；链起步后再出现就计 `pending_inside`、`first_bad` 点名该行的 id、报「链内出现空 entry_hash（疑似清空哈希冒充升级前遗留）」，`healthy` 与 CLI 退出码只看 `pendingInside`，并用对抗用例把「徽标红 ⇔ `verify.ok=False`」钉在同一处断言里。
- **老库的遗留空哈希让完整性徽标「一上线就全红」（真缺陷，对抗测试揪出）**：`counts.pending = total - hashed` 被直接当成健康判据，而老数据行的哈希永远补不上 → 只要库里有历史记录，`healthy` 恒 `false`，用户看到的就是「产品刚上线就声称自己被篡改了」（恰好是「老数据兼容、不能一上线即红」要避免的情形）。改为区分 `pendingPrefix`（链前遗留，容忍）与 `pendingInside`（链内空洞，异常），`healthy` 只看后者，徽标另显示「升级前遗留 N 行（无哈希 · 不影响完整性）」。
- **链只能证明「手上这串是连续的」：整段删尾行在库内自洽（结构性边界，已补库外锚点）**：删掉最后 N 行后 `verify` 依旧 `ok=True` —— 这是链式哈希的固有边界（不是实现 bug），但产品必须给出可操作的答案：CLI 新增 `--print-head`（抄三张表的真链尾哈希到库外：异地日志/工单）与 `--expect-head TABLE=HASH`（复核，对不上 **exit 1**），接口加 `expected_head` 参数（缺 `table` 时 400 `ANCHOR_NEEDS_TABLE`），`verify_table_chain()` 增 `anchor_checked`，并把边界原话写进审计页的校验结论与详情抽屉文案（不再含糊地说「任何改动都能立即检出」）。

AIOps（AI 运维）相关的真实缺陷，同样都带回归用例：

- **流式对话一开就 `DetachedInstanceError`**：`_stream_turn()` 是在 Flask 请求返回后才被 SSE 迭代消费的，此时请求上下文已经结束，而它手里还攥着创建对话时查出来的 ORM 对象（`AiConversation`）—— 一读属性就炸。修法：SSE 生成器只接收 `conversation_id`，进入迭代后自己 `with app.app_context(): db.session.get(AiConversation, conversation_id)` 重新取对象；取不到就发一条 `error` 事件后收流。
- **duck-typing 读 `client.model` 触发 `AttributeError`**：服务层在 `start` 事件与消息落库里直接写 `client.model`，而测试替身/自建客户端没有这个属性 → 整个回合挂掉。新增 `_client_model(client)`（`getattr(client, "model", "") or ""`）兜底，两处调用点统一改走它。
- **未知工具的原因被吞掉**：模型挑了一个不存在的工具名时，事件与审计里只写「未知工具」，排查时看不到到底是哪个名字。改为 `f"未知工具：{name} 不存在，请从可用工具里选择"`（模型侧文本本来就是「[工具 x] 不存在」，现在事件、审计与终端提示口径一致）。
- **`Decision` 字段名写错导致 `POST /api/terminal/exec` 直接 500**：`app/api/sessions.py` 里读了 `decision.matched_rule_id` / `decision.matched_rule_pattern`，而 `app/policy.py` 的 `Decision` 真实字段是 `rule_id` / `rule_pattern`（4 处）→ AI 让「在目标机执行命令」这类工具必然 500。按真实字段名修正。
- **敏感操作的待确认事件缺字段**：`_confirm_event()` 早期只回 `toolCalls` 与少量字段，网页弹窗和终端提示都要靠它显示「哪个工具、什么参数、需要什么权限、一共几条」→ 补齐 `tool`/`name`/`sensitive`/`permission`/`count`/`callId`/`toolCallId`/`args`/`reason`/`conversationId` 与每条 `toolCalls[]` 的 `id/callId/name/args/description/permission`。
- **没配 Key 时先返回 200、再在流里报错**：前端会先亮起「对话已开始」，随后收到一条 error，看起来像用着用着崩了。改为请求入口先判空 Key，直接 400 `AI_NOT_CONFIGURED`（`AI_ENABLED=0` 则是 403 `AI_DISABLED`）。
- **SSH 网关 `/ask-ai` 会把整条会话掀掉（真缺陷，真机揪出）**：`ai_shell` 只在 `_ai_shell()` 里做延迟导入（为了避开与 `server.py` 的循环导入），而 `_run_session` 里却直接写了裸名 `ai_shell.extract_question(ai_line)` → 网关线程抛 `name 'ai_shell' is not defined`，**用户正在用的那条 SSH 会话直接断开**（后端日志 `app.gateway.server: gateway client 127.0.0.1 error: name 'ai_shell' is not defined`）。修为 `_ai_shell().extract_question(...)`，并给 `/ask-ai` 全程加 try/except：AI 侧出错只在终端打一行 `[堡垒机] AI 调用失败：…` 并写 warning 日志，会话继续留在 shell 里。
- **卡片 JSON 在流式过程中整段漏到终端（真缺陷，真机揪出）**：`TerminalMarkdown._render_line(partial=True)`（未收到的半行预览）**也会改状态** —— `\`\`\`` 围栏是逐字符匹配的（`\`\`\``、`\`\`\`a`…），预览阶段就把 `_in_code`/`_in_card` 反复翻转；等这一行真正落盘时再处理一遍，`ai-card` 块已被提前判定结束，后面的 JSON 就按正文渲染（超宽还会落盘不重绘）。现象是终端先滚出一大段 `{"type": "table", …` 再出现正确卡片。修法：`partial=True` 时**一律不改状态**（卡片块内直接 `return None`、围栏行先 `return ""`），状态翻转只发生在落盘那次。回归用例**逐字符**喂入 `` ```ai-card `` 整块并断言输出里没有 `"type"`/`rows`；另用 monkeypatch 把旧实现装回去复现，确认用例真的抓得住（旧实现 `leaked=True`，新实现 `leaked=False`）。
- **前端 AI 页两处类型错误（`tsc` 拦下）**：① `useRef<string>()` 缺初值（`TS2554`）→ `useRef<string | undefined>(undefined)`；② 给 `Bubble` 传 `avatar={{ icon: … }}` 报 `TS2353`，因为 `@ant-design/x` 的 `avatar?: BubbleSlot<ContentType>` 只接受 `ReactNode` 或渲染函数（不是 antd 的 `AvatarProps`）→ 改成 `<Avatar icon={<RobotOutlined />} style={{ background: '#1677ff' }} />`。
- **Biome `lint/suspicious/noArrayIndexKey`**：AI 页里把示例问题列表的 `map` index 当 `key`（列表重排时 React 会复用错节点）→ 改用 `item.label`。
- **`/ask-ai` 在网页终端里根本没人分流（真缺陷，用户实测反馈「无法在SSH里面使用ai」）**：`app/webterm/events.py` 的 `on_input` 把按键**原样**丢给 `bridge.feed_input()`，影子行缓冲只存在于 SSH 网关那一侧 —— 于是浏览器终端里敲 `/ask-ai` 就是让远端 bash 执行一条叫 `/ask-ai` 的命令（`-bash: /ask-ai: 没有那个文件或目录`）；同时旧的分流实现**命中即 `return`**，同一 chunk 里回车之后的字节被整段丢弃，粘贴收尾的 `ESC[201~` 也随之消失，远端 readline 卡在 bracketed paste 模式。修法：把影子行状态机抽成 **`app/ai/line_split.py`（`LineShadow.feed(raw) -> (forward_bytes, question|None)`）**，SSH 网关与网页终端两条入口共用同一份实现，命中后只吞该行的回车、**循环跑完整个 chunk**、其余字节照常转发。回归：`tests/test_ai_line_split.py`（34）+ `tests/test_webterm_ai.py`（6，含真实 Socket.IO 链路），并抽了 10 条清屏用例。
- **敏感操作确认弹窗里「管理员账号留空、只填密码」被后端 400（真缺陷，用户实测反馈「弹出授权卡片输入密码报错」）**：`app/api/ai.py` 的确认接口要求 `adminUsername` 与 `adminPassword` **都非空**，而前端标签写的是「管理员账号（留空则用当前账号）」→ 照着标签填（账号留空）必然 400，页面同时弹出两条互相矛盾的提示（`Request failed with status code 400` + 「请输入管理员账号与密码」）。修法：只有**密码为空**才 400（`请输入管理员密码`），账号留空则回退 `actor.username`；安全口径一步未放松 —— 仍要求 `is_admin()` 且口令校验通过，非管理员用自己口令确认依旧是 403、工具保持 pending 且不写库。
- **`/ai` 页「卡片套卡片」+ 对话列表悬浮 + 右侧 15% 空白（用户实测反馈）**：① 最外层是 `PageContainer` 自带的内容白卡片，里面又套 x 组件自己的卡片；② 覆盖 x 组件时选择器权重不够 —— antd 的 cssinjs 把 hashId 包进 `:where()`（权重 0），x 自己的 `.ant-bubble-list .ant-bubble-start:not(...)` 是 (0,4,0)，我用 (0,3,0) 的规则自然被盖掉，助手气泡的右侧留白一直存在（这就是「build 跑了两次」的原因）。修法：`PageContainer` 加 `ghost` 去掉外层白卡片、左栏对话条目改成平铺（无圆角/无投影/当前项左侧色条）、覆盖规则统一加 `.bastion-ai-page` 前缀并镜像 `:not()` 把权重抬到 (0,5,0)；另外删掉页头那行超长 `subTitle`（既被截断成 `AI ...`、又属于「说明性文案」，用户长期偏好里明确不要）。浏览器实测 13 项全绿（`rightGap=10`、`nestedCards=0`、`toolCardConflicts=0`、条目 `border-radius:0`/`box-shadow:none`）。
- **`/ask-ai` 突然每一次都报 `DeepSeek 返回 HTTP 400`（真缺陷，用户实测反馈）**：根因不在模型、Key 或参数（实测 payload 各变体全部 200），而是**历史报文非法**：管理员拒绝（或取消）敏感操作时，`reject_tool_call()` 只把 `ai_tool_calls.status` 改成 `rejected`，**从不写回 `role="tool"` 消息**，于是那条 assistant 消息声明的 `tool_calls` 永久悬空；OpenAI 兼容协议要求「assistant.tool_calls 的每个 id 后面必须紧跟同 `tool_call_id` 的 tool 响应」，而历史是累积的 → **这个对话之后每一次请求都是非法报文**，上游直接 400，且响应体为空，终端只能看到一句「DeepSeek 返回 HTTP 400」。库内取证：对话 3/5 都是「拒绝敏感操作 → 之后每条 assistant 都是「（调用模型失败：DeepSeek 返回 HTTP 400）」」。修法三层：① `reject_tool_call()` 拒绝时补写一条 tool 响应（`[工具 x] 未执行：管理员 y 拒绝（或取消）了该操作：…`）；② `build_messages()` 还原历史时做**协议修复** —— 没等到结果的 `tool_calls` 就地补一条「未执行」的 tool 响应、窗口切在中间产生的孤儿 tool 消息直接丢弃、`status="error"` 的报错占位不回灌（**老对话因此自愈，不需要清库**）；③ `_http_message()` 在上游响应体为空时明说「（上游响应体为空，无错误详情）」，不再只丢一个状态码。回归：`tests/test_ai_api.py` 新增 5 例（含 `_protocol_problems()` 协议校验器），并做真机验证（把线上库拷一份，对话 3/5 各续问一句 → HTTP 200、零 `error` 事件）。
- **AI 工具返回的 JSON 在审计里就是一堵墙（真缺陷，用户实测反馈「一大堆 json 堆在那里不美观」）**：`/audit/ai` 的对话详情里，`role="tool"` 消息被 `renderMessage` 直接 `<Paragraph>{item.content}</Paragraph>` 糊出来 —— 那段正文是 `[工具 list_ai_tools] 成功：ok\n{...}` 的**混合文本**，尾部还带 `…（结果过长，已截断，共 63909 字符）`；用户截图里那一屏 `args` / `category` / `description` / `method` / `name` / `path` / `permission` / `sensitive` 的字段表就是它（库里最长一条 8048 字符、原始 63909 字符）。先前我把 `JsonBlock` 换成 `PayloadBlock` 等于没修：正文不是合法 JSON，解析入口直接判定为纯文本，照样整段直出。修法：新增**纯 TS** 的 `src/components/Bastion/jsonText.ts`（不引 React/antd，好单测）—— `splitToolMessage()` 把「工具名 + 状态 + 摘要」与「JSON 载荷」拆开（载荷起点取第一行以 `{`/`[` 开头且不是 `[工具` 的行；失败消息多出来的那行 `HTTP 403` 不挡起点）；`repairTruncatedJson()` 按「**只认闭括号收尾**」的安全点把被截断的 JSON 修回**完整条目**（截断常落在条目中间，退到上一个逗号会把 `"count": 7` 这种半截值当完整值 —— 宁可少渲染一条也不给假数据），并在卡片上打「后端已截断，这里按完整条目渲染」标注；`parseMaybeJson()` 统一三条路径（原样 `/` 修复 `/` 回退纯文本）。审计页的入参列、结果列（「查看输出」弹层）、对话详情抽屉、「工具」角色消息，以及 AI 页的工具块与敏感操作确认弹窗全部换成 `JsonCards` 卡片，**每一层都保留「原始 JSON」入口**（审计口径不因好看而让步）。回归：`jsonText.test.ts` 9 条 + 浏览器探针 22 项（含「详情里 8KB 载荷 `pre=0`、卡片渲染、已截断标注可见」）。
- **审计文案里机器词与中文混排（真缺陷，用户实测反馈「给人看的用中文直白描写，给系统看的用英文」）**：概览「最近审计事件」里能看到 `AI 调用工具 list_files：OK（共 0 条）`、`list_file_sessions：ok`、`失败：权限不足，需要：setting:view` 三种口径混在一起。根因有两处：① 工具结果摘要里的 `ok` / `OK（共 N 条）` 是工具自己返回的机器词，被直接拼进审计 `message`；② 文件域的失败审计直接把 Python/paramiko 的英文异常 `str(exc)` 写进去。修法分三层：`app/ai/tools.py` 的 `_human_message()`（`ok`→`成功`、`OK（共 N 条）`→`成功（共 N 条）`）、`app/files/service.py` 的 `_human_error()`（失败统一「修改文件权限失败：Permission denied」—— **只加中文框，异常原文一字不改**，一处收口全部失败分支）；**新写入的审计记录本身就是中文**，而**历史记录不可回改**（审计只增不删的语义），所以展示层再加一道 `humanAuditMessage()`（`src/services/bastion/constants.ts`，只归一开头的机器词，后面括号说明与异常原文原样保留），接线在概览、操作日志、文件记录、AI 工具调用结果四处。机器可读字段（`ai` / `ai_tool_call` / `ai_chat` / 权限码 / 工具名）始终保留英文。回归：`tests/test_ai_tools.py` 18 条 + `tests/test_files_service.py` 的 `test_failure_audit_message_is_framed_in_chinese_but_keeps_the_raw_error` + [audit-events-zh.png](docs/screenshots/audit-events-zh.png)。

- **门禁工具自己在中文控制台上崩掉，把「96 项全过」跑成了退出码 1（真缺陷，本轮跑 live 时暴露）**：`tools/live_e2e_check.py` 的 `check()` 直接 `print` 检查项名字，而 AIOps 那一段的项名带带圈数字（`需求⑥~⑬`）；Windows 的控制台/管道默认 `cp936`（GBK），`⑪`（U+246A）在该编码里不存在 → `UnicodeEncodeError: 'gbk' codec can't encode character '\u246a'`，报告在打印中途被打断、`$LASTEXITCODE` 变成 1；顺带中文经管道还会变乱码（`�� 21 ��·�ɣ�ͨ�� 21��ʧ�� 0`），**门禁的结论因此不可信**（我一度把它读成产品失败）。修法：三个门禁工具（`tools/live_e2e_check.py`、`tools/ui_check.py`、`tools/console_check.py`）在 import 之后统一 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（带 `hasattr` 守卫）：任何字符都编码得出来、报告不会再自崩，中文在 UTF-8 管道下也正常显示。
- **WebRDP 死活连不上：合法连接被自己的握手白名单否掉（真缺陷，真机联调揪出）**：浏览器侧 `ironrdp-wasm` 请求的是 `PROTOCOL_HYBRID_EX (0x08)`，而 `app/rdp/cleanpath.py` 的 `perform_handshake()` 只认 `SSL`/`HYBRID`，遇到 `HYBRID_EX` 直接判为「不支持的协议」把连接否掉（RDCleanPath 里的 `X224_CC_HYBRID_EX`）。修法：把 `HYBRID_EX` 加进 `TLS_CAPABLE_PROTOCOLS`（它同样意味着「先起 TLS 再谈凭据」），并补 `X224_CC_HYBRID_EX` 的封包/协商回归用例。**教训**：调试期一起出现的 werkzeug `write() before start_response` 只是开发服务器的噪声，不是原因（别顺着它改）。
- **远程桌面会话能连上却收不了口：`DetachedInstanceError` 让 `rdp_session_close` 凭空消失（真缺陷，浏览器实测揪出）**：关掉控制台标签页后，`sessions` 行永远停在 `status='active'`、`bytes_in/out` 全 0，审计里也**没有** `rdp_session_close`。根因是 `app/rdp/hooks.py` 的 `_open_session()` 把 **ORM 实例**交给了网关：`_run()` 结束时会 `db.session.remove()`，实例随即 detach，收口时 `_close_session()` 在**新的** app context 里再碰它 → `sqlalchemy.orm.exc.DetachedInstanceError`，异常被网关的 `try/except` 吞掉只留一行日志。修法：`_open_session()` 只返回**记录 id**，`_close_session(session_id, counters)` 在新 context 里 `db.session.get(SessionRecord, session_id)` 重新取；顺带修掉 `proxy.py` 里 `session = hooks.open_session(…)` 抛异常时的 `UnboundLocalError`（`session = None` 提前初始化）。回归：`test_hooks_open_and_close_session`（断言 `status=closed`、`bytes_in=1234`、`bytes_out=5678`、`end_reason`、`ended_at` 与 open/close 两条审计）；实机复核 `sessions` id=14 与 `rdp_session_close` id=159 双绿。
- **全屏后鼠标整体偏移（真缺陷，用户实测报出）**：`toDesktopPoint()` 原来按 `canvas.getBoundingClientRect()` 的整块矩形算比例（`scaleX = canvas.width / rect.width`），而画面是 `object-fit: contain` —— 全屏时元素被撑满整屏、画面却居中留黑边，比例把黑边也算进去，越靠边缘偏得越多。修法：按 `min(rect.width / canvas.width, rect.height / canvas.height)` 求真实内容缩放，再减去居中偏移 `(rect.width - canvas.width * scale) / 2`，最后把坐标钳进 `[0, canvas.width]`；`src/pages/bastion/rdp/input.test.ts`（**5 例**）钉住 1:1、等比放大、左右黑边、上下黑边、越界钳制，其中显式断言「黑边上的点击**不能**得到旧算法的错值 7」（`expect(...).not.toBe(7)`）。**教训**：这种「看上去只是偏一点」的问题靠截图判不了，必须有数值断言。
- **会话结束（或被踢下线）后窗口一直不关（真缺陷，用户实测报出）**：Linux 网页终端断开后有关窗倒计时，Windows 远程桌面没有。修法：统一出口 `handleSessionEnd(reason)` → 弹「远程桌面会话已结束」+ **10 秒倒计时**（与终端共用同一个常量 `CLOSE_COUNTDOWN_SECONDS = 10`），footer 给「留在本窗口 / 立即关闭」；**录像还在上传时不关窗**（等 `finishRecording()` 落地），`window.close()` 被浏览器拒时提示手动 `Ctrl/Cmd + W`。
- **RDP 中继会「假死」：`sendall()` 返回成功但字节没上线（真缺陷，测试暴露）**：`tests/test_rdp_gateway.py` 整文件连跑的通过率只有约 1/3，多数时候挂死在 `test_tunnel_relays_bytes_and_calls_hooks`，单跑却不复现。faulthandler 三线程栈（主线程在 `relay()` 空转、pump 线程阻塞在 `tls_socket.recv`、假目标机阻塞在 `tls.recv`）加上挂起现场 TCP 表（客户端与服务端**成对 `Established`**）共同证明：连接是通的，消失的是 SSL 层里那 4 个字节 —— **同一个 `SSLSocket` 被两个线程同时读写**（OpenSSL 的 `SSL` 对象不是线程安全的）。第一版修法（新增常量 `RELAY_POLL_SECONDS = 0.05`，读线程先 `select.select([tls_socket], [], [], 0.05)` 等可读、再在 `io_lock` 内 `recv`，写线程在 `io_lock` 内 `sendall`）把挂起概率降到约 1/6，但**全量回归时被新加的看门狗抓到死锁**：`select()` 说「可读」并不等于 `recv()` 不会阻塞 —— TLS 握手后的记录（`NewSessionTicket` 之类）被 OpenSSL 内部吃掉之后如果还没有应用数据，阻塞模式的 `recv()` 就挂住，而此时读线程**正握着 `io_lock`**，写线程永远等不到锁，两个方向一起死（线程栈：一个停在 `recv`、一个停在抢锁）。最终修法：**单线程 + 非阻塞套接字**（`tls_socket.setblocking(False)`；`recv` 只认 `SSLWantReadError`/`BlockingIOError`，当作「暂时没数据」先回去处理 WebSocket 方向；`send` 遇 `SSLWantWriteError` 就 `select` 等可写再续写），全程没有任何一处会长时间阻塞，也就一把锁都不需要。修后整文件**连跑 8 次全绿**。配套两处：这条用例加 30 秒看门狗（仓库没有装 `pytest-timeout`），超时先 `faulthandler.dump_traceback(all_threads=True)` 再 `pytest.fail`，把「无限挂住」变成「带现场证据的失败」；`FakeWebSocket.receive()` 在 inbox 为空时补 `time.sleep(0.005)`，因为真实的 `simple_websocket.receive()` 会阻塞等待，而不是忙等烧 CPU。
- **远程桌面「窗口拉大后右下留黑带、工具条分辨率还撒谎」（用户实测反馈，真缺陷）**：根因是自己写的 `applyViewport()` —— 它按舞台尺寸手工设置 `canvas.width/height`（**这会清空画布**），但画布后备缓冲的所有权其实在 wasm 手里（它作画时会把尺寸重置回桌面分辨率、`Session.resize()` 时按请求改），而 RDP 只补发增量区域、静态桌面根本不再发帧 ⇒ 被我清掉的那块永远是黑的；工具条又把「我们请求的尺寸」当成了「远端真实尺寸」。探针取证：视口 1600×900 时画布内容包围盒只有 `(0,192)-(1276,700)`、非黑占比 **0.128**；把画布 CSS 盒钉成与位图 1:1 后连拍，`resize(800,600)` 得到的桌面是**按原生像素重新排版**的（任务栏只占 800 px、开始按钮/时钟字号与原生一致、窗口溢出右边缘而不是被缩小）⇒ `Session.resize()` 确实经 MS-RDPEDISP 改了远端分辨率，**不是客户端缩放**，「远端不跟」的判断是错的。修法：不再手工改画布缓冲（交给 wasm）+ 画布 CSS 盒按位图宽高比等比 contain + 工具条分辨率改读画布缓冲（500 ms 轮询）+ 连接首帧那次被丢弃的 `resize` 在 1.2 秒后补发、最多 3 次。真机复验：视口 1440×900 / 1600×950 / 1000×700 / 1312×764 四次，非黑占比 `0.9951~0.9972`、工具条分别显示 `1280×720` / `1600×910` / `1000×632` / `1312×724`（后三次与舞台一致 ⇒ 远端真的换了分辨率），黑带消失。
- **「会话记录」页点「中断」对 RDP 会话毫无作用（用户实测反馈，真缺陷）**：`POST /api/sessions/<id>/terminate` 原本靠 `session_registry.get(sid)` 判断「这条会话还在线吗」——只有网页终端/SSH 与文件管理器会登记，**`app/rdp/` 从来不登记**，于是 RDP 永远走「不在线」那条分支：只把库里的行改成 `terminated`，浏览器里那条 WebSocket 隧道照旧活着，几秒/几十秒后隧道自己断开又把行覆盖成 `closed`（旧库里留有铁证：audit 305 `terminate_session` 在 04:25:11，会话 40 却到 04:26:46 才 `closed`/`客户端已断开`）。修法：① `session_registry.close()` 除 `bridge` 外再认一个通用的 `stop` 可调用对象；② 网关侧新增 `_StopRequest`，`handle_connection()` 用 `open_session()` 交回的 `sid` 把隧道登记进在线表（`kind="rdp"`），中继期间按字节活动喂 `touch`（否则正在用的会话会被空闲清理误杀）；③ `relay()` 接受外部 `stop` 事件，被中断时把原因改成「管理员强制中断」并标记 `forced`，`_close_session()` 据此把行收口成 `terminated`。真机复验：点「中断」后 41~118 毫秒内成对出现 `terminate_session` → `rdp_session_close`，行变「已被中断」。顺带把机器味报错翻成人话（`humanize_socket_error()` 认 `WinError 10054/10061/10060`；控制台在已经连上后掉线时不再显示「连接失败｜重试」，而是走会话结束弹窗）。
- **录像会「整段消失」：窗口被强行关掉时浏览器侧还没来得及上传（用户实测报出，真缺陷）**：用户那次真实会话 65 秒、服务端回传 5 987 080 字节，但审计里**没有任何** `rdp_recording_saved`，`rdp_recordings` 最新一条还停在 5 小时前 —— 根因是整包方案：`MediaRecorder` 的 webm 只在**走页面内结束流程**（会话结束弹窗 / 倒计时关闭）时才 `POST /api/rdp/recordings`，用户直接关窗口时那个请求被浏览器取消，视频整段丢失。修法：改成**边录边传** —— 前端每 5 秒把 `MediaRecorder` 的分片按 seq 串行 `POST /api/rdp/recordings/chunk`（`ChunkQueue` 单通道：同一时刻只有一片在飞，乱序拼起来就是坏文件；失败重试两次，重试用尽只记失败、不整段放弃），服务端按 `uploadId` 追加进 `instance/rdp_recordings/staging/<uploadId>.part`，正常结束时 `finalize` 原子改名转正；窗口被强行关掉时没人调 finalize，于是**在「有人看录像列表 / 有人开新远程桌面」这两个时刻**把静默超过 45 秒的任务自动收口成 `recovered=true` 的录像（界面标橙色「未正常结束」，不再冒充完整录像）。真机实证：分片 `chunks` 1→2→3 长大到 203 121 B → 关掉标签页 → 52 秒后一次列表请求就把它收口成 `#16`（顺带把上一次探针丢下的 `#15` 也收了），回放实测 `currentTime` 0→1.77→3.75→6.22→13.45、画面非黑占比 ≈1、单帧颜色数 67 781 —— 自动收口的录像**真的能播**。
- **自动收口「静默失效」：`_session_id_for_upload()` 少了 import（真机探针揪出，真缺陷）**：真机第一次跑边录边传时，分片明明已经进了暂存文件，但窗口关掉 52 秒后列表请求没有收口 —— 后端日志里只有一行 `NameError: name 'SessionRecord' is not defined`。`app/api/rdp.py` 的模块级 import 漏了 `SessionRecord`（别的函数用的是函数内 import，所以那两处没事），而 `sweep_stale_uploads()` 对单条任务的异常是 `log.exception` + `rollback` ⇒ 行被留下、接口照常 200，现象就是「收口静默失效」。修掉 import 后补了回归：把 import 去掉跑 `-k sweep` → `test_sweep_finalizes_a_stale_upload_as_recovered` 与 `test_finalize_is_idempotent_after_the_sweep_already_collected_it` 双双 FAILED。**教训**：动过 import 块之后必须先重跑受影响的测试文件再做真机验证。
- **RDP 连 Windows Server 2003 只报「客户端内部错误（WebSocket connection failed）」（用户实测报出：真缺陷 + 能力边界）**：老目标机只支持 TLS 1.0，Python 3.13 的默认上下文（最低 TLS 1.2）在握手中被对端 `ConnectionResetError 10054` 重置；更糟的是 `ConnectionResetError` 属于 `OSError` 而不在 `perform_handshake()` 的 `except ssl.SSLError` 之内，异常一路冒到 WSGI 中间件兜底 `_safe_close(ws, 1011, "Internal error")` —— 浏览器只看到「客户端内部错误」，审计里连 `rdp_failed` 都没有。修法三件：① 握手改**两段式**（现代 TLS 失败 → 换连接按 `TLSv1…TLSv1_2 + ALL:@SECLEVEL=0` 兼容模式再握，实测能握上 `192.168.0.105`）；② `handle_connection()` 外层补 `except Exception`，任何意外都落 `rdp_failed` 并把原因写进 1011 的 close reason；③ 报错全部人话化（后端 `humanize_socket_error()` / `_with_security_hint()`，前端 `describeRdpError()` 认 `/decode error/` 与 `/websocket connection failed/`）并**明确写出出路**。另加主机级「RDP 安全层」（`auto` / `ssl`）：`ssl` 时改写 X.224 请求只报 `PROTOCOL_SSL`、绕开 NLA，把会话从「NLA 阶段就被掐断」推进到「真的建立起会话」。回归：`tests/test_rdp_gateway.py` 新增 3 例（兼容模式重试、两次都失败的可读文案、协商阶段被重置的人话化）+ 8 例（安全层贯通与 API 校验）+ 2 例（NLA 阶段 0 字节时结束原因带「强制 SSL」提示、对端有回包时不加提示），前端 `rdp/types.test.ts` 7 例。**能力边界**：Server 2003 这类 RDP 5.x 即使建起会话，`ironrdp-wasm` 仍 `decode error` 收场 —— 已如实告知用户「这类老系统不支持 WebRDP，请用 Windows 自带的远程桌面连接」，并给出资产级的「RDP 安全层」开关；**用户侧反证**：用系统自带 `mstsc` 的同一账号可连上这台机器，目标机、账号、网络都是好的。

---

## 六、安全须知

1. **立即修改默认管理员口令**（`admin/admin123`）。注意：**首次用管理员登录 SSH 网关会被强制改密**（产品行为），改完之后 `admin123` 即失效 —— 此时跑联调脚本用 `python tools/live_e2e_check.py --admin-password <当前口令>`，或执行 `python run.py --reset-admin` 把口令恢复为配置里的默认值。
2. `bastion-backend/instance/` 含数据库与三把密钥：`secret.key`（JWT）、`fernet.key`（资产口令加解密）、`gateway_host_rsa.key`（网关主机私钥）—— 请设置严格文件权限并定期备份，**丢失后无法恢复**。
3. 生产环境必须用 HTTPS/WSS 终结浏览器流量，并把后端 `CORS_ORIGINS` 收窄到实际域名（默认 `*` 仅便于部署调试）。
4. 网关主机指纹请在首次 `ssh` 连接时核对（设置页可见），避免中间人。
5. 审计数据默认长期保留；**清除审计是管理员专属操作**（会话 / 命令 / **文件** / 操作日志四个页面：勾选删除、或按筛选条件清除）。清除会**级联删除**命令日志与录像文件、跳过进行中的会话，并且**每次清除本身都会写一条审计留痕**（谁、何时、按什么条件、删了多少条），所以请把这条管理动作纳入合规审查范围。文件记录的清除在权限模型里等价于「命令记录清除」（需要 `command:view_all`）。
