# js-demo

AutoOps 堡垒机的 **`js` 模式**扩展示例。`hello-bastion`、`rdp-gateway` 也都是 `js` 模式；本示例最小化，只演示 SDK 基础能力。

这个示例故意不引入构建链：

- `web/index.js` 是浏览器可直接执行的 ES Module；
- **没有一条 `import` 语句**，不打包 React、ReactDOM、antd；
- 所有运行时都从宿主注入的 `window.AutoOpsSDK` 取；
- UI 全部用 `React.createElement` 写，不需要 JSX/Babel。

## 演示了什么

1. 从 `window.AutoOpsSDK` 取同一份 `React` / `ReactDOM` / `antd` / `request` / `getToken` / `message`；
2. 兼容宿主把 `antd` 注入成普通对象或 `Promise` 两种形态；
3. 用宿主 `request` 调 `GET /api/health` 并展示结果；
4. 用 antd `Switch` 启停一个每秒 +1 的计数器，卸载时清理定时器；
5. 用宿主 `App.useApp()` 的 `message.success()` 弹提示；
6. React 19 优先走 `ReactDOM.createRoot()`，老版本回退 `ReactDOM.render()`；
7. `unmount()` 幂等清理 React root 与容器节点。

## 目录结构

```text
js-demo/
├── description.json       # id=js-demo，nav 一项，type="js"，entry="web/index.js"
├── web/
│   └── index.js           # 无 import 的 ESM，注册 window.AutoOpsModules['js-demo']
└── README.md
```

## 运行契约

宿主会：

1. 先构造并挂载 `window.AutoOpsSDK`；
2. 注入 `<script type="module" src="/api/modules/js-demo/asset/web/index.js">`；
3. 脚本加载后读取 `window.AutoOpsModules['js-demo']`，调用 `mount(el, sdk)`；
4. 离开页面时调用 `unmount()`，并移除 script。

因此 `web/index.js` 除了 `export function mount/unmount`，还必须把同一个对象注册到全局：

```js
window.AutoOpsModules = window.AutoOpsModules || {};
window.AutoOpsModules['js-demo'] = { mount, unmount };
```

只写 `export` 不注册，宿主会报“模块脚本已加载，但没有注册 mount()”。本示例两种都保留。

`mount()` 是 `async`：它会先占位，再 `await` 宿主注入的 `antd`（兼容 Promise），最后创建 React root。宿主不 `await` 它，这没有问题；真正的 UI 会在 antd 就绪后出现。

## 打包

在仓库根目录执行：

```powershell
python bastion-backend\tools\pack_module.py modules-src\js-demo -o .
```

只校验：

```powershell
python bastion-backend\tools\pack_module.py modules-src\js-demo --check
```

产物名由 `description.json` 的 `id` 和 `version` 决定：

```text
js-demo-1.0.0.mod.ops_ext
```

产物输出到仓库根目录。系统不内置任何模块，`modules-src/` 只是源码和打包输入，运行时不会自动安装。

如果改了 `version`，文件名会跟着变；不要手工重命名来伪装版本。

## 安装与打开

1. 用管理员登录；
2. 「系统设置 → 扩展模块」上传仓库根目录的 `js-demo-1.0.0.mod.ops_ext`；
3. 它不是实验性模块，不需要烧杯确认；
4. 侧边栏出现「JS 模式示例」，进入后 URL 是 `/m/js-demo/ui`。

卸载就是真卸载，没有墓碑或一键重装；要恢复就重新上传同一份包。

模块 API 默认要求 `module:js-demo:use` 权限。当前后端的角色接口只接受静态 `ALL_PERMISSIONS` 中的权限码，模块权限还没有合并进角色目录，因此**非超级管理员暂时无法在角色页获得该权限**；先用超级管理员验证（这与 `hello-bastion` README 中的说明一致）。

## 页面行为

- 标题：`JS 模式示例 / JS Mode Demo`，右上角 `js mode` 标签；
- 「调用 GET /api/health」：通过宿主 `request` 请求，展示 `service` / `gateway` / `port`，失败展示后端信封的 `message`；
- JWT 状态：只显示 `getToken()` 是否存在，不打印 token 本身；
- Switch 计数器：打开后每秒 +1，关闭即停；另有重置按钮；
- 「触发 message.success()」：调用宿主 `message`，验证 antd App 上下文复用；
- 所有内容都在传入的 `el` 内，不修改宿主导航或全局布局。

## 本地校验（不需要启动宿主）

```powershell
# Node 24 会把带 export 的 .js 当 ESM 解析
node --check modules-src\js-demo\web\index.js

# description.json
python -c "import json; json.load(open(r'modules-src/js-demo/description.json', encoding='utf-8')); print('ok')"
```

如果本机 Node 较老、`node --check` 不识别 `export`，复制成临时 `.mjs` 再检查：

```powershell
Copy-Item modules-src\js-demo\web\index.js "$env:TEMP\js-demo-check.mjs"
node --check "$env:TEMP\js-demo-check.mjs"
```

## 排障

| 现象 | 排查 |
| --- | --- |
| 页面一直转圈或“模块入口脚本加载失败” | Network 看 `asset/web/index.js` 是否 404；Console 看语法错误 |
| “脚本已加载，但没有注册 mount()” | 检查 `window.AutoOpsModules['js-demo']` 是否赋值；模块 id 是否写错 |
| `window.AutoOpsSDK` 未注入 | 宿主版本不支持 js 模式，或 SDK 在脚本之后才注入 |
| 页面空白但没有报错 | 看 `mount()` 的 Promise 是否 reject；本示例会把错误写进容器 |
| 403 | 当前账号没有 `module:js-demo:use`；先用超级管理员 |
| React hooks 报错 | 检查是否自己又打包了一份 React；必须用 `window.AutoOpsSDK.React` |

## 相关文档

- [`docs/modules/CONTRACT.md`](../../docs/modules/CONTRACT.md) —— 模块系统契约
- [`docs/modules/WHITEPAPER.md`](../../docs/modules/WHITEPAPER.md) —— 第三方开发者白皮书，§6 讲 `iframe` / `js` 两种模式
