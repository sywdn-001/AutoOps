# 提交 PR 指南

> 英文版：[Pull request guide (English)](i18n/pr-guide.en.md)

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份 PR 指南里要是哪句话说得不清楚、或者流程上有你觉得别扭的地方，请大家多多谅解，
> 也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this pull request guide reads awkwardly, please
> bear with me (and tell me if you can).

---

## 先说结论：一份好 PR 长什么样

**一件事、能复现、有证据**。好的 PR 通常满足三条：

1. **小而聚焦** —— 一个 PR 只做一件事（修一个 bug、加一个功能、改一处文档）。顺手重构别的地方会让评审没法看。
2. **说清「为什么」** —— 光看 diff 能知道「改了什么」，但看不出「为什么非改不可」。
3. **能证明它真的работает** —— 贴出你实际跑过的命令与输出，而不是「应该没问题」。

## 第一步：动手之前，先花一分钟

- **搜一下已有 issue / PR**：可能有人已经在做，或者维护者已经解释过为什么不这么做。
- **读一遍你要改的文件开头的「文件简介」**：这个仓库每个自研源文件的开头都有一段
  文件简介，写着它负责什么、和哪些模块交互、有哪些容易踩的坑。**先读再改**，能省掉很多来回。
- **大改动先开 issue 商量**（见下面「哪些改动需要先商量」），别一口气写完两千行再被拒。
- 确认基线：从 `main` 最新代码切分支（`git switch -c feat/xxx main`）。

## 第二步：分支与提交信息

- 分支名建议 `feat/简短说明`、`fix/简短说明`、`docs/简短说明`，用英文小写加连字符。
- 提交信息用 **`feat(scope): 说明`** 或 **`fix(scope): 说明`**，例如：

  ```
  feat(webterm): WinRM 会话里支持就地清屏

  目标机是 Windows 时，cls 之前被当成普通命令发过去，会在 PowerShell 里报错。
  改成在字符终端层拦截：命中的命令不再下发，只清本地屏幕，并写一条 action=allow
  的审计（reason 里注明「本地清屏指令」）。

  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  ```

- **主题行一句话说清做了什么**，正文写「为什么这么改、怎么验证的」。
- 这个仓库的提交大多是**人机协作**产物（人提需求与验收、AI 参与实现），因此最后一行常带
  `Co-Authored-By:`；你如果用 AI 帮忙写了代码，也照这个加一行说明协作者。
- **别用 `git add -A` / `git add .`** —— 很容易把 `instance/` 里的数据库、密钥、
  `node_modules/`、`dist/` 一起提交上去。一个个文件指名加。
- Windows 上写提交信息**别用会加 BOM 的工具**（那个字节在 GitHub 上看不见，但会让
  `git cat-file` 这类工具的输出前面多三个字节）。用编辑器默认的 UTF-8 存即可。

## 第三步：提交前，门禁必须全绿

改了哪一块就跑哪一块，**并且把实际输出贴进 PR 说明**：

```bash
# 后端（Python）
cd bastion-backend
python -m pytest -q                       # 全量 705 个用例 / 33 个文件

# 前端（Node ≥ 22）
cd ant-design-pro
npx biome check                           # 风格与静态检查
npx tsc --noEmit                          # 类型检查
npx vitest run                            # 前端单测
npm run build                             # 必须能构建出 dist/
```

改到「真机会话」这一类功能（终端 / 文件 / 网关 / 远程桌面 / 审计链）时，再补跑对应的联调脚本：

| 脚本 | 覆盖 |
| --- | --- |
| `python tools/live_e2e_check.py --admin-password '<口令>'` | HTTP + Socket.IO + SSH 网关 + SFTP + AI 端到端 97 项 |
| `python tools/ui_check.py --admin-password '<口令>'` | 真实 Chrome 逐路由巡检 21 项 |
| `python tools/console_check.py --admin-password '<口令>'` | 真实 Chrome 驱动网页终端与文件管理器 33 项 |
| `python tools/winrm_gw_check.py --admin-password '<口令>'` | 网关里选 Windows（WinRM）主机跑命令 9 项 |
| `python tools/verify_audit_chain.py` | 离线校验审计流水的哈希链 |

