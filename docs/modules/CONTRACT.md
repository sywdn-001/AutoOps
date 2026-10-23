# AutoOps 扩展模块系统 —— 内部实现契约 v1

> 这份文件是**实现契约**（后端 / 前端 / 打包工具 / 官方模块四方共同遵守）。
> 面向第三方开发者的文档见 `docs/modules/WHITEPAPER.md`（由本契约派生）。

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
README.md               可选
bin/                    可选，可执行文件（RDP 网关这种），包内路径 instance/modules/<id>/package/bin/
```

安全约束（安装期硬校验）：
- 拒绝绝对路径、`..`、反斜杠、盘符、NUL、以及符号链接/设备文件（ZIP 条目只允许普通文件与目录）。
- 单包解压后总大小 ≤ 256 MB，条目数 ≤ 20000，`description.json` ≤ 256 KB。
- 文件名非法字符 → 直接失败。
- **安装期绝不 import 模块代码**。加载发生在安装成功之后、独立 try/except 里。

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

保留 id（不允许第三方占用）：`core`、`system`、`admin`、`modules`、`_builtin`。

标签颜色由前端固定映射（后端不下发颜色，只下发 tag 标识）：

| tag | 颜色 | 图标 |
| --- | --- | --- |
| `experimental` | `magenta` + `ExperimentOutlined`（烧杯） | 安装前必须二次确认 |
| `official` | `gold` | 由 `official` 字段自动加，不需要写进 tags |
| `security` | `red` | |
| `network` / `rdp` / `ssh` | `blue` | |
| `monitor` / `audit` | `cyan` | |
| `storage` / `file` | `green` | |
| `ai` | `purple` | |
| 其它 | `default`（灰） | |

「烧杯确认」文本必须**逐字**等于：

```
我知道这是实验性功能，使用不当的话也许会造成系统损毁
```

前端提示用户在输入框里手工敲一遍（不接受粘贴，比对去首尾空白后的完整字符串），
后端二次校验 `ack` 字段（去首尾空白后全等），不匹配回 400 `EXPERIMENTAL_NOT_ACKED`。

## 3. 后端运行态

新增包 `bastion-backend/app/modules/`：

| 文件 | 职责 |
| --- | --- |
| `spec.py` | 常量（保留 id、允许后缀、大小上限）、`parse_archive()`、`normalize_description()`、`validate()` |
| `store.py` | 落盘：`instance/modules/<id>/{package,state.json,data}`；`install/remove/list/read_state/write_state`；原子安装（temp → rename） |
| `registry.py` | 内存注册表：已装模块、已加载模块、导航项、模块设置项、Loader 句柄 |
| `loader.py` | `sys.modules['autoops']` 注入 + `import` 模块包 + `setup(ctx)` + 卸载 |
| `context.py` | `ModuleContext`（见 §4） |
| `sdk.py` | `autoops.bastion` 门面（见 §5），全部 async |
| `api.py` 或 `app/api/modules.py` | REST 接口（见 §6） |
| `builtin.py` | 内置模块发现：把 `modules-src/<id>/*.mod.ops_ext` 复制进 `instance/modules` 并标 `builtin=true` |

启动顺序（`create_app` 里，必须在 `seed_data` 之后、`socketio` 事件注册之前调用）：

```python
from .modules import init_modules
init_modules(app)   # 内部：扫描 → 注册设置默认值 → 加载 enabled 模块
```

`init_modules` 必须**永不抛异常**（任何模块的错误都收敛到该模块的 `errors[]`）。

### 设置项接入

- 模块设置落 `SystemSetting`，key = `mod.<moduleId>.<key>`。
- `settings_store` 新增：
  ```python
  def register_module_defaults(module_id: str, items: list[dict]) -> None
  def unregister_module_defaults(module_id: str) -> None
  def all_defaults() -> dict[str, str]
  def default_meta(key: str) -> dict | None      # {label, type, min, max, group}
  ```
- `update_settings()` / `ensure_defaults()` / `all_settings()` 一律改用 `all_defaults()`。
- `GET /api/settings` 的 item 增加可选字段 `group`（模块显示名，中文优先）。
  `PUT /api/settings` 接受 `mod.*` 键，类型校验走模块声明的 type/min/max。
- 老的 `DEFAULTS` 常量保留（核心设置），`all_defaults()` = `DEFAULTS` + 模块默认值。

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

| 成员 | 说明 |
| --- | --- |
| `ctx.id` / `ctx.version` / `ctx.author` / `ctx.official` | 只读模块元信息 |
| `ctx.app` | Flask `app`（谨慎使用） |
| `ctx.package_dir` | `Path`，包根目录 |
| `ctx.data_dir` | `Path`，**模块私有可写目录**（`instance/modules/<id>/data`，安装时创建） |
| `ctx.logger` | `logging.Logger("module.<id>")` |
| `ctx.get_setting(key, default=None)` | 取模块设置（自动补 `mod.<id>.` 前缀） |
| `ctx.set_setting(key, value)` | 写模块设置（落库 + commit） |
| `ctx.settings` | `dict[str, str]` 快照（只读语义） |
| `ctx.route(path, methods=("GET",), permission=None)` | 装饰器，把函数挂到 `/api/modules/<id>/api<path>`；返回统一信封 |
| `ctx.blueprint(name="api")` | 返回 `Blueprint(url_prefix=f"/api/modules/{id}/api")`，自由挂路由 |
| `ctx.on(event)` | `event ∈ {"install","uninstall","enable","disable"}`，注册钩子 |
| `ctx.emit(topic, payload)` | Socket.IO 广播 `module:<id>:<topic>` |

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
- 异步：`asyncio.get_running_loop().run_in_executor(_EXECUTOR, ...)`，`_EXECUTOR` 是
  `ThreadPoolExecutor(max_workers=8, thread_name_prefix="module-io")`；线程内 `with app.app_context():`。
- 没有任何 `async def` 里做同步 DB 调用（会拖住事件循环）。
- `credentials.get` 写一条 `audit_logs(action="module_credential", ...)`，`detail` 不含明文。

## 6. REST 接口（宿主侧）

蓝图 `modules_bp`（`app/api/modules.py`，必须注册进 `app/api/__init__.py`）：

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/modules` | `setting:view` | 列表（含 `loaded`/`errors`），按 id 排序 |
| GET | `/api/modules/nav` | `login_required` | 当前账号可见的导航项（按 nav 权限过滤），供前端拼侧边栏 |
| GET | `/api/modules/<id>` | `setting:view` | 详情（同一个 normalize 对象） |
| POST | `/api/modules/install` | `admin_required` | multipart：`file`；实验性模块另需 `ack`。成功回归一对象 |
| POST | `/api/modules/<id>/enable` | `admin_required` | 置 enabled 并尝试加载 |
| POST | `/api/modules/<id>/disable` | `admin_required` | 置 disabled 并 teardown |
| DELETE | `/api/modules/<id>` | `admin_required` | 卸载（先 disable） |
| GET | `/api/modules/<id>/asset/<path:rel>` | 免登录 | 只允许 `web/`、`images/`、`README.md`；`Cache-Control: no-cache`；用 `send_from_directory` 防穿越 |
| GET | `/api/modules/<id>/logs` | `setting:view` | 最近 200 行模块日志（内存环形缓冲） |
| POST | `/api/modules/upload-check` | `admin_required` | 只解析 description.json 回预览（不落盘、不执行），用于安装前展示 |

错误码：`MODULE_INVALID_PACKAGE` / `MODULE_RESERVED_ID` / `MODULE_EXISTS` / `MODULE_NOT_FOUND` /
`EXPERIMENTAL_NOT_ACKED` / `MODULE_LOAD_FAILED`（均 4xx，信封同 `api_error`）。

## 7. 前端契约

- **侧边栏**：`getInitialState()` 里（已有 currentUser 时）拉 `/api/modules/nav`，
  存 `initialState.modules`；`layout.menuDataRender` 在「系统设置」分组前追加这些项。
  图标：`icon` 字段是 antd 图标名，用现有 `@ant-design/icons` 动态取（找不到回退 `AppstoreOutlined`）。
- **路由**：`config/routes.ts` 增加一条静态兜底路由

  ```ts
  {
    path: '/m/:moduleId/*',
    name: '扩展模块',
    hideInMenu: true,
    component: './bastion/modules/host',
  }
  ```

  必须排在 `path: '*'` 的 404 之前。
- **宿主页** `src/pages/bastion/modules/host.tsx`：
  - 统一外壳：模块内容一律套在 `PageContainer` 里（标题 = 模块名、副标题 = 模块 description、
    `extra` 展示 `v<版本> / 官方 / 实验性` 标签），保证和堡垒机其它页面视觉一致。
  - `iframe` 模式：`<iframe src={assetUrl(moduleId, entry)}>`，`width:100%`，高度
    `calc(100vh - 180px)`，白底 + 8px 圆角；同源，iframe 内可直接读 `localStorage.bastion_token`。
    **不推荐**用于官方模块：自绘 HTML/CSS 很难和宿主保持一致的观感。
  - `js` 模式（**推荐**）：动态注入 `<script type="module" src=entry>`，脚本从
    `window.AutoOpsSDK` 取 `{ React, ReactDOM, antd, request, getToken, message, t, history }`，
    然后调用 `window.AutoOpsModules[moduleId].mount(el, sdk)`（ESM 默认导出 `mount/unmount` 亦可）。
    模块内**只用 antd 组件**（`Card/Statistic/Descriptions/Table/Tag/Button/Space/Alert/…`），
    不写自定义 CSS、不写渐变/Hero/自造卡片，页面标题由宿主提供；
    `sdk.history.push('/system/settings')` 可做 SPA 跳转，`sdk.message` 弹提示。
  - 模块禁用/不存在 → 友好 403/404 卡片。
- **服务层** `src/services/bastion/modules.ts` + `types.ts` 增补，必须 `unwrap()`。
- **安装页** `src/pages/bastion/modules/index.tsx`（挂在「系统设置 → 扩展模块」）：
  - 列表：名称（双语）、版本、作者徽标（`AutoOps` → 「官方」金标）、标签（色板见 §2）、
    实验性烧杯图标、启用开关、卸载、更新时间、体积、加载错误提示。
  - 安装：拖拽/选择 `.mod.ops_ext` → 先调 `upload-check` 展示 description 预览
    （图片、名称、版本、作者、标签、说明）→ 若实验性，弹出 Modal 要求**手敲**确认语，
    输入不匹配则禁用「安装」按钮 → 再调 `install`。
  - i18n：所有新文案中英都要加（`src/i18n/`）。

## 8. 打包工具

`bastion-backend/tools/pack_module.py`：

```
python tools/pack_module.py <源码目录> [-o <输出目录>] [--check]
```

- 源码目录 = 一个完整包目录（含 `description.json`）。
- `--check`：只校验不打包（等价于安装期校验）。
- 输出 `modules-dist/<id>-<version>.mod.ops_ext`。
- 打包时排除 `__pycache__`、`.git`、`node_modules`、`*.pyc`、`.DS_Store`。
- 打包后打印 id/version/size/条目数，并对 zip 再做一次 `spec.validate`。

## 9. 官方模块

- 首个官方模块：`modules-src/rdp-gateway/`（RDP 网关，`experimental: true`，`author: "AutoOps"`）。
- 宿主里原来 `app/api/rdp_gateway.py` 与 `/api/rdp-gateway/*` 接口、`/system/rdp-gateway` 前端页
  在**模块安装后**由模块提供；宿主不再内置（解耦目标）。
- 保留兼容：`RDP_GATEWAY_ENABLED`/`RDP_GATEWAY_PORT` 环境变量仍被模块读取。
