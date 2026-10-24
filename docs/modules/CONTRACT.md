# AutoOps 扩展模块系统 —— 内部实现契约 v1

> 这份文件是**实现契约**（后端 / 前端 / 打包工具 / 官方模块四方共同遵守）。
> 面向第三方开发者的文档见 `docs/modules/WHITEPAPER.md`（由本契约派生）。
> **English**: [Extension Module System Contract v1](../i18n/contract.en.md)

## 0. 设计原则

1. **文件系统即真相**：模块装在 `bastion-backend/instance/modules/<id>/`，不新增数据库表。
目录里 `state.json` 记录启用状态与安装时间，`package/` 是解压后的包。
2. **声明式优先**：导航、设置项、权限全部写在 `description.json` 里；Python 只负责
注册运行时路由 / 生命周期钩子。这样安装页**不执行任何模块代码**就能预览。
3. **模块是可信代码**：Python 扩展在堡垒机进程内运行，等价于插件，天然拥有全部内部数据
访问能力（这是需求，不是漏洞）。不提供进程隔离沙箱；白皮书必须写清这一点。
4. **静态资源免登录，模块接口必须鉴权**：`/api/modules/<id>/asset/**` 直接回文件；
模块自己的 `/api/modules/<id>/api/**` 由模块声明权限，默认 `module:<id>:use`。
5. **失败要响**：安装失败必须回滚到「未安装」，并给出可读原因；加载失败的模块要
`loaded=false` + `errors[]`，不能把整个后端拖挂。

## 1. 包格式 `.mod.ops_ext`

ZIP（deflate）。文件名 `<id>-<version>.mod.ops_ext`，但**安装时以 description.json 的
id/version 为准**，文件名不参与校验。

```
description.json        必填，包根目录
modules/                可选，Python 扩展根（存在则必须有 __init__.py，且必须能 setup(ctx)）
  __init__.py
images/                 可选，演示图
web/                    可选，静态前端资源（任意 HTML/JS/CSS）
data/                   可选，初始数据，安装时复制到 instance/modules/<id>/data
README.md               可选，中文功能说明（安装预览窗口展示）
README.en.md            可选，英文功能说明；英文界面优先用它，没有则退回 README.md
bin/                    可选，可执行文件（RDP 网关这种），包内路径 instance/modules/<id>/package/bin/
```

安全约束（安装期硬校验）：

* 拒绝绝对路径、`..`、反斜杠、盘符、NUL、以及符号链接/设备文件（ZIP 条目只允许普通文件与目录）。
* 单包解压后总大小 ≤ 256 MB，条目数 ≤ 20000，`description.json` ≤ 256 KB。
* 文件名非法字符 → 直接失败。
* **安装期绝不 import 模块代码**。加载发生在安装成功之后、独立 try/except 里。

## 2. `description.json`

```jsonc
{
  "id": "rdp-gateway",                 // 必填 ^[a-z][a-z0-9-]{2,39}$，且不能是保留字
  "name": {"zh": "RDP 网关", "en": "RDP Gateway"},   // 必填；也接受纯字符串
  "version": "1.0.0",                  // 必填，严格 semver（MAJOR.MINOR.PATCH[-pre][+build]）
  "author": "AutoOps",                 // 必填；author == "AutoOps"（忽略大小写/首尾空格）= 官方
  "description": {"zh": "...", "en": "..."},        // 可选，默认空
  "tags": ["experimental", "rdp"],     // 可选，字符串数组；experimental 触发烧杯图标
  "experimental": true,                // 可选，等价于 tags 里含 experimental（二者取并集）
  "icon": "AppstoreOutlined",          // 可选，侧边栏图标（antd 图标名）
  "homepage": "",                      // 可选
  "license": "MIT",                    // 可选
  "minBastionVersion": "1.0.0",        // 可选，后端配置 APP_VERSION 比较
  "images": ["images/demo-1.png"],     // 可选，包内相对路径，安装页展示
  "entry": {"web": "web/index.html"},  // 可选，默认 web/index.html
  "nav": [                             // 可选，侧边栏入口
    {
      "key": "console",                // 必填，模块内唯一
      "title": {"zh": "RDP 网关", "en": "RDP Gateway"},
      "path": "/",                     // 可选，缺省 "/"；相对路径，最终 URL = /m/<id><path>
      "entry": "web/index.html",       // 可选，缺省 entry.web
      "type": "iframe",                // "iframe"(默认) | "js"
      "icon": "DesktopOutlined",       // 可选
      "permission": "module:rdp-gateway:use"  // 可选，缺省 module:<id>:use
    }
  ],
  "settings": [                        // 可选，自动出现在「参数设置」页
    {
      "key": "enabled",                // 必填 ^[a-zA-Z][a-zA-Z0-9_]{0,39}$
      "label": {"zh": "启用 RDP 网关"},
      "type": "bool",                  // bool | int | string
      "default": "0",
      "min": 0, "max": 65535,          // int 可选
      "description": {"zh": "..."}     // 可选
    }
  ],
  "permissions": ["module:rdp-gateway:use"],  // 可选，声明给角色配置页用
  "requires": ["some-module"]          // 可选，依赖其它模块 id
}
```

