# RDP 网关（mstsc → 堡垒机 4489）实验记录

> 分支：`experiment/rdp-gateway`（本地实验分支，**未推送**）。
> 结论先写在前面：**核心那块（RDP 服务端）在当前环境做不了**，所以按你的要求「不行就丢掉」，
> 分支只留作技术验证与存档；其中**选单模型**是能落地的真代码，已带 7 条用例。

## 一、需求与验收

| # | 你的要求 | 验收方式 |
| --- | --- | --- |
| 1 | Windows `mstsc` 连堡垒机 **4489**，登录后看到「自己有权连的主机」选单（鼠标点击 / ↑↓+Enter 都要），选中后把目标机 RDP 画面转发进这条会话 | mstsc 能连上、能选、能出画面 |
| 2 | 用户名密码就是**堡垒机账号**，全程审计，并要求**录像** | 会话表 / 审计里能看到记录，录像能回放 |

## 二、为什么不能照搬 SSH 网关

SSH 网关（`app/gateway/server.py`）能直接「打印菜单」是因为 **SSH 是文本协议**：服务端
`write()` 一个字符串就完事。RDP 完全不是一个量级：

```
mstsc ──X.224 连接请求──▶ X.224 Confirm
      ──MCS 连接/频道加入──▶
      ──安全协商（RDP/NLA/CredSSP）──▶
      ──许可、能力交换、DVC/SVC──▶
      ──图形通道（位图 / RemoteFX / EGFX）──▶ 一帧一帧的像素
      ◀── 鼠标键盘（输入 PDU，含相对/绝对坐标、扫描码）──
```

也就是说：**「菜单」必须由服务端自己画成位图下发**，鼠标点击要按 RDP 输入 PDU 解出来再
做命中测试。这要求一个完整的 **RDP 服务端**（不是客户端、不是代理）。而 `mstsc` 只认 RDP，
没有任何"先给我一个选择框"的扩展点。

## 三、三条技术路线的可行性

| 路线 | 能否满足需求 | 依赖 | 判断 |
| --- | --- | --- | --- |
| **A. IronRDP `ironrdp-server` + `ironrdp-client`**（Rust） | ✅ 能：服务端接受 mstsc、自绘菜单、选中后用 client 连目标机并转发（`ironrdp-svc` 可做剪贴板/输入；`ironrdp-capture-replay` 可以直接录） | Rust 工具链 + MSVC 链接器；需自写 Rust 服务端进程并与 Python 后端通过 HTTP/IPC 交互 | **推荐**，但本机 **没有 rustc/cargo**（实测 `rustc` 未安装），下载安装 + 编译 + 联调是数天量级 |
| **B. FreeRDP `freerdp-shadow-cli`**（现成二进制） | ⚠️ 部分：它是"影子服务器"，把**既有会话语**转发给别人看，不给出选单，也不能按堡垒机授权挑机器 | Linux 下的 X11/Wayland 会话；Windows 没有可用发行 | 不符合需求 |
| **C. 从零实现 RDP 服务端**（Python / Node） | ❌ 不可行：X.224/MCS/许可/图形编解码/输入 PDU 是一整套协议栈，Python 与 Node 生态**没有**可用的 RDP server 库（`pyrdgw` 只是 MS-TSGU 网关，`node-rdpjs` 只有客户端） | — | 放弃 |

结论：只有路线 A 能真正做出来，而它被环境（无 Rust 工具链）卡住。

## 四、本分支已经落地的部分

`bastion-backend/app/gateway/rdp_menu.py` —— **选单模型**（与渲染/传输解耦，将来 A 路线
的 Rust 服务端只要把它产出的行画成位图即可）：

- `Menu.Item`：一台主机 + 一个堡垒机账号（`host_id/address/port/account_id/os_type`）；
- `RdpMenu`：`move()`（↑↓ 跨页环绕）、`page_move()`（PgUp/PgDn）、`home()/end()`、
  `hit_test(row)`（鼠标点第几行 → 选中，空白行忽略）、`render(width)`（给服务端画图用的纯文本，含页码 / 总数 / 操作提示）；
