# 文档索引 / Documentation index

> AutoOps 堡垒机（开源运维堡垒机）的文档都在这个目录里。中文文档是主语言，英文文档放在
> `docs/i18n/` 下；两份内容各自独立、都可直接阅读。整机介绍、特性与部署见根
> [README](../README.md)。
>
> Every document of AutoOps Bastion lives here. Chinese is the primary language; the English
> versions sit under `docs/i18n/`. The project overview, features and deployment guide are in the
> root [README](../README.md) (Chinese) and [README.en.md](i18n/README.en.md) (English).

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份文档索引里要是有写得不够好的地方，请大家多多谅解，也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this documentation index reads awkwardly, please
> bear with me (and tell me if you can).

---

## 从这里开始 / Start here

| 文档 · Document | English | 内容 · What it covers |
| --- | --- | --- |
| [图文教程.md](图文教程.md) | [tutorial.en.md](i18n/tutorial.en.md) | **推荐第一份读的**：从起服务 → 纳管主机 → 建用户与角色 → 授权 → 网页终端 / 文件管理器 / WinRM / 远程桌面 → 查审计，18 个步骤每步配一张真实操作截图 |
| [使用手册.md](使用手册.md) | [manual.en.md](i18n/manual.en.md) | 按页面/场景逐条说明：管理员怎么配、运维人员几种接入方式、审计员看什么、AI 助手两种入口 |
| [审计与安全.md](审计与安全.md) | — | 审计数据流、哈希链与验签、凭据与密钥的处理、威胁模型与「超级管理员准入例外」 |
| [需求对照.md](需求对照.md) | — | 四条原始需求 + 扩展需求，逐条对应到实现位置与验证证据 |
| [验证记录.md](验证记录.md) | — | 门禁与真机联调的实测流水：每条判据、命令、实际输出与截图取证 |
| [i18n/README.en.md](i18n/README.en.md) | — | 仓库英文介绍（英文 README，与根 README 对等） |

> 只想跑起来：根 [README](../README.md) 的「快速开始」三条命令即可；想边看边做：
> 从 [图文教程](图文教程.md) 开始；想知道「为什么这么设计」：[需求对照](需求对照.md) +
> [审计与安全](审计与安全.md)。

---

## 目录里还有什么 / What else is in here

| 路径 · Path | 内容 · What it is |
| --- | --- |
| `screenshots/` | 60+ 张实测截图：`docs/验证记录.md` 的取证图 + 根 README 的界面图 + 教程配图 |
| `screenshots/tutorial/` | `图文教程.md` / `tutorial.en.md` 的 18 步配图（`01-login.png` … `18-ai-console.png`） |
| `i18n/` | 英文文档：`README.en.md`（仓库介绍）、`manual.en.md`（使用手册）、`tutorial.en.md`（图文教程） |

### 截图和横幅是怎么来的 / How the images are produced

截图不是画出来的，是脚本驱动**真实 Chrome**（CDP 协议）登录本机服务、真的连上演示目标机
（`e2e-demo-01`）与 Windows 主机（`win-75`）之后逐步拍下来的；改完代码重跑一次即可刷新，
文档里不会留下过期的界面：

```bash
cd bastion-backend
python -u tools/tutorial_shots.py --admin-password '<管理员口令>'   # 教程 18 步配图
python -u tools/ui_shots.py       --admin-password '<管理员口令>'   # README / 验证记录用图
```

两个脚本都有失败保护：先写 `.<名字>.new`，就绪判据满足才覆盖同名旧图；没就绪就保留旧图并在
结尾列出来。它们只负责出图，不参与判据（门禁见根 README 的「验证记录」）。

---

## 反馈 / Feedback

文档里如果出现与实现不一致的描述，**以代码与实测记录为准**，并欢迎开 issue 指出
（根 README 的「贡献指南」里有提交与门禁要求）。