`nav[].type` 仍允许 `iframe`（上面的字段注释是语法示例），但**官方示例模块现在全部使用 `js` 模式**；下面的归一化对象以 `rdp-gateway` 为例。

归一化后的模块对象（后端 `spec.normalize_description()` 的输出，也是 `/api/modules` 的元素）：

```jsonc
{
  "id": "rdp-gateway",
  "name": {"zh": "RDP 网关", "en": "RDP Gateway"},   // 永远归一化成双语对象
  "version": "1.0.0",
  "author": "AutoOps",
  "official": true,                    // 由 author 推导
  "description": {"zh": "", "en": ""},
  "tags": ["experimental", "rdp"],
  "experimental": true,
  "icon": "AppstoreOutlined",
  "license": "", "homepage": "", "minBastionVersion": "",
  "images": [{"path": "images/demo-1.png", "url": "/api/modules/rdp-gateway/asset/images/demo-1.png"}],
  "nav": [{"key": "console", "title": {...}, "path": "/m/rdp-gateway/", "icon": "...", "type": "js", "permission": "module:rdp-gateway:use"}],
  "settings": [{"key": "mod.rdp-gateway.enabled", "rawKey": "enabled", "label": {...}, "type": "bool", "default": "0", "min": null, "max": null, "group": "RDP 网关"}],
  "permissions": ["module:rdp-gateway:use"],
  "requires": [],
  "reserved": false,
  "enabled": true,
  "loaded": true,
  "installedAt": "2026-10-23T15:04:05Z",
  "updatedAt": "...",
  "size": 14745600,
  "errors": []
}
```

保留 id（不允许第三方占用）：`core`、`system`、`admin`、`modules`。

标签颜色由前端固定映射（后端不下发颜色，只下发 tag 标识）：

|tag|颜色|图标|
|-|-|-|
|`experimental`|`magenta` + `ExperimentOutlined`（烧杯）|安装前必须二次确认|
|`official`|`gold`|由 `official` 字段自动加，不需要写进 tags|
|`security`|`red`||
|`network` / `rdp` / `ssh`|`blue`||
|`monitor` / `audit`|`cyan`||
|`storage` / `file`|`green`||
|`ai`|`purple`||
|其它|`default`（灰）||

文本必须**逐字**等于：

```
我知道这是实验性功能，使用不当的话也许会造成系统损毁
```

前端提示用户在输入框里手工敲一遍（不接受粘贴，比对去首尾空白后的完整字符串），
后端二次校验 `ack` 字段（去首尾空白后全等），不匹配回 400 `EXPERIMENTAL_NOT_ACKED`。

## 3. 后端运行态

新增包 `bastion-backend/app/modules/`：

