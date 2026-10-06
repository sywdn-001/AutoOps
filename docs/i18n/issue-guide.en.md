# Issue guide

> 中文版：[提交 Issue 指南](../%E6%8F%90%E4%BA%A4Issue%E6%8C%87%E5%8D%97.md)

> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this issue guide reads awkwardly, please bear
> with me (and tell me if you can).
>
> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份 Issue 指南的英文版里要是有哪句话讲得绕，请大家多多谅解，也欢迎直接指正。

---

## The short version: what a good issue looks like

Four things, and you are done: **where it happened, how to reproduce it, what you expected, what
actually happened** — plus the version and the environment. The thing that really slows a maintainer
down is not a hard bug, it is a report like "it just doesn't work": there is nothing to act on.

Where to file: <https://github.com/sywdn-001/AutoOps/issues/new/choose> (click "New issue" and you
will see the three forms).

## Step 1 — spend 30 seconds before you write anything

- **Search existing issues (including closed ones).** Search the exact English words from the error
  message; that hits far more often than a prose description.
- **Skim the docs.** Plenty of "is this a bug or is it meant to be like this?" questions are already
  answered there:

| Your situation | Read this first |
| --- | --- |
| You do not know where to start | [Illustrated tutorial](tutorial.en.md) |
| You want to know what a page or button does | [User manual](manual.en.md) |
| Install problems, busy ports, keys | "Quick start / Deploy to production" in the root [README](../../README.md) |
| You want the design rationale | [Requirements matrix](../需求对照.md), [Audit & security](../审计与安全.md) |
| You want to know whether this is a known issue | [Verification log](../验证记录.md) (it records the pitfalls) |

- Check that you are on the latest code: compare `git rev-parse --short HEAD` with `main`.

## Step 2 — pick the right template

| Template | When to use it | Required |
| --- | --- | --- |
| 🐞 **Bug report** | Something is broken, or behaviour disagrees with the docs | version, environment, steps, expected vs actual |
| 💡 **Feature request** | You want a capability added or changed | the use case, what is painful today, the shape you want |
| ❓ **Question** | You followed the docs and are still stuck | what you are trying to do, where you got stuck, what you already read |

Please keep the fields the form marks as required — that is the minimum needed to reproduce.
**Do not use these templates for security issues**; see the last section.

## Step 3 — how to write the title

- `[area] symptom`, e.g. `[Web terminal] cls does not clear the screen in a WinRM session`,
  `[SSH gateway] the host menu does not list my granted hosts`.
- One line describing the symptom; do not paste the whole traceback into the title.
- "HELP", "URGENT" and similar titles carry zero information and only make it slower.

## Step 4 — version and environment (just paste the command output)

```bash
cd bastion-backend
git rev-parse --short HEAD                    # the version you run
python -V                                     # backend Python
curl -s http://127.0.0.1:5000/api/health      # whether the gateway is enabled, etc.
```

For frontend issues add three more: browser and version, `node -v`, and whether you run
`npm run dev` or use the `dist/` bundle served by the backend (the "the UI did not update" symptom
looks completely different in those two setups).

Also state the managed host, e.g. `Ubuntu 22.04 / ssh:22` or
`Windows Server 2025 / winrm:5985 + rdp:3389`.

## Step 5 — write reproduction steps that someone else can follow

- **Number them starting from login**, no skipped steps: `1. sign in as admin → 2. open "Assets / Hosts" → 3. click "Edit" in the win-75 row → 4. …`
- Always reproducible? Say "always". Seen it once or twice? Say "intermittent, ~2 out of 10 tries" —
  **that number is valuable**, do not drop it.
- If you narrowed it down to a condition (only on Edge, only WinRM hosts, only hosts with Chinese
  names), definitely say so; that is the most valuable part of the report.

## Step 6 — evidence: screenshots, logs, audit ids

- **UI issues**: drag the screenshot into the box and mark the offending row.
- **API issues**: in F12 → Network, the URL, status code and response JSON of that request; the red
  message from the Console, verbatim.
- **Backend issues**: the output of the terminal running the service, from the error upwards
  (twenty or thirty lines is fine).
- **"Who connected to which machine, and when"**: the id, timestamp and `action` of that record in
  the audit centre — far better than any prose.
- Never paste secrets (`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`) or asset
  passwords; redact them first.

## Step 7 — run this checklist before hitting submit

- [ ] I searched the issues (closed ones too); this is not a duplicate
- [ ] I run the latest `main` (or I stated the tag/commit I use)
- [ ] The steps start from login and someone else can follow them
- [ ] I wrote both "what I expected" and "what happened"
- [ ] I attached the real error text / logs / screenshots, not just "it doesn't work"
- [ ] I stated the environment: Python, Node or browser, bastion OS, managed host OS and endpoints
- [ ] I leaked no keys or passwords in the issue

## Step 8 — after you submit

- The maintainer will add labels (`bug` / `enhancement` / `question`) and may ask follow-up questions.
  **Issues are not deleted** — they become the context of that problem.
- If you find the cause yourself, **comment on the issue**, even when the conclusion is "my mistake,
  not a bug" — that clarification is worth more than silently closing it.
- Want to fix it yourself? See the "Contributing" section of the root [README](../../README.md)
  (commit message format, the gates that must pass).

## Questions you may have to answer later — get ahead of them

| They will ask | Answer it up front with |
| --- | --- |
| Which version? | `git rev-parse --short HEAD` |
| Did you change any config? | the `BASTION_*` variable names you set (**never the values**) |
| Any backend logs? | the relevant block from the terminal running the service |
| Does it happen in another browser / host? | the result if you tried, or a plain "I did not try" |
| Is there an audit record? | the id and `action` from the audit centre |
| Frontend or backend? | F12 → Network: a 4xx/5xx means backend; a request that never left means frontend |

## Three things that should not be a public issue

- **Security vulnerabilities** (privilege escalation, leaked keys, command injection, audit
  bypass…): please use "Report a vulnerability" in the repository's **Security** panel (if you
  cannot see that entry, message the maintainer privately). **Do not post details or exploit steps
  in a public issue.** Supported versions, scope, timelines and safe harbor live in the same
  [SECURITY.md](../../SECURITY.md).
- **Something you can already fix yourself**: a PR is more welcome — just mention "fixes #NN" in it.
- **A report with no reproduction information**: it will be asked for more detail, and closed if
  none is available. That is not a lack of welcome, it is simply not actionable.

## One last thing

Filing an issue is help, not a burden. Write down how to reproduce it, and this project ends up with
one fewer pitfall — thank you.