- `build_menu_items(user)`：复用 `app.access.accessible_targets(protocols=("rdp",))` + `serialize_target()`，按「主机名 + 账号」排序；**授权查询异常时返回空列表**，绝不让网关 500（空态的提示中英各一行）。

用例 `tests/test_rdp_menu.py`（7 条，全绿）：空态提示、↑↓ 环绕与翻页、PgUp/PgDn、鼠标命中与空白行、
`row_of` 的跨页语义、渲染里的光标标记与页码、授权查询失败时的兜底。

## 五、接通还差什么（下一步清单）

1. **RDP 服务端进程**（Rust）：监听 4489；收到连接先走 RDP 安全协商，用**堡垒机用户名口令**
   校验（调用现有 `POST /api/auth/login` 或直接查库 + scrypt），失败三次锁定（沿用
   `login_max_failures`）；成功后渲染选单位图，接输入 PDU 做 ↑↓/Enter/鼠标命中；
2. **转发**：选中后用 `ironrdp-client` 连目标机 `3389`，把目标画面的帧改写进服务端会话
   （或直接把 `ironrdp-client` 的输出接到 `ironrdp-server` 的绘制回调）；
3. **审计 + 录像**：复用现有 `audit.new_session/log_event` 记 `rdp_gateway_login` / `rdp_session_open`；
   录像走 `ironrdp-capture-replay` 落盘，再用现有 `POST /api/rdp/recordings/*` 登记（现在网页
   WebRDP 的录像在浏览器侧录，网关这条路可以改成**服务端录**，反而更可靠）；
4. **配置**：`RDP_GATEWAY_ENABLED` / `RDP_GATEWAY_PORT=4489`（在 `app/config.py` 里预留），
   开机时与 SSH 网关一起由 `run.py` 拉起；
5. **验收脚本**：`tools/rdp_gateway_check.py` —— 用 mstsc 或 FreeRDP 客户端脚本化连 4489 跑一遍
   登录 → 选单 → 连目标 → 收一帧画面 → 断开，断言审计与录像都有记录。

## 六、判定

- 现在的分支状态：**选单逻辑可用且有用例**，但**没有 RDP 服务端 → mstsc 连不上 4489**，
  因此按你的要求「不行就丢掉」，本分支不合并；将来装好 Rust 工具链可以直接从路线 A 接着做，
  上面的 `rdp_menu.py` 与用例是可复用的那一半。
- 如果你希望**现在就能用 mstsc 连到目标机**，还有一条"半网关"折中：给目标机做一个
  **只允许堡垒机来源的端口映射**（`netsh interface portproxy`）+ 在堡垒机上限制来源 IP，
  审计靠目标机的 4624/4649 日志与堡垒机侧的连接日志，**没有选单、没有录像**——和你的
  需求 1、2 都不完全对齐，但能马上用。要不要走这条，你定。

## 七、2026-10-21：Rust 工具链装好了，网关真的连上了（里程碑）

**环境（这台机器上实际做的）**：
- 
ustup-init.exe 从 static.rust-lang.org 只跑到 12MB 就卡（约 20KB/s），换 **清华镜像**（RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup，实测 1.4MB/s）后秒装：**rustc / cargo 1.99.1，stable-x86_64-pc-windows-msvc**，装在 D:\\Rust（RUSTUP_HOME/CARGO_HOME 已写进用户环境变量，PATH 加了 D:\\Rust\\cargo\\bin）；VS 18 Community 自带的 MSVC 直接可用。
- crates 走清华 sparse 镜像（D:\\Rust\\cargo\\config.toml）。