|文件|职责|
|-|-|
|`spec.py`|常量（保留 id、允许后缀、大小上限）、`parse_archive()`、`normalize_description()`、`validate()`|
|`store.py`|落盘：`instance/modules/<id>/{package,state.json,data}`；`install/remove/list/read_state/write_state`；原子安装（temp → rename）|
|`registry.py`|内存注册表：已装模块、已加载模块、导航项、模块设置项、Loader 句柄|
|`loader.py`|`sys.modules['autoops']` 注入 + `import` 模块包 + `setup(ctx)` + 卸载|
|`context.py`|`ModuleContext`（见 §4）|
|`sdk.py`|`autoops.bastion` 门面（见 §5），全部 async|
|`api.py` 或 `app/api/modules.py`|REST 接口（见 §6）|

### 不内置任何模块

宿主**不预置、不自动安装任何模块**：全新实例装完就是零模块状态，所有扩展能力
（包括官方示例 `hello-bastion`、`js-demo`、`rdp-gateway`）都必须由管理员上传
`*.mod.ops_ext` 安装，和第三方扩展走完全相同的代码路径。

* `create_app` 里的 `init_modules(app)` 只做一件事：扫描 `instance/modules/` 下**已经存在**的
  模块目录并加载，不会去任何源码目录里找包、也不会补装缺失的模块。
* 官方模块的源码在仓库 `modules-src/`，打包命令见 §8；产物放仓库根目录，随仓库分发。
* 卸载就是真卸载（目录删除、设置项注销、进程内路由摘除），重装＝重新上传同一份包。
* `modules-src/` 只在**开发/打包时**使用，运行时完全不读它。

启动顺序（`create_app` 里，必须在 `seed_data` 之后、`socketio` 事件注册之前调用）：

```python
from .modules import init_modules
init_modules(app)   # 内部：扫描 → 注册设置默认值 → 加载 enabled 模块
```

`init_modules` 必须**永不抛异常**（任何模块的错误都收敛到该模块的 `errors[]`）。

### 设置项接入

* 模块设置落 `SystemSetting`，key = `mod.<moduleId>.<key>`。
* `settings_store` 新增：

```python
  def register_module_defaults(module_id: str, items: list[dict]) -> None
  def unregister_module_defaults(module_id: str) -> None
  def all_defaults() -> dict[str, str]
  def default_meta(key: str) -> dict | None      # {label, type, min, max, group}
  ```

* `update_settings()` / `ensure_defaults()` / `all_settings()` 一律改用 `all_defaults()`。
* `GET /api/settings` 的 item 增加可选字段 `group`（模块显示名，中文优先）。
`PUT /api/settings` 接受 `mod.*` 键，类型校验走模块声明的 type/min/max。
* 老的 `DEFAULTS` 常量保留（核心设置），`all_defaults()` = `DEFAULTS` + 模块默认值。

## 4. Python 模块入口

```python
# instance/modules/hello/package/modules/__init__.py
from autoops import bastion

def setup(ctx):
    """模块被加载时调用一次；抛异常 = 加载失败。"""
    ctx.logger.info("hello from %s", ctx.id)

def teardown(ctx):
    """卸载 / 禁用时调用，可选。"""

def on_enable(ctx): ...
def on_disable(ctx): ...
```

`ModuleContext`（`ctx`）字段与方法：

|成员|说明|
|-|-|
|`ctx.id` / `ctx.version` / `ctx.author` / `ctx.official`|只读模块元信息|
|`ctx.app`|Flask `app`（谨慎使用）|
|`ctx.package_dir`|`Path`，包根目录|
|`ctx.data_dir`|`Path`，**模块私有可写目录**（`instance/modules/<id>/data`，安装时创建）|
|`ctx.logger`|`logging.Logger("module.<id>")`|
|`ctx.get_setting(key, default=None)`|取模块设置（自动补 `mod.<id>.` 前缀）|
|`ctx.set_setting(key, value)`|写模块设置（落库 + commit）|
|`ctx.settings`|`dict[str, str]` 快照（只读语义）|
|`ctx.route(path, methods=("GET",), permission=None)`|装饰器，把函数挂到 `/api/modules/<id>/api<path>`；返回统一信封|
|`ctx.blueprint(name="api")`|返回 `Blueprint(url_prefix=f"/api/modules/{id}/api")`，自由挂路由|
|`ctx.on(event)`|`event ∈ {"install","uninstall","enable","disable"}`，注册钩子|
|`ctx.emit(topic, payload)`|Socket.IO 广播 `module:<id>:<topic>`|

