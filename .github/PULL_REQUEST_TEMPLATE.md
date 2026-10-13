<!--
提交 PR 之前先看一眼指南 / Read the guide before you start:
  中文：docs/提交PR指南.md          英文：docs/i18n/pr-guide.en.md
下面用不到的小节请直接删掉，保持说明干净。/ Delete the sections that do not apply.
-->

> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 所以这份 PR 模板可能还有不周到的地方，请大家多多谅解，也欢迎直接指正。
>
> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — so this pull request template may still have rough edges,
> please bear with me (and tell me if you can).

<!-- 上面这段是项目作者的自述，PR 里可以留着，也可以删掉 / Keep or delete the note above. -->

## 改了什么 / What changed

<!-- 一两句说清这个 PR 做了什么 / One or two sentences -->

## 为什么 / Why

<!-- 触发它的现象，或引用的 issue：Fixes #12 / The symptom, or the issue: Fixes #12 -->

## 怎么验证的 / How I verified it

<!--
把你实际跑过的命令与关键输出贴在这里（别写「应该没问题」）：
Backend:  cd bastion-backend && python -m pytest -q
Frontend: cd ant-design-pro && npx biome check && npx tsc --noEmit && npx vitest run && npm run build
Paste the commands you actually ran and their output — not "should be fine".
-->

```text

```

## 影响面 / Impact

- [ ] 改了数据库结构 / database schema change
- [ ] 改了审计格式或哈希链 / audit format or hash chain
- [ ] 改了默认配置或安全默认值 / default settings or security defaults
- [ ] 老库升级需要注意 / existing installs need attention
- [ ] 纯文档或注释改动 / docs and comments only

## 自检清单 / Checklist

- [ ] 一个 PR 只做一件事，没有夹带无关重构 / one thing only, no unrelated refactoring
- [ ] 提交信息是 `feat(scope): …` / `fix(scope): …`，正文写了「为什么」/ conventional commit with a body
- [ ] 没有用 `git add -A`，diff 里没有 `instance/`、`.env`、`node_modules/`、`dist/`
- [ ] 改动的文件我读过它开头的「文件简介」/ I read the file summary at the top of each file
- [ ] 门禁跑过且全绿，输出已贴在上面 / gates pass, output pasted above
- [ ] 新增行为补了测试或联调断言 / new behaviour has a test
- [ ] 没有改动换行符（源码 LF、`.bat`/`.cmd`/`.ps1` CRLF）/ no line-ending churn
- [ ] 涉及界面或流程的改动，同步更新了文档与截图 / docs and screenshots updated
- [ ] 如果用了 AI 帮忙，我已如实说明并且能解释每一处改动 / AI help disclosed and understood
- [ ] 没有密钥、口令、内网地址 / no secrets or internal addresses

<!-- 没有界面改动就删掉这一节 / delete this section when there is no UI change -->

## 截图 / Screenshots

| 改之前 / Before | 改之后 / After |
| --- | --- |
|  |  |
