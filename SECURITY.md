# 安全策略 / Security Policy

本文件是 AutoOps 堡垒机的安全策略：**支持的版本、怎么私下报告漏洞、哪些问题算安全问题、我们会怎么处理**。
GitHub 会自动把根目录的这份文件展示在仓库 **Security** 面板里。

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份安全策略里要是哪句话说得不清楚，请大家多多谅解，也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this security policy reads awkwardly, please
> bear with me (and tell me if you can).

---

## 1. 支持的版本 / Supported Versions

| 版本 | 是否支持 | 说明 |
| --- | --- | --- |
| `main` 分支的最新提交 | ✅ 是 | 唯一的支持线，修复直接合入 `main` |
| 更早的提交、fork、自己打包的快照 | ❌ 否 | 请先更新到最新 `main` 再复现，旧快照不再单独出补丁 |
| 历史 tag（`green-*` 等里程碑） | ⚠️ 仅存档 | 只作为开发路上的记录，不承诺安全修复 |

运行环境：后端 Python 3.13（Flask 3.1），前端 Node ≥ 22（只影响构建）。不受支持的环境不保证安全修复。

## 2. 报告漏洞 / Reporting a Vulnerability

**请不要开公开 issue、也不要在讨论区或聊天群里贴细节。**

- **首选**：仓库 **Security** 面板 → 「Advisories」→ **Report a vulnerability**
  （直达：<https://github.com/sywdn-001/AutoOps/security/advisories/new>）——
  这是私密披露，只有维护者能看到。