`ctx.route` 包装约定：函数返回 `dict/list/None` → `api_ok(data)`；抛 `ModuleApiError(message, status)` →
`api_error`。`permission` 缺省取该模块的第一个 nav 权限，再缺省 `module:<id>:use`。

## 5. SDK 门面 `autoops.bastion`（全部异步）

模块代码在 `from autoops import bastion` 之后使用。**所有会碰到数据库/网络的方法都是
`async def`**；同时提供 `_sync` 后缀的同步版本方便简单脚本。

```python
# 凭据（需求指定：必须异步）
await bastion.credentials.get(host_id, account_id=None) -> Credentials
    # Credentials(host_id, account_id, host_name, address, protocol, port,
    #             username, password, private_key, passphrase)
    # password 已用 Fernet 解密；明文只在返回值里存在，不落日志
await bastion.credentials.list(host_id=None) -> list[CredentialMeta]   # 不含密钥

# 资产
await bastion.hosts.list(protocols=None, keyword=None) -> list[dict]
await bastion.hosts.get(host_id) -> dict

# 账号
await bastion.accounts.list(host_id=None) -> list[dict]
await bastion.accounts.reveal_password(account_id) -> str     # 解密 + 写审计
await bastion.accounts.reveal_private_key(account_id) -> str

# 会话 / 审计
await bastion.sessions.list_active() -> list[dict]
await bastion.sessions.open(host_id, account_id, protocol="ssh", source="module:<id>") -> dict
await bastion.audit.log(action, target_type=None, target_id=None, message="", detail=None) -> None
await bastion.audit.log_command(session_id, command, output=None, allowed=True, reason="") -> None

# 设置 / 用户 / 策略 / 网络
await bastion.settings.get(key) -> str
await bastion.settings.set(key, value) -> None
await bastion.users.list() -> list[dict]
await bastion.policy.check(account_id, command) -> dict   # {allowed, matched_rule, action}
await bastion.ssh.run(host_id, command, account_id=None, timeout=30) -> dict
    # {code, stdout, stderr, elapsed}; 强制过命令策略；失败抛 bastion.ModuleError
await bastion.events.emit(topic, payload) -> None          # Socket.IO 广播

# 通用
bastion.util.now() -> datetime          # aware UTC
await bastion.util.run_blocking(fn, *a, **kw) -> Any      # 在带 app context 的线程里跑阻塞代码
```

实现要点：

* 异步：`asyncio.get_running_loop().run_in_executor(_EXECUTOR, ...)`，`_EXECUTOR` 是
`ThreadPoolExecutor(max_workers=8, thread_name_prefix="module-io")`；线程内 `with app.app_context():`。
* 没有任何 `async def` 里做同步 DB 调用（会拖住事件循环）。
* `credentials.get` 写一条 `audit_logs(action="module_credential", ...)`，`detail` 不含明文。

## 6. REST 接口（宿主侧）

蓝图 `modules_bp`（`app/api/modules.py`，必须注册进 `app/api/__init__.py`）：

