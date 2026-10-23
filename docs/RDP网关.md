# RDP 网关（mstsc 直连堡垒机）

> 用 Windows 自带的「远程桌面连接」(`mstsc`) 连堡垒机的 **4489** 端口，用**堡垒机账号**登录，
> 从「你有权访问的主机」里挑一台，目标机的桌面直接显示在这个窗口里 —— 不用装任何客户端，
> 也不用把目标机口令交给操作员。
>
> Point Windows' built-in Remote Desktop client at the bastion's **4489** port, sign in with your
> **bastion account**, pick a host you may reach, and that machine's desktop is streamed into the
> same window — no extra client to install, no target credential in the operator's hands.

英文版 / English: [i18n/rdp-gateway.en.md](i18n/rdp-gateway.en.md)

> **模块化说明**：RDP 网关已从宿主解耦为官方扩展模块 `rdp-gateway`（`.mod.ops_ext`），
> 宿主不再内置 `/api/rdp-gateway/*` 蓝图与「系统设置 → RDP 网关」页面。网关进程由模块托管，
> 模块 API 前缀是 `/api/modules/rdp-gateway/api`，模块页面由宿主在「扩展模块」菜单中加载。

---

## 一、它解决什么问题

网页 RDP（WebRDP）要在浏览器里跑一个 wasm 客户端；很多内网场景里，运维手上只有 Windows 自带的
`mstsc`，或者策略不允许装浏览器插件。RDP 网关就是给这种场景用的：

| 场景 | WebRDP | RDP 网关（本文） |
| --- | --- | --- |
| 客户端 | 浏览器（wasm 解码） | Windows 自带 `mstsc` |
| 目标机口令 | 网关解密后交给浏览器（有专门审计） | **只留在堡垒机进程里**，客户端拿不到 |
| 选机器 | 网页列表 | 网关在会话里**自己画**的选单（↑↓ / Enter / 鼠标点击） |
| 审计 | 会话 + 口令下发 + 录像 | 会话 + 口令取用 + 登录/选机器全进审计（录像见「已知限制」） |

## 二、架构

```
mstsc ──TLS(RDP)──► bastion-rdp-gateway:4489 ──RDP(TLS+NLA)──► 目标机 3389
                          │
                          ├─ 登录页：用堡垒机账号口令换 JWT（POST /api/auth/login）
                          ├─ 选单：GET  /api/modules/rdp-gateway/api/hosts（与网页端同一套授权判定）
                          └─ 选中：POST /api/modules/rdp-gateway/api/sessions → 拿目标机地址/端口/账号/口令（写审计）
```

- **服务端**：`bastion-rdp-gateway/`（Rust，`ironrdp-server` 0.13）—— 对 `mstsc` 来说它就是一台
  RDP 服务器：X.224 → MCS → TLS → 授权 → 图形通道，画面由它**自己画**（`font8x8` 画 ASCII，再转位图）。
- **客户端侧**：同一个进程里用 `ironrdp-client`（vendored）连目标机 3389，
  目标机画面进来后直接贴到 1280×800 帧缓冲，键盘/鼠标按 FastPath 事件回送目标机。
- **和网页端的权限同源**：选单数据来自 `accessible_targets(protocols=("rdp",))`，
  和网页 WebRDP、SSH 网关用的是同一套「授权 + 时段 + 配额 + 允许交互式登录」判定。

## 三、用起来（三步）

1. **安装并启动模块**（任选一种）

   ```powershell
   # ① 在「系统设置 → 扩展模块」上传 modules-dist\rdp-gateway-<版本>.mod.ops_ext，
   #    勾选实验性确认并启用；打开「扩展模块 → RDP 网关」页面点「启动」（推荐，能看状态/日志）
   # ② 命令行：
   cd bastion-rdp-gateway
   $env:RDP_GATEWAY_PORT = "4489"
   $env:BASTION_INSTANCE_DIR = "..\bastion-backend\instance"
   $env:BASTION_API_BASE = "http://127.0.0.1:5000"   # 后端不在本机/不是 5000 时改这里
   .\target\release\bastion-rdp-gateway.exe
   ```

2. **连接**

   ```
   mstsc /v:<堡垒机地址>:4489
   ```

   首次会提示「证书来自不受信任的机构」（用的是网关自签证书，落在
   `bastion-backend/instance/rdp-gateway-cert.pem`），点「是」继续。

3. **登录 → 选机器**

   ```
   AutoOps Bastion - RDP Gateway
   Username: <堡垒机账号>     （Tab 切换字段，Enter 登录）
   Password: ********
   ```
   登录成功后是选单：`↑↓` 选择、`Enter` 连接、鼠标点哪行选哪行；
   `Esc` 从目标机画面回到选单。

## 四、需要的权限与账号

- 账号要有 `rdp:use` 权限（内置角色「运维工程师」及以上默认有），否则登录后选单是空的；
- 目标机要在「资产」里把协议配成 **rdp**（端点端口默认 3389，可以在主机端点里改），
  并在「访问授权」里给这个账号授权、勾上「允许交互式登录」；
- 目标机账号口令存在「主机账号」里，由堡垒机集中托管；网关取用时会写
  `rdp_credential_reveal` 审计。

## 五、配置项