**踩到并绕过的坑**：
1. cargo build（debug）→ libz-sys 的 C 编译报 **D8050**（命令行放不进调试记录）→ 改用 --release（不带 -Z7）就好了；
2. ws-lc-sys 归档时报 **LNK1104: 无法打开 lnk{GUID}.tmp**（CMake 构建也失败）→ 根因是这台机器上 lib.exe/CMake 建大归档的临时文件有问题；**解法**：把 	okio-rustls **vendor 进仓库**（astion-rdp-gateway/vendor/tokio-rustls）并把它的默认加密后端从 ws_lc_rs 改成 **
ing**，再用 [patch.crates-io] 指过去 —— 依赖树里 ws-lc-rs 彻底消失（cargo tree -i aws-lc-rs → nothing），
ing 能正常编；
3. 顺带把 ironrdp-server 的默认特性关掉（去掉 zstd/qoi），只要 helper（TlsIdentityCtx）。

**已经跑通的事**（astion-rdp-gateway，cargo build --release 成功）：
- mstsc /v:127.0.0.1:4489 → 弹出「证书来自不受信任的机构」→ 点「是」→ **看到网关自己画的选单画面**（AutoOps Bastion - RDP Gateway (experimental)，深色底 + 白字），说明 X.224/MCS/TLS/图形通道全通了；
- 首次启动用 
cgen 自签证书，落在 astion-backend/instance/rdp-gateway-{cert,key}.pem；
- 键盘事件实时到达网关日志（keyboard event=Pressed { code: 30 } / Released），输入通道也是通的。

**下一步**（接着做）：把 
dp_menu.py 的授权主机列表接进来画成选单 → 加鼠标命中（现成 hit_test）→ 用 ironrdp-client 连目标机、把画面接到这条会话 → 审计 + ironrdp-capture-replay 录像。

**运行方式**（当前）：
`
cd bastion-rdp-gateway
set RDP_GATEWAY_PORT=4489
set BASTION_INSTANCE_DIR=..\\bastion-backend\\instance
cargo run --release          # 或 target\\release\\bastion-rdp-gateway.exe
`

## 八、2026-10-21 续：登录页 + 选单 + 会话登记（后端已通，画面刷新待修）

**后端（Python，已测）**：新增 `app/api/rdp_gateway.py`：

- `GET /api/rdp-gateway/hosts` —— 按 `accessible_targets(protocols=("rdp",))` 出「主机 + 可登录账号」，
  与网页 WebRDP 完全同源的权限判定；
- `POST /api/rdp-gateway/sessions` —— 选中机器后写 `sessions` 表（`source=gateway, protocol=rdp`），
  并写一条 `rdp_gateway_open` 审计；
- `tests/test_rdp_gateway_api.py` **4 条用例全绿**（只列 RDP 主机、开记录 + 审计、缺参 400、未登录 401）；
- 真机验证：建了测试账号 `rdpgw-test`（ops 角色）+ win-75 的授权，`GET /hosts` 返回 1 台
  （`win-75 192.168.0.75:3389 账号 Administrator`）。

**Rust 网关 v2**：登录页（用户名 / 口令，Tab 切换字段、Enter 登录，调 `/api/auth/login` 换 JWT）
→ 选单（↑↓ / Enter / 鼠标点击，数据来自 `/api/rdp-gateway/hosts`）→ 选中后调 `/sessions`
登记会话并回显 sid。编译通过，mstsc 连上后**登录页正常显示**。

**两个现场发现**：

1. mstsc 发来的按键是 **`UnicodePressed/UnicodeReleased`**（不是扫描码 PDU），直接按扫描码处理会
   完全没反应 —— 已加 unicode 分支（回车 13 / Tab 9 / 退格 8 / 可打印字符）；
2. **已知问题**：首帧之后客户端不再重绘（输入事件确实收到了 42 条，但画面停在登录页）。
   下一步：查 display updates 的后续帧 —— 大概率是没显式 `with_bitmap_codecs`，
   或服务端没有再 poll `next_update`；加一行「帧已发送」的日志即可定位。