|方法|路径|权限|说明|
|-|-|-|-|
|GET|`/api/modules`|`setting:view`|列表（含 `loaded`/`errors`），按 id 排序|
|GET|`/api/modules/nav`|`login_required`|当前账号可见的导航项（按 nav 权限过滤），供前端拼侧边栏|
|GET|`/api/modules/<id>`|`setting:view`|详情（同一个 normalize 对象）|
|POST|`/api/modules/install`|`admin_required`|multipart：`file`；实验性模块另需 `ack`。成功回归一对象|
|POST|`/api/modules/<id>/enable`|`admin_required`|置 enabled 并尝试加载|
|POST|`/api/modules/<id>/disable`|`admin_required`|置 disabled 并 teardown|
|DELETE|`/api/modules/<id>`|`admin_required`|卸载（先 disable 再删目录）；系统不内置模块，卸载后要恢复只能重新上传包|
|GET|`/api/modules/<id>/asset/<path:rel>`|免登录|只允许 `web/`、`images/`、`README.md`、`README.en.md`；`Cache-Control: no-cache`；用 `send_from_directory` 防穿越|
|GET|`/api/modules/<id>/logs`|`setting:view`|最近 200 行模块日志（内存环形缓冲）|
|GET|`/api/modules/preview/<token>/asset/<path:rel>`|免登录|安装前预览会话里的截图（只允许 `images/`），token 短期有效，`Cache-Control: private, max-age=600`|
|POST|`/api/modules/preview/<token>/install`|`admin_required`|在预览窗口里直接安装：JSON `{ack}`；成功回归一对象并销毁会话|
|POST|`/api/modules/upload-check`|`admin_required`|只解析 description.json 回预览（不落盘、不执行），用于安装前展示|

`upload-check` 的返回在模块对象基础上多这些字段：

|字段|说明|
|-|-|
|`installable`|只由**硬阻塞**（已安装、保留 id…）决定；实验性不算阻塞|
|`issues`|问题项，中文句子数组；旧前端兼容用|
|`warnings`|提示项（如"实验性模块需要逐字确认"）|
|`issueItems` / `warningItems`|与 `issues` / `warnings` 一一对应的可翻译版本；每项 `{key, params}`，`key` 是中文模板（如 `模块 {id} 已安装`），`params` 是占位符对象，前端用 `t(key, params)` 渲染。`issues` / `warnings` 保留中文句子，向后兼容|
|`readme` / `readmeName`|包内中文 README 正文（≤20000 字符）与路径，用于安装预览窗口|
|`readmeEn` / `readmeEnName`|包内英文 README（`README.en.md`）正文与路径；没有则空串。前端按界面语言挑一份（英文优先 `readmeEn`）|
|`size` / `fileCount` / `folders`|解压后总字节数、文件数、顶层目录（`["web/","modules/"]`）|
|`previewToken`|32 位十六进制预览会话 token；`install` / 截图 URL 都用它|
|`images[]`|`{path, size?, url?}`；`url` 指向预览会话里的截图（最多 8 张、单张 ≤4MB）|

**安装前预览会话**：`upload-check` 会把包本身与 `images/` 里的截图留在
`instance/module-previews/<token>/{package.mod.ops_ext, asset/, meta.json}`，TTL 30 分钟
（`preview.SESSION_TTL_SECONDS`），过期目录在下次上传/读取时惰性清理。
`POST /api/modules/preview/<token>/install` 直接用这份包安装，因此用户不必在预览窗口里
再选一次文件；安装成功后会话被删除，重复安装得到 `410 MODULE_PREVIEW_EXPIRED`。

**零模块状态**：这是全新实例的**默认状态**（宿主不内置任何模块）。此时
`GET /api/modules` 与 `/api/modules/nav` 返回 `[]`，模块页显示空态引导，
`/system/settings` 只剩「全局参数」与「维护操作」两张卡；宿主其余功能不受影响。

错误码：`MODULE_INVALID_PACKAGE` / `MODULE_RESERVED_ID` / `MODULE_EXISTS` / `MODULE_NOT_FOUND` /
`EXPERIMENTAL_NOT_ACKED` / `MODULE_LOAD_FAILED` / `MODULE_PREVIEW_EXPIRED`（均 4xx，信封同 `api_error`）。

## 7. 前端契约

* **侧边栏**：`getInitialState()` 里（已有 currentUser 时）拉 `/api/modules/nav`，
存 `initialState.modules`；`layout.menuDataRender` 在「系统设置」分组前追加这些项。
图标：`icon` 字段是 antd 图标名，用现有 `@ant-design/icons` 动态取（找不到回退 `AppstoreOutlined`）。
* **路由**：`config/routes.ts` 增加一条静态兜底路由

```ts
  {
    path: '/m/:moduleId/*',
    name: '扩展模块',
    hideInMenu: true,
    component: './bastion/modules/host',
  }
  ```

  必须排在 `path: '*'` 的 404 之前。

