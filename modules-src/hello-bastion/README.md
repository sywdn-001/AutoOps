# hello-bastion

AutoOps 堡垒机的**官方示例扩展模块**，使用 **`js` 模式**。它故意保持很小，但把第三方开发者最常问的几件事都跑通了：

- 在侧边栏注册一个入口（`nav[].type: "js"`），页面由宿主 `PageContainer` 包壳；
- 在「系统设置 → 参数设置」里注册 `bool` / `int` / `string` 三个配置项；
- 用 `async def` 的 `autoops.bastion` SDK 读取主机数、在线会话数；
- 用 `autoops.bastion.credentials.get()` 异步读取资产凭证，但**只返回用户名和口令长度**，绝不回传明文；
- 页面用宿主注入的 React / antd / request / history，不打包 React，不写自定义 CSS；
- 用 `sdk.history.push('/system/settings')` 做宿主内跳转，并处理 401 / 403 / 其它错误。

> 这是实验性模块（`experimental: true` / `tags` 含 `experimental`）。安装时宿主会要求手工输入确认语：
>
> `我知道这是实验性功能，使用不当的话也许会造成系统损毁`

## 目录结构

```text
hello-bastion/
├── description.json          # 模块元数据、导航、设置项
├── modules/
│   └── __init__.py           # Python 扩展：setup(ctx) + 两个 GET 接口
├── web/
│   └── index.js              # js 模式入口：export mount/unmount，纯 React.createElement
├── images/
│   └── demo-1.png            # 安装页演示图（800x450）
└── README.md
```

`.mod.ops_ext` 本质上就是一个 ZIP 包，包根目录必须直接包含 `description.json`。`modules/__init__.py` 里的 `setup(ctx)` 是 Python 扩展的唯一入口；`entry.web` 指向 js 模式的 ES Module。

## `description.json` 关键字段

```json
{
  "id": "hello-bastion",
  "version": "1.1.0",
  "author": "AutoOps",
  "experimental": true,
  "entry": { "web": "web/index.js" },
  "nav": [
    {
      "key": "stats",
      "title": { "zh": "Hello Bastion", "en": "Hello Bastion" },
      "path": "/",
      "entry": "web/index.js",
      "type": "js",
      "icon": "ExperimentOutlined",
      "permission": "module:hello-bastion:use"
    }
  ]
}
```

- `entry.web` 和 `nav[].entry` 都是 `web/index.js`；`nav[].type` 必须是 `"js"`，宿主才会按 js 模式加载。
- `path: "/"` 的完整前端路由是 `/m/hello-bastion/`。
- 页面标题、副标题、版本、官方/实验性标签由宿主的 `PageContainer` 提供，模块内不要再画标题或 Hero。
- `author: "AutoOps"` 只是「官方」元数据徽标，不是代码签名；模块代码仍以堡垒机进程身份运行。

## 接口

所有模块路由最终都挂在：

```text
/api/modules/hello-bastion/api<path>
```

请求必须带登录 JWT：

```http
Authorization: Bearer <bastion_token>
```

### `GET /api/modules/hello-bastion/api/stats`

返回主机总数、在线会话数、模块元信息和三个设置项的当前值。主机与会话全部来自异步 SDK：

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

实现要点：

```python
hosts, sessions = await asyncio.gather(
    bastion.hosts.list(),
    bastion.sessions.list_active(),
)
```

代码只依赖「返回 list」这一条契约，不猜 `hosts[].id`、`sessions[].sid` 之类的字段结构，所以后端字段增删不会把示例打挂。

### `GET /api/modules/hello-bastion/api/credentials-sample?host_id=1&account_id=2`

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `host_id` | 是 | 资产 ID，正整数 |
| `account_id` | 否 | 资产账号 ID；留空表示使用 SDK 的默认账号选择逻辑 |

成功响应（**注意没有 `password` 字段**）：

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

失败响应保持宿主统一信封，例如资产不存在：

```json
{
  "success": false,
  "code": "ERROR",
  "message": "资产不存在或当前账号不可见：host_id=99999",
  "data": null
}
```

实现方式：

```python
credentials = await bastion.credentials.get(host_id, account_id=account_id)
return {
    "hostId": host_id,
    "accountId": getattr(credentials, "account_id", account_id),
    "username": getattr(credentials, "username", "") or "",
    "passwordLength": len(getattr(credentials, "password", "") or ""),
}
```

