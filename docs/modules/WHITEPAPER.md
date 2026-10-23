# AutoOps 扩展模块开发白皮书

> **读者**：想给 AutoOps 堡垒机开发 `.mod.ops_ext` 扩展模块的第三方工程师。<br>
> **前置知识**：Python 3.10+、Flask 基础、JavaScript/ES Module 基础。<br>
> **权威契约**：[`CONTRACT.md`](./CONTRACT.md) v1。本白皮书是契约的开发者向展开；两者冲突时以契约和后端实现为准。<br>
> **配套示例**：[`hello-bastion`](../../modules-src/hello-bastion/)、[`rdp-gateway`](../../modules-src/rdp-gateway/)、[`js-demo`](../../modules-src/js-demo/)（均可直接打包安装；第 12 章以 `hello-bastion` 走查）。<br>
> **适用宿主**：实现了 `app/modules/` 与 `autoops.bastion` 门面的 AutoOps 版本；模块侧通过 `description.json.minBastionVersion` 声明最低版本。

> ⚠️ **安全边界（务必先读）**
>
> Python 扩展以堡垒机进程身份运行 = **完全可信代码，等价于直接改后端**；**没有沙箱隔离**；请只安装**签名可信来源**的模块。
>
> 这不是一句免责声明，而是架构事实：模块可以 `import app`、读数据库、读 `FERNET_KEY`、改全局路由、开线程、访问网络。宿主不会限制这些能力。静态资源（`web/`、`images/`、`README.md`）是**免登录**的，不要在里面放密钥。

---

## 目录

> **怎么用这份目录**：只写页面看第 2、3、6 章；只写后端能力看第 4、5、7 章；要发布看第 9、10 章与附录 B。第一次接触建议按 1 → 2 → 3 → 4 → 6 → 9 走一遍，第 12 章是一份可以照着对照执行的完整记录。

**第一部分 · 认识与边界**