* **宿主页** `src/pages/bastion/modules/host.tsx`：

  * 统一外壳：模块内容一律套在 `PageContainer` 里（标题 = 模块名、副标题 = 模块 description、
`extra` 展示 `v<版本> / 官方 / 实验性` 标签），保证和堡垒机其它页面视觉一致。
  * `iframe` 模式：`<iframe src={assetUrl(moduleId, entry)}>`，`width:100%`，高度
`calc(100vh - 180px)`，白底 + 8px 圆角；同源，iframe 内可直接读 `localStorage.bastion_token`。
**不推荐**用于官方模块：自绘 HTML/CSS 很难和宿主保持一致的观感。
  * `js` 模式（**推荐**）：动态注入 `<script type="module" src=entry>`，脚本从
`window.AutoOpsSDK` 取 `{ React, ReactDOM, antd, request, getToken, message, t, isEnglish, history }`，
然后调用 `window.AutoOpsModules[moduleId].mount(el, sdk)`（ESM 默认导出 `mount/unmount` 亦可）。
模块内**只用 antd 组件**（`Card/Statistic/Descriptions/Table/Tag/Button/Space/Alert/…`），
不写自定义 CSS、不写渐变/Hero/自造卡片，页面标题由宿主提供；
`sdk.history.push('/system/settings')` 可做 SPA 跳转，`sdk.message` 弹提示。
  * **模块 UI 必须支持中英双语**（宿主有语言切换）：所有用户可见文案写成 `sdk.t('中文原文')`，
兜底 `const tr = typeof sdk.t === 'function' ? sdk.t : (zh) => zh;`。`t` 在中文界面原样返回，
英文界面查宿主词典 `ant-design-pro/src/i18n/dict/en-US.json`（键就是中文原文，`{count}` 这类占位符保持）。
带变量的句子写 `tr('共 {count} 台', { count: n })`。机器可读常量（键名、URL、代码片段、`bool`/`int`、`console` 日志、开发者异常信息）不包 `t`。
需要按语言分支时用 `sdk.isEnglish()`（每次调用读最新语言；语言切换会整页重载，不必在 `mount` 里监听）。
新增的中文原文要同步补进宿主词典，否则英文界面会显示中文。
  * 模块禁用/不存在 → 友好 403/404 卡片。
* **服务层** `src/services/bastion/modules.ts` + `types.ts` 增补，必须 `unwrap()`。
* **安装页** `src/pages/bastion/modules/index.tsx`（挂在「系统设置 → 扩展模块」）：

  * 列表：名称（双语）、版本、作者徽标（`AutoOps` → 「官方」金标）、标签（色板见 §2）、
实验性烧杯图标、启用开关、卸载、更新时间、体积、加载错误提示。
  * 卸载：Popconfirm 提示「卸载后模块文件会被删除，要恢复需重新上传安装包」。
  * **没有任何引导横幅**：不内置模块是默认状态，列表为空只显示表格自身的 Empty（`尚未安装任何扩展模块`），
不接受「系统不内置任何模块…」这类说明性 Alert，也不显示「当前账号只能查看…」这类权限横幅（按钮 Tooltip 足够）。
  * 安装入口（关键约束）：点击「安装扩展模块」**只做一件事** —— 在点击事件里同步
`window.open(origin + '/module-preview#pick','_blank','width=1040,height=820')`；只有它返回 `null`（确实被拦截）才
`message.warning('浏览器拦截了弹窗，请允许本站弹窗后重试')`，其余情况不打扰用户。
  * **选文件必须在预览窗口里做，不能在主页面顺手 `input.click()`**：`window.open()` 会消耗掉这次点击的