| 位置 | 键 / 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| 模块页面 → 模块设置 | `mod.rdp-gateway.enabled` | 关 | 是否启用 RDP 网关（模块设置） |
| 模块页面 → 模块设置 | `mod.rdp-gateway.port` | 4489 | 网关监听端口 |
| 模块页面 → 模块设置 | `mod.rdp-gateway.autoStart` | 关 | 模块启用时自动拉起网关进程 |
| 网关进程 | `RDP_GATEWAY_PORT` | 4489 | 同上，命令行启动时用 |
| 网关进程 | `BASTION_INSTANCE_DIR` | `../bastion-backend/instance` | 证书/日志所在目录 |
| 网关进程 | `BASTION_API_BASE` | `http://127.0.0.1:5000` | 后端地址（也支持 `BASTION_API_PORT`） |
| 网关进程 | `BASTION_API_PREFIX` | `/api/modules/rdp-gateway/api` | 模块 API 前缀（由模块启动时注入） |
| 网关进程 | `RUST_LOG` | `info` | 日志级别；排查键盘/鼠标细节时用 `trace` |

改完端口记得「停止 → 启动」让网关重新绑定。

## 六、审计

| action | 什么时候写 |
| --- | --- |
| `rdp_gateway_start` / `rdp_gateway_stop` | 管理员在页面上启停网关 |
| `login` | 有人在网关登录页登录（和网页登录同一条通道） |
| `rdp_gateway_open` | 选中主机、开了一条会话记录（`sessions` 表里 `source=gateway, protocol=rdp`） |
| `rdp_credential_reveal` | 网关取用某个目标机账号的口令（谁、何时、哪台机、哪个账号） |
| `rdp_gateway_close` | 从目标机画面返回选单 |

## 七、日志怎么读

- 连接生命周期：`RDP 客户端接入 / 断开`（带对端地址、持续时长、错误原因）；
- 会话隔离：每个连接开始/结束都会打一行「会话状态已清空（未登录）」——**上一个用户的登录态不会留给下一个人**；
- 输入：单条键盘/鼠标事件是 `trace`，`info` 只留每 30 条一行的汇总，避免刷屏；
- 后端调用：`debug` 级别，带方法/路径/HTTP 状态码/耗时/字节数；
- 画面泵：30 秒一行（键盘数、鼠标数、累计帧数）。

日志文件：`bastion-backend/instance/modules/rdp-gateway/data/gateway.log`（由模块页面「启动」托管时写这里；
模块页面的「日志」面板和 `GET /api/modules/rdp-gateway/api/logs` 都能直接读）。

## 八、自己编译

```powershell
cd bastion-rdp-gateway
cargo build --release          # 产物：target\release\bastion-rdp-gateway.exe
```

两个环境注意点（本项目已经踩过）：

1. 这台机器上 `aws-lc-sys` 编不过（`LNK1104`）——仓库把 `tokio-rustls` 与 `ironrdp-client`
   **vendor** 进 `bastion-rdp-gateway/vendor/`，并用 `[patch.crates-io]` 指过去，
   加密后端固定用 `ring`（`ironrdp-tls` 的 `rustls-ring` 特性）。不要随手换成 `rustls` 默认特性；
2. `RdpClient::run()` 的 future 不是 `Send`，所以客户端与画面泵各跑一个**单线程运行时**线程，
   不要挪回 `tokio::spawn`。

## 九、已知限制

- **录像还没接**：目前只记录会话行与审计，目标机画面/操作还没有落成可回放的录像文件；
- **单客户端**：一个网关进程同时只服务一个 `mstsc` 连接（多连接会共享同一份会话状态，正在改）；
- **剪贴板未开**：目标机与本地之间还不能复制粘贴（client 的 `clipboard` 特性没启用）；
- **目标机单会话**：如果目标机只允许一个会话（普通 Windows 客户端），网关连上去会把现有会话挤掉，
  这是目标机策略，不是网关行为；
- `Esc` 被网关占用（回选单），不会转发给目标机。

## 十、验收记录（本机实测）

1. `mstsc /v:127.0.0.1:4489` → 接受自签证书 → 登录页；
2. 堡垒机账号登录 → 选单列出「有权访问」的主机；
3. `Enter` → 目标机（Windows Server）桌面完整显示，颜色正确（蓝壁纸、橙按钮）；
4. `Win+R` 在目标机弹出「运行」窗口 → 输入 `notepad` 字符正确落进目标机焦点窗口；
5. 鼠标点击目标机「运行」窗口的「取消」→ 窗口关闭；双击桌面「回收站」→ 打开；
6. 断开重连 → 回到登录页（不继承上一个用户的登录态）；
7. 审计里能看到 `login` / `rdp_gateway_open` / `rdp_credential_reveal` 三条。

## 十一、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面显示「未运行」，但进程明明在跑 | 点「刷新」；页面右上角的「诊断」卡片会列出后端算出来的路径与端口 |
| 页面提示「读不到网关状态」 | 后端还是改动前的旧进程，重启后端；或账号缺 `rdp:use` |
| 登录页一直提示「登录失败」 | 堡垒机账号口令错；或后端不在 `127.0.0.1:5000`（设 `BASTION_API_BASE`） |
| 选单是空的 | 该账号没有 rdp 协议的授权，或主机没配 rdp 端点 |
| 连目标机失败 | 目标机 3389 不通 / 账号口令错 / 目标机只允许一个会话 |
| 画面颜色发黄、花屏 | 客户端版本太旧，更新到带「R/G/B 通道修正」的构建 |