## 九、2026-10-21 晚：修掉「上一个用户的登录态残留」+ 网页端加 RDP 网关设置

### 1. 安全修复：换人连进来必须重新登录

**问题**：`ironrdp-server` 的 display / input handler 只接受**一份**实例（builder 里 move 进去），
所以我把登录态（username / JWT / hosts）放在 `Shared` 里就等于**全进程共享**：
上一轮用户断开后状态还在内存里，下一个人连进来直接看到他的选单，还能点进他有权限的机器 ——
**无授权越权**。

**修法**：实现 `ConnectionHandler`（`on_accept` + `on_disconnected`），两个时机都把
`Shared` 重置成 `State::default()`（回到未登录的登录页）并重绘：

```rust
fn on_accept(&mut self, peer: SocketAddr) -> bool {
    self.shared.reset_for_new_connection();   // 新连接：清空上一轮的 token / hosts
    true
}
fn on_disconnected(&mut self, ...) -> PostConnectionAction {
    self.shared.reset_for_new_connection();
    PostConnectionAction::Continue
}
```

**现场验证**（网关日志）：

```
INFO bastion_rdp_gateway: RDP 客户端断开 peer=127.0.0.1:1042 elapsed=49.3s error=None
INFO bastion_rdp_gateway: 新连接：会话状态已清空（未登录）
INFO bastion_rdp_gateway: RDP 客户端接入 peer=127.0.0.1:...
INFO bastion_rdp_gateway: 新连接：会话状态已清空（未登录）
```

再连进来看到的是**登录页**，不是上一个人的选单。

> 已知边界：目前整个进程只服务一个客户端（多客户端同时连会共享同一份状态）。
> 生产化时要按连接分配状态（`RdpServer` 每次连接新建 display/input 实例，或用 connection id 分包）。

### 2. 网页端（Flask + 前端）加了 RDP 网关设置

**后端**：

- `app/settings_store.py` 默认值 + `app/api/settings.py` 目录项：`rdp_gateway_enabled`（bool）、
  `rdp_gateway_port`（int，1–65535），「系统设置 → 参数设置」里自动出现；
- `GET /api/rdp-gateway/status`（`rdp:use`）：端口是否在听、pid、二进制路径与是否存在、日志路径；
- `POST /api/rdp-gateway/start`（管理员）：按设置端口拉起 `bastion-rdp-gateway` 进程，写 pid 文件，
  写 `rdp_gateway_start` 审计；已在跑则幂等返回；
- `POST /api/rdp-gateway/stop`（管理员）：按 pid 结束进程，写 `rdp_gateway_stop` 审计；
- `tests/test_rdp_gateway_api.py` 扩到 **8 条全绿**（新增 status / start 幂等 / 二进制缺失 / stop）。

**前端**：新增页面「系统设置 → RDP 网关」（`src/pages/system/rdp-gateway/index.tsx`）：

- 运行状态卡（运行中 / 未运行、监听端口、`mstsc /v:<堡垒机>:<端口>` 命令、二进制与日志路径）；
- 「启动 / 停止 / 刷新」按钮（`setting:manage` 才显示）；
- 配置提示：开关与端口在「参数设置」里改，保存后在这里停止→启动生效；
- 中英双语（词典 +18 条），`tsc --noEmit` 无错、`npm run build` 通过。

**真机验证**：`POST /start` → `started=True pid=15036`，`GET /status` → `listening=True`；
`GET /api/settings` 里两项都在（`启用 RDP 网关` / `RDP 网关端口`）。

### 3. 还没做的（下一步）

**「选中机器之后把目标机的画面接进来」仍然没有** —— 这一步需要一个 RDP **客户端**：

- crates.io 上的 `ironrdp-client` 只有 `0.1.0` 占位版本，`cargo add` 之后拉进来的 `ironrdp-tls`
  直接编不过（`a TLS backend must be selected...`），已移除；