**新增行为请补测试**（后端 `bastion-backend/tests/`、前端同名 `*.test.tsx`），
「改完手动点了一下没问题」不算验证。

## 第四步：PR 说明怎么写

标题同提交信息格式（`fix(webterm): …`）。说明建议四段，缺一段维护者就得回来问：

| 段落 | 写什么 |
| --- | --- |
| **改了什么** | 一两句，必要时列文件 |
| **为什么** | 触发它的现象 / 引用相关 issue（`Fixes #12`） |
| **怎么验证的** | 实际跑的命令 + 关键输出；截图见下方 |
| **影响面** | 会不会改数据表结构、审计格式、默认配置；老库升级要注意什么 |

- 界面上能看出来的改动**贴前后对比截图**（可以直接把图拖进输入框）。
- 涉及接口的改动贴请求与响应（F12 → Network 里那条的 URL、状态码、返回 JSON）。
- 涉及审计的改动贴审计记录：`action`、`result`、以及哈希链是否仍然连续。

## 第五步：提交前对着这张清单过一遍

- [ ] 一个 PR 只做一件事，没有夹带无关的重构或格式化
- [ ] 分支从 `main` 最新代码切出，且已经 rebase / 合并过最新 main
- [ ] 提交信息是 `feat(scope): …` / `fix(scope): …`，正文写了「为什么」
- [ ] 我没有用 `git add -A`，也确认没有把 `instance/`、`.env`、`node_modules/`、`dist/` 提交上去
- [ ] 改动的文件我读过它开头的「文件简介」
- [ ] 门禁跑过并且全绿，输出贴进了说明（后端 pytest 705；前端 biome / tsc / vitest / build）
- [ ] 新增行为补了测试或联调断言
- [ ] 我**没有改动换行符**（仓库用 `.gitattributes` 固定：源码 LF、`.bat`/`.cmd`/`.ps1` CRLF）
- [ ] 如果这次改动涉及界面或流程，我更新了对应文档与截图
- [ ] 如果用了 AI 帮忙，我在说明里如实写了，并且能解释每一处改动
- [ ] 里面没有密钥、口令、内网地址等敏感信息

## 第六步：提完之后

- 维护者可能会请你**拆小**、补测试或补说明；这不是刁难，是为了以后敢改这块代码。
- 评审期间**别 force-push 已经评审过的提交**，新增一个提交更省事；等合并前再决定要不要整理成一条。
- 合并后记得同步你的 fork（`git switch main && git pull upstream main`），下次改动从最新 main 切。
- 讨论卡住时直说「我不确定这里该怎么做」比硬撑更快解决。

## 哪些改动需要先开 issue 商量

不是不欢迎，而是这几类改完很可能白做，先商量省两个人的时间：

- **大重构**（改目录结构、换状态管理、换构建链）
- **加依赖**（尤其是体积大、许可证不清、或者引入新的运行时）
- **改数据模型 / 数据库结构**（现在没有迁移框架，靠 `ensure_schema()` 加字段，删改字段要先想好老库怎么办）
- **改审计格式 / 哈希链算法**（会破坏历史可验证性）
- **改安全默认值**（默认口令策略、会话超时、票据有效期）
- **动上游模板残留代码与语言包**（除非确实有 bug）

## 关于 AI 辅助的代码

这个项目本身就是**人机协作**做出来的（见根 [README](../README.md) 的「关于本项目」），
所以 AI 参与一点都不丢人，但有三条底线：

- **如实标注**：说明里写清哪些部分是 AI 参与写的。
- **你得能解释它**：评审问「这行为什么这么写」要答得上；答不上就先别提交。
- **别提交不懂的代码**：尤其是涉及权限判断、凭据处理、审计写入这三块。

## 安全相关

- **安全漏洞不要走公开 PR**：公开的修复 diff 等于把利用方法一起发出去。请按
  [SECURITY.md](../SECURITY.md) 走私下渠道，修好之后再约一个公开的时间点。
- PR 里**不要贴密钥与口令**（`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`、
  资产口令、`instance/` 里的内容），要举例就自己造一个假值。

## 最后一句

提 PR 是帮忙，不是添麻烦。你把「为什么改、怎么验的」写清楚，这个项目就往前走一步——谢谢。