- 看不到这个入口时：在 GitHub 上私信维护者 [@sywdn-001](https://github.com/sywdn-001)，
  第一条消息只说「有安全问题想私下聊」，**不要附利用细节**，等确认渠道后再发。

报告里尽量包含这些（有多少写多少）：

- 受影响的提交或版本：`git rev-parse --short HEAD` 的输出；
- 出问题的组件：SSH 网关 / 网页终端（SSH、WinRM）/ 文件管理器（SFTP）/ WebRDP / REST 接口 /
  权限与角色 / 策略引擎 / 审计与哈希链 / AI 助手 / 部署配置；
- 影响：能拿到什么（越权操作？读到资产口令？改动审计记录？拒绝服务？），
  以及需要什么前提（是否已登录、什么角色、能否直连某个端口）；
- 复现步骤或 PoC：脚本、HTTP 请求、截图都可以，能在**你自己搭的实例**上稳定复现最好；
- 是否已公开、是否已有 CVE、是否希望你被署名；
- 你建议的修复方向（可选，但非常有用）。

## 3. 我们会怎么处理 / What to expect

| 时间 | 动作 |
| --- | --- |
| 3 天内 | 确认收到（人工回复；这是业余时间维护的学生项目，偶尔会慢一两天） |
| 7 天内 | 给出初步结论：确认是漏洞 / 需要更多信息 / 判定不在范围内 |
| 30 天内 | 高危问题给出修复或缓解方案，并合入 `main` |
| 修复后 | 在 GitHub Security Advisory 中公布；默认按 90 天协调披露，你同意的话可以更早 |
| 全程 | 除非你要求匿名，会在 Advisory 的 Credits 里署名致谢 |

如果 14 天以上没有任何回应，请在 Advisory 里追加一条提醒，或换个渠道再联系一次——不是不理你，是漏看了。

## 4. 算安全问题的 / In scope

判定标准就一条：**能不能破坏本项目承诺的东西**（谁能连哪台机、能执行什么命令、每条命令与输出都被记录且改不了）。
典型的有：

- **认证与授权**：伪造/重放 JWT；非管理员越权调用管理接口（如实际拿到 `host:manage`、`identity:manage`）；
  前端权限与后端校验不一致导致真实越权。
- **凭据保护**：资产口令、私钥、口令短语泄漏或明文落盘；接口在权限不足时仍回显敏感字段；
  三把密钥（`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`）的处理缺陷。
- **审计完整性**：哈希链能被静默改写、删除或绕过；命令/文件/AI 操作没有落审计；审计记录可伪造、
  可把来源伪装成别的会话或别的账号。
- **策略绕过**：命令策略、文件策略可被绕过（含本地清屏类指令、白名单/黑名单的绕过、
  SFTP 路径穿越、录像分片上传的越权写入）。
- **会话越权**：SSH 网关、网页终端、文件管理器、WebRDP 之间能串会话；会话号可枚举；
  Socket.IO 命名空间或终端事件没有鉴权。
- **远程代码执行与注入**：AI 工具调用被 prompt 注入后执行危险命令、绕过管理员确认；
  文件管理器/录像接口的参数注入或路径穿越。
- **默认配置风险**：默认密钥、`instance/` 目录权限导致凭据可被读走。
  （注意：`admin/admin123` 是文档里**明写**的初始口令，上线必须改——它本身不是漏洞；
  但能用它拿到本不该拿的东西，就是漏洞。）

## 5. 不算的 / Out of scope

- 已经能登录堡垒机所在主机（有 shell）、已经能改数据库或 `instance/` 文件之后做的事；
- 需要物理接触、需要在受害者浏览器装扩展、需要中间人劫持 TLS 的场景；
- 依赖库的已知 CVE，但**没有**通向本项目的利用路径（请给出路径，否则只能算「待升级依赖」）；
- 缺少安全响应头、Cookie 属性、CSP 之类的加固建议 → 当普通 issue 提就很好；
- 拒绝服务与暴力破解（除非代价极低、稳定、且确实能打挂服务）；
- 社会工程、钓鱼，以及对**别人部署的实例**或非你所有的被管主机发起的测试；
- 自动扫描器的原始输出（没有验证、没有影响说明的那种）。

## 6. 测试也请守规矩 / Safe harbor

- 只在**你自己搭建的实例**上测试；不要对公网实例、他人的实例、或不属于你的主机做测试。
- 不要读取、修改、删除别人的数据；测试产生的数据请自行清理，并在报告里说明你动过什么。
- 不要做影响可用性的压测或爆破，不要横向移动到本项目之外的系统。
- 善意、按本策略行事的测试与披露，维护者不会追究，并会尽量配合你的披露时间。

## 7. 部署方请注意 / For deployers

- 上线前**必须**改掉初始管理员口令、换掉三把密钥；`instance/`（SQLite 与密钥）只给运行用户可读。
- SSH 网关（默认 `2222`）与后台端口不要直接暴露公网，放到反向代理/防火墙后面并启用 TLS。
- 更多威胁模型见 [docs/审计与安全.md](docs/审计与安全.md)，部署注意事项见
  [README 的「安全须知」](README.md)，提普通问题见 [docs/提交Issue指南.md](docs/提交Issue指南.md)。

## 8. 语言 / Preferred languages

中文、English 都可以，我两种都会认真读。

---

# Security Policy (English)

This file is the security policy of AutoOps Bastion: **which versions are supported, how to report a
vulnerability privately, what counts as a security issue, and what happens after you report one.**
GitHub shows this file on the repository's **Security** tab.

## 1. Supported versions

| Version | Supported | Notes |
| --- | --- | --- |
| Latest commit on `main` | ✅ Yes | The only supported line; fixes land on `main` |
| Older commits, forks, self-built snapshots | ❌ No | Please update to latest `main` and retry; old snapshots get no backports |
| Historical tags (`green-*` milestones) | ⚠️ Archive only | Kept as a development trail, no security fixes promised |

Runtime: Python 3.13 for the backend (Flask 3.1), Node ≥ 22 for the frontend (build only).
Unsupported environments are not guaranteed to receive security fixes.

## 2. Reporting a vulnerability

**Please do not open a public issue, and do not post details in discussions or chat groups.**

- **Preferred**: repository **Security** tab → "Advisories" → **Report a vulnerability**
  (direct link: <https://github.com/sywdn-001/AutoOps/security/advisories/new>). This is private
  disclosure and only the maintainer can see it.
- If you cannot find that entry point: message the maintainer [@sywdn-001](https://github.com/sywdn-001)
  privately on GitHub, saying only that you have a security issue to discuss — **no exploit details in
  the first message**. Send those once a private channel is confirmed.

Please include as much of the following as you can:

- the affected commit (`git rev-parse --short HEAD`);
- the component: SSH gateway, web terminal (SSH / WinRM), file manager (SFTP), WebRDP, REST API,
  identity and roles, the policy engine, audit and hash chain, the AI assistant, deployment config;
- the impact: what an attacker gains (privilege escalation? reading asset credentials? editing audit
  records? denial of service?) and the preconditions (signed in? which role? can they reach a port?);
- reproduction steps or a PoC (script, HTTP request, screenshots) — ideally reproducible on **your
  own instance**;
- whether it is already public, whether a CVE exists, and whether you want credit;
- your suggested fix direction (optional, but very useful).

## 3. What to expect

| Timeline | What happens |
| --- | --- |
| Within 3 days | Acknowledgement (a human reply; this is a student project maintained in spare time, so a day or two late is possible) |
| Within 7 days | Initial verdict: confirmed / need more information / out of scope |
| Within 30 days | A fix or mitigation for high-severity issues, merged into `main` |
| After the fix | Published as a GitHub Security Advisory; 90-day coordinated disclosure by default, earlier if you agree |
| Throughout | You are credited in the advisory's Credits unless you ask to stay anonymous |

If you hear nothing for 14 days, please bump the advisory or reach out through another channel — it
means it was missed, not ignored.

## 4. In scope

The test is simple: **can it break what this project promises** (who may reach which machine, which
commands may run, and every command plus its output being recorded in a way that cannot be altered)?
Typically:

- **Authentication and authorisation**: forged or replayed JWTs; a non-admin reaching management
  endpoints (effectively obtaining `host:manage`, `identity:manage`); frontend permission checks and
  backend validation disagreeing so that real privilege escalation happens.
- **Credential protection**: asset passwords, private keys or passphrases leaked or stored in clear
  text; APIs echoing sensitive fields to under-privileged callers; flaws in how the three keys
  (`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`) are handled.
- **Audit integrity**: the hash chain being silently rewritten, deleted or bypassed; commands, file
  operations or AI actions not being recorded; audit records that can be forged or attributed to
  another session or account.
- **Policy bypass**: command or file policies that can be circumvented (including local clear-screen
  commands, allow/deny list bypasses, SFTP path traversal, unauthorised writes to recording chunks).
- **Session isolation**: sessions crossing between the SSH gateway, web terminal, file manager and
  WebRDP; enumerable session ids; Socket.IO namespaces or terminal events without authorisation.
- **Remote code execution and injection**: AI tool calls turned into dangerous commands via prompt
  injection, bypassing the admin confirmation; parameter injection or path traversal in the file
  manager or recording endpoints.
- **Insecure defaults**: default keys, or `instance/` permissions that let credentials be read.
  (Note: `admin/admin123` is the initial password **documented** for first login and must be changed
  in production — that alone is not a vulnerability. Using it to reach something you should not is.)

## 5. Out of scope

- Anything possible only after you already have a shell on the bastion host, or can edit the database
  or files under `instance/`;
- scenarios requiring physical access, a browser extension on the victim's machine, or TLS man-in-the-middle;
- known CVEs in dependencies **without** a path into this project (please show the path; otherwise it
  is just a dependency to upgrade);
- hardening suggestions such as missing security headers, cookie attributes or CSP → a normal issue is
  perfect for those;
- denial of service and brute force (unless it is cheap, reliable and actually takes the service down);
- social engineering, phishing, and testing **someone else's** deployment or hosts you do not own;
- raw scanner output with no verification and no impact statement.

## 6. Safe harbor

- Test only on **your own instance**; do not test public instances, other people's deployments, or
  hosts you do not own.
- Do not read, modify or delete other people's data; clean up whatever your testing created and say
  what you touched.
- Do not run load or brute-force tests that affect availability, and do not pivot to systems outside
  this project.
- Good-faith research and disclosure that follows this policy will not be pursued by the maintainer,
  who will also try to work with your disclosure timeline.

## 7. For deployers

- Before going live, **change the initial admin password and replace the three keys**; `instance/`
  (SQLite plus keys) should be readable only by the account running the service.
- Do not expose the SSH gateway (default `2222`) or the admin port directly to the internet — put them
  behind a reverse proxy or firewall and enable TLS.
- See [docs/审计与安全.md](docs/审计与安全.md) for the threat model, the "Security notes" section of
  the [README](README.md), and [docs/提交Issue指南.md](docs/提交Issue指南.md) for ordinary questions.

## 8. Preferred languages

Chinese or English — I read both carefully.
