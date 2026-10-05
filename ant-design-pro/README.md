> **文件简介（AI 生成）**：AutoOps 堡垒机**前端**的说明文档，替换 ant-design-pro
> 模板自带的 README。这里只讲前端：怎么装、怎么跑、怎么打包、怎么和后端联调、
> 目录怎么分层、模板里哪些东西被删掉了。整机部署与功能说明见仓库根目录的
> [README](../README.md) 和 [docs/](../docs/)。

# AutoOps 堡垒机 · 前端

堡垒机后台管理界面。技术底座是 [ant-design-pro](https://github.com/ant-design/ant-design-pro)
v6（Umi Max 4 + React 19 + Ant Design 6），**业务页面全部为本项目自研**：
主机与协议端点、身份与权限、访问授权、命令/文件策略、网页终端（SSH / WinRM）、
WebRDP 远程桌面与录像、SFTP 文件管理器、审计中心（会话 / 命令 / 文件 / AI 对话 /
录像 / 操作日志与哈希链）、AI 运维助手、系统设置与个人设置。

> 本目录保留模板的 `LICENSE`（MIT），模板自带的说明文档、CI、mock、Cloudflare
> Worker、husky/lint-staged 钩子等与本项目无关的文件已删除，详见文末「相对模板的改动」。

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份前端说明（怎么装、怎么跑、怎么打包、怎么和后端联调）里要是有写得不够好的地方，
> 请大家多多谅解，也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this frontend README (install, run, build, and how
> it talks to the backend) reads awkwardly, please bear with me (and tell me if you can).

## 环境要求

| 项目 | 版本 | 说明 |
| --- | --- | --- |
| Node.js | **≥ 22**（开发机实测 v24.16.0） | `package.json` 的 `engines.node` 已声明 |
| npm | 随 Node 附带 | 仓库用 `package-lock.json` 锁定依赖 |
| 后端 | Flask 服务在 `127.0.0.1:5000` | 前端所有 `/api/`、`/socket.io/` 请求都代理到它 |

## 快速开始

```bash
# 1) 装依赖（首次约 1-3 分钟）
cd ant-design-pro
npm install

# 2) 起后端（另开一个终端，必须先起来，否则登录会 502）
cd ../bastion-backend
python run.py            # API http://127.0.0.1:5000，SSH 网关 0.0.0.0:2222

# 3) 起前端开发服务器
cd ../ant-design-pro
npm run dev              # http://localhost:8000，默认管理员 admin / admin123
```

浏览器打开 <http://localhost:8000>，用 `admin / admin123` 登录（首次登录会强制改口令）。

### 常用脚本

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 开发服务器（`UMI_ENV=dev MOCK=none`，热更新，端口 8000） |
| `npm run build` | 生产构建，产物在 `dist/`（后端可直接托管，或交给 Nginx） |
| `npm run preview` | 本地预览已构建的产物（端口 8000） |
| `npm run lint` | `biome lint` + `tsc --noEmit`，等价于门禁里的静态检查 |
| `npm run biome` | `biome check --write`，格式化 + 自动修可修的问题 |
| `npm run tsc` | 只做类型检查（TS strict） |
| `npm test` | `vitest run`，前端单测 |
| `npm run test:coverage` | 带覆盖率的单测 |
| `npm run analyze` | 构建产物体积分析 |

> 门禁习惯：改完代码跑 `npx biome check` → `npx tsc --noEmit` → `npx vitest run` →
> `npm run build`，四条全绿才算完。

## 和后端怎么联调

- `config/proxy.ts` 把 `/api/`、`/socket.io/`（含 WebSocket 升级）代理到
  `http://127.0.0.1:5000`；想指向别的后端就用环境变量 `BASTION_API`，例如
  `BASTION_API=http://192.168.0.10:5000 npm run dev`。
- 登录成功后 JWT 存在 `localStorage.bastion_token`，之后所有请求带
  `Authorization: Bearer <token>`；接口信封固定为
  `{ success, message, data }`，列表额外带 `total / page / pageSize`。
- 终端与文件管理器走 Socket.IO（`app/webterm/`、`app/sftp/` 两条命名空间链路），
  代理必须透传 `Upgrade` / `Connection`，否则终端连不上。
- 远程桌面客户端是 WASM：`public/rdp_client_bg.wasm` 是运行期资源，**不要删**。

## 目录结构（只列本项目相关的）

```
ant-design-pro/
├── config/                     # Umi 构建配置
│   ├── config.ts               # 路由/插件/代理/favicon/tailwind 等总入口
│   ├── defaultSettings.ts      # ProLayout 默认设置（logo、标题、主题）
│   ├── proxy.ts                # /api、/socket.io → 127.0.0.1:5000
│   └── routes.ts               # 菜单与页面路由（权限码在前端只做体验层）
├── public/                     # 静态资源：favicon、logo、rdp_client_bg.wasm
├── src/
│   ├── app.tsx                 # 运行时：布局、鉴权、错误处理
│   ├── access.ts               # 前端权限开关（后端仍会再校验一遍）
│   ├── components/Bastion/     # 业务通用组件（状态条、工具条、抽屉等）
│   ├── pages/bastion/          # 业务页面（仪表盘/终端/文件/审计/AI/资产…）
│   ├── pages/user/login/       # 登录页（模板原页面，接的是堡垒机登录接口）
│   ├── pages/exception/404/    # 404（唯一保留的模板异常页）
│   ├── services/bastion/       # 接口封装（endpoints.ts 是后端接口清单）
│   └── pages/Welcome.tsx 等    # 本项目早期页面
├── tests/setupTests.ts         # vitest 环境准备
└── package.json                # 依赖与脚本（见上表）
```

## 相对模板的改动

**保留**：`config/`、`src/`（业务页面重写）、`public/`（favicon / logo / RDP wasm）、
`tests/setupTests.ts`、`types/index.d.ts`、`biome.json`、`tailwind.css` +
`tailwind.config.js`（`config/config.ts:191` 启用了 tailwind 插件）、`LICENSE`。

**删除**：

- 模板的元文件与工程设施：`README.md` / `README.zh-CN.md`、`docs/`（官方 cheatsheet 与
  roadmap）、`CLAUDE.md` / `AGENTS.md` / `.claude/` / `.agents/`、`CODE_OF_CONDUCT.md`、
  `.github/`（CI 与预览部署）、`codecov.yml`、`doctor.config.json`、`.husky/`、
  `.commitlintrc.js`、`.lintstagedrc`、`cloudflare-worker/`、`mock/`（假数据）、
  `scripts/`（模板脚手架）、`types/cache/`、`public/CNAME`（模板演示站域名）。
- **模板的示例页面**（`config/routes.ts` 里没有任何路由指向它们，属于死代码）：
  `src/pages/dashboard/`（analysis / monitor / workplace 三套演示大盘）、
  `src/pages/list/`、`src/pages/form/`、`src/pages/profile/`、`src/pages/result/`、
  `src/pages/table-list/`、`src/pages/chatbot/`、`src/pages/exception/{403,500}`、
  `src/pages/user/{register,register-result}` —— 一共 111 个文件。
  它们删掉之后 `npx tsc --noEmit` 才不再报 `Cannot find module '…/mock/utils'`。
- 本项目文件一律 LF 入库（根目录 [`.gitattributes`](../.gitattributes)：`* text=auto eol=lf`）：
  这个仓库在 Windows 上开发、部署到 Linux，放任 git 按平台转换会让 `biome` 因为
  「换行符与格式化结果不一致」整片报错。

**依赖与脚本**：`package.json` 去掉了 `husky`、`lint-staged`、`gh-pages`、
`react-doctor`、`@commitlint/*` 这些只服务于模板 CI/发布/钩子的依赖，并删掉了
`deploy`、`i18n-remove`、`lint-staged`、`openapi`、`record`、`simple`、`doctor`、
`start:no-mock` 脚本；`prepare` 改为 `max setup`（不再安装 git 钩子）。

## 门禁怎么跑

```bash
npx biome check          # 格式化 + lint（0 error，26 warning 是 CSS !important 之类的建议）
npx tsc --noEmit         # 类型检查
npx vitest run           # 单测
npm run build            # 生产构建（dist/ 由后端在启动时挂载）
```

> `src/app.test.tsx` 第一次 `import('./app')` 会把整个 Umi + antd 运行时拉进来，本机要几十秒，
> 所以那个文件用 `beforeAll` 预热，并把 `testTimeout` 放到 30s（见 `vitest.config.ts`）；
> 否则首个用例会以「超时」的样子假失败。

## 许可

前端目录沿用模板的 [MIT License](./LICENSE)；整个 AutoOps 项目也是
[MIT](../LICENSE)，版权归 `sywdn-001`。
