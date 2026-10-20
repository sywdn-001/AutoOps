"""按**英文界面**（`umi_locale=en-US`）截取 README 与英文教程要用的图。

复用 `ui_shots.py` 的 CDP 机制（同一个 `run_shots`，只是多传一个 `locale`）：

    # 前置：后端在跑（5000）、演示目标机在跑（tools/demo_ssh_target.py --port 2200）、
    #       Chrome 带 --remote-debugging-port=9222 在跑
    python -u tools/i18n_shots.py --token-file %TEMP%/cu_token.txt
    python -u tools/i18n_shots.py --only tutorial --only-step 01,02

产物：
    docs/screenshots/en/            README.en.md 用的 7 张（英文界面）
    docs/screenshots/tutorial-en/   tutorial.en.md 用的 18 张（英文界面）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_shots import (  # noqa: E402
    DEFAULT_BASE,
    DEFAULT_CDP,
    REPO_ROOT,
    Shot,
    run_shots,
)

OUT_README = REPO_ROOT / "docs" / "screenshots" / "en"
OUT_TUTORIAL = REPO_ROOT / "docs" / "screenshots" / "tutorial-en"

LIST_READY = "!!document.querySelector('.ant-table')"
CARD_READY = "!!document.querySelector('.ant-card')"
LAUNCHER_READY = "!!document.querySelector('.ant-table, .ant-card')"
XTERM_READY = "!!document.querySelector('.xterm-rows')"
FILES_READY = "!!document.querySelector('.bastion-files-pathbar')"
# 英文界面的状态条文案是 Connected（中文是「已连接」）
CONNECTED = (
    "((document.querySelector('.bastion-statusbar') || {}).innerText || '').includes('Connected')"
)
RDP_CONNECTED = (
    "document.querySelectorAll('canvas').length > 0 && "
    "((document.body.innerText) || '').includes('Connected')"
)
CLICK_EDIT_EN = """(() => {
  const rows = document.querySelectorAll('.ant-table-row');
  if (!rows.length) return 'NO_ROW';
  const btn = Array.from(rows[0].querySelectorAll('button, a'))
    .find((el) => ((el.innerText || '').trim()) === 'Edit');
  if (!btn) return 'NO_BTN';
  btn.click();
  return 'CLICKED';
})()"""

README_SHOTS: list[Shot] = [
    Shot("dashboard.png", "/dashboard", CARD_READY, settle=3.0, note="Dashboard"),
    Shot("multiproto-launcher.png", "/terminal", LAUNCHER_READY, settle=2.5, note="Launcher"),
    Shot("multiproto-hosts-list.png", "/assets/hosts", LIST_READY, settle=2.5, note="Host list"),
    Shot(
        "web-rdp-win75.png",
        "/rdp/console?hostId=5&accountId=5&title=win-75",
        RDP_CONNECTED,
        settle=10.0,
        timeout=90.0,
        note="WebRDP",
    ),
    Shot(
        "file-manager.png",
        "/files/console?hostId=2&accountId=2&title=e2e-demo-01",
        FILES_READY,
        settle=3.0,
        note="File manager",
    ),
    Shot("ai-console.png", "/ai", "!!document.querySelector('textarea, .ant-card')", settle=3.0, note="AI"),
    Shot("audit-chain-detail.png", "/audit/logs", LIST_READY, settle=3.0, note="Audit chain"),
    Shot(
        "ssh-gateway.png",
        "/system/gateway",
        CARD_READY,
        settle=3.0,
        note="SSH gateway settings",
    ),
]

TUTORIAL_SHOTS: list[Shot] = [
    Shot(
        "01-login.png",
        "/user/login",
        "!!document.querySelector('input[type=password]')",
        settle=2.0,
        anonymous=True,
        note="Login",
    ),
    Shot("02-dashboard.png", "/dashboard", CARD_READY, settle=3.0, note="Dashboard"),
    Shot("03-hosts.png", "/assets/hosts", LIST_READY, settle=2.5, note="Hosts"),
    Shot(
        "04-host-form.png",
        "/assets/hosts",
        "document.querySelectorAll('.ant-table-row').length > 0",
        action=CLICK_EDIT_EN,
        settle=2.5,
        note="Host form",
    ),
    Shot("05-identity-users.png", "/identity/users", LIST_READY, settle=2.5, note="Users"),
    Shot("06-identity-roles.png", "/identity/roles", LIST_READY, settle=2.5, note="Roles"),
    Shot("07-grants.png", "/grants", LIST_READY, settle=2.5, note="Grants"),
    Shot("08-policies.png", "/policies", LIST_READY, settle=2.5, note="Policies"),
    Shot("09-launcher.png", "/terminal", LAUNCHER_READY, settle=2.5, note="Launcher"),
    Shot(
        "10-console-linux.png",
        "/terminal/console?hostId=2&accountId=2&title=e2e-demo-01",
        XTERM_READY,
        action=CONNECTED,
        settle=1.0,
        keys=["whoami", "Enter"],
        note="Linux console",
    ),
    Shot(
        "11-file-manager.png",
        "/files/console?hostId=2&accountId=2&title=e2e-demo-01",
        FILES_READY,
        settle=3.0,
        note="File manager",
    ),
    Shot(
        "12-winrm-console.png",
        "/terminal/console?hostId=5&accountId=5&title=win-75&protocol=winrm",
        XTERM_READY,
        action=CONNECTED,
        settle=1.0,
        keys=["hostname", "Enter"],
        note="WinRM console",
    ),
    Shot(
        "13-rdp-console.png",
        "/rdp/console?hostId=5&accountId=5&title=win-75",
        RDP_CONNECTED,
        settle=10.0,
        timeout=90.0,
        note="RDP console",
    ),
    Shot("14-rdp-recordings.png", "/audit/recordings", LIST_READY, settle=2.5, note="Recordings"),
    Shot("15-audit-commands.png", "/audit/commands", LIST_READY, settle=2.5, note="Commands"),
    Shot("16-audit-sessions.png", "/audit/sessions", LIST_READY, settle=2.5, note="Sessions"),
    Shot("17-audit-chain.png", "/audit/logs", LIST_READY, settle=2.5, note="Audit chain"),
    Shot("18-ai-console.png", "/ai", "!!document.querySelector('textarea, .ant-card')", settle=3.0, note="AI"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="英文界面截图（README.en.md / tutorial.en.md）")
    parser.add_argument("--base", default=os.environ.get("BASTION_BASE", DEFAULT_BASE))
    parser.add_argument("--cdp", default=os.environ.get("BASTION_CDP", DEFAULT_CDP))
    parser.add_argument("--token-file", default="", help="mint_token.py --write 写出的 JWT 文件")
    parser.add_argument("--only", default="", help="readme / tutorial，逗号分隔，默认都拍")
    parser.add_argument("--only-step", default="", help="只拍名字里包含这些片段的教程步骤")
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if not args.token_file:
        print("需要 --token-file（先跑 python tools/mint_token.py --write）")
        return 2
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("token 文件是空的")
        return 2

    wanted = {x.strip() for x in args.only.split(",") if x.strip()} or {"readme", "tutorial"}
    failed: list[str] = []

    if args.list:
        for shot in README_SHOTS + TUTORIAL_SHOTS:
            print(f"{shot.name:26} {shot.route}")
        return 0

    if "readme" in wanted:
        OUT_README.mkdir(parents=True, exist_ok=True)
        print(f"\n== README.en.md 截图 -> {OUT_README}")
        failed += run_shots(
            README_SHOTS,
            OUT_README,
            base=args.base,
            cdp=args.cdp,
            token=token,
            width=args.width,
            height=args.height,
            locale="en-US",
        )

    if "tutorial" in wanted:
        OUT_TUTORIAL.mkdir(parents=True, exist_ok=True)
        steps = TUTORIAL_SHOTS
        if args.only_step:
            parts = [x.strip().lower() for x in args.only_step.split(",") if x.strip()]
            steps = [s for s in steps if any(p in s.name.lower() for p in parts)]
        print(f"\n== tutorial.en.md 截图 -> {OUT_TUTORIAL}")
        failed += run_shots(
            steps,
            OUT_TUTORIAL,
            base=args.base,
            cdp=args.cdp,
            token=token,
            width=args.width,
            height=args.height,
            locale="en-US",
        )

    if failed:
        print("\n未就绪（没有覆盖旧图）：" + ", ".join(failed))
        return 1
    print("\n全部就绪 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