- [1. 能做什么 / 不能做什么](#sec-1) — 先划边界：模块能挂路由、加设置、开页面、发事件；不能改前端路由，不能把密钥放进免登录的静态资源。
  - [1.1 能做](#sec-1-1) — 模块能触及的宿主能力清单
  - [1.2 不能做 / 不要做](#sec-1-2) — 明确禁区，以及为什么现在做不到
  - [1.3 安全模型与责任边界](#sec-1-3) — 为什么说「模块代码完全可信」

**第二部分 · 写代码**

- [2. 包结构：`.mod.ops_ext` 里有什么](#sec-2) — 一个 ZIP 的顶层布局、安装后的磁盘布局、安装期硬校验（防 Zip Slip、体积与条目上限），以及最小可用骨架。
  - [2.1 ZIP 顶层布局](#sec-2-1) — 必需的 `description.json` 与 `modules/`，可选的 `web/`、`images/`、`data/`、`bin/`
  - [2.2 安装后的磁盘布局](#sec-2-2) — `instance/modules/<id>/{package,data,state.json}`
  - [2.3 安装期硬校验与体积上限](#sec-2-3) — 路径穿越、符号链接、总大小 256MB、条目 20000
  - [2.4 每个目录的用途与禁区](#sec-2-4) — 哪些目录会被宿主当静态资源公开
  - [2.5 最小骨架](#sec-2-5) — 只有页面、没有 Python 的模块长什么样
- [3. `description.json` 全字段参考](#sec-3) — 清单文件的每一个键：必填、选填、默认值，以及宿主在安装与展示时怎么用它。
  - [3.1 完整示例](#sec-3-1) — 一份把可选字段全部写满的清单
  - [3.2 字段表](#sec-3-2) — 类型、是否必填、默认值与说明
  - [3.3 id 规则与保留 id](#sec-3-3) — 正则、长度，以及不能用的 id
  - [3.4 官方发行与 `author`](#sec-3-4) — `author: "AutoOps"` 如何被判成 official
  - [3.5 标签、实验性与烧杯确认](#sec-3-5) — 标签配色、实验性标记与逐字确认语
  - [3.6 归一化模块对象与 `/api/modules`](#sec-3-6) — 宿主回给前端的形状，和清单字段的对应关系
  - [3.7 多语言与 JSON 书写约定](#sec-3-7) — 双语写法、UTF-8 与 BOM 的坑
- [4. Python 扩展：生命周期与 `ctx`](#sec-4) — 模块后端代码的入口、钩子、`ctx` 的每个成员，以及它到底能在 Flask 应用里做哪些事。
  - [4.1 入口约定](#sec-4-1) — `modules/__init__.py` 的 `setup(ctx)`
  - [4.2 生命周期钩子](#sec-4-2) — install / uninstall / enable / disable
  - [4.3 `ctx` 成员全表](#sec-4-3) — 属性、方法与返回类型
  - [4.4 `ctx.route`：注册模块 API](#sec-4-4) — 路由前缀、权限码、统一信封
  - [4.5 `ctx.blueprint`：需要 Flask 原生能力时](#sec-4-5) — 直接拿 Blueprint 的代价
  - [4.6 `ctx.on` / `ctx.emit`：事件](#sec-4-6) — 订阅宿主事件、广播自己的事件
  - [4.7 设置与私有数据目录](#sec-4-7) — `ctx.get_setting`、`ctx.data_dir`
  - [4.8 后台线程与资源释放](#sec-4-8) — 别泄漏线程与文件句柄
- [5. `autoops.bastion` SDK](#sec-5) — 宿主能力的正式门面：资产、账号、凭证、会话、审计、策略、SSH，以及它们为什么全是 `async`。
  - [5.1 第一原则：碰库/触网全是 `async def`](#sec-5-1) — 设计动机与后果
  - [5.2 API 全表](#sec-5-2) — 按命名空间列出的每个方法
  - [5.3 返回值与数据结构](#sec-5-3) — dataclass 与 dict 的字段
  - [5.4 在 Flask 同步视图里正确调用 async](#sec-5-4) — 三种桥接方式与选型
  - [5.5 凭证：完整示例与安全提醒](#sec-5-5) — `credentials.get` 的用法与脱敏约定
  - [5.6 异常与统一信封](#sec-5-6) — `ModuleApiError` 与错误码
  - [5.7 审计](#sec-5-7) — 把关键动作写进审计链
  - [5.8 事件广播](#sec-5-8) — 让前端知道后台发生了什么
- [6. Web UI 两种模式：`iframe` 与 `js`](#sec-6) — 模块页面的两种做法、各自的 URL 规则，以及为什么官方示例全部用 `js` 模式。
  - [6.1 选型对比](#sec-6-1) — 什么时候用 iframe，什么时候必须用 js
  - [6.2 静态资源与 URL 规则](#sec-6-2) — `/api/modules/<id>/asset/...` 的映射
  - [6.3 `iframe` 模式完整示例](#sec-6-3) — 最小可行骨架
  - [6.4 `js` 模式完整示例](#sec-6-4) — 从 `window.AutoOpsSDK` 取运行时
  - [6.5 共享运行时与构建建议](#sec-6-5) — 不打包 React/antd，怎么写现代代码
  - [6.6 UI/安全约定](#sec-6-6) — 只用宿主组件库、不要整页刷新
- [7. 往「参数设置」加配置项](#sec-7) — 让模块的开关出现在宿主「系统设置 → 参数设置」里，并正确读写。
  - [7.1 声明](#sec-7-1) — `settings[]` 的写法
  - [7.2 键名、类型与校验](#sec-7-2) — `mod.<id>.<key>` 与 bool/int/string 的边界
  - [7.3 读取与写回](#sec-7-3) — `ctx.get_setting` 与写回后的生效时机
  - [7.4 默认值、卸载与注意事项](#sec-7-4) — 缺省值从哪来、卸载时清不清

**第三部分 · 调试与发布**

- [8. 调试与排障](#sec-8) — 出问题时先看哪里：日志、数据目录、接口、页面白屏，以及常见错误对照表。
  - [8.1 日志在哪](#sec-8-1) — `module.<id>` 记录器与 200 行环形缓冲
  - [8.2 模块私有数据目录](#sec-8-2) — 可以随便写的地方
  - [8.3 用 curl / PowerShell 直接测 API](#sec-8-3) — 绕过前端验证后端
  - [8.4 iframe 页面排障](#sec-8-4) — 白屏、404、权限
  - [8.5 js 模式排障](#sec-8-5) — SDK 未注入、mount 没注册
  - [8.6 常见错误对照表](#sec-8-6) — 症状 → 原因 → 处理
- [9. 打包、安装与卸载](#sec-9) — 从源码目录到 `.mod.ops_ext`，再到安装进实例的完整链路，以及每一步背后的校验。
  - [9.1 打包前检查](#sec-9-1) — 会被自动剔除的文件
  - [9.2 `pack_module.py` 用法](#sec-9-2) — `--check`、`-o`、产物命名
  - [9.3 安装页流程与实验性确认](#sec-9-3) — 选包窗口、预览页与逐字确认
  - [9.4 直接调安装 API（自动化场景）](#sec-9-4) — `upload-check` 与预览会话安装
  - [9.5 安装/启用/禁用/卸载的内部行为](#sec-9-5) — 装了什么、删了什么
  - [9.6 供应链：签名与哈希](#sec-9-6) — 目前能做的校验与还缺什么
- [10. 版本与兼容](#sec-10) — 版本号规则、宿主版本门槛，以及升级/降级/卸载时会发生什么。
  - [10.1 `version` 必须是严格 semver](#sec-10-1) — 打包与升级都依赖它
  - [10.2 `minBastionVersion`](#sec-10-2) — 宿主太老时会发生什么
  - [10.3 升级、降级与卸载](#sec-10-3) — 重新上传即升级，没有「点一下升」的入口
  - [10.4 兼容性写法建议](#sec-10-4) — 面向未来宿主写法

**第四部分 · 参考**

- [11. FAQ 与已知限制](#sec-11) — 高频提问，以及当前版本明确做不到的事。
- [12. hello-world 全流程走查](#sec-12) — 拿 `hello-bastion` 从源码走到安装、实测接口、打开页面、卸载的一份完整记录。
  - [12.1 源码清单](#sec-12-1) — 一份可对照的源码树，逐项说明
  - [12.2 从校验到安装](#sec-12-2) — 校验不通过会卡在哪一步
  - [12.3 接口实测](#sec-12-3) — 用 curl 打通模块 API
  - [12.4 页面与设置](#sec-12-4) — 页面、导航与参数设置联动
  - [12.5 排障与卸载](#sec-12-5) — 卸干净，包括数据目录
  - [12.6 从这个示例继续扩展](#sec-12-6) — 可以接着改哪些地方
- [附录 A：本白皮书依赖的契约条款](#sec-a) — 每条规则对应的 `CONTRACT.md` 章节。
- [附录 B：发布前自查清单](#sec-b) — 提交/发布前逐条打勾。

---

<a id="sec-1"></a>
## 1. 能做什么 / 不能做什么

> **发行形态：系统不内置任何模块。** 全新实例安装完成后就是 **0 个模块**；所有能力（包括 `hello-bastion`、`js-demo`、`rdp-gateway` 三个官方示例）都必须由管理员上传 `.mod.ops_ext` 安装。官方示例源码在 `modules-src/`，它只是开发与打包输入，运行时不会被扫描或自动安装；打包产物放在**仓库根目录**，随仓库分发。卸载就是真卸载，重装＝重新上传同一份包。

<a id="sec-1-1"></a>
### 1.1 能做

| 能力 | 做法 | 契约依据 |
| --- | --- | --- |
| 在侧边栏加一个入口 | `description.json.nav[]`，`type` 选 `iframe` 或 `js` | §2、§7 |
| 提供完整自定义页面 | 首选 `js` 模式：放一个 ES Module 到 `web/`，用宿主注入的 React/antd；需要完全隔离或非 React 技术栈时用 `iframe` | §1、§6 |
| 在「参数设置」加配置项 | `settings[]` 声明 `bool`/`int`/`string`，自动落 `mod.<id>.<key>` | §2、§3 |
| 调堡垒机内部数据 | `autoops.bastion` 的 hosts（资产）/accounts/sessions/audit/policy/settings 门面 | §5 |
| 异步取资产凭证 | `await bastion.credentials.get(host_id, account_id=None)` | §5 |
| 注册模块自己的 HTTP API | `@ctx.route(...)` 或 `ctx.blueprint()` | §4 |
| 写模块私有文件 | `ctx.data_dir`（`instance/modules/<id>/data`） | §4 |
| 主动写审计 | `bastion.audit.log()` / `log_command()` | §5 |
| 向浏览器广播事件 | `ctx.emit()` / `bastion.events.emit()`（Socket.IO） | §4、§5 |
| 携带可执行文件 | `bin/`，运行期在 `ctx.package_dir / "bin"` | §1 |
| 声明依赖其他模块 | `requires: []`（只做声明，语义见 §11） | §2 |

<a id="sec-1-2"></a>
### 1.2 不能做 / 不要做

- **不能改宿主导航以外的布局**。你不能往宿主左侧菜单之外塞页面、改顶部栏、改登录页；能加的只有 `nav[]` 里的入口，最终 URL 固定为 `/m/<id><path>`。
- **不能新增宿主核心权限码**。核心权限码定义在 `bastion-backend/app/security.py` 的 `ALL_PERMISSIONS`，模块不能往里加。你可以在 `description.json.permissions` 里声明模块级权限（通常 `module:<id>:use`），但角色配置页是否展示、如何授权由宿主实现决定。
- **不能在安装页预览时执行 Python**。上传预览走 `POST /api/modules/upload-check`：只解析 `description.json`、不落盘、不 import 模块代码。安装成功后才加载。
- **没有进程内沙箱**。Python 扩展可以读数据库、密钥、其他模块文件、访问网络。宿主只做「安装包路径/大小/结构」校验，不做能力隔离。
- **不能新增数据库迁移**。模块系统没有 schema migration 机制；不要改宿主表结构。需要持久化就用 `ctx.data_dir` 下的 JSON/SQLite 文件。
- **不能假设宿主环境能 `pip install`**。安装包不会触发依赖安装。第三方库必须随包 vendoring（纯 Python 放 `modules/` 下）或使用宿主已有依赖；有平台二进制/外部程序的放 `bin/` 并自己做探测。
- **不能覆盖宿主核心路由/接口**。技术上作为可信代码可以 monkeypatch，但这属于未支持行为，升级必炸；用 `ctx.route` 的独立前缀。
- **不能保证崩溃完全不影响宿主**。契约提供的是「加载失败隔离」和「单请求异常隔离」；恶意代码、`os._exit`、耗尽内存/GIL、阻塞主线程仍能拖垮整个后端。
- **不能把秘密放静态资源**。`/api/modules/<id>/asset/**` 免登录，`web/`、`images/`、`README.md` 任何登录用户甚至未登录者都能读。
- **不能依赖安装页执行你的代码来做初始化**。初始化逻辑放 `setup(ctx)`，它只在安装成功后的加载阶段执行。

<a id="sec-1-3"></a>
### 1.3 安全模型与责任边界

**运行身份**：模块的 Python 代码运行在堡垒机后端进程里，使用后端的数据库连接、应用配置和密钥。它等价于你把代码直接提交进 `bastion-backend/app/`——不是「客户端插件」，也不是「受限 DSL」。

由此推导出几条硬规则：

1. **只安装可信来源**。当前模块系统没有内置代码签名验证；`author: "AutoOps"` 只是元数据（官方徽标），不是密码学证明。第三方包在安装前应校验来源、哈希、发布渠道。宿主只能保证 ZIP 路径/大小/结构合法，不能保证代码无害。
2. **模块 API 的权限是模块级的**。`ctx.route` 默认只校验 `module:<id>:use`，它回答的是「这个用户能不能用这个模块」，**不回答**「这个用户能不能看这台主机」。如果接口按 `host_id` 返回敏感数据，模块必须自己校验当前用户对该资产的授权（见 §11 Q11 与 §5.5），否则会形成越权面。
3. **SDK 以进程身份执行，不等于当前用户身份**。`credentials.get()`、`hosts.list()` 等不会自动按 JWT 用户的 Grant 过滤；权限模型在模块侧。当前实现中 `hosts.list()`/`hosts.get()`、`accounts.list()`、`credentials.*`、`sessions.list_active()` 都返回全量数据；只有 `sessions.open()`、`ssh.run()`、`policy.check()` 会按当前用户授权判定。按资产敏感的接口必须自己做授权检查（见 §11 Q10）。
4. **静态资源免登录**。不要在里面嵌入 JWT、内部 URL、资产口令、调试 dump。
5. **审计只覆盖你走 SDK 的操作**。直接用 SQLAlchemy 改库、绕过 `bastion.audit`，不会自动产生审计记录；这会让堡垒机的「全程审计」承诺失真。敏感操作请走 SDK 的审计门面。
6. **网络不受限**。模块可以访问任意地址；数据外发风险由模块作者和安装者承担。企业环境建议在代码审查/网络策略层面兜底。

---

<a id="sec-2"></a>
## 2. 包结构：`.mod.ops_ext` 里有什么

<a id="sec-2-1"></a>
### 2.1 ZIP 顶层布局

`.mod.ops_ext` 是 **ZIP（deflate）** 容器。文件名约定 `<id>-<version>.mod.ops_ext`，但**安装时以 `description.json` 的 `id`/`version` 为准**，文件名不参与校验。

```text
my-module-1.0.0.mod.ops_ext
├── description.json        # 必填，必须在包根目录
├── modules/                # 可选，Python 扩展根
│   ├── __init__.py         # 存在 modules/ 时必填，且必须定义 setup(ctx)
│   └── helpers.py          # 可选，相对 import
├── images/                 # 可选，安装页演示图
│   └── demo-1.png
├── web/                    # 可选，前端静态资源（HTML/JS/CSS/字体…）
│   ├── index.html
│   └── app.js
├── data/                   # 可选，初始数据；安装时复制到 instance/modules/<id>/data
├── bin/                    # 可选，可执行文件/外部程序
│   └── my-helper.exe
└── README.md               # 可选，安装页可读；静态资源接口允许访问
```

要求与限制（契约 §1）：

- `description.json` 必须是**包根目录下的普通文件**，≤ 256 KB。
- `modules/` 存在时，`modules/__init__.py` 必须存在且能 `import` 出可调用的 `setup(ctx)`。
- `data/` 是**初始数据**，安装时复制到模块私有数据目录；不是运行期写入位置（运行期写 `ctx.data_dir`）。
- 只允许普通文件和目录；**符号链接、设备文件、绝对路径、`..`、反斜杠、盘符、NUL、非法文件名一律拒绝**。
- 整个包解压后总大小 ≤ 256 MB，条目数 ≤ 20000。
- 安装期**绝不 import** 模块代码；代码加载发生在安装成功后的独立 `try/except` 中。

<a id="sec-2-2"></a>
### 2.2 安装后的磁盘布局

```text
bastion-backend/instance/modules/<id>/
├── package/                # ZIP 解压后的内容（模块代码/资源；只读语义）
│   ├── description.json
│   ├── modules/__init__.py
│   ├── web/...
│   └── bin/...
├── state.json              # 宿主写：enabled、installedAt 等安装状态
└── data/                   # 模块私有可写目录（ctx.data_dir）
    └── ...                 # 你的运行期状态
```

模块视角：

| 属性 | 指向 | 用途 |
| --- | --- | --- |
| `ctx.package_dir` | `instance/modules/<id>/package` | 读包内只读资源（`web/`、`bin/`、`README.md`） |
| `ctx.data_dir` | `instance/modules/<id>/data` | **唯一**运行期可写目录 |
| `bin/` 运行路径 | `ctx.package_dir / "bin" / <file>` | 外部可执行文件（如 RDP 网关） |
| 静态资源 URL | `/api/modules/<id>/asset/<相对路径>` | 只对 `web/`、`images/`、`README.md` 生效 |

> 这个目录只在管理员上传安装后出现；全新实例的 `instance/modules/` 是空的。系统不内置、不预装任何模块，也不会在启动时从 `modules-src/` 补装。

**不要写 `package_dir`**。升级/重装可能替换整个 `package/`；你写进去的文件会丢，且可能破坏包内容。所有状态写 `data_dir`。

<a id="sec-2-3"></a>
### 2.3 安装期硬校验与体积上限

| 项 | 限制/规则 | 失败表现 |
| --- | --- | --- |
| 容器 | ZIP，deflate | 安装失败，回滚到未安装 |
| `description.json` | 包根、≤256 KB、合法 JSON | `MODULE_INVALID_PACKAGE` |
| 单包解压后总大小 | ≤256 MB | 安装失败 |
| 条目数 | ≤20000 | 安装失败 |
| 路径 | 禁止绝对路径、`..`、反斜杠、盘符、NUL | 安装失败 |
| 条目类型 | 只允许普通文件/目录；拒绝符号链接/设备文件 | 安装失败 |
| 文件名 | 非法字符直接失败 | 安装失败 |
| `modules/` | 有则必须有 `__init__.py` + `setup` | 安装成功但 `loaded=false`，`errors[]` 有原因 |
| `experimental` | 需要 `ack` 原文匹配 | `EXPERIMENTAL_NOT_ACKED`（400） |

> 建议：包里不要带 `__pycache__`、`.git`、`node_modules`、`*.pyc`、临时文件。打包工具会默认排除，但手写 ZIP 时要自己清理。

<a id="sec-2-4"></a>
### 2.4 每个目录的用途与禁区

| 目录 | 用途 | 运行期可写 | 静态可访问 | 备注 |
| --- | --- | --- | --- | --- |
| 根 `description.json` | 声明元数据/导航/设置 | 否 | 否（只有预览接口解析） | 必须 UTF-8 JSON |
| `modules/` | Python 扩展 | 否 | 否 | 可含子模块；用相对 import |
| `images/` | 安装页演示图 | 否 | 是 | 建议 PNG/JPG/WebP，别放超大图 |
| `web/` | 前端页面与资源 | 否 | 是 | 入口来自 `entry.web` 或 `nav[].entry` |
| `data/` | 初始数据 | 安装后复制到 `data_dir` | 否 | 安装时复制；运行期读写 `ctx.data_dir` |
| `bin/` | 可执行文件 | 否（但程序自身可能写文件） | 否 | 注意 Linux/Windows 平台与执行权限 |
| `README.md` | 说明 | 否 | 是 | 会被免登录读取，别写秘密 |

<a id="sec-2-5"></a>
### 2.5 最小骨架

一个「只有页面、没有 Python」的模块：

```text
my-module/
├── description.json
└── web/
    └── index.html
```

一个「有 Python API」的模块：

```text
my-module/
├── description.json
├── modules/
│   ├── __init__.py
│   └── helpers.py
├── web/
│   └── index.html
└── data/
    └── seed.json
```

对应的 `description.json` 最小可用版本：

```json
{
  "id": "my-module",
  "name": { "zh": "我的模块", "en": "My Module" },
  "version": "1.0.0",
  "author": "Your Name",
  "entry": { "web": "web/index.html" },
  "nav": [
    {
      "key": "home",
      "title": { "zh": "我的模块", "en": "My Module" },
      "path": "/",
      "type": "iframe"
    }
  ]
}
```

---

<a id="sec-3"></a>
## 3. `description.json` 全字段参考

<a id="sec-3-1"></a>
### 3.1 完整示例

```json
{
  "id": "my-module",
  "name": { "zh": "我的模块", "en": "My Module" },
  "version": "1.2.0",
  "author": "Your Name",
  "description": {
    "zh": "一句话说明这个模块做什么。",
    "en": "One line about what this module does."
  },
  "tags": ["experimental", "monitor"],
  "experimental": true,
  "icon": "AppstoreOutlined",
  "homepage": "https://example.com/my-module",
  "license": "MIT",
  "minBastionVersion": "1.0.0",
  "images": ["images/demo-1.png"],
  "entry": { "web": "web/index.html" },
  "nav": [
    {
      "key": "dashboard",
      "title": { "zh": "监控面板", "en": "Dashboard" },
      "path": "/dashboard",
      "entry": "web/dashboard.html",
      "type": "iframe",
      "icon": "DashboardOutlined",
      "permission": "module:my-module:use"
    },
    {
      "key": "settings-ui",
      "title": { "zh": "可视化配置", "en": "Visual Config" },
      "path": "/config",
      "entry": "web/config.js",
      "type": "js",
      "icon": "SettingOutlined"
    }
  ],
  "settings": [
    {
      "key": "enabled",
      "label": { "zh": "启用", "en": "Enabled" },
      "type": "bool",
      "default": "1",
      "description": { "zh": "总开关", "en": "Master switch" }
    },
    {
      "key": "refresh_seconds",
      "label": { "zh": "刷新间隔（秒）", "en": "Refresh interval (s)" },
      "type": "int",
      "default": "30",
      "min": 5,
      "max": 600
    },
    {
      "key": "api_base",
      "label": { "zh": "外部 API 地址", "en": "External API base" },
      "type": "string",
      "default": ""
    }
  ],
  "permissions": ["module:my-module:use"],
  "requires": []
}
```

<a id="sec-3-2"></a>
### 3.2 字段表

| 字段 | 必填 | 类型 | 默认 | 校验/说明 |
| --- | --- | --- | --- | --- |
| `id` | 是 | string | — | `^[a-z][a-z0-9-]{2,39}$`；不能是保留字；安装后不可改 |
| `name` | 是 | string \| `{zh, en}` | — | 归一化后永远是双语对象；建议两种语言都写 |
| `version` | 是 | string | — | 严格 semver：`MAJOR.MINOR.PATCH[-pre][+build]`，不带 `v` |
| `author` | 是 | string | — | `AutoOps`（忽略大小写/首尾空格）= 官方，前端显示金色「官方」徽标 |
| `description` | 否 | string \| `{zh, en}` | 空 | 安装页展示 |
| `tags` | 否 | string[] | `[]` | 固定色板映射；`experimental` 触发烧杯；`official` 由 `author` 推导，不用写 |
| `experimental` | 否 | bool | `false` | 与 `tags` 里的 `experimental` 取**并集** |
| `icon` | 否 | string | `AppstoreOutlined` | antd 图标名；找不到时前端回退 `AppstoreOutlined` |
| `homepage` | 否 | string | `""` | 展示用 |
| `license` | 否 | string | `""` | 展示用 |
| `minBastionVersion` | 否 | string | `""` | 与宿主 `APP_VERSION` 比较；建议严格 semver |
| `images` | 否 | string[] | `[]` | 包内相对路径；通过 `/api/modules/<id>/asset/<path>` 展示；建议放 `images/` |
| `entry` | 否 | object | `{"web":"web/index.html"}` | `entry.web`：默认 Web 入口，必须是包内相对路径 |
| `nav` | 否 | object[] | `[]` | 侧边栏入口；至少一项才有菜单 |
| `nav[].key` | 是 | string | — | 模块内唯一；用于前端 key/路由标识 |
| `nav[].title` | 是 | string \| `{zh, en}` | — | 菜单文字 |
| `nav[].path` | 否 | string | `/` | 相对路径，最终 URL = `/m/<id><path>`；必须以 `/` 开头 |
| `nav[].entry` | 否 | string | `entry.web` | 该入口的页面/脚本，包内相对路径；须在 `web/` 下才能被静态接口访问 |
| `nav[].type` | 否 | `"iframe"` \| `"js"` | `"iframe"` | 页面加载模式 |
| `nav[].icon` | 否 | string | 继承 `icon` | antd 图标名 |
| `nav[].permission` | 否 | string | `module:<id>:use` | 访问该入口/模块 API 的权限码 |
| `settings` | 否 | object[] | `[]` | 自动出现在「参数设置」 |
| `settings[].key` | 是 | string | — | `^[a-zA-Z][a-zA-Z0-9_]{0,39}$`；最终键名 `mod.<id>.<key>` |
| `settings[].label` | 否 | string \| `{zh, en}` | 回退 key | 设置项显示名 |
| `settings[].type` | 否 | `"bool"` \| `"int"` \| `"string"` | `"string"` | 其它值不会获得额外类型支持 |
| `settings[].default` | 否 | string \| number \| bool | `""` | 宿主按字符串落库；建议统一写字符串；bool 归一化为 `"1"`/`"0"` |
| `settings[].min` | 否 | int | `null` | 仅 `int` 有意义 |
| `settings[].max` | 否 | int | `null` | 仅 `int` 有意义 |
| `settings[].description` | 否 | string \| `{zh, en}` | 空 | 参数设置页辅助说明 |
| `permissions` | 否 | string[] | `[]` | 声明模块权限，给角色配置页用；建议含 `module:<id>:use` |
| `requires` | 否 | string[] | `[]` | 依赖的模块 id 列表；当前只有声明语义（见 §11） |

> 未知字段：契约没有承诺「忽略未知字段」，不要写宿主不认识的键；升级时它们可能被丢弃或触发校验失败。
>
> `name`/`description`/`label` 也接受纯字符串；归一化后是双语对象。为了让中英文环境都正常，建议始终写 `{zh, en}`。

当前实现还叠加了以下长度/默认值校验（契约未逐条列出，打包/安装时会拦）：

- `name` 每种语言 ≤64 字符；`description` 每种语言 ≤1000 字符；
- `nav[].key` 匹配 `^[A-Za-z0-9_-]{1,64}$`，模块内唯一；`nav[].title` 每种语言 ≤64 字符；
- `nav[].type` 只允许 `iframe`/`js`；`nav[].path` 必须是包内相对前端路径（不能 `..`、反斜杠、绝对路径）；
- `settings[].label` ≤64 字符，`settings[].description` ≤256 字符；
- `settings[].min/max` 必须是整数，且 `min <= max`；
- `settings[].default`：`bool` 接受真值/假值并归一化成 `"1"`/`"0"`；`int` 必须是整数且落在 `min`/`max` 内；`string` ≤1024 字符；
- `images[]`、`entry.web`、`nav[].entry` 必须是安全的相对路径（反斜杠/`..`/绝对路径/盘符会被拒）；
- `permissions[]` 只能是合法的权限码格式，`requires[]` 不能包含自己。

<a id="sec-3-3"></a>
### 3.3 id 规则与保留 id

```text
^[a-z][a-z0-9-]{2,39}$
```

- 必须以小写字母开头；
- 只允许小写字母、数字、连字符；
- 长度 3–40；
- 不能是保留 id：`core`、`system`、`admin`、`modules`、`_builtin`；
- id 决定 URL（`/m/<id>...`、`/api/modules/<id>/...`）、数据目录和设置键前缀，**装上去后不要改**。

<a id="sec-3-4"></a>
### 3.4 官方发行与 `author`

- `author` 与 `"AutoOps"` 比较时忽略大小写和首尾空格；相等则归一化对象 `official: true`，前端显示金色官方徽标。
- 官方徽标只是**元数据**，不是安全签名。第三方包也可以写 `"AutoOps"`，宿主不会做密码学验证；不要用它来判断可信。
- `tags` 里不需要写 `official`；前端由 `official` 字段自动加。

<a id="sec-3-5"></a>
### 3.5 标签、实验性与烧杯确认

`tags` 与 `experimental` 是并集关系：只要 `experimental: true` **或** `tags` 含 `"experimental"`，模块就是实验性。

标签颜色由前端固定映射（后端只下发 tag 字符串）：

| tag | 颜色 | 图标/行为 |
| --- | --- | --- |
| `experimental` | magenta | 烧杯；安装前必须二次确认 |
| `official` | gold | 由 `author` 自动加 |
| `security` | red | |
| `network` / `rdp` / `ssh` | blue | |
| `monitor` / `audit` | cyan | |
| `storage` / `file` | green | |
| `ai` | purple | |
| 其它 | default（灰） | |

**实验性模块安装确认**：

- 前端弹出 Modal，要求用户在输入框里**手工敲**一遍下面的确认语；
- **不接受粘贴**；
- 比对方式是**去首尾空白后的完整字符串全等**；
- 后端二次校验 multipart 表单的 `ack` 字段，不匹配返回 `400 EXPERIMENTAL_NOT_ACKED`。

确认语必须逐字等于：

```text
我知道这是实验性功能，使用不当的话也许会造成系统损毁
```

> 注意：这句话里**没有**句号、没有感叹号、没有额外空格。少一个字都会 400。

<a id="sec-3-6"></a>
### 3.6 归一化模块对象与 `/api/modules`

`GET /api/modules`、`GET /api/modules/<id>` 返回的是后端 `normalize_description()` 之后的对象，不是你写的原始 JSON。常用字段：

```jsonc
{
  "id": "my-module",
  "name": { "zh": "我的模块", "en": "My Module" },
  "version": "1.2.0",
  "author": "Your Name",
  "official": false,
  "description": { "zh": "", "en": "" },
  "tags": ["experimental", "monitor"],
  "experimental": true,
  "icon": "AppstoreOutlined",
  "images": [
    {
      "path": "images/demo-1.png",
      "url": "/api/modules/my-module/asset/images/demo-1.png"
    }
  ],
  "nav": [
    {
      "key": "dashboard",
      "title": { "zh": "监控面板", "en": "Dashboard" },
      "path": "/m/my-module/dashboard",
      "entry": "web/dashboard.html",
      "icon": "DashboardOutlined",
      "type": "iframe",
      "permission": "module:my-module:use"
    }
  ],
  "settings": [
    {
      "key": "mod.my-module.enabled",
      "rawKey": "enabled",
      "label": { "zh": "启用", "en": "Enabled" },
      "type": "bool",
      "default": "1",
      "min": null,
      "max": null,
      "group": "我的模块"
    }
  ],
  "permissions": ["module:my-module:use"],
  "requires": [],
  "reserved": false,
  "enabled": true,
  "loaded": true,
  "installedAt": "2026-10-23T15:04:05Z",
  "updatedAt": "2026-10-23T15:04:05Z",
  "size": 14745600,
  "errors": []
}
```

排查时重点看：

- `enabled`：管理员是否启用；
- `loaded`：Python 扩展是否加载成功；
- `errors[]`：加载失败原因（`setup` 抛异常、依赖缺失等）；
- `nav[].path`：归一化后是**完整 URL**，不是原始相对路径；
- `nav[].entry`：前端加载页面/脚本需要它；契约 §2 的归一化示例漏写了这个字段，当前实现会保留。以宿主实现为准，建议始终显式写 `nav[].entry`；
- `settings[].key`：归一化后带 `mod.<id>.` 前缀。

<a id="sec-3-7"></a>
### 3.7 多语言与 JSON 书写约定

- 文件必须是 **UTF-8**；无 BOM 最稳。当前实现用 `utf-8-sig` 读取，能兼容 BOM，但不要依赖这一点。
- 用 LF 换行；不要手写重复键；不要写 JSONC 注释（契约示例里的 `//` 只是说明）。
- `name`/`description`/`label`/`nav[].title` 建议都写 `{zh, en}`；纯字符串虽然接受，但英文环境只能拿到同一份文本。
- 不要在 `description.json` 里放秘密（安装页、接口都会展示它）。

---

<a id="sec-4"></a>
## 4. Python 扩展：生命周期与 `ctx`

<a id="sec-4-1"></a>
### 4.1 入口约定

- 文件：`package/modules/__init__.py`；
- 必须存在可调用的 `setup(ctx)`；
- 加载由宿主 `app/modules/loader.py` 完成，发生在安装成功之后；安装期不会 import；
- `setup` 抛异常 = 该模块加载失败：`loaded=false`、`errors[]` 记录原因，**不影响其它模块和后端启动**；
- 模块内部可以拆多个文件，用相对 import：

```python
# package/modules/__init__.py
from .helpers import normalize
```

不要假设当前工作目录；所有路径从 `ctx.package_dir` / `ctx.data_dir` 出发，用 `pathlib.Path`。

<a id="sec-4-2"></a>
### 4.2 生命周期钩子

```python
def setup(ctx): ...
def teardown(ctx): ...
def on_enable(ctx): ...
def on_disable(ctx): ...
```

| 钩子 | 何时调用 | 是否必须 | 注意 |
| --- | --- | --- | --- |
| `setup(ctx)` | 模块加载时一次（后端启动、启用、安装后加载） | **必须** | 注册路由/钩子、初始化资源；抛异常=加载失败 |
| `teardown(ctx)` | 禁用/卸载前（已加载的模块） | 可选 | 停线程、关连接、flush 状态；必须幂等 |
| `on_enable(ctx)` | 管理员启用模块时 | 可选 | 与 `setup` 可能先后发生；不要重复初始化 |
| `on_disable(ctx)` | 管理员禁用模块时 | 可选 | 真正清理仍应在 `teardown` |

补充约定（按契约推断，落地以实现为准）：

- `setup` 可能在后端每次启动时重新执行；不要假设「只执行一次就永久有效」。
- `setup` 里注册的路由在模块卸载时应由 loader 清理；如果你在 `ctx.app` 上手工挂了全局对象，`teardown` 要自己撤。
- `ctx.on("install", handler)` / `ctx.on("uninstall", handler)` 的触发时机依赖宿主装载顺序；安装期不 import 模块，所以**不要依赖 install 钩子做首次初始化**，把幂等初始化放 `setup`。
- 任何钩子都不要 `sys.exit` / `os._exit` / 长时间阻塞。

<a id="sec-4-3"></a>
### 4.3 `ctx` 成员全表

| 成员 | 类型/签名 | 说明 |
| --- | --- | --- |
| `ctx.id` | `str` | 模块 id（只读） |
| `ctx.version` | `str` | 模块版本（只读） |
| `ctx.author` | `str` | 作者（只读） |
| `ctx.official` | `bool` | 是否官方（由 author 推导，只读） |
| `ctx.app` | `Flask` | 宿主 app；**谨慎使用**，优先用 `ctx.route` |
| `ctx.package_dir` | `Path` | 包根目录（只读） |
| `ctx.data_dir` | `Path` | 私有可写目录（`instance/modules/<id>/data`） |
| `ctx.logger` | `logging.Logger` | logger 名 `module.<id>` |
| `ctx.get_setting(key, default=None)` | `str \| default` | 读模块设置，自动补 `mod.<id>.` 前缀 |
| `ctx.set_setting(key, value)` | `str`（当前实现） | 写模块设置（落库 + commit）；只能写声明过的 raw key |
| `ctx.settings` | `dict[str, str]` | 设置快照（只读语义，可能过期） |
| `ctx.route(path, methods=("GET",), permission=None)` | decorator | 把函数挂到 `/api/modules/<id>/api<path>`；返回统一信封 |
| `ctx.blueprint(name="api")` | `Blueprint` | 返回 Flask 蓝图；路由由 loader 在 `setup` 结束后收集，最终挂在 `/api/modules/<id>/api` 下 |
| `ctx.on(event, handler)` | `None` | 注册钩子；`event ∈ {"install","uninstall","enable","disable"}`；handler 接受 `ctx` 或无参 |
| `ctx.emit(topic, payload)` | `None` | Socket.IO 广播 `module:<id>:<topic>`；`topic` 不能含冒号、长度 ≤64 |

<a id="sec-4-4"></a>
### 4.4 `ctx.route`：注册模块 API

最终 URL：

```text
/api/modules/<id>/api<path>
```

完整示例：

```python
from flask import request
from autoops import ModuleApiError, bastion


def setup(ctx):
    @ctx.route("/stats", methods=("GET",))
    def stats():
        # 返回 dict → 宿主包成 {"success": true, "message": "ok", "data": {...}}
        return {"module": ctx.id, "version": ctx.version}

    @ctx.route("/echo", methods=("POST",))
    def echo():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            # 抛 ModuleApiError → 宿主包成 {"success": false, "code": "ERROR", "message": ...}
            raise ModuleApiError("缺少 name", 400)
        return {"hello": name}

    @ctx.route("/missing")
    def missing():
        raise ModuleApiError("对象不存在", 404)
```

调用：

```bash
TOKEN="<登录后拿到的 JWT>"
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/my-module/api/stats
```

成功信封：

```json
{ "success": true, "message": "ok", "data": { "module": "my-module", "version": "1.0.0" } }
```

失败信封：

```json
{ "success": false, "code": "ERROR", "message": "对象不存在", "data": null }
```

规则与坑：

- 函数返回 `dict` / `list` / `None` → `api_ok(data)`；返回其它类型不要假设会被正确处理（`None` 会变成 `data: null`）。
- 抛 `ModuleApiError(message, status)` 才能控制 HTTP 状态码；返回 dict 永远是 `200`。目前没有通过 `ctx.route` 自定义 `message`/额外字段的正式入口，需要自定义响应就上 `ctx.blueprint()`（见下）。
- `methods` 默认 `("GET",)`；需要 POST/PUT/DELETE 显式声明。
- `permission` 缺省规则：该模块第一个 `nav[].permission`，否则 `module:<id>:use`。不要以为路由默认公开。
- **不要依赖 `async def` 视图**。契约没有承诺 `ctx.route` 支持异步视图；本白皮书的示例全部用同步视图 + §5.4 的桥接方式调用 async SDK。如果宿主实现支持 async 视图，同步写法也仍然可用。
- 路径不要带 `/api`，不要带模块 id；它们已由宿主拼好。不要注册 `/<path:...>` 捕获宿主其它路由。
- 路由函数不要吞掉所有异常后返回 `{"success": false}`；那会被再包一层成功信封。要么正常返回，要么抛 `ModuleApiError`。

<a id="sec-4-5"></a>
### 4.5 `ctx.blueprint`：需要 Flask 原生能力时

需要自定义状态码、文件响应、`send_file`、流式响应、Flask 原生装饰器时，用蓝图：

```python
from flask import send_file


def setup(ctx):
    # name 只用于内部标识；宿主会给蓝图名加模块前缀，不用担心和其它模块撞名
    bp = ctx.blueprint("raw")

    @bp.get("/raw")
    def raw():
        return {"ok": True}

    @bp.get("/download")
    def download():
        return send_file(ctx.package_dir / "web" / "demo.zip", as_attachment=True)

    # 不要调用 ctx.app.register_blueprint(bp)：
    # loader 在 setup 结束后会统一收集蓝图，手动注册会导致路由重复/前缀丢失。
```

约定：

- `ctx.blueprint(name)` 返回的是 **Flask Blueprint**；你在上面挂的路由，最终由 loader 收集并挂到 `/api/modules/<id>/api` 下，所以 `@bp.get("/raw")` 的最终路径是 `/api/modules/<id>/api/raw`；
- 不要自己 `register_blueprint`，也不要给蓝图加 `url_prefix`；前缀由宿主统一管理；
- 蓝图名由宿主加 `module_<id>_` 前缀，默认 `"api"` 不会和其它模块冲突；
- 蓝图内抛出的异常**不会**自动变成统一信封（`ModuleApiError` 也只在 `ctx.route` 分发器里被转换）；需要统一信封就自己返回 `{"success": false, ...}` 并设置状态码；
- 蓝图也能用 `before_request`/`after_request`/`teardown_request`；它们只对模块自己的路由生效。

<a id="sec-4-6"></a>
### 4.6 `ctx.on` / `ctx.emit`：事件

注册生命周期事件（注意：`ctx.on` 是普通方法，**不是装饰器**）：

```python
def _on_enable(ctx):
    ctx.logger.info("enabled")


def _on_disable(ctx):
    ctx.logger.info("disabled")


def setup(ctx):
    ctx.on("enable", _on_enable)
    ctx.on("disable", _on_disable)
```

handler 可以接受 `ctx`，也可以完全无参；宿主两种都兼容。契约 §4 的表格只写了 `ctx.on(event)`；当前实现签名是 `ctx.on(event, handler)`，也不是装饰器，别写错。

广播事件：

```python
def setup(ctx):
    @ctx.route("/ping", methods=("POST",))
    def ping():
        ctx.emit("pong", {"at": bastion.util.now().isoformat()})
        return {"sent": True}
```

- `ctx.emit(topic, payload)` 实际广播的 Socket.IO topic 是 `module:<id>:<topic>`；
- `topic` 不能为空、不能含冒号、最长 64 字符；想表达层级用 `.` 或 `-`；
- 前端如何订阅由宿主页面决定；模块页面没有标准 SDK 订阅接口（见 §11）；
- `ctx.on("install", handler)`/`"uninstall"` 的触发依赖 loader，别把关键初始化只放在这两个钩子里。

<a id="sec-4-7"></a>
### 4.7 设置与私有数据目录

```python
import json
import os
import tempfile
from pathlib import Path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def setup(ctx):
    interval = ctx.get_setting("refresh_seconds", "30")
    ctx.logger.info("interval=%s", interval)

    state_path = ctx.data_dir / "state.json"
    if not state_path.exists():
        _atomic_write(state_path, json.dumps({"installed": True}, ensure_ascii=False))
```

- 设置读取是**同步 DB 调用**，放在同步视图/`setup` 里；不要放进 `async def`（会阻塞事件循环）。
- `ctx.set_setting()` 会 commit，别在请求热路径/循环里频繁写；先判断是否真的变了。它只能写 `description.json.settings[]` 里声明过的 raw key，未声明的键会抛 `KeyError`（当前实现）；返回值是归一化后的字符串，但契约没有承诺返回类型，不要依赖。
- `ctx.settings` 是快照，跨请求可能过期；要最新值用 `ctx.get_setting()`。
- `get_setting` 推荐传 raw key（`refresh_seconds`），宿主自动补 `mod.<id>.`；完整键在部分实现里也能识别，但跨版本最稳的是 raw key。
- `data_dir` 在卸载时会被删除；需要保留的数据必须导出到外部存储。

<a id="sec-4-8"></a>
### 4.8 后台线程与资源释放

契约不提供调度器。你需要后台任务时：

```python
import threading

# 不要假设可以往 ctx 上任意挂属性；用模块级字典保存运行态。
_WORKERS: dict[str, tuple[threading.Thread, threading.Event]] = {}


def setup(ctx):
    stop = threading.Event()
    thread = threading.Thread(target=_loop, args=(ctx, stop), name="my-module-worker", daemon=True)
    thread.start()
    _WORKERS[ctx.id] = (thread, stop)


def teardown(ctx):
    worker = _WORKERS.pop(ctx.id, None)
    if worker is None:
        return
    thread, stop = worker
    stop.set()
    thread.join(timeout=5)
```

注意：

- 线程必须是 daemon 或能在 `teardown` 里可靠停止；否则禁用/卸载后仍在跑。
- 多进程部署时每个 worker 都会 `setup`，后台任务可能重复执行；用 `data_dir` 文件锁/主机锁去重，或避免后台任务。
- 后台线程里访问数据库/请求上下文，走 `bastion.util.run_blocking` 或宿主提供的线程安全门面；不要直接拿请求 `g`。
- 不要在 `setup` 里做长时间网络等待，会拖慢启动。

---

<a id="sec-5"></a>
## 5. `autoops.bastion` SDK

<a id="sec-5-1"></a>
### 5.1 第一原则：碰库/触网全是 `async def`

```python
from autoops import bastion


async def example():
    # 正确
    hosts = await bastion.hosts.list()

    # 错误：返回 coroutine，什么都不会发生，还会报 "coroutine was never awaited"
    # hosts = bastion.hosts.list()
```

契约 §5 的原文是：**所有会碰到数据库/网络的方法都是 `async def`**；同时提供 `_sync` 后缀的同步版本方便简单脚本。

- `async def` 方法必须 `await`；
- `_sync` 版本只能在**没有运行中事件循环**的地方调用；在 `async def` 里调用会死锁/报错；
- 同步 Flask 视图如何桥接，见 §5.4；
- 不要在 async 函数里做同步 DB 查询（例如直接 `Host.query`），会拖住事件循环；确需阻塞代码用 `await bastion.util.run_blocking(...)`。

<a id="sec-5-2"></a>
### 5.2 API 全表

以下签名逐项对应契约 §5。`->` 后是返回类型；所有未标 `_sync` 的方法都是 `async def`。

| 门面 | 签名 | 说明 |
| --- | --- | --- |
| 凭证 | `await bastion.credentials.get(host_id, account_id=None) -> Credentials` | 解密指定主机/账号的凭据；会自动写 `module_credential` 审计；明文只在返回值里 |
| 凭证 | `await bastion.credentials.list(host_id=None) -> list[CredentialMeta]` | 列出凭据元数据，**不含密钥/口令** |
| 资产 | `await bastion.hosts.list(protocols=None, keyword=None) -> list[dict]` | 主机列表；`protocols` 可按协议过滤，`keyword` 做关键词过滤 |
| 资产 | `await bastion.hosts.get(host_id) -> dict` | 单台主机详情 |
| 账号 | `await bastion.accounts.list(host_id=None) -> list[dict]` | 资产账号列表 |
| 账号 | `await bastion.accounts.reveal_password(account_id) -> str` | 解密账号口令；**会写审计**，返回明文 `str` |
| 账号 | `await bastion.accounts.reveal_private_key(account_id) -> str` | 解密账号私钥；**会写审计**，返回明文 `str` |
| 会话 | `await bastion.sessions.list_active() -> list[dict]` | 当前在线会话 |
| 会话 | `await bastion.sessions.open(host_id, account_id, protocol="ssh", source="module:<id>") -> dict` | 登记/打开会话；`source` 用 `module:<id>` 标识来源 |
| 审计 | `await bastion.audit.log(action, target_type=None, target_id=None, message="", detail=None) -> None` | 写一条操作审计；`detail` 不要放明文 |
| 审计 | `await bastion.audit.log_command(session_id, command, output=None, allowed=True, reason="") -> None` | 写一条命令审计 |
| 设置 | `await bastion.settings.get(key) -> str` | 读系统设置（字符串） |
| 设置 | `await bastion.settings.set(key, value) -> None` | 写系统设置（**谨慎**，见 §7.4） |
| 用户 | `await bastion.users.list() -> list[dict]` | 用户列表 |
| 策略 | `await bastion.policy.check(account_id, command) -> dict` | 命令策略判定：`{allowed, matched_rule, action}` |
| SSH | `await bastion.ssh.run(host_id, command, account_id=None, timeout=30) -> dict` | 远程执行；强制过命令策略；失败抛 `bastion.ModuleError`；返回 `{code, stdout, stderr, elapsed}` |
| 事件 | `await bastion.events.emit(topic, payload) -> None` | Socket.IO 广播 |
| 通用 | `bastion.util.now() -> datetime` | aware UTC 当前时间（**同步**） |
| 通用 | `await bastion.util.run_blocking(fn, *a, **kw) -> Any` | 在带 app context 的线程里跑阻塞代码（**异步包装**） |

同步版本：

- 命名规则是方法名 + `_sync`，例如 `bastion.hosts.list_sync()`、`bastion.credentials.get_sync(host_id)`；
- 契约只承诺「提供 `_sync` 后缀的同步版本」，没有逐项列出覆盖范围；以目标宿主版本为准；
- 只能在没有运行中事件循环的线程调用。

<a id="sec-5-3"></a>
### 5.3 返回值与数据结构

#### `Credentials`

`credentials.get()` 返回对象至少有以下字段（契约 §5）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `host_id` | int | 资产 id |
| `account_id` | int | 账号 id |
| `host_name` | str | 资产名 |
| `address` | str | 地址 |
| `protocol` | str | `ssh` / `rdp` / `winrm` 等 |
| `port` | int | 端口 |
| `username` | str | 登录用户名 |
| `password` | str | 解密后的口令明文；密钥登录时可能为空 |
| `private_key` | str | 解密后的私钥明文；口令登录时可能为空 |
| `passphrase` | str | 私钥口令；可能为空 |

**只把 `username` 和 `password` 的长度返回给前端**，不要把整个对象序列化。

#### `CredentialMeta`

`credentials.list()` 明确承诺「不含密钥」；契约写返回 `list[CredentialMeta]`，当前实现返回 `list[dict]`（账号元数据 + 主机信息）。不要依赖未文档化的字段；需要账号 id/用户名时优先用 `accounts.list()` 或 `credentials.get()`。

#### `hosts.list()` / `accounts.list()` / `sessions.list_active()`

返回 `list[dict]`，具体字段以 SDK 实现为准。契约没有逐一固定 schema，所以：

- 只依赖你实际需要的字段，并做防御式读取（`item.get("id")`）；
- 不要把整个 dict 原样回前端（可能含敏感字段/内部字段）；
- 示例 `hello-bastion` 只用 `len(...)`，就是刻意避免绑死字段；
- **这些读取接口当前实现不做用户级过滤**：有模块权限的账号能看到全部主机/账号/在线会话。若你的模块面向普通用户，必须自己做资产级授权校验，或者把接口限制为管理员权限。

#### `ssh.run()`

```python
from autoops import bastion


async def run_uptime(host_id):
    result = await bastion.ssh.run(host_id, "uptime", timeout=15)
    # {"code": 0, "stdout": "...", "stderr": "", "elapsed": 0.42}
    return result
```

- 强制执行命令策略；被策略拒绝/执行失败会抛 `bastion.ModuleError`；
- 错误信息可能包含目标机输出，回前端前要裁剪/脱敏；
- 这是高危接口，务必先做用户对资产的授权校验。

#### `policy.check()`

```python
from autoops import bastion


async def check_command(account_id, command):
    verdict = await bastion.policy.check(account_id, command)
    # {"allowed": False, "matched_rule": "...", "action": "deny"}
    return verdict
```

`ssh.run()` 内部也会过策略；不要在模块里自己「先 check 再跳过策略执行」。

<a id="sec-5-4"></a>
### 5.4 在 Flask 同步视图里正确调用 async

这是模块开发最容易踩的坑。

**背景**：本项目 Flask-SocketIO 使用 `async_mode="threading"`（`bastion-backend/app/__init__.py`），普通 Flask 视图在 worker 线程执行，该线程**没有运行中的事件循环**。因此 `asyncio.run(...)` 在普通同步视图里是安全的。

但如果你把同一段代码放进 `async def` 视图、或宿主未来切换到 ASGI/事件循环模式，`asyncio.run()` 会抛：

```text
RuntimeError: asyncio.run() cannot be called from a running event loop
```

#### 写法 A：同步视图 + `asyncio.run`（本项目默认场景）

```python
import asyncio
from autoops import bastion


async def _collect():
    hosts = await bastion.hosts.list()
    return {"hostTotal": len(hosts)}


def setup(ctx):
    @ctx.route("/count")
    def count():
        return asyncio.run(_collect())   # 视图线程没有 loop，安全
```

要点：

- `asyncio.run()` 每次都会新建/关闭一个事件循环，适合低频模块接口；高并发接口应改用常驻线程池（见写法 C）；
- 传入的协程必须在当前线程创建（直接 `asyncio.run(_collect())` 就是）；
- 不要在已经 `await` 的 `async def` 里再用 `asyncio.run`。

#### 写法 B：优先用 `_sync` 版本

```python
from autoops import bastion


@ctx.route("/count")
def count():
    hosts = bastion.hosts.list_sync()   # 契约承诺的同步版本
    return {"hostTotal": len(hosts)}
```

只适用于同步视图；不要在 `async def` 里调用。

#### 写法 C：独立线程桥（上下文不确定时的保险写法）

当你不确定视图跑在哪个线程/是否有事件循环时，用协程工厂 + 独立线程：

```python
import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any


def run_async(factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """同步函数里执行协程。factory 必须返回协程，而不是协程对象。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 当前线程没有事件循环：直接跑
        return asyncio.run(factory())

    # 当前线程已有事件循环：换一个线程，避免 asyncio.run 嵌套
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # 原样抛回调用线程
            box["error"] = exc

    thread = threading.Thread(target=target, name="module-api-bridge", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
```

使用：

```python
@ctx.route("/count")
def count():
    return run_async(_collect)
```

注意：

- 必须传**工厂**：如果先创建协程再丢给别的线程，协程可能绑定到错误的事件循环；
- 每个请求起一个线程，成本不低；高 QPS 场景用 `ThreadPoolExecutor(max_workers=4)` 复用；
- `thread.join()` 会给请求加超时风险；SDK 本身应保证返回，必要时自己加超时/熔断。

#### 写法 D：async 内部调用阻塞代码用 `run_blocking`

```python
def _blocking_read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


async def _load():
    data = await bastion.util.run_blocking(_blocking_read, "/tmp/x.bin")
    return len(data)
```

`run_blocking` 会把 `fn` 放到带 Flask app context 的 `module-io` 线程池执行。它**不是**用来包协程的：`fn` 必须是普通同步函数。

#### 写法 E：并行 SDK 调用

```python
import asyncio
from autoops import bastion


async def _stats():
    hosts, sessions = await asyncio.gather(
        bastion.hosts.list(),
        bastion.sessions.list_active(),
    )
    return len(hosts), len(sessions)
```

#### 反模式清单

| 反模式 | 后果 |
| --- | --- |
| `bastion.hosts.list()` 忘记 `await` | 拿到 coroutine，逻辑不执行，日志出现 `coroutine was never awaited` |
| 在 `async def` 视图里 `asyncio.run(...)` | `RuntimeError: asyncio.run() cannot be called from a running event loop` |
| 先创建协程再交给另一个线程 `asyncio.run` | 协程可能绑定错误 loop，`RuntimeError: ... attached to a different loop` |
| 在 `async def` 里直接 `Host.query...` | 阻塞事件循环，宿主所有模块/请求变慢 |
| 在异步代码里 `time.sleep()` | 同上；用 `await asyncio.sleep()` 或 `run_blocking` |
| 用 `_sync` 版本包在 `async def` 里 | 阻塞/死锁 |

<a id="sec-5-5"></a>
### 5.5 凭证：完整示例与安全提醒

```python
import asyncio
from autoops import ModuleApiError, bastion


async def _credential_summary(host_id: int, account_id: int | None):
    # 1) 先确认资产存在，才能给「找不到资产」一个可读的 404
    host = await bastion.hosts.get(host_id)
    if not host:
        raise ModuleApiError(f"资产不存在：{host_id}", 404)

    # 2) 异步取凭证；SDK 自动写一条 module_credential 审计
    credentials = await bastion.credentials.get(host_id, account_id=account_id)
    try:
        # 3) 只回传可公开的元数据
        return {
            "hostId": host_id,
            "accountId": credentials.account_id,
            "username": credentials.username,
            "passwordLength": len(credentials.password or ""),
        }
    finally:
        # 显式断开引用；Python 字符串无法真正擦除内存，但能缩短明文驻留时间
        credentials = None


def setup(ctx):
    @ctx.route("/credential-sample", methods=("GET",))
    def credential_sample():
        from flask import request
        raw = (request.args.get("host_id") or "").strip()
        try:
            host_id = int(raw)
        except ValueError:
            raise ModuleApiError("host_id 必须是正整数", 400) from None
        return asyncio.run(_credential_summary(host_id, None))
```

**安全提醒**：

1. **明文只进内存**。不要写日志、不要 `print`、不要回前端、不要写 `data_dir`、不要塞进异常消息。日志/审计 detail 里只放 `host_id`、`account_id`、用户名、长度。
2. **不要缓存**。`credentials.get()` 每次都会解密并写审计；把明文缓存到全局变量会长期扩大泄露面，也绕过了「每次取用可审计」的语义。
3. **Python 的字符串不可控**。你无法可靠地 zeroize；能做的是缩短生命周期、避免拷贝、避免进入日志/序列化。
4. **只有必要的人才该取**。`ctx.route` 的 `permission` 只控制「能不能用模块」，不控制「能不能看这台主机」。按资产敏感的接口必须自己校验当前用户对 `host_id` 的授权（见 §1.3、§11 Q11）。
5. **不要在页面里显示密码**。即使是密码框，也意味着明文进了 DOM/浏览器内存；示例只显示长度。
6. **密钥登录时 `password` 可能为空**。不要用 `len(password) > 0` 判断账号是否可登录；用 `auth_type`/`private_key` 语义。
7. **`accounts.reveal_password` / `reveal_private_key` 同样危险**，且也会写审计。能不用就不用；必须用时遵循上面的规则。
8. **审计 detail 不要放敏感信息**：

```python
from autoops import bastion


async def log_credential_read(host_id, account_id):
    await bastion.audit.log(
        "module_read_credential",
        target_type="host",
        target_id=host_id,
        message="module requested credential metadata",
        detail={"accountId": account_id, "purpose": "diagnose"},  # 不放 username/password
    )
```

<a id="sec-5-6"></a>
### 5.6 异常与统一信封

- `ctx.route` 包装下：返回 `dict/list/None` → 成功信封；抛 `ModuleApiError(message, status)` → 失败信封 + 指定状态码。
- `bastion.ModuleError` 是 SDK 的通用异常（契约明确 `ssh.run` 会抛它）。契约没有固定它的字段和 `status`；不要依赖 `exc.status_code` 这类未定义属性，先 `getattr(exc, "status", None)` 防御式读取，拿不到就按 502/400 兜底。
- 路由里捕获 SDK 异常时，不要把底层异常原文直接回前端（可能含主机/路径/凭据线索）；记录异常**类型**到 `ctx.logger`，返回可读的业务错误。
- 模块接口的失败信封 `code` 默认是 `ERROR`；需要更精确 code 的话，用 `ctx.blueprint()` 自己返回 `api_error` 结构。

<a id="sec-5-7"></a>
### 5.7 审计

```python
from autoops import bastion


async def audit_patrol():
    await bastion.audit.log(
        "module_action",
        target_type="host",
        target_id=42,
        message="用户从模块触发了一次巡检",
        detail={"module": "my-module", "step": "collect"},
    )
```

- `detail` 必须是可 JSON 序列化的 dict；不要放明文凭据、完整命令输出、token。
- `audit.log_command(...)` 用于命令级审计；`session_id` 当前实现先按 `sid` 字符串查、再按数值 `id` 查，两种都能用，但契约未固定口径；跨版本最稳的是传 `/api/sessions` 返回的 `sid`，并在目标宿主版本上验证。
- 审计写入永远不应让业务失败；SDK 内部会处理，但你的异常处理也不要因为审计失败而回滚业务。

<a id="sec-5-8"></a>
### 5.8 事件广播

```python
from autoops import bastion


async def broadcast_refresh():
    await bastion.events.emit(
        "my-module:refresh",
        {"at": bastion.util.now().isoformat()},
    )
```

- 这是 Socket.IO 广播；当前实现和 `ctx.emit` 一样会自动加 `module:<id>:` 前缀，并拒绝含冒号、超过 64 字符的 topic。建议只传事件名（如 `refresh`），不要自己拼模块前缀。
- 模块页面如何订阅没有标准 SDK；对于 v1，优先让页面轮询自己的 API，事件广播用于宿主侧通知。

---

<a id="sec-6"></a>
## 6. Web UI 两种模式：`iframe` 与 `js`

官方示例 [`hello-bastion`](../../modules-src/hello-bastion)、[`rdp-gateway`](../../modules-src/rdp-gateway) 和 [`js-demo`](../../modules-src/js-demo) **全部使用 `js` 模式**。其中 `hello-bastion` 和 `js-demo` 是最小教学示例，`rdp-gateway` 是带 Python 后端的完整业务模块。三者都不随宿主预装，均以 `.mod.ops_ext` 上传安装；`modules-src/` 只是源码与打包输入。

<a id="sec-6-1"></a>
### 6.1 选型对比

**默认选 `js` 模式**：它和宿主共用同一份 React/antd/主题，页面看起来就是堡垒机的一部分，还能直接复用宿主的 `request`、`message` 和 `history`。只有在你需要完全隔离（第三方脚本、独立路由、非 React 技术栈）时才用 `iframe`。

| 维度 | `iframe` | `js`（首选） |
| --- | --- | --- |
| 技术栈 | 任意：纯 HTML、Vue、React、Svelte、构建产物 | 必须是 ES Module；推荐用宿主共享的 React/antd |
| 视觉一致性 | 独立文档，字体/间距/主题都要自己对齐，容易和宿主割裂 | 直接复用宿主 antd 与主题，天然一致 |
| 页面隔离 | 独立 window/document，CSS 不互相污染 | 与宿主同 DOM，CSS 可能互相影响 |
| 获取 JWT | `localStorage.getItem('bastion_token')`（同源） | `sdk.getToken()` 或宿主 `request` 自动带 |
| 路由/URL | 独立页面，入口固定 | 挂载在宿主页面容器里，通常单页 |
| 构建要求 | 无；直接把产物放 `web/` | 无也行（`React.createElement`）；复杂 UI 建议预构建 |
| 适合 | 非 React 技术栈、完整独立控制台、需要硬隔离的第三方页面 | 官方模块、小型面板、嵌入式卡片、需要和宿主视觉统一 |
| 风险 | iframe 内 XSS 仍同源；token 可被读 | 样式/全局变量污染；React 重复副本会崩 |
| 官方示例 | 无（白皮书 §6.3 的最小骨架仅作语法参考） | `hello-bastion`、`rdp-gateway`、`js-demo` |

<a id="sec-6-2"></a>
### 6.2 静态资源与 URL 规则

模块安装后，`web/`、`images/`、`README.md` 通过以下接口免登录访问：

```text
GET /api/modules/<id>/asset/<包内相对路径>
```

例如：

```text
description.json 的 entry.web = "web/index.html"
→ 页面 URL: /api/modules/my-module/asset/web/index.html

description.json 的 nav[].entry = "web/dashboard.html"
→ 页面 URL: /api/modules/my-module/asset/web/dashboard.html
```

页面内的相对路径会以**当前资源目录**为基准解析。假设页面地址是
`/api/modules/my-module/asset/web/index.html`：

| 页面里的写法 | 实际请求 | 说明 |
| --- | --- | --- |
| `./app.js` | `/api/modules/my-module/asset/web/app.js` | ✅ 同目录资源 |
| `../images/logo.png` | `/api/modules/my-module/asset/images/logo.png` | ✅ 可以跨到 `images/` |
| `/api/modules/my-module/api/stats` | 模块 API | ✅ 推荐写成绝对路径 |
| `../api/stats` | `/api/modules/my-module/asset/api/stats` | ❌ 会被当成静态文件，404 |

限制：

- **只有 `web/`、`images/`、`README.md` 可访问**；`data/`、`modules/`、`bin/`、`description.json` 不对外。
- 响应头是 `Cache-Control: no-cache`；开发时改完文件刷新即可。
- 静态资源接口**免登录**；页面里的 API 才走 JWT。
- 入口必须在 `web/` 下；`entry: "index.html"`（缺 `web/`）会 404。
- 不要用 `?token=` 传 JWT；token 会进浏览器历史、代理日志、Referer。用 `Authorization` 头。

<a id="sec-6-3"></a>
### 6.3 `iframe` 模式完整示例

只有确需完全隔离或使用 Vue/Svelte/原生页面时才选 `iframe`。下面是一个**第三方模块的最小骨架**（不是官方示例；把 `my-module` 换成你自己的 id），存成 `web/index.html`，`description.json` 的 `entry.web` 指向它：

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>我的模块</title>
  <style>
    body { margin: 0; padding: 24px; font: 14px/1.6 "Microsoft YaHei", sans-serif; background: #f5f8fc; color: #1c2b3a; }
    .card { max-width: 720px; padding: 20px; background: #fff; border: 1px solid #dbe6f4; border-radius: 12px; }
    button { padding: 6px 14px; cursor: pointer; }
    pre { padding: 12px; overflow: auto; background: #f3f6fa; border-radius: 8px; }
    .error { color: #b42318; }
    .muted { color: #68788c; }
  </style>
</head>
<body>
  <section class="card">
    <h1>我的模块</h1>
    <p class="muted">当前页在宿主同源 iframe 中运行。</p>
    <button id="load" type="button">加载 /stats</button>
    <p id="status" class="error" role="alert"></p>
    <pre id="out">尚未加载</pre>
  </section>

  <script>
    (() => {
      const MODULE_ID = 'my-module';
      const API_BASE = `/api/modules/${MODULE_ID}/api`;
      const TOKEN_KEY = 'bastion_token';
      const statusEl = document.getElementById('status');
      const outEl = document.getElementById('out');

      const getToken = () => localStorage.getItem(TOKEN_KEY) || '';

      function goLogin() {
        const redirect = encodeURIComponent(location.pathname + location.search);
        // iframe 与宿主同源，顶层跳转是允许的；不要只在本 iframe 内打开登录页
        window.top.location.href = `/user/login?redirect=${redirect}`;
      }

      function setError(message, withLogin) {
        statusEl.textContent = '';
        const text = document.createElement('span');
        text.textContent = message;
        statusEl.appendChild(text);
        if (withLogin) {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = '重新登录';
          button.style.marginLeft = '8px';
          button.addEventListener('click', goLogin);
          statusEl.appendChild(button);
        }
      }

      async function api(path, options = {}) {
        const token = getToken();
        if (!token) {
          const error = new Error('本地没有 bastion_token，可能尚未登录。');
          error.status = 401;
          throw error;
        }

        const response = await fetch(API_BASE + path, {
          ...options,
          headers: {
            Accept: 'application/json',
            ...(options.headers || {}),
            Authorization: `Bearer ${token}`,
          },
          credentials: 'same-origin',
          cache: 'no-store',
        });

        let body = null;
        try {
          body = await response.json();
        } catch (_) {
          body = null;
        }

        if (response.status === 401) {
          const error = new Error('登录状态已失效。');
          error.status = 401;
          throw error;
        }
        if (response.status === 403) {
          const error = new Error((body && body.message) || '当前账号没有该权限。');
          error.status = 403;
          throw error;
        }
        if (!response.ok || !body || body.success !== true) {
          const error = new Error((body && body.message) || `请求失败（HTTP ${response.status}）`);
          error.status = response.status;
          throw error;
        }
        return body.data;
      }

      async function load() {
        const button = document.getElementById('load');
        button.disabled = true;
        statusEl.textContent = '';
        try {
          const data = await api('/stats');
          outEl.textContent = JSON.stringify(data, null, 2);
        } catch (error) {
          if (error.status === 401) {
            setError(error.message, true);
          } else if (error.status === 403) {
            setError(error.message, false);
          } else {
            setError(error.message || '请求失败', false);
          }
        } finally {
          button.disabled = false;
        }
      }

      document.getElementById('load').addEventListener('click', load);
      load();
    })();
  </script>
</body>
</html>
```

要点：

- iframe 是**同源**的，所以能读宿主的 `localStorage.bastion_token`；这也是它最大的风险面（token 对模块页面可见）。
- 401 要跳**顶层**登录页，不要只把登录页加载进 iframe；登录成功后宿主会回到原页面。
- 所有动态文本用 `textContent`，不要用 `innerHTML` 拼后端数据。
- 模块 API 失败时优先展示后端信封里的 `message`，不要只显示 HTTP 状态码。

<a id="sec-6-4"></a>
### 6.4 `js` 模式完整示例

`js` 模式的入口是一个 ES Module，例如 `web/dashboard.js`。宿主会：

1. 把模块的入口脚本以 `<script type="module" src="/api/modules/<id>/asset/web/dashboard.js">` 注入页面；
2. 提供共享运行时 `window.AutoOpsSDK = { React, ReactDOM, antd, request, getToken, message, t, history }`；
3. 调用 `window.AutoOpsModules[<id>].mount(el, sdk)`，或在支持时使用 ESM 默认导出的 `mount/unmount`；
4. 离开页面时调用 `unmount()`。

完整示例（不依赖构建，直接用 `React.createElement`）：

```js
// web/dashboard.js
const MODULE_ID = 'my-module';
const API = `/api/modules/${MODULE_ID}/api`;

const sdk = window.AutoOpsSDK;
if (!sdk || !sdk.React || !sdk.ReactDOM) {
  throw new Error('window.AutoOpsSDK 未注入：当前宿主版本可能不支持 js 模式');
}

const { React, ReactDOM } = sdk;
const { useEffect, useRef, useState, useCallback } = React;
const h = React.createElement;

function describeError(error) {
  const status = error && error.response ? error.response.status : 0;
  const body = (error && error.response && error.response.data) || {};
  if (status === 401) return '登录状态已失效，请重新登录。';
  if (status === 403) return body.message || '当前账号没有使用该模块的权限。';
  if (status) return body.message || `请求失败（HTTP ${status}）`;
  return '网络异常，或后端没有响应。';
}

function App({ sdk: host }) {
  const { antd, request } = host;
  const { Alert, Button, Card, Space, Statistic, Typography } = antd;
  const [stats, setStats] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const timerRef = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      // request 由宿主注入，默认会带 JWT、走宿主错误处理；返回解析后的 JSON 信封
      const body = await request(`${API}/stats`, { method: 'GET' });
      setStats((body && body.data) || null);
    } catch (error) {
      setError(describeError(error));
    } finally {
      setLoading(false);
    }
  }, [request]);

  useEffect(() => {
    load();
    timerRef.current = window.setInterval(load, 30000);
    return () => {
      if (timerRef.current) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [load]);

  return h(
    'div',
    { style: { padding: 24 } },
    h(
      Card,
      {
        title: h(Typography.Text, { strong: true }, '我的模块'),
        extra: h(Button, { type: 'primary', loading, onClick: load }, '刷新'),
      },
      error
        ? h(Alert, {
            type: 'error',
            showIcon: true,
            message: error,
            style: { marginBottom: 16 },
          })
        : null,
      h(
        Space,
        { size: 40 },
        h(Statistic, { title: '主机总数', value: (stats && stats.hostTotal) || 0 }),
        h(Statistic, { title: '在线会话', value: (stats && stats.activeSessions) || 0 }),
      ),
      h(
        Button,
        {
          style: { marginTop: 16 },
          onClick: () => {
            if (host.history && typeof host.history.push === 'function') {
              host.history.push('/system/settings');
            }
          },
        },
        '打开参数设置',
      ),
    ),
  );
}

let root = null;

export function mount(el, injectedSdk) {
  root = injectedSdk.ReactDOM.createRoot(el);
  root.render(injectedSdk.React.createElement(App, { sdk: injectedSdk }));
}

export function unmount() {
  if (root) {
    root.unmount();
    root = null;
  }
}

const api = { mount, unmount };
window.AutoOpsModules = window.AutoOpsModules || {};
window.AutoOpsModules[MODULE_ID] = api;
export default api;
```

要点与坑：

- **不要自己打包 React/ReactDOM/antd**。同一页面出现两份 React 会导致 hooks 报错、antd 上下文失效。用 `window.AutoOpsSDK`。
- `request` 是宿主的 umi-request 口径函数：默认自动带 JWT，4xx/5xx 会抛异常；错误对象通常有 `error.response.status`、`error.response.data`。如果宿主全局错误处理会跳登录页，你的 `catch` 可能只看到跳转前的短暂状态，按实际行为处理。
- `t` 是宿主 i18n 函数（宿主只保证有它，不保证包含你模块自己的中英文词条）。模块自有文案建议直接写中文，或自带一个简单字典；不要指望 `t('My Module')` 能翻译成中文。
- `getToken()` 只在需要显示/调试 token 时用；正常请求用 `request`，不要手工拼 `Authorization` 再走一遍宿主拦截器。
- 页面内跳转用 `sdk.history.push('/system/settings')`（或 `/system/modules`），不要用 `window.location` 整页刷新；`history` 由宿主路由提供，模块卸载后不要再调用。
- **UI 只用宿主注入的 antd**：不写自定义 CSS、不做渐变/Hero、不自己画页面标题。标题、面包屑和官方/实验性标签由宿主的 `PageContainer` 提供；模块只返回内容卡片（`Card`/`Statistic`/`Descriptions`/`Table` 等），这样才和堡垒机其它页面一致。
- `unmount()` 必须清理定时器、事件监听、`AbortController`、React root；否则切换页面后仍会轮询/报错。
- `mount` 只操作传入的 `el`，不要 `document.body.appendChild` 或改宿主全局布局。
- 如果你用 JSX/TypeScript，先在本地构建成 ESM 再放 `web/`；构建时把 `react`、`react-dom`、`antd` 标为 external 并替换为 `window.AutoOpsSDK`，或者干脆不用构建器。

<a id="sec-6-5"></a>
### 6.5 共享运行时与构建建议

| 依赖 | 来源 | 建议 |
| --- | --- | --- |
| React / ReactDOM | `window.AutoOpsSDK` | 不打包；版本跟随宿主（当前 React 19） |
| antd | `window.AutoOpsSDK` | 不打包；使用宿主注入的组件与版本 |
| request | `window.AutoOpsSDK` | 用宿主请求，自动带 token/统一错误处理 |
| `getToken` | `window.AutoOpsSDK` | 读取 `bastion_token`；注意同源 XSS 风险 |
| `message` | `window.AutoOpsSDK` | 轻量提示；不要用它展示敏感数据 |
| `t` | `window.AutoOpsSDK` | 宿主词条有限；模块自有文案自备 |
| `history` | `window.AutoOpsSDK` | 宿主 SPA 路由；用 `history.push(path)` 跳转，不要整页刷新 |

构建方案：

1. **不构建**（推荐给小型面板）：直接用 `React.createElement` + `sdk.antd`，把 `.js` 放进 `web/`。本文示例即为该方案。
2. **预构建**：用 esbuild/Rollup 产出 ESM；把 React/antd 标为 external，构建后手动改成从 `window.AutoOpsSDK` 取；不要产出带 `import React from 'react'` 的裸模块（浏览器没有 import map 会直接失败）。
3. **iframe + 任意构建**：最自由。Vue/React/Svelte 产物都行，放 `web/`；注意 base path 要设成相对路径（`./`），否则资源 404。

<a id="sec-6-6"></a>
### 6.6 UI/安全约定

- 页面只占宿主内容区，不要试图隐藏/覆盖宿主侧边栏、顶栏、登录页。
- **`js` 模式只用宿主注入的 antd 组件**（`Card`/`Statistic`/`Descriptions`/`Table`/`Tag`/`Button`/`Space`/`Alert` 等），不自带 CSS、不写渐变或大 Hero、不自己画页面标题。标题、面包屑和版本/官方/实验性标签都由宿主的 `PageContainer` 提供；模块返回的根节点只放内容卡片。
- 不要加载第三方 CDN 脚本；堡垒机可能在内网离线运行，且第三方脚本能读同源 `bastion_token`。
- 不要把 token 写进 URL、日志、`postMessage` 给不可信窗口。
- 仅 `iframe` 模式：跳宿主页面用 `window.top`，且仅限登录等明确场景；不要操纵宿主 DOM。`js` 模式用 `sdk.history.push()`。
- 如果确实需要局部微调，用 antd 的 `style` 属性做小范围间距/宽度调整，不要注入全局 `<style>` 或覆盖 `.ant-*` 类名。
- 移动端/窄屏布局自己负责；宿主内容区宽度不固定。
- 任何后端返回的字符串进 DOM 前用 `textContent`；必须用 `innerHTML` 时先转义。

---

<a id="sec-7"></a>
## 7. 往「参数设置」加配置项

<a id="sec-7-1"></a>
### 7.1 声明

在 `description.json` 里写 `settings[]`：

```json
{
  "settings": [
    {
      "key": "enabled",
      "label": { "zh": "启用采集", "en": "Enable collection" },
      "type": "bool",
      "default": "1",
      "description": { "zh": "关闭后不再采集", "en": "Turn off collection" }
    },
    {
      "key": "interval_seconds",
      "label": { "zh": "采集间隔（秒）", "en": "Interval (seconds)" },
      "type": "int",
      "default": "60",
      "min": 10,
      "max": 3600
    },
    {
      "key": "endpoint",
      "label": { "zh": "上报地址", "en": "Endpoint" },
      "type": "string",
      "default": ""
    }
  ]
}
```

安装/加载后，宿主会把它们注册进设置系统，键名为：

```text
mod.<moduleId>.<key>
# 例：mod.my-module.enabled
#     mod.my-module.interval_seconds
#     mod.my-module.endpoint
```

它们会自动出现在「系统设置 → 参数设置」，分组名用模块显示名（中文优先）。

<a id="sec-7-2"></a>
### 7.2 键名、类型与校验

| 类型 | 存储形式 | 界面 | 校验/注意 |
| --- | --- | --- | --- |
| `bool` | `"1"` / `"0"` | 开关 | 写入时归一化；读取时不要直接 `bool("0")`（这是 `True`） |
| `int` | 十进制字符串 | 数字输入 | `min`/`max` 由宿主校验；模块仍要自己兜底（旧数据/直接改库） |
| `string` | 原样字符串 | 文本输入 | 可为空；长度限制由模块自己控制 |

- `key` 正则：`^[a-zA-Z][a-zA-Z0-9_]{0,39}$`；不要带 `mod.` 前缀。
- `default` 建议统一写字符串；`bool` 可写 `"1"`/`"0"` 或 `true`/`false`，宿主会归一化。
- `min`/`max` 只对 `int` 有意义；不要给 `string` 写长度限制（契约没有定义）。
- 声明期校验（当前实现）：`bool` 默认值归一化为 `"1"`/`"0"`；`int` 默认值必须是整数且落在 `min`/`max` 内；`string` 默认值 ≤1024 字符；`min`/`max` 必须是整数且 `min <= max`。不满足会在打包/安装时失败，而不是等到运行期。
- 设置值**不是秘密存储**：`GET /api/settings` 对有 `setting:view` 的用户可见，数据库里也是明文 JSON。不要把 API Key、口令放设置项；需要秘密时让管理员用环境变量/外部密钥管理，或明确告知风险。

<a id="sec-7-3"></a>
### 7.3 读取与写回

```python
def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def setup(ctx):
    enabled = _as_bool(ctx.get_setting("enabled", "1"), True)
    interval = _as_int(ctx.get_setting("interval_seconds", "60"), 60, 10, 3600)
    endpoint = str(ctx.get_setting("endpoint", "") or "").strip()

    ctx.logger.info("enabled=%s interval=%s endpoint=%s", enabled, interval, endpoint)

    # 写回：会落库 + commit
    if interval < 10:
        ctx.set_setting("interval_seconds", 10)
```

注意：

- `ctx.get_setting(key, default)` 的 `key` 是**raw key**（不带 `mod.<id>.`）；宿主自动补前缀。不要自己拼完整键再传，避免双前缀。
- `ctx.set_setting` 会立即 commit；不要在每次请求/循环里写。先比较当前值，变化才写。
- 读取发生在同步上下文（`setup`/同步视图）；不要在 `async def` 里直接 `ctx.get_setting`，它是同步 DB 调用。
- 修改设置后，宿主不会主动推送事件给模块；模块下次读取才生效。页面要刷新/重新拉取。

<a id="sec-7-4"></a>
### 7.4 默认值、卸载与注意事项

- 默认值在模块安装/加载时注册；已有数据库值优先，`ensure_defaults()` 不会覆盖用户改过的值。
- 卸载会注销模块默认值与元信息；当前宿主实现还会删除 `SystemSetting` 里 `mod.<id>.*` 的行（契约未明文）。因此不要依赖「重装后保留旧设置」；需要保留就先导出 `data_dir`/设置值，重装后自己恢复。
- 升级/重装时对设置做兼容处理：新增键给默认值，删除键忽略，重命名键做迁移（在你的 `setup` 里读旧键、写新键）。
- 设置项的 `group` 由宿主按模块显示名生成，模块不需要也不能自定义分组顺序。
- 不要把「是否启用模块」和模块内部的 `enabled` 设置混淆：前者是 `state.json` 的启用状态（管理员在扩展模块页操作），后者只是模块自己的业务开关。

---

<a id="sec-8"></a>
## 8. 调试与排障

<a id="sec-8-1"></a>
### 8.1 日志在哪

**模块日志接口**：

```text
GET /api/modules/<id>/logs
权限：setting:view
返回：最近 200 行（内存环形缓冲）
```

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/my-module/logs
```

**代码里写日志**：

```python
def setup(ctx):
    ctx.logger.info("my-module %s loaded", ctx.version)
    ctx.logger.warning("something looks wrong: %s", "detail")
```

- logger 名是 `module.<id>`；会传播到宿主根 logger，和 Flask 日志一起输出到后端控制台（`python run.py` 的 `logging.basicConfig`）。
- 日志格式由宿主决定（当前包含时间、级别、logger 名）。
- 不要在日志里打凭据明文、token、完整命令输出；需要定位时打 id/长度/类型。

**加载器日志**：

- `app/modules/loader.py` 负责 `sys.modules['autoops']` 注入、import 模块、调用 `setup(ctx)`；
- 加载失败时，异常会收敛到该模块的 `errors[]`，同时 loader 会写日志；后端**不会**因为单个模块 `setup` 抛异常而退出；
- 启动时 `init_modules(app)` 在 `seed_data` 之后、Socket.IO 事件注册之前执行；它保证永不抛异常。它只加载 `instance/modules/` 下已安装且启用的模块；全新空实例直接跳过，**不会**扫描或安装 `modules-src/`。

**看模块状态**：

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:5000/api/modules/my-module
```

重点字段：`enabled`、`loaded`、`errors[]`、`installedAt`、`size`。

<a id="sec-8-2"></a>
### 8.2 模块私有数据目录

```text
bastion-backend/instance/modules/<id>/data/
```

- 这是模块**唯一**应该写入的目录；`ctx.data_dir` 已是指向它的 `Path`。
- 安装时包里的 `data/` 会复制到这里；之后运行期读写都在这里。
- 卸载会删除整个 `data/`；需要保留的状态请导出到外部存储/数据库，或在卸载前提示管理员备份。
- 调试时可以：

```powershell
Get-ChildItem -Recurse bastion-backend\instance\modules\my-module\data
Get-Content bastion-backend\instance\modules\my-module\data\state.json
```

- 写文件用「临时文件 + `os.replace`」的原子写法，避免进程崩溃留下半个 JSON（示例见 §4.7）。
- 不要在 `data_dir` 里放可执行文件/大文件；不要假设它有无限空间。

<a id="sec-8-3"></a>
### 8.3 用 curl / PowerShell 直接测 API

先登录拿 JWT（示例，不要把真实密码写进脚本历史）：

```bash
curl -s -X POST http://127.0.0.1:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<你的密码>"}'
```

响应里取 `data.token`（或按当前接口实际字段）。然后：

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/my-module/api/stats
```

PowerShell：

```powershell
$token = "<token>"
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/modules/my-module/api/stats" `
  -Headers @{ Authorization = "Bearer $token" }
```

调试顺序：

1. `GET /api/modules/<id>` 看 `loaded`/`errors`；
2. `GET /api/modules/<id>/logs` 看模块日志；
3. 直接用 curl 调模块 API（绕过页面，判断是后端还是前端问题）；
4. 检查权限：你的账号是否有 `module:<id>:use` / 该路由声明的权限；
5. 检查参数（404 可能只是 `host_id` 不存在）。

<a id="sec-8-4"></a>
### 8.4 iframe 页面排障

| 现象 | 检查 |
| --- | --- |
| 页面空白/404 | `nav[].entry`/`entry.web` 是否在 `web/` 下；资源 URL 是否是 `/api/modules/<id>/asset/web/...` |
| 静态 JS/CSS 404 | 相对路径是否按 `asset/web/` 解析；不要用 `../api/...` |
| API 401 | `localStorage.bastion_token` 是否存在且未过期；请求头是否 `Bearer ` 前缀 |
| API 403 | 当前用户是否有 `module:<id>:use` 或路由自定义权限；模块是否被禁用 |
| API 404 | URL 是否 `/api/modules/<id>/api/...`；路由是否在 `setup` 中注册；模块是否 `loaded=true` |
| 控制台 CORS 错误 | iframe 是否真的是同源（宿主代理/端口变化可能导致不同源）；不要跨域 fetch |
| 改了文件不生效 | 静态接口是 `Cache-Control: no-cache`，但仍可能被浏览器强缓存；Ctrl+F5 |
| token 为空 | iframe 是否被宿主加了 `sandbox` 且没有 `allow-same-origin`；这会让 localStorage 不可用 |

调试建议：打开浏览器 DevTools → Network，先看静态资源请求，再看 API 请求；401/403 看响应 body 的 `message`，不要只看状态码。

<a id="sec-8-5"></a>
### 8.5 js 模式排障

| 现象 | 检查 |
| --- | --- |
| 页面空白，控制台 `Unexpected token '<'` | `entry` 指到了 HTML 或 404 页面；检查 `.js` 路径与 MIME |
| `window.AutoOpsSDK` 不存在 | 宿主版本不支持 js 模式，或脚本在 SDK 注入前执行 |
| `window.AutoOpsModules[id]` 不存在 | 脚本没有执行/语法错误/模块 id 写错；检查 Network 和 Console |
| React hooks 报错/antd 样式错乱 | 自己打包了一份 React/antd；改用 `sdk.React`/`sdk.antd` |
| `mount` 报 `el` 为空 | 宿主页面没有渲染容器或时序问题；不要在模块加载前操作 DOM |
| 切换页面后仍请求 | `unmount` 没清定时器/AbortController；检查 effect cleanup |
| 请求 401 直接被全局处理 | 宿主 `request` 的 401 拦截会跳登录；这是预期行为，模块侧只做兜底 |

js 模式脚本必须是 **ES Module**（`export`/`import` 语法），并且入口路径以 `.js`/`.mjs` 结尾。把脚本放到 `web/` 下，`nav[].entry` 写 `web/dashboard.js`。

<a id="sec-8-6"></a>
### 8.6 常见错误对照表

| 错误码/现象 | 含义 | 处理 |
| --- | --- | --- |
| `MODULE_INVALID_PACKAGE` | ZIP 结构/路径/大小/JSON 非法 | 用 `pack_module.py --check` 重验；检查 `description.json` 与包根 |
| `MODULE_RESERVED_ID` | id 是保留字 | 换 id（`core`/`system`/`admin`/`modules`/`_builtin`） |
| `MODULE_EXISTS` | 同 id 已安装 | 卸载旧版或走升级流程（见 §10.3） |
| `MODULE_NOT_FOUND` | 模块不存在/已卸载 | 刷新 `/api/modules` |
| `EXPERIMENTAL_NOT_ACKED` | 实验性确认语不匹配 | 逐字手敲确认语；检查空格 |
| `MODULE_LOAD_FAILED` | 安装成功但加载失败 | 看 `/api/modules/<id>` 的 `errors[]`、`/logs`、后端 traceback |
| 401 | JWT 缺失/过期 | 重新登录；检查 `Authorization` |
| 403 | 权限不足/模块禁用 | 分配 `module:<id>:use`；检查 `enabled` |
| 404（模块 API） | 路由未注册/URL 错 | 检查 `ctx.route` 路径与 `/api/modules/<id>/api` 前缀 |
| 500（模块 API） | 视图异常/SDK 异常 | `/logs` + 后端 traceback；不要吞异常 |
| `coroutine was never awaited` | 忘写 `await` | 按 §5.4 修 |
| `asyncio.run() cannot be called from a running event loop` | 在 async 上下文里调 `asyncio.run` | 用 `await` 或线程桥 |
| 设置项不出现 | 模块未加载/`settings` 未声明 | 看 `loaded`、`errors`；重装后刷新参数设置页 |
| `No module named autoops` | 宿主 loader 未注入门面/版本不匹配 | 确认宿主实现了模块系统且版本 ≥ `minBastionVersion` |

> `POST /api/modules/upload-check` 只解析 `description.json`，**不会执行 Python**。所以「预览正常但安装后加载失败」通常是代码本身的问题，去 `/logs` 看 traceback。

---

<a id="sec-9"></a>
## 9. 打包、安装与卸载

<a id="sec-9-1"></a>
### 9.1 打包前检查

- [ ] `description.json` 的 `id`/`version` 正确，`version` 已按 semver 递增；
- [ ] `modules/__init__.py` 定义了 `setup(ctx)`，本地 `python -m py_compile` 通过；
- [ ] `web/` 里的资源使用相对路径/模块 API 绝对路径，没有 `/static/...` 之类宿主路径；
- [ ] `data/` 只放初始数据，没有运行期临时文件；
- [ ] 没有 `__pycache__`、`.git`、`node_modules`、`*.pyc`、日志、密钥、测试 dump；
- [ ] `README.md` 与 `images/` 可公开（它们免登录可读）；
- [ ] 在目标宿主版本上手工装过一次，`loaded=true`、`errors=[]`；
- [ ] 实验性模块准备好确认语文本。

<a id="sec-9-2"></a>
### 9.2 `pack_module.py` 用法

工具由后端提供：`bastion-backend/tools/pack_module.py`。命令行形态（契约 §8）：

```text
python bastion-backend/tools/pack_module.py <源码目录> [-o <输出目录>] [--check]
```

在仓库根目录执行：

```powershell
cd D:\Desktop\AutoOps

# 只校验，不打包
python bastion-backend\tools\pack_module.py modules-src\hello-bastion --check

# 打包到仓库根目录（官方包随仓库分发）
python bastion-backend\tools\pack_module.py modules-src\hello-bastion -o .
```

如果你的当前目录是 `bastion-backend/`，等价命令是：

```powershell
python tools\pack_module.py ..\modules-src\hello-bastion -o ..
```

产物：

```text
<id>-<version>.mod.ops_ext
# 例：hello-bastion-1.1.0.mod.ops_ext（就在仓库根目录）
```

工具行为（契约 §8）：

- 输入是**一个完整包目录**（含 `description.json`）；
- `--check` 只做校验（等价安装期校验），不产出 ZIP；
- 打包时排除 `__pycache__`、`.git`、`node_modules`、`*.pyc`、`.DS_Store`；
- 打包后打印 `id`/`version`/`size`/条目数，并对 ZIP 再做一次 `spec.validate`；
- 校验失败会非零退出并打印原因。

如果当前分支没有这个工具，说明后端模块化实现还没合入；不要用手工 ZIP 绕过安装校验，先对齐宿主版本。

<a id="sec-9-3"></a>
### 9.3 安装页流程与实验性确认

> 系统默认是 0 个模块，没有「内置模块」或预装入口；三个官方示例也必须走下面这条上传流程。

1. 管理员登录，进入「系统设置 → 扩展模块」；
2. 选择/拖入 `.mod.ops_ext`；
3. 前端调用 `POST /api/modules/upload-check`，展示 `description.json` 预览（名称、版本、作者、标签、说明、演示图）；
4. 若 `experimental`（或 tags 含 `experimental`），弹出 Modal，要求**手工敲**：
   ```text
   我知道这是实验性功能，使用不当的话也许会造成系统损毁
   ```
   输入不匹配则「安装」按钮不可用；粘贴不算；
5. 调用 `POST /api/modules/install`（multipart `file`，实验性模块另带 `ack`）；
6. 安装成功后模块进入列表，默认按宿主策略启用/加载；侧边栏出现 `nav[]` 入口。

安装期的错误码见 §8.6。安装失败必须回滚到「未安装」，不会留下半个模块。

<a id="sec-9-4"></a>
### 9.4 直接调安装 API（自动化场景）

```bash
# 上传预览：不落盘、不执行
curl -s -X POST "http://127.0.0.1:5000/api/modules/upload-check" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./my-module-1.0.0.mod.ops_ext"

# 安装：multipart file；实验性模块加 ack
curl -s -X POST "http://127.0.0.1:5000/api/modules/install" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./my-module-1.0.0.mod.ops_ext" \
  -F "ack=我知道这是实验性功能，使用不当的话也许会造成系统损毁"
```

- `install` 需要管理员权限；
- `ack` 只在实验性模块时必需；非实验性模块不要传；
- 成功返回归一化模块对象（同 §3.6）。

<a id="sec-9-5"></a>
### 9.5 安装/启用/禁用/卸载的内部行为

| 操作 | 宿主行为 |
| --- | --- |
| 安装 | 解析 ZIP → 校验路径/大小/条目/描述 → 原子解压到临时目录 → rename 到 `instance/modules/<id>/package` → 写 `state.json` → 注册设置默认值/导航 → 尝试加载模块 |
| 加载 | 注入 `autoops` 门面 → import `modules` 包 → 调 `setup(ctx)`；失败只影响本模块（`loaded=false` + `errors[]`） |
| 启用 | 置 `enabled=true`，必要时加载，调 `on_enable` |
| 禁用 | 置 `enabled=false`，调 `on_disable` / `teardown`，模块路由不再可用 |
| 卸载 | 先禁用 → `teardown` → 删除 `package`/`state.json`/`data` → 注销默认值/导航 |

- 安装成功后是否默认启用，由宿主策略决定；管理员可以在扩展模块页切换。
- 禁用后，模块的 `/api/modules/<id>/api/**` 应返回不可用；侧边栏入口消失。
- 卸载会**删除 `data/`**；先提醒用户备份。
- 卸载就是真卸载：不保留墓碑、没有 `restore` 接口、没有「一键重装」；要恢复就重新上传同一份 `.mod.ops_ext`。
- 正在执行的请求/后台线程不会被强行杀死；模块要在 `teardown` 里自己停。

<a id="sec-9-6"></a>
### 9.6 供应链：签名与哈希

当前契约（v1）**没有实现包签名验证**；`author: "AutoOps"` 只是元数据。实际可做的：

1. 只从可信仓库/发布渠道获取 `.mod.ops_ext`；
2. 发布方提供 SHA-256：

```powershell
Get-FileHash .\my-module-1.0.0.mod.ops_ext -Algorithm SHA256
```

3. 安装前比对哈希；企业内网可再做代码审查/沙箱扫描（扫描工具本身不影响宿主运行）；
4. 不要把 `experimental` 当安全边界；它只是「需要二次确认」的提示。

---

<a id="sec-10"></a>
## 10. 版本与兼容

<a id="sec-10-1"></a>
### 10.1 `version` 必须是严格 semver

格式：

```text
MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
```

合法：

```text
1.0.0
1.2.3
2.0.0-rc.1
1.4.0-beta.2+build.7
```

非法：

```text
v1.0.0        # 不要带 v
1.0           # 必须是三段
1.0.0.0       # 四段
1.0.0-        # 空 pre-release
01.0.0        # 数字标识不要前导零
```

- `id` 不变、`version` 递增；安装时以 `description.json` 为准，文件名不参与校验；
- 版本字符串会出现在安装页、模块列表和 `/api/modules`；不要塞日期以外的营销词。

<a id="sec-10-2"></a>
### 10.2 `minBastionVersion`

- 可选；声明你的模块最低需要的宿主版本；
- 宿主用后端配置的 `APP_VERSION` 比较（契约 §2）；
- 建议填**你实际测试过的最低版本**；不确定就从 `1.0.0` 起步，别虚高；
- 「不满足最低版本」时的具体行为（拒绝安装/加载失败/仅警告）契约没有细化；不要只依赖宿主拦截，模块自身仍应做兼容判断：

```python
def setup(ctx):
    min_version = "1.0.0"  # 与 description.json 保持一致
    ctx.logger.info("module %s requires bastion >= %s", ctx.version, min_version)
```

- 预发布版本比较（`-rc`/`+build`）语义未在契约中细化；正式发布不要依赖预发布排序。

<a id="sec-10-3"></a>
### 10.3 升级、降级与卸载

当前契约**没有定义专门的升级 API**；同 id 再次安装会遇到 `MODULE_EXISTS`。在宿主提供升级流程之前，安全做法是：

1. 通知管理员备份 `bastion-backend/instance/modules/<id>/data/`（或模块自己的导出功能）；
2. 禁用模块；
3. 卸载（会删除 `data/`，所以备份必须在前）；
4. 重新上传安装新版本（没有升级入口，也没有墓碑/一键恢复）；
5. 恢复 `data/`（如果格式兼容）；
6. 启用并验证 `loaded=true`、`errors=[]`。

注意事项：

- 设置项落在 `SystemSetting` 表；当前实现卸载时会删除 `mod.<id>.*` 行，但契约未明文，重装前仍应自行备份/迁移设置值；
- 降级同样没有专门流程；按「备份 → 卸载 → 安装旧包 → 恢复」处理，不保证数据/设置兼容；
- 任何情况下都不要改 `description.json.id` 来「升级」；那会被当成另一个模块。

<a id="sec-10-4"></a>
### 10.4 兼容性写法建议

- 只使用契约里列出的 `ctx`/SDK API；不要 import `app.modules.*` 内部实现（升级会变）。
- 对可选字段做防御式读取：`getattr(obj, "field", default)`、`item.get("field")`；不要假设 dict 一定有某个键。
- 设置/数据文件带 `schemaVersion`，在 `setup` 里做迁移：

```json
{ "schemaVersion": 2, "items": [] }
```

- 不要在 `setup` 里做重活；宿主启动会加载所有启用模块，慢模块拖慢整体启动。
- 多进程部署下每个 worker 都会加载模块；后台任务/定时器要能去重或明确接受重复。
- 升级前在目标 `APP_VERSION` 上回归：安装、启用、API、页面、设置、禁用、卸载。

---

<a id="sec-11"></a>
## 11. FAQ 与已知限制

**Q1：模块能改宿主导航以外的布局吗？**<br>
不能。契约只提供 `nav[]` 入口和 `/m/<id>/*` 内容区；不能改登录页、顶栏、侧边栏结构。JS 模式虽然同 DOM，但改宿主布局属于未支持行为。

**Q2：能新增权限码吗？**<br>
不能往宿主核心权限目录（`ALL_PERMISSIONS`）里加。可以在 `description.json.permissions` 声明模块级权限（如 `module:<id>:use`），但是否出现在角色配置页、如何授权由宿主实现决定；在宿主未接入前，非超级管理员可能拿不到该权限。

**Q3：安装页预览会执行 Python 吗？**<br>
不会。`upload-check` 只解析 `description.json`，不落盘、不 import。安装成功后才加载模块；`setup` 里的错误会体现在 `loaded=false` / `errors[]`。

**Q4：模块崩溃会影响整个堡垒机吗？**<br>
契约提供加载失败隔离和请求异常隔离：`setup` 抛异常只让本模块 `loaded=false`；路由异常只让该请求 500。但没有进程级沙箱：死循环、内存耗尽、`os._exit`、阻塞 GIL 仍可能拖垮进程。写健壮的代码，别把未捕获异常丢进后台线程。

**Q5：能 `pip install` 依赖吗？**<br>
不能。安装包不会触发依赖安装。纯 Python 库可 vendoring 到 `modules/`（注意 license/体积）；平台二进制放 `bin/` 并探测；优先只用宿主已有依赖。

**Q6：能用宿主内部模块/数据库吗？**<br>
技术上可以（进程内可信代码），但属于非契约行为，升级可能坏。优先 SDK；确需内部能力时，把 import 收敛到一处并做版本探测，且自行承担兼容风险。

**Q7：能新建数据库表吗？**<br>
没有迁移机制，不要改宿主 schema。私有状态写 `ctx.data_dir`（JSON/SQLite 文件均可）；需要跨机共享时接外部存储。

**Q8：能跑后台任务/定时器吗？**<br>
契约没有调度器 API。可以自己开线程，但必须在 `teardown` 停止；多 worker 会重复执行；不要在 `setup` 里长阻塞。

**Q9：为什么我的路由默认就要 `module:<id>:use`？**<br>
`ctx.route` 的 `permission` 缺省取该模块第一个 nav 权限，再缺省 `module:<id>:use`。想改就在装饰器上传 `permission`，但要确保该权限真的能被授予。

**Q10：SDK 会自动按当前用户授权过滤数据吗？**<br>
不会。`credentials.get()`、`hosts.list()`/`get()`、`accounts.list()`、`credentials.list()`、`sessions.list_active()` 当前实现都返回全量；`permission` 只控制「能不能用模块」，不控制「能不能看这台主机」。按资产敏感的接口必须自己在模块侧校验当前用户对 `host_id` 的授权（宿主没有在 SDK 暴露统一的 current-user/授权检查；可参考 §1.3 的说明，谨慎使用宿主内部 `g.actor`/`find_access`，并做版本兼容）。

**Q11：为什么侧边栏看不到模块？**<br>
依次检查：模块 `enabled=true`、`loaded=true`、`errors=[]`；当前账号有 `nav[].permission`；`GET /api/modules/nav` 是否返回该项；前端是否已刷新 `initialState`。

**Q12：为什么参数设置里没有我的项？**<br>
模块未加载或 `settings` 未声明；设置默认值在加载/安装时注册。看 `/api/modules/<id>` 与 `/logs`，重装后刷新页面。

**Q13：静态资源要登录吗？**<br>
不要。`/api/modules/<id>/asset/**` 免登录，只允许 `web/`、`images/`、`README.md`。不要放秘密。

**Q14：能返回文件/流/自定义状态码吗？**<br>
`ctx.route` 只适合返回 dict/list/None + `ModuleApiError` 状态码。文件/流/自定义响应走 `ctx.blueprint()` + Flask 原生响应，自己按统一信封约定处理错误。

**Q15：能加 i18n 吗？**<br>
iframe 页面完全自己做。JS 模式的 `t` 是宿主词条函数，不保证包含你的模块文案；模块自带字典或直接写中文。`description.json` 的 `name`/`title`/`label` 支持 `{zh,en}`。

**Q16：能访问外网吗？**<br>
能，没有网络沙箱。请自行处理超时、代理、TLS、失败重试，并注意数据外发合规。

**Q17：能发 Socket.IO 事件吗？**<br>
能。`ctx.emit(topic, payload)` 与 `bastion.events.emit(topic, payload)` 当前都会广播 `module:<id>:<topic>`，topic 不能含冒号、最长 64 字符。模块前端如何订阅没有标准 SDK；v1 建议轮询自己的 API。

**Q18：`requires` 会自动安装依赖吗？**<br>
契约只定义了声明字段，没有定义自动安装/排序/启用语义。不要假设依赖一定存在；`setup` 里探测并降级，或把依赖模块写进部署文档。

**Q19：模块 id 能改吗？**<br>
不要。id 决定 URL、数据目录、设置键、权限码；改了等同新模块，旧数据和授权全丢。

**Q20：卸载会删什么？**<br>
`package/`、`state.json`、`data/`；当前实现还会删除 `SystemSetting` 里的 `mod.<id>.*`，但契约未明文。卸载前备份 `data/` 与需要的设置值，并告知用户。

**Q21：为什么安装成功但 `loaded=false`？**<br>
`setup` 抛异常、`import` 失败、依赖缺失、`autoops` 门面版本不匹配。看 `/api/modules/<id>` 的 `errors[]` 与 `/logs`。

**Q22：能改宿主已有接口行为吗？**<br>
技术上能（monkeypatch），但不要。那会让升级、审计、安全边界全部失控；用模块自己的路由前缀。

**Q23：模块前端能拿到当前用户信息吗？**<br>
契约没有给前端模块专用 SDK；iframe 可以调宿主 `/api/auth/me`（带 JWT），js 模式用 `sdk.request('/api/auth/me')`。注意这依赖宿主接口稳定性。

**Q24：开发时前端 8000、后端 5000，跨域吗？**<br>
开发环境通常由 Umi 代理 `/api`；生产同源。若你直接访问后端端口，iframe 与 API 可能不同源，token/fetch 都会失败；以宿主实际部署方式为准。

---

<a id="sec-12"></a>
## 12. hello-world 全流程走查

本章用仓库里的真实示例 [`modules-src/hello-bastion/`](../../modules-src/hello-bastion/) 走一遍「源码 → 校验 → 打包 → 安装 → 页面 → 卸载」。这个示例是 **`js` 模式**，页面只负责内容卡片，标题/面包屑/实验性标签由宿主 `PageContainer` 提供。

<a id="sec-12-1"></a>
### 12.1 源码清单

```text
modules-src/hello-bastion/
├── description.json        # 元数据、nav（type=js）、settings、permissions
├── modules/
│   └── __init__.py         # setup(ctx)、/stats、/credentials-sample、生命周期钩子
├── web/
│   └── index.js            # js 模式入口：export mount/unmount + React.createElement + antd
├── images/
│   └── demo-1.png          # 安装页演示图（800×450）
└── README.md
```

| 文件 | 作用 |
| --- | --- |
| [`description.json`](../../modules-src/hello-bastion/description.json) | `version: "1.1.0"`、`entry.web: "web/index.js"`、`nav[0].type: "js"`、3 个 settings、实验性标记 |
| [`modules/__init__.py`](../../modules-src/hello-bastion/modules/__init__.py) | `setup(ctx)`、`/stats`、`/credentials-sample`、async 桥、生命周期钩子 |
| [`web/index.js`](../../modules-src/hello-bastion/web/index.js) | js 页面：4 张 antd `Card`，用 `React.createElement` 拼装，无 JSX、无 import、无自定义 CSS |
| [`images/demo-1.png`](../../modules-src/hello-bastion/images/demo-1.png) | 800×450 安装页演示图 |
| [`README.md`](../../modules-src/hello-bastion/README.md) | 该示例的安装/接口/排障说明 |

对应关系：

- `nav[]` → 侧边栏「Hello Bastion」；`type: "js"`、`entry: "web/index.js"`、`path: "/"`（完整 URL 是 `/m/hello-bastion/`）；页面标题、面包屑和「实验性」标签由宿主 `PageContainer` 渲染，模块自己**不要**再画 Hero/标题。
- `settings[]` → `show_greeting`（bool）、`refresh_seconds`（int, 5–600）、`greeting_text`（string）。
- `/stats` → `asyncio.gather(bastion.hosts.list(), bastion.sessions.list_active())` + `ctx.get_setting()`。
- `/credentials-sample` → `await bastion.credentials.get(host_id, account_id=...)`，只返回 `username` + `passwordLength`。
- `web/index.js` → 用 `window.AutoOpsSDK` 的 `React`/`ReactDOM`/`antd`/`request`/`history`，通过宿主 `request` 调两个模块 API；`sdk.history.push('/system/settings')` 跳参数设置，`unmount()` 清理 root。

<a id="sec-12-2"></a>
### 12.2 从校验到安装

全新实例初始是 **0 个模块**；`hello-bastion` 也一样，要从源码打包、再上传安装，不会因为仓库里有 `modules-src/` 就自动出现。

**Step 1：语法/JSON 校验**（不需要宿主环境）：

```powershell
cd D:\Desktop\AutoOps
python -m py_compile modules-src\hello-bastion\modules\__init__.py
python -c "import json; json.load(open(r'modules-src/hello-bastion/description.json', encoding='utf-8')); print('json ok')"
node --check modules-src\hello-bastion\web\index.js
```

`node --check` 用于确认 `web/index.js` 没有语法错误。老版本 Node 若把 `export` 当 CommonJS 报错，把文件临时复制成 `.mjs` 再检查：`Copy-Item modules-src\hello-bastion\web\index.js "$env:TEMP\hello-check.mjs"; node --check "$env:TEMP\hello-check.mjs"`。示例没有 TypeScript/JSX，不需要 `tsc`。

**Step 2：打包校验**：

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion --check
```

**Step 3：打包**：

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion -o .
```

预期产物（仓库根目录）：

```text
hello-bastion-1.1.0.mod.ops_ext
```

**Step 4：安装**：

1. 管理员登录前端；
2. 「系统设置 → 扩展模块」上传仓库根目录的 `hello-bastion-1.1.0.mod.ops_ext`；
3. 看到演示图/名称/版本/标签；
4. 因为 `experimental: true`，手敲确认语；
5. 安装成功，模块列表出现 `hello-bastion`，`loaded=true`。

**Step 5：确认权限**：

- 超级管理员（`*`）默认可用；
- 普通账号需要 `module:hello-bastion:use`；如果角色配置页没有该权限项，用超级管理员先验证，或等宿主把模块权限接入角色页。

<a id="sec-12-3"></a>
### 12.3 接口实测

侧边栏进入「Hello Bastion」，或直接 curl：

```bash
# 主机总数 / 在线会话 / 三个设置值
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/hello-bastion/api/stats
```

预期（数值随环境变化）：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "module": { "id": "hello-bastion", "version": "1.1.0" },
    "generatedAt": "2026-10-23T15:04:05.123456+00:00",
    "hostTotal": 12,
    "activeSessions": 3,
    "settings": {
      "showGreeting": true,
      "refreshSeconds": 30,
      "greetingText": "Hello, Bastion!"
    }
  }
}
```

凭证示例（只返回用户名与长度）：

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:5000/api/modules/hello-bastion/api/credentials-sample?host_id=1"
```

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "hostId": 1,
    "accountId": 2,
    "username": "root",
    "passwordLength": 14
  }
}
```

`host_id=99999` 时：

```json
{
  "success": false,
  "code": "ERROR",
  "message": "资产不存在或当前账号不可见：host_id=99999",
  "data": null
}
```

> 示例代码把「资产不存在/凭证读取失败」映射成 404；生产模块请按你的业务区分 400/403/404/502，并记录可排查的日志。

<a id="sec-12-4"></a>
### 12.4 页面与设置

宿主用 `PageContainer` 包住模块内容，标题为「Hello Bastion」，`extra` 显示版本、官方、实验性标签；`web/index.js` 的 `mount()` 只向传入的 `el` 里渲染内容。页面是四张 antd `Card`：

1. **运行概况**：`Alert` 展示问候语（来自 `greeting_text`，`show_greeting=false` 时隐藏），下面 4 个 `Statistic`：主机总数、在线会话、自动刷新间隔、最近刷新；`extra` 有「每 N 秒自动刷新」标签和「立即刷新」按钮。
2. **模块设置**：`Descriptions` 显示三个设置当前值，`Table` 列出 `mod.hello-bastion.show_greeting` / `refresh_seconds` / `greeting_text` 的类型和默认值；「去参数设置修改」按钮用 `sdk.history.push('/system/settings')` 做宿主内跳转。
3. **异步 SDK 演示**：两个 `InputNumber`（资产 ID 必填、账号 ID 可选）+「读取凭证摘要」按钮；通过宿主 `request` 调 `/credentials-sample`，结果用 `Descriptions` 只显示 `hostId`、`accountId`、`username`、`passwordLength`。
4. **关于本示例**：`Descriptions` 展示模块 id、版本（来自 `/stats` 的 `module.version`）、页面模式、API 前缀、作者、数据时间；按钮用 `sdk.history.push('/system/modules')` 打开扩展模块管理，另一个按钮调用 `sdk.message.success()`。

设置行为：

- 页面加载后自动请求 `/stats`；之后按 `refresh_seconds` 轮询（最小 5 秒），改设置并保存后下一轮生效，也可点「立即刷新」；
- 「参数设置」里改三个值 → 保存 → 回模块页等下一轮轮询或点「立即刷新」；
- 401 先重新登录（宿主的 `request` 可能已按全局策略跳登录）；403 显示「没有使用该模块的权限」；其它错误展示后端信封的 `message`；
- 模块本身不写 CSS：没有渐变、Hero、自造标题；所有视觉由 antd 和宿主 `PageContainer` 提供。

<a id="sec-12-5"></a>
### 12.5 排障与卸载

**看状态**：

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/hello-bastion
```

**看日志**：

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:5000/api/modules/hello-bastion/logs
```

**常见问题**：

- 页面 401：登录状态已失效，重新登录；`js` 模式下宿主的 `request` 可能已按全局错误策略跳登录；
- 页面 403：账号没有 `module:hello-bastion:use`；
- `/stats` 500：看日志；多半是宿主 `autoops.bastion` 门面未实现/未注入；
- `credentials-sample` 404：`host_id` 不存在或当前资产对模块不可见；
- `python -m py_compile` 通过但加载失败：看 `errors[]` 的 traceback，常见是 `from autoops import ModuleApiError` 的导出位置与宿主实现不一致（见附录 A 的契约待补项）。

**卸载**：

1. 扩展模块页 → 禁用 → 卸载；
2. 宿主会删除 `package/`、`state.json`、`data/`；
3. 确认侧边栏入口消失；
4. 如需重装，重新上传包即可。

<a id="sec-12-6"></a>
### 12.6 从这个示例继续扩展

- 加接口：在 `modules/__init__.py` 的 `_register_routes(ctx)` 里加一个 `@ctx.route("/my-endpoint")`，按 §5.4 桥接 async SDK；
- 加设置：在 `description.json.settings[]` 加一项，`setup` 里 `ctx.get_setting("new_key", default)`；
- 改页面：直接改 `web/index.js` 里的 `React.createElement` 树，照四张 `Card` 的结构加一张；入口仍是 `entry.web: "web/index.js"`、`nav[].type: "js"`，不需要改后端；
- 加一张新卡片：先从 `antd` 解构需要的组件，再在 `Space` 里加 `e(Card, ...)`；不要在模块里写 CSS，标题/布局交给宿主；
- 加资产级鉴权：在敏感路由里校验当前用户对 `host_id` 的授权（见 §1.3/Q10），不要只依赖模块级 permission；
- 加审计：在关键操作后调用 `bastion.audit.log(...)`，detail 不放明文。

---

<a id="sec-a"></a>
## 附录 A：本白皮书依赖的契约条款

本白皮书按 [`CONTRACT.md`](./CONTRACT.md) v1 编写。主要依赖：

| 契约条款 | 本白皮书使用点 |
| --- | --- |
| §0 设计原则 | 文件系统即真相、声明式优先、可信代码、静态资源免登录、失败要响 |
| §1 包格式 | ZIP 布局、目录职责、安装期路径/大小/条目校验、安装期不 import |
| §2 `description.json` | 全字段、id 正则/保留字、author 官方判定、tags/experimental、烧杯确认语、归一化对象、标签色板 |
| §3 后端运行态 | `instance/modules/<id>/{package,state.json,data}`、`init_modules` 顺序与永不抛异常、设置键 `mod.<id>.<key>`、`GET/PUT /api/settings` 的 group/类型校验 |
| §4 Python 入口 | `setup`/`teardown`/`on_enable`/`on_disable`、`ctx` 成员、`ctx.route` 包装与权限缺省、`ctx.blueprint`、`ctx.on`/`ctx.emit` |
| §5 SDK | 全部方法签名/返回类型、全异步、`_sync` 版本、`credentials.get` 审计、`run_blocking`、`ModuleError` |
| §6 REST 接口 | `/api/modules*` 路由、权限、错误码、静态资源白名单、日志/上传预览 |
| §7 前端契约 | iframe/js 两种模式、`window.AutoOpsSDK`、`window.AutoOpsModules`、`/m/:moduleId/*`、安装页流程 |
| §8 打包工具 | CLI、`--check`、输出目录、排除规则、打包后复验 |
| §9 官方模块 | `modules-src/rdp-gateway/` 首个官方模块的定位；`hello-bastion`、`rdp-gateway`、`js-demo` 均使用 `js` 模式 |

**已知契约待补项**（开发时请与宿主实现对齐，白皮书不对未定义行为做承诺）：

1. `ModuleApiError` 的导入路径（当前实现从 `autoops` 根导出，与示例一致；契约只写了类名，建议写死）。
2. 宿主 `APP_VERSION` 的来源与 `minBastionVersion` 不满足时的具体行为。
3. `credentials.list()` 的 `CredentialMeta` 字段、`hosts.list()`/`accounts.list()`/`sessions.list_active()` 的 dict schema。
4. `_sync` 版本是否覆盖所有方法、命名是否严格为 `<method>_sync`。
5. `ctx.blueprint()` 由 loader 在 `setup` 后统一收集（当前实现），以及蓝图名自动加 `module_<id>_` 前缀的规则；契约应写明「不要手动 register」。
6. `ctx.route` 是否支持 `async def` 视图（本白皮书按同步视图写）。
7. `ctx.on("install")` 在「安装期不 import」前提下的触发时机。
8. `permissions` 声明如何合并进角色配置页（非超级管理员如何获得 `module:<id>:use`）。
9. 升级（同 id 新版本）是否/如何支持；`MODULE_EXISTS` 的替代流程。
10. 卸载时 `SystemSetting` 行是否删除；当前实现会删，但契约未明文。
11. `audit.log_command` 的 `session_id` 是 `SessionRecord.id` 还是 `sid` 字符串（当前实现两者都接受，契约应固定一种）。
12. `events.emit` 是否自动加 `module:<id>:` 前缀（当前实现与 `ctx.emit` 一致，会加并限制 topic 字符）。
13. 模块接口的「当前登录用户」上下文与按资产授权检查能力（SDK 目前未暴露）。
14. js 模式的 `t` 词条范围、`request` 的错误/401 全局行为是否与宿主页面一致。
15. 模块日志接口的响应 `data` 结构（数组还是 `{lines: [...]}`）。
16. `ctx.on(event, handler)` 的参数形式（当前实现要求传 handler，不支持当装饰器）与 `ctx.set_setting` 的返回类型/未声明 key 行为。
17. `ctx.emit` 的 topic 限制（当前实现禁止冒号、限长 64），契约应写明。
18. 契约 §2 的归一化 nav 示例漏了 `entry`；前端加载 iframe/js 需要它，当前实现会保留，建议补进契约。

---

<a id="sec-b"></a>
## 附录 B：发布前自查清单

**包与元数据**

- [ ] `description.json` 可被 `json.load` 解析，UTF-8、无 BOM；
- [ ] `id` 匹配 `^[a-z][a-z0-9-]{2,39}$`，不是保留字；
- [ ] `version` 是严格 semver，且比上一版大；
- [ ] `name`/`nav[].title`/`settings[].label` 有 `{zh, en}`；
- [ ] `entry.web` / `nav[].entry` 都在 `web/` 下；
- [ ] `images` 都在 `images/` 下且文件存在；
- [ ] 实验性标记与实际一致，确认语文本正确；
- [ ] `permissions`/`requires` 没有笔误。

**代码**

- [ ] `python -m py_compile modules/__init__.py` 通过；
- [ ] 所有 SDK 调用都 `await`，或使用 `_sync` 于同步上下文；
- [ ] 没有在 `async def` 里做同步 DB/网络/`time.sleep`；
- [ ] `setup` 快速、幂等；`teardown` 停线程/关连接；
- [ ] 路由权限明确，敏感接口有资产级授权校验；
- [ ] 凭据明文不写日志、不回前端、不落盘；
- [ ] 敏感操作写审计，`detail` 不含秘密；
- [ ] 后台线程能在禁用/卸载时停止。

**前端**

- [ ] **`js` 模式（推荐）只用宿主注入的 antd**：没有自定义 CSS、没有渐变/Hero、没有自造页面标题；标题/面包屑/官方与实验性标签由宿主 `PageContainer` 提供；
- [ ] js 模式 `mount/unmount` 清理定时器/事件/root，`unmount()` 可重复调用；
- [ ] js 模式不自带 React/ReactDOM/antd，所有运行时从 `window.AutoOpsSDK` 取；
- [ ] 页面内跳转用 `sdk.history.push()`，不用 `window.location` 整页刷新（登录跳转除外）；
- [ ] 401 展示可读提示或交给宿主 `request` 的全局策略，403 展示可读原因；
- [ ] 动态文本用 `textContent`，没有 `innerHTML` 注入；
- [ ] `web/` 里没有 token、密钥、内部地址。
- [ ] **仅 `iframe` 模式**：相对资源路径正确，API 用绝对 `/api/modules/<id>/api/...`；401 跳顶层登录页，不要只把登录页加载进 iframe。

**发布**

- [ ] `pack_module.py --check` 通过；
- [ ] 打包产物在目标宿主版本上安装、启用、使用、禁用、卸载全流程通过；
- [ ] `GET /api/modules/<id>` 为 `loaded=true, errors=[]`；
- [ ] 发布 SHA-256 与安装说明；
- [ ] 卸载会删 `data/` 的提醒写进 README。

