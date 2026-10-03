"""真实 SSH 网关里的 `/ask-ai` 实测（选跑，不在默认门禁里）。

`tools/live_e2e_check.py` 覆盖网关的字符画/配色/命令落库，但**不打真实模型**
（保证门禁便宜、可反复跑）；而「连上机器后 `/ask-ai` 到底能不能答」这条路只有它覆盖 ——
2026-10 用户实测报的「每次 `/ask-ai` 都报 DeepSeek 返回 HTTP 400」就发生在这里。
需要后端在跑、`.env` 配了 `DEEPSEEK_API_KEY`，并且**会消耗一次真实模型调用**。

用法：
    python tools/gw_ai_check.py                      # 默认真机 Ubuntu_Linux，问「你好」
    python tools/gw_ai_check.py --host e2e-demo-01   # 换成演示目标机
    python tools/gw_ai_check.py --question "这台机器磁盘满了吗"

断言（9 项）：网关登录 + 主机菜单 → 进入目标机会话 → 粘贴形态的 `/ask-ai` 不被远端 bash 执行
→ 终端里不出现「AI 出错 / HTTP 400」→ 出现 AI 上下文与提问行 → 粘贴标记不残留
→ 有真实答案正文（流式原地重绘）→ AI 回合之后终端仍可用。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

import paramiko

# 报告里有中文，Windows 控制台/管道默认 GBK（cp936）会让 print 抛 UnicodeEncodeError 截断报告。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GW_HOST, GW_PORT = "127.0.0.1", 2222
ADMIN_USER = os.environ.get("BASTION_ADMIN", "admin")
ADMIN_PW = os.environ.get("BASTION_ADMIN_PASSWORD", "admin123")
ANSI_SGR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MENU_ITEM = re.compile(r"\[(\s*\d+)\]\s+(\S+)")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""), flush=True)
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实 SSH 网关里的 /ask-ai 实测（选跑，消耗一次模型调用）")
    parser.add_argument("--host", default="Ubuntu_Linux", help="菜单里要选的主机名（子串匹配）")
    parser.add_argument("--fallback-host", default="e2e-demo-01", help="第一台连不上时的备选主机")
    parser.add_argument("--question", default="你好")
    parser.add_argument("--idle", type=float, default=20.0, help="AI 回合空闲收手秒数")
    parser.add_argument("--wait", type=float, default=180.0, help="AI 回合总超时秒数")
    parser.add_argument("--admin-user", default=ADMIN_USER, help="网关登录账号（默认 admin，可被 BASTION_ADMIN 覆盖）")
    parser.add_argument("--admin-password", default=ADMIN_PW, help="网关登录口令（默认 admin123，可被 BASTION_ADMIN_PASSWORD 覆盖）")
    args = parser.parse_args(argv)
    admin_user, admin_pw = args.admin_user, args.admin_password

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    chan = None
    buf = [""]
    try:
        cli.connect(
            GW_HOST,
            port=GW_PORT,
            username=admin_user,
            password=admin_pw,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            banner_timeout=30,
        )
        check(f"SSH 网关登录成功（{admin_user}）", True)
        chan = cli.invoke_shell(width=120, height=32)

        def read_until(needle: str, timeout: float = 30.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if needle in buf[0]:
                    return True
                if chan.recv_ready():
                    buf[0] += chan.recv(65536).decode("utf-8", "replace")
                    continue
                time.sleep(0.15)
            return needle in buf[0]

        check("网关下发主机菜单", read_until("你可访问的主机", 30), repr(ANSI_SGR.sub("", buf[0])[-160:]))

        def menu_number(target: str) -> str | None:
            for num, name in MENU_ITEM.findall(ANSI_SGR.sub("", buf[0])):
                if target in name:
                    return num.strip()
            return None

        chosen = None
        for candidate in (args.host, args.fallback_host):
            num = menu_number(candidate)
            if num is None:
                continue
            buf[0] = ""
            chan.send(num + "\n")
            if read_until("已连接", 60):
                chosen = candidate
                break
            check(f"主机 {candidate} 进入会话", False, repr(ANSI_SGR.sub("", buf[0])[-200:]))
        check("进入目标机会话", chosen is not None, f"chosen={chosen}")
        if chosen is None:
            raise SystemExit(1)
        buf[0] = ""
        time.sleep(1.2)

        # 用户路径：浏览器/终端粘贴会带 bracketed paste 标记
        chan.send("\x1b[200~/ask-ai " + args.question + "\x1b[201~")
        time.sleep(0.3)
        chan.send("\r")

        deadline = time.time() + args.wait
        last = time.time()
        while time.time() < deadline:
            if chan.recv_ready():
                buf[0] += chan.recv(65536).decode("utf-8", "replace")
                last = time.time()
                continue
            if buf[0] and time.time() - last > args.idle:
                break
            time.sleep(0.2)

        plain = ANSI_SGR.sub("", buf[0])
        check(
            "终端里没有「AI 出错」/「HTTP 400」",
            "AI 出错" not in plain and "HTTP 400" not in plain,
            repr([ln for ln in plain.splitlines() if "出错" in ln or "400" in ln][:3]),
        )
        check("远端 bash 没把 /ask-ai 当命令执行", "没有那个文件或目录" not in plain and "command not found" not in plain)
        check("出现 AI 上下文与提问行", "[堡垒机] AI 上下文" in plain and f"提问：{args.question}" in plain)
        check("粘贴标记没残留", "[200~" not in plain and "[201~" not in plain, repr(plain[:80]))
        marker = f"提问：{args.question}"
        body = plain.split(marker, 1)[-1].strip() if marker in plain else ""
        check("AI 给出真实答案正文（流式原地重绘）", len(body) > 40, repr(body[:140]))

        buf[0] = ""
        chan.send("echo AFTER-AI-PROBE\n")
        check("AI 回合之后终端仍可用", read_until("AFTER-AI-PROBE", 30), repr(ANSI_SGR.sub("", buf[0])[-120:]))
    except Exception as exc:  # noqa: BLE001
        check("网关 /ask-ai 实测", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if chan is not None:
                chan.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            cli.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"\n共 {len(PASS) + len(FAIL)} 项，通过 {len(PASS)}，失败 {len(FAIL)}", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