「瞬时用户激活」，同一手势里紧跟的 `input[type=file].click()` 拉不起文件选择器 —— 浏览器静默忽略，表现就是
「窗口弹出来了，但文件对话框没弹」（CDP `Page.setInterceptFileChooserDialog` 可复现：`fileChooserOpened` 事件不出现）。
窗口内的按钮是一次全新的用户手势，才能拉起选择器。因此 `#pick` 态的预览窗口先渲染「选择扩展包」卡片
（antd `Card` + 主按钮 + 隐藏 `input`），点按钮 → `input.click()` → `change` → `upload-check` →
写 `localStorage['module-preview:<token>']` → 本窗口把 hash 换成 token 并就地渲染详情（**不二次开窗、不导航、不留白页**）。
  * **禁止**在 `Upload.beforeUpload` / 任何 `await` 之后 `window.open`（那时已脱离用户手势，必被拦截）。
  * 主页面**不弹**任何预览/确认 Modal。
  * **详情预览窗口**（`src/pages/bastion/modules/preview.tsx`，路由 `{ path: '/module-preview', layout: false }`，
数据用 `localStorage['module-preview:<token>']` + URL hash 传 token）：展示功能说明（按语言选 `readmeEn`/`readme`，用 antd 组件
渲染标题/列表/表格/引用/代码，**不引入 markdown 依赖**）、导航页面、参数设置、权限与依赖、包内目录（Tag 展示）、
体积/文件数、截图（`images[].url`，antd `Image`）。
  * **安装动作在预览窗口里完成**：底部「安装」卡 → 实验性模块先逐字确认 → `POST /api/modules/preview/<token>/install`
→ 成功写 `localStorage['module-installed:<id>'] = Date.now()` 并提示可关闭窗口；
主窗口监听 `window.addEventListener('storage', …)`（key 前缀 `module-installed:`）刷新列表/侧边栏。
功能说明优先展示 `readmeEn`（英文界面且包里有 `README.en.md` 时），否则回退 `readme`；两者都没有显示 Empty。
  * i18n：所有新文案中英都要加（`src/i18n/`）；模块自己的 UI 双语规则见 §7 的 js 模式条目。

## 8. 打包工具

`bastion-backend/tools/pack_module.py`：

```
python tools/pack_module.py <源码目录> [-o <输出目录>] [--check]
```

* 源码目录 = 一个完整包目录（含 `description.json`）。
* `--check`：只校验不打包（等价于安装期校验）。
* 默认输出 `modules-dist/<id>-<version>.mod.ops_ext`；`-o` 可指定任意目录
（**官方三个示例模块的产物统一生成到仓库根目录** `python tools/pack_module.py modules-src/<id> -o .`，方便直接分发）。
* 打包时排除 `__pycache__`、`.git`、`node_modules`、`*.pyc`、`.DS_Store`。
* 打包后打印 id/version/size/条目数，并对 zip 再做一次 `spec.validate`。

## 9. 官方模块

系统**不预装任何模块**，三个官方示例都在 `modules-src/`，打包产物放在仓库根，靠上传安装：

| 源码 | 版本 | 说明 |
|-|-|-|
|`modules-src/rdp-gateway/`|1.2.2|RDP 网关（`experimental: true`）。自带 Rust 网关进程（`bin/bastion-rdp-gateway.exe`），宿主里原来的 `app/api/rdp_gateway.py`、`/api/rdp-gateway/*`、`/system/rdp-gateway` 页都由它提供|
|`modules-src/hello-bastion/`|1.1.1|最小可用示例：导航注入 + 模块设置 + 异步 SDK（含凭证摘要演示）|
|`modules-src/js-demo/`|1.0.1|js 模式最小示例：只演示 SDK 基础能力|

* **RDP 网关模块没有「直连」通道**：早期网页里可以把目标机地址和口令交给用户本地 mstsc 直连，那条路绕过录制与审计，
已删除；现在只保留 `mstsc /v:<堡垒机IP>:<端口>` → 登录堡垒机账号 → 在网关会话里按编号选主机。
模块页的目标机列表是**只读**的授权概览。`POST /api/modules/rdp-gateway/api/sessions` 仍保留，
因为 Rust 网关进程自己要用它登记受审计会话。
* 保留兼容：`RDP_GATEWAY_ENABLED`/`RDP_GATEWAY_PORT` 环境变量仍被模块读取。