- `password` 明文只在协程局部变量里存在，不写日志、不回前端、不落盘。
- 每次调用 `credentials.get()` 都会由 SDK 写一条 `module_credential` 审计（detail 不含明文）。
- 代码只记录异常**类型**，不记录异常文本，避免底层把连接串或凭据带进日志。
- 示例里「资产不存在」映射为 `404`；`ModuleApiError` 由 `ctx.route` 转成统一错误信封。

## 设置项

`description.json.settings[]` 会在「系统设置 → 参数设置」里自动出现，落库键名是 `mod.hello-bastion.<key>`：

| raw key | 类型 | 默认值 | 约束 | Python 读法 |
| --- | --- | --- | --- | --- |
| `show_greeting` | `bool` | `"1"` | 只存 `"1"` / `"0"` | `ctx.get_setting("show_greeting", "1")` |
| `refresh_seconds` | `int` | `"30"` | `5 ≤ x ≤ 600` | `ctx.get_setting("refresh_seconds", "30")` |
| `greeting_text` | `string` | `"Hello, Bastion!"` | 无 | `ctx.get_setting("greeting_text", "Hello, Bastion!")` |

宿主的设置值一律以字符串存储，模块自己负责转类型（见 `modules/__init__.py` 中的 `_as_bool` / `_as_int`）。这三个值会随 `/stats` 返回；保存设置后等下一轮轮询，或点页面上的「立即刷新」。

## 页面结构

`web/index.js` 是浏览器可直接执行的 ES Module，由宿主动态注入并调用 `mount(el, sdk)` / `unmount()`。它没有 `import`、没有 JSX，React / ReactDOM / antd / request / history 全部从 `window.AutoOpsSDK` 取。

宿主用 `PageContainer` 渲染标题、面包屑和标签；模块只往传入的 `el` 里放内容，共四张 `Card`：

1. **运行概况**：`Alert` 显示 `greeting_text` 问候语，4 个 `Statistic` 显示主机总数、在线会话、自动刷新间隔、最近刷新；右上角有「立即刷新」。
2. **模块设置**：`Descriptions` 显示三个设置当前值，`Table` 列出 `mod.hello-bastion.*` 的键名、类型和默认值；「去参数设置修改」用 `sdk.history.push('/system/settings')` 跳转。
3. **异步 SDK 演示**：资产 ID（必填）/ 账号 ID（可选）两个 `InputNumber`，「读取凭证摘要」通过宿主 `request` 调 `/credentials-sample`，只展示用户名与口令长度。
4. **关于本示例**：展示模块 id、版本、页面模式、API 前缀、作者和数据时间；按钮用 `sdk.history.push('/system/modules')` 打开扩展模块管理，或调用 `sdk.message.success()`。

页面不写自定义 CSS、不做渐变或 Hero。要改文案，改 `description.json` 或 `web/index.js` 里的字符串；要加卡片，在 `Space` 子节点里照现有 `Card` 结构追加一个。

## 打包

在仓库根目录执行（打包工具由 `bastion-backend/tools/pack_module.py` 提供，见白皮书第 9 章）：

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion -o .
```

只校验、不打包：

```powershell
python bastion-backend\tools\pack_module.py modules-src\hello-bastion --check
```

预期产物：

```text
hello-bastion-1.1.0.mod.ops_ext
```

产物输出到仓库根目录，随仓库分发；系统不会在启动时扫描 `modules-src/` 自动安装它。

`description.json` 里的 `id` / `version` 才是权威，文件名不参与校验。打包工具会排除 `__pycache__`、`*.pyc`、`.git`、`node_modules`、`.DS_Store`，并在打包后重新校验一次 ZIP。

如果当前分支还没有 `pack_module.py`，说明打包工具尚未合入，不要去手工 `Compress-Archive` 绕过安装期校验。

## 本地验证（不启动宿主）

```powershell
cd D:\Desktop\AutoOps

# Python 语法校验（不会 import autoops，所以不需要宿主环境）
python -m py_compile modules-src\hello-bastion\modules\__init__.py

# description.json 校验
python -c "import json; json.load(open(r'modules-src/hello-bastion/description.json', encoding='utf-8')); print('ok')"

# 前端 ESM 语法校验（Node 24 可直接检查 export）
node --check modules-src\hello-bastion\web\index.js

