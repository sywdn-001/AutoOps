"""按教学顺序逐步截取后台操作步骤图，给 `docs/图文教程.md`（中英各一份）配图。

这不是判据门禁，只是文档配图生成器：驱动真实 Chrome（CDP）登录本机服务，
按下面 `STEPS` 的顺序走一遍「管理员日常会做的事」，每一步出一张图到
`docs/screenshots/tutorial/`。图名与教程里的编号一一对应（`01-` … `13-`）。

用法：

    python -u tools/tutorial_shots.py --admin-password '<管理员口令>'   # 全部重拍
    python -u tools/tutorial_shots.py --list                            # 只看会拍哪些
    python -u tools/tutorial_shots.py --only 09,10                      # 只重拍某几步

与 `ui_shots.py` 一样有失败保护：先写 `.<名字>.new`，就绪判据满足才覆盖同名旧图，
没就绪就保留旧图并在结尾列出来 —— 文档里不会出现半张图。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_shots import (  # noqa: E402
    CLICK_IN_ROW,
    CONNECTED,
    DEFAULT_BASE,
    DEFAULT_CDP,
    REPO_ROOT,
    ROW_OF,
    Shot,
    XTERM_READY,
    login,
    run_shots,
)

DEFAULT_OUT = REPO_ROOT / "docs" / "screenshots" / "tutorial"

LIST_READY = "!!document.querySelector('.ant-table')"
# 点行内按钮前必须等真行渲染出来：`.ant-table` 在数据到达前就已经存在（表头/骨架），
# 用它当判据会在空表上点，行定位函数直接返回 NO_ROW（第一轮就踩过）。
ROWS_READY = "document.querySelectorAll('.ant-table-row').length > 0"
CARD_READY = "!!document.querySelector('.ant-card')"
RDP_READY = "document.querySelectorAll('canvas').length > 0"
# RDP 要等真的连上（工具栏出现「已连接」），不然拍到的可能是「正在协商」甚至全黑的一帧
RDP_CONNECTED = (
    "document.querySelectorAll('canvas').length > 0 && "
    "((document.body.innerText) || '').includes('已连接')"
)

STEPS: list[Shot] = [
    Shot(
        "01-login.png",
        "/user/login",
        "!!document.querySelector('input[type=password]')",
        settle=2.0,
        anonymous=True,
        note="① 打开堡垒机，用堡垒机账号登录（首次登录会强制改初始口令）",
    ),
    Shot(
        "02-dashboard.png",
        "/dashboard",
        CARD_READY,
        settle=3.0,
        note="② 概览：资产、在线会话、风险与最近审计一屏总览",
    ),
    Shot(
        "03-hosts.png",
        "/assets/hosts",
        LIST_READY,
        settle=2.5,
        note="③ 资产管理 → 主机列表：一台机器可以挂多个协议端点",
    ),
    Shot(
        "04-host-form.png",
        "/assets/hosts",
        ROWS_READY,
        action=CLICK_IN_ROW % (ROW_OF, "编辑"),
        settle=2.5,
        note="④ 编辑主机：协议入口多选，每个协议各自一个端口",
    ),
    Shot(
        "05-identity-users.png",
        "/identity/users",
        LIST_READY,
        settle=2.5,
        note="⑤ 身份与权限 → 用户管理：谁能登录堡垒机",
    ),
    Shot(
        "06-identity-roles.png",
        "/identity/roles",
        LIST_READY,
        settle=2.5,
        note="⑥ 角色管理：每个角色勾哪些权限码",
    ),
    Shot(
        "07-grants.png",
        "/grants",
        LIST_READY,
        settle=2.5,
        note="⑦ 访问授权：谁 → 哪台主机 → 哪个账号 → 能不能开网页终端 / 文件管理",
    ),
    Shot(
        "08-policies.png",
        "/policies",
        LIST_READY,
        settle=2.5,
        note="⑧ 命令策略：放行 / 拦下的命令规则，命中即拒绝并留审计",
    ),
    Shot(
        "09-launcher.png",
        "/terminal",
        LIST_READY,
        settle=2.5,
        note="⑨ 网页终端：一台主机一行，行内按协议给入口按钮",
    ),
    Shot(
        "10-console-linux.png",
        "/terminal/console?hostId=2&accountId=2&title=e2e-demo-01",
        XTERM_READY,
        action=CONNECTED,
        settle=1.0,
        keys=["whoami", "Enter"],
        note="⑩ 浏览器里直接连上 Linux 主机（示例：演示目标机跑 whoami）",
    ),
    Shot(
        "11-file-manager.png",
        "/files/console?hostId=2&accountId=2&title=e2e-demo-01",
        "!!document.querySelector('.bastion-files-pathbar')",
        settle=3.0,
        note="⑪ 文件管理器：SFTP 浏览 / 上传 / 下载 / 改权限，走同一套授权与策略",
    ),
    Shot(
        "12-winrm-console.png",
        "/terminal/console?hostId=5&accountId=5&title=win-75&protocol=winrm",
        XTERM_READY,
        action=CONNECTED,
        settle=1.0,
        keys=["hostname", "Enter"],
        note="⑫ Windows 主机也能开字符会话（WinRM / PowerShell）",
    ),
    Shot(
        "13-rdp-console.png",
        "/rdp/console?hostId=5&accountId=5&title=win-75",
        RDP_CONNECTED,
        # 画布就绪 ≠ 桌面画出来了：settle 太短会拍到全黑的一帧（第一轮 4.0 就是黑板），
        # 协商 RDP 的耗时也会抖（实测同一张图 5s~30s 都有），所以判据等「已连接」+ 放宽超时。
        settle=10.0,
        timeout=90.0,
        note="⑬ Windows 远程桌面：浏览器里操作，右上角显示正在录制",
    ),
    Shot(
        "14-rdp-recordings.png",
        "/audit/recordings",
        LIST_READY,
        settle=2.5,
        note="⑭ 审计中心 → 远程桌面录像：随时回看，可导出",
    ),
    Shot(
        "15-audit-commands.png",
        "/audit/commands",
        LIST_READY,
        settle=2.5,
        note="⑮ 命令记录：每条命令与输出、命中策略、是否被拒都能查",
    ),
    Shot(
        "16-audit-sessions.png",
        "/audit/sessions",
        LIST_READY,
        settle=2.5,
        note="⑯ 会话记录：谁在什么时候连了哪台机器，可强制中断",
    ),
    Shot(
        "17-audit-chain.png",
        "/audit/logs",
        LIST_READY,
        settle=2.5,
        note="⑰ 操作日志与哈希链：改动任何一条历史记录都会被查出来",
    ),
    Shot(
        "18-ai-console.png",
        "/ai",
        "!!document.querySelector('.ant-card, textarea')",
        settle=2.5,
        note="⑱ AI 运维助手：自然语言下指令，敏感操作要管理员当场确认",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="按教学顺序逐步截取后台操作步骤图")
    parser.add_argument("--base", default=os.environ.get("BASTION_BASE", DEFAULT_BASE))
    parser.add_argument("--cdp", default=os.environ.get("BASTION_CDP", DEFAULT_CDP))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--admin-user", default=os.environ.get("BASTION_ADMIN_USERNAME", "admin"))
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("BASTION_ADMIN_PASSWORD", ""),
        help="管理员口令（默认读 BASTION_ADMIN_PASSWORD）",
    )
    parser.add_argument("--only", default="", help="只拍名字包含这些片段（逗号分隔）的步骤")
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--list", action="store_true", help="只列出会拍哪些步骤")
    args = parser.parse_args()

    picked = STEPS
    if args.only:
        wanted = [item.strip().lower() for item in args.only.split(",") if item.strip()]
        picked = [s for s in STEPS if any(w in s.name.lower() for w in wanted)]
    if args.list:
        for shot in picked:
            print(f"{shot.name:24} {shot.route:52} {shot.note}")
        return 0
    if not picked:
        print("没有匹配的步骤")
        return 2
    if not args.admin_password:
        print("缺少管理员口令：--admin-password 或 BASTION_ADMIN_PASSWORD")
        return 2

    out_dir = Path(args.out)
    token = login(args.base, args.admin_user, args.admin_password)
    failures = run_shots(
        picked,
        out_dir,
        base=args.base,
        cdp=args.cdp,
        token=token,
        width=args.width,
        height=args.height,
    )

    print(f"\n教程配图目录：{out_dir}")
    if failures:
        print(f"未就绪、保留旧图的：{', '.join(failures)}")
        return 1
    print(f"全部 {len(picked)} 步就绪，已覆盖同名旧图")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
