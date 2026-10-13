# Pull request guide

> 中文版：[提交 PR 指南](../%E6%8F%90%E4%BA%A4PR%E6%8C%87%E5%8D%97.md)

> **Nice to meet you, stranger!** I'm an eighth-grade student in Xinjiang, China. My English and my
> code are still a work in progress — if anything in this pull request guide reads awkwardly, please
> bear with me (and tell me if you can).
>
> **很高兴认识你，陌生人！** 我是一名初二学生，在中国新疆读初中。英语和写的代码都还在慢慢练，
> 这份 PR 指南里要是哪句话说得不清楚，请大家多多谅解，也欢迎直接指正。

---

## The short version: what a good PR looks like

**One thing, reproducible, with evidence.** A good pull request usually does three things:

1. **Stays small and focused** — one bug, one feature, one doc fix. Reformatting unrelated files makes the diff impossible to review.
2. **Explains *why*** — a diff shows what changed, not why it had to change.
3. **Proves it actually works** — paste the commands you ran and their real output, not "should be fine".

## Step 1: Before you start, spend one minute

- **Search existing issues and PRs** — someone may already be working on it, or a maintainer may have explained why it is done this way.
- **Read the file summary at the top of every file you are about to touch.** Every source file we wrote opens with a short summary: what it is responsible for, which modules it talks to, and which traps it already knows about. Reading it first saves a lot of back and forth.
- **Open an issue first for anything big** (see "Changes that need an issue first" below). Do not write two thousand lines and then find out the direction is wrong.
- Branch from the latest `main`: `git switch -c feat/your-thing main`.

## Step 2: Branch and commit message

- Branch names: `feat/short-name`, `fix/short-name`, `docs/short-name` — lowercase English with hyphens.
- Commit messages use **`feat(scope): summary`** or **`fix(scope): summary`**, for example:

  ```
  feat(webterm): support in-place clearing in WinRM sessions

  On Windows targets a typed `cls` used to be forwarded to the remote side and
  failed inside PowerShell. It is now intercepted in the character-terminal
  layer: the command is not sent, the local screen is cleared, and an
  action=allow audit entry records it with a "local clear-screen" reason.

  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  ```

- **The subject line says what changed**; the body says why it had to change and how you verified it.
- This repository is built by **a human and an AI working together**, so many commits end with a `Co-Authored-By:` line. If you used AI help, add one the same way.
- **Do not use `git add -A` / `git add .`** — it is very easy to commit `instance/` (database and keys), `.env`, `node_modules/` or `dist/` by accident. Name the files you mean.
- On Windows, do not write the commit message with a tool that prepends a BOM — that byte is invisible on GitHub but shows up in tools like `git cat-file`. Plain UTF-8 from your editor is fine.

## Step 3: The gates must be green before you push

Run the gates for whatever you changed, **and paste the real output into the PR description**:

```bash
# Backend (Python)
cd bastion-backend
python -m pytest -q                       # 705 tests across 33 files

# Frontend (Node >= 22)
cd ant-design-pro
npx biome check                           # style and static checks
npx tsc --noEmit                          # type check
npx vitest run                            # unit tests
npm run build                             # must produce dist/
```

If you touched anything that talks to real machines (terminal, files, gateway, remote desktop, audit chain), also run the matching end-to-end script:

| Script | Coverage |
| --- | --- |
| `python tools/live_e2e_check.py --admin-password '<password>'` | HTTP + Socket.IO + SSH gateway + SFTP + AI, 97 assertions |
| `python tools/ui_check.py --admin-password '<password>'` | Real Chrome walkthrough of the admin UI, 21 assertions |
| `python tools/console_check.py --admin-password '<password>'` | Real Chrome driving the web terminal and file manager, 33 assertions |
| `python tools/winrm_gw_check.py --admin-password '<password>'` | Running a command on a Windows (WinRM) host through the gateway, 9 assertions |
| `python tools/verify_audit_chain.py` | Offline verification of the audit hash chain |