# 打包校验
python bastion-backend\tools\pack_module.py modules-src\hello-bastion --check
```

不需要跑 `tsc`：示例没有 TypeScript/JSX。若本机 Node 较老、把 `export` 当 CommonJS 报错，把 `web/index.js` 临时复制成 `.mjs` 再执行 `node --check`。

启动后端后，可以先用管理员 JWT 直接打接口：

```powershell
$token = "<登录接口返回的 token>"
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/modules/hello-bastion/api/stats" `
  -Headers @{ Authorization = "Bearer $token" }

Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/modules/hello-bastion/api/credentials-sample?host_id=1" `
  -Headers @{ Authorization = "Bearer $token" }
```

## 安装与打开

系统不内置任何模块，全新实例是 0 个模块；本示例也要先打包再上传，不能从 `modules-src/` 直接加载。

1. 以管理员账号登录堡垒机前端；
2. 打开「系统设置 → 扩展模块」；
3. 上传仓库根目录的 `hello-bastion-1.1.0.mod.ops_ext`；
4. 安装页会先调用 `upload-check` 展示 `description.json` 预览（名称、版本、作者、标签、演示图）；
5. 因为它是实验性模块，弹窗要求**手工敲**一遍确认语（粘贴不算）：
   `我知道这是实验性功能，使用不当的话也许会造成系统损毁`
6. 确认安装，侧边栏出现「Hello Bastion」入口；从侧边栏进入，完整路由是 `/m/hello-bastion/`。

安装到磁盘后的布局：

```text
bastion-backend/instance/modules/hello-bastion/
├── package/                  # 解压后的包（本仓库源码的拷贝）
├── state.json                # enabled / installedAt 等状态
└── data/                     # 模块私有可写目录（本例会写 hello-bastion.json）
```

> 卸载会删除 `data/`。如果模块以后接入外部存储，卸载前自行备份。

## 常见问题

**页面显示「登录状态已失效」**<br>
`bastion_token` 不存在或已过期。重新登录后从侧边栏进入；`js` 模式下宿主的 `request` 可能已按全局策略跳登录。

**页面显示「当前账号没有使用该模块的权限」**<br>
模块 API 默认要求 `module:hello-bastion:use`。当前后端的角色接口只接受 `ALL_PERMISSIONS` 里的静态权限码，模块权限还没有合并进角色目录，所以**非超级管理员暂时无法在角色配置页获得该权限**；先用超级管理员验证，等宿主补齐模块权限目录后再授权（见白皮书附录 A 第 8 条）。

**`/stats` 返回 500**<br>
看模块日志：`GET /api/modules/hello-bastion/logs`（需 `setting:view`），或后端控制台里 `module.hello-bastion` 这个 logger 的输出。最常见原因是宿主模块实现还没把 `autoops.bastion` 门面装好。

**`credentials-sample` 返回 404**<br>
`host_id` 不存在，或当前登录账号看不到该资产。先用 `/api/hosts` 或资产页确认 ID；`account_id` 留空会走 SDK 的默认账号逻辑。

**页面空白或一直转圈**<br>
看浏览器 Console 与 Network：入口脚本是 `/api/modules/hello-bastion/asset/web/index.js`；确认 `window.AutoOpsSDK` 已注入，且 `mount()` 没有抛错。`window.AutoOpsModules['hello-bastion']` 必须注册。

**卸载再装会丢数据吗**<br>
会。卸载会删除 `instance/modules/hello-bastion/data/`，也不保留墓碑或恢复入口；重新安装就是把同一份 `.mod.ops_ext` 再上传一次。示例只在里面写了一个标记文件，没有真实数据，但正式模块要按这个行为设计。

## 安全边界

Python 扩展在堡垒机进程内运行，等价于直接修改后端代码：它可以看到数据库、密钥和所有资产凭据。本示例只演示「能拿到什么」，并刻意把凭据收窄成用户名与长度。请只从可信来源安装 `.mod.ops_ext`，不要把 `web/` 当作秘密存放地——静态资源接口是免登录的，任何人都能读取 `web/`、`images/` 和 `README.md`。

本示例的 `credentials-sample` **没有**做资产级授权校验：拥有 `module:hello-bastion:use` 的账号可以传入任意 `host_id`。它只适合演示，不能作为生产实现照搬。生产模块若按资产返回敏感数据，必须在路由里校验当前用户对 `host_id` 的授权（参见白皮书 §1.3 与 §11 Q10）。

## 相关文档

- `docs/modules/CONTRACT.md` —— 模块系统实现契约（包格式、`description.json`、SDK、接口）
- `docs/modules/WHITEPAPER.md` —— 第三方开发者白皮书（本示例的完整走查见第 12 章）