- 正路是用已发布的 `ironrdp-connector`（连接序列 + CredSSP/NLA）+ `ironrdp-session` 0.11
  （画面解码）+ `ironrdp-tokio`（传输）自己拼一个最小客户端：连目标 3389 → 把解码出来的
  帧转成网关这侧的 `BitmapUpdate` → 把网关收到的键盘/鼠标按 `ironrdp-ainput` 编码回目标机。
- 目标机账号口令由 `POST /api/rdp-gateway/sessions` 下发（与网页 WebRDP 同一套「口令集中托管 +
  审计留痕」的口径：新增 `rdp_credential_reveal` 类审计）。

## 十、2026-10-21 深夜：通信加固 / 日志折叠 / 目标机画面桥

### 1. 网关 ↔ Flask 的通信（原来写死 127.0.0.1:5000，现在可配 + 可诊断）

- 地址：`BASTION_API_BASE=http://host:port` 优先，其次 `BASTION_API_PORT`，默认 `127.0.0.1:5000`
  （`api_addr()` 解析，允许不带 `http://`）；
- 每次调用都记一条 `debug` 日志：**方法 / 路径 / HTTP 状态码 / 耗时 ms / 响应字节数**；
- 响应先按 `\r\n\r\n` 拆头，取状态码，**非 200 直接把后端返回的正文前 200 字带进错误信息**
  （之前只报「响应不是 JSON」，看不出是 401 还是 404）；
- `frame` 走二进制通道（`http_bytes`），其它接口走 JSON（`http_json`）。

### 2. 日志策略：输入折叠，其余详细

| 日志 | 级别 | 说明 |
| --- | --- | --- |
| 单个键盘 / 鼠标事件 | `trace` | 默认看不到；排查时 `RUST_LOG=...=trace` |
| 键盘 / 鼠标累计 | `info`，每 30 条一行 | 「键盘事件累计（每 30 条汇总一次）」 |
| 连接接入 / 断开（含对端地址、时长、错误） | `info` | `RDP 客户端接入 peer=...` |
| 会话状态清空 | `info` | 每连接一次，确认不会串号 |
| 登录成功 / 失败、选主机、开会话、连目标机 | `info` / `warn` | 带目标名、sid、错误原因 |
| 后端每次调用 | `debug` | 方法/路径/状态码/耗时/字节数 |
| 画面泵统计 | `info`，每 30 秒一行 | 键盘数、鼠标数、累计帧数 |

### 3. 目标机画面桥（过渡实现，先把功能跑通）

`app/rdp/window_bridge.py` + `/api/rdp-gateway/{connect,frame,input,disconnect}`：

1. `connect`：按会话登记的主机 + 账号口令（`rdp_credential_reveal` 审计）用 `cmdkey` 存凭据、
   拉起 `mstsc`（1280×800），**自动关掉「安全警告 / 证书」弹窗**，等真正的 `TscShellContainerClass` 窗口；
2. `frame`：每 64×64 为一块比对前后帧，**只回变化的块**（`[u32 n][n × (u16 x,y,w,h + RGBA)]`），
   没变化回 204；`?full=1` 强制回全量（网关重绘用）；
3. `input`：把网关收到的键盘（VK）/鼠标事件用 `keybd_event` / `mouse_event` 转发给那个 mstsc 窗口；
4. `disconnect`：关窗口 + 删凭据（`cmdkey /delete`）+ 写 `rdp_gateway_close` 审计。

网关侧（Rust）：`start_target()` 调 `/connect` → 起一个 60ms 的泵：把 `pending_input` POST 到 `/input`、
GET `/frame` 把脏块贴进 1280×800 帧缓冲 → 推给 mstsc 客户端；`Esc` 回选单并 `/disconnect`。

**已实测（API 层）**：`connect` 6 秒返回、`target=192.168.0.75:3389`；`/frame` 首帧 260 块 ≈ 4.1MB、
之后每轮只有少量脏块（0.2–2.3MB）；把脏块拼成 PNG 能看到画面内容。