**New behaviour needs a test** (backend `bastion-backend/tests/`, frontend a matching `*.test.tsx`). "I clicked through it once and it looked fine" is not verification.

## Step 4: How to write the PR description

The title uses the same format as the commit message (`fix(webterm): ...`). The description should answer four questions — if one is missing, a maintainer has to come back and ask:

| Section | What to write |
| --- | --- |
| **What changed** | One or two sentences, list the files if useful |
| **Why** | The symptom that triggered it, or the issue it fixes (`Fixes #12`) |
| **How you verified it** | The commands you actually ran plus the key output; screenshots below |
| **Impact** | Does it change the database schema, the audit format or a default setting? What about upgrading an existing install? |

- For anything visible in the UI, **attach before/after screenshots** (you can drag images straight into the box).
- For API changes, paste the request and response (F12 → Network: URL, status code, JSON body).
- For audit-related changes, paste the audit entry: `action`, `result`, and whether the hash chain is still continuous.

## Step 5: Checklist before you hit “Create”

- [ ] One PR does one thing; no unrelated refactoring or reformatting
- [ ] The branch is based on the latest `main` (rebased or merged)
- [ ] The commit message is `feat(scope): ...` / `fix(scope): ...` with a body explaining why
- [ ] I did **not** use `git add -A`, and `instance/`, `.env`, `node_modules/`, `dist/` are not in the diff
- [ ] I read the file summary at the top of every file I changed
- [ ] The gates pass and I pasted the output (backend pytest 705; frontend biome / tsc / vitest / build)
- [ ] New behaviour has a test or an end-to-end assertion
- [ ] I did **not** change line endings (the repository fixes them with `.gitattributes`: source is LF, `.bat`/`.cmd`/`.ps1` are CRLF)
- [ ] If the change affects the UI or a workflow, I updated the matching docs and screenshots
- [ ] If AI helped, I said so in the description and I can explain every change
- [ ] No secrets, passwords or internal addresses anywhere in the diff

## Step 6: After you submit

- A maintainer may ask you to **split it up**, add tests, or explain more. That is not gatekeeping — it is so the code can be changed again later without fear.
- While a review is in progress, **do not force-push over commits that have already been reviewed**; add a new commit instead and decide about squashing just before merging.
- After it is merged, sync your fork (`git switch main && git pull upstream main`) and branch from the fresh `main` next time.
- If you are stuck, saying “I am not sure how this part should be done” gets you further than guessing.

## Changes that need an issue first

Not because we do not want them, but because these are likely to be wasted work if the direction is wrong:

- **Large refactors** (directory layout, state management, build chain)
- **New dependencies** (especially heavy ones, unclear licences, or a new runtime)
- **Data model / database changes** (there is no migration framework: `ensure_schema()` only adds columns, so removing or retyping a column needs a plan for existing installs)
- **Changes to the audit format or the hash-chain algorithm** (this breaks the ability to verify history)
- **Changes to security defaults** (password policy, session timeout, ticket lifetimes)
- **Touching the leftover upstream template code and locale bundles** (unless there is a real bug)

## About AI-assisted code

This project is built **by a human and an AI together** (see “About this project” in the [root README](../../README.md)), so AI help is nothing to hide. Three ground rules:

- **Say so** — mention in the description which parts were AI-assisted.
- **Be able to explain it** — if a reviewer asks "why is this line like that", you should be able to answer. If you cannot, do not submit it yet.
- **Do not submit code you do not understand** — especially around permission checks, credential handling, and audit writes.

## Security

- **Security fixes do not go through a public PR.** A public diff is a public exploit. Follow [SECURITY.md](../../SECURITY.md) and report privately; we can publish the fix at an agreed time afterwards.
- **Do not paste secrets** into a PR (`BASTION_SECRET_KEY` / `BASTION_JWT_SECRET` / `BASTION_FERNET_KEY`, asset passwords, anything from `instance/`). If you need an example, invent a fake value.

## One last thing

Filing a pull request is help, not a burden. Write down what you changed and how you verified it, and the project moves one step forward — thank you.
