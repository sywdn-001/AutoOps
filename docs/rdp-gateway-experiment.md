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