**当前限制（必须说清楚）**：

- 抓帧是「屏幕 BitBlt 该窗口客户区」—— mstsc 用 DirectX 混合渲染，`PrintWindow` 抓出来基本全黑，
  所以**窗口必须在前台可见**（桥每 1 秒会把 mstsc 顶到前台），这意味着流式期间这台堡垒机上
  **桌面被占用**，不能当正式实现；
- 目标机如果只允许一个会话，而我们连上去会把它现有的会话挤掉（实测报「你已断开连接，因为与远程
  计算机建立了另一个连接」）—— 这不是桥的 bug，是目标机会话策略；
- 因此「mstsc 里看到目标机桌面」这条完整链路的**现场演示**还没做到，需要一台允许新会话的目标机，
  或者把桥换成真正的 RDP 客户端（`ironrdp-connector` + `ironrdp-session` + `ironrdp-tokio`）。

## 十一、2026-10-21 深夜：换成真正的 RDP 客户端，mstsc 里能看到目标机桌面了

### 1. 为什么放弃「mstsc 窗口桥」

桥能连上目标机，但抓帧只能靠屏幕 BitBlt（mstsc 是 DirectX 混合渲染，PrintWindow 出全黑），
而 Windows 不允许后台进程把窗口顶到前台 —— 用户正在看网关窗口时，抓到的就是网关自己。
换句话说「在网关会话里看目标机画面」和「截屏」天然冲突，这条路走不通。

### 2. 现在的实现（Rust 侧自己当 RDP 客户端）

- 依赖 `ironrdp-client 0.1`（IronRDP 官方的客户端引擎）+ `ironrdp-tls`（`rustls-ring` 后端，
  避免 aws-lc 在这台机器上编不过）；
- 上游 client 的 RDCleanPath(WebSocket) 传输需要 `tokio-tungstenite` 的 rustls 特性（会拉回 aws-lc），
  所以把 crate **vendor 进仓库**（`bastion-rdp-gateway/vendor/ironrdp-client`），只用「直连 TCP+TLS」；
- `RdpClient::run()` 的 future 不是 `Send`，不能 `tokio::spawn` 到多线程运行时 ——
  给它单独开一个**单线程运行时**的线程，画面泵再开一个，两者用 mpsc 通道通信；
- 目标机账号口令由 `POST /api/rdp-gateway/sessions` 下发（写 `rdp_credential_reveal` 审计）；
- 画面：`RdpOutputEvent::Image` 拿到整屏 `Vec<u32>` → 贴进网关自己的 1280×800 帧缓冲（分辨率不同则最近邻缩放）；
- 输入：网关收到的扫描码/Unicode/鼠标 → `FastPathInputEvent` → `input_sender()` 发给目标机；
- `Esc` 回选单并关闭目标机会话。

### 3. 端到端验收（真的跑通了）

1. `mstsc /v:127.0.0.1:4489` → 接受自签证书 → **登录页**；
2. 输 `rdpgw-test` / `RdpGw2026` → **选单**：`> 1. win-75 (192.168.0.75:3389) account Administrator`；
3. 回车 → `Streaming win-75 — press Esc to go back` → 几秒后**目标机的 Windows Server 桌面
   （服务器管理器、Hyper-V、任务栏时间）完整显示在 mstsc 窗口里**；
4. 点目标机任务栏的「开始」→ 目标机出现「开始」提示，**鼠标输入通了**。

### 4. 顺手修的两个坑

- `open_session` 里有一段**重复的 `Ok(body)` 分支**（旧代码在上面），导致新的 `start_target`
  永远不执行、屏幕上一直显示旧的 “Session recorded …” —— 这就是之前「选中主机没反应」的根因；
- `ConfigBuilder::build()` 要求客户端 build/目录/平台/键盘等字段，缺一个就报
  「构造 RDP 客户端配置失败」（错误被 `anyhow` 吃掉了）—— 已补齐并让错误带上下文。
