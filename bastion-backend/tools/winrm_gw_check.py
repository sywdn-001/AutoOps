"""真实 SSH 网关里连一台 Windows（WinRM）主机并执行命令（选跑，不在默认门禁里）。

需求④（`ssh <堡垒机IP>` 进审计 shell、选主机、命令与输出全程留痕）原本只覆盖 Linux：
网关的字符菜单按 `protocols=("ssh",)` 取主机，Windows 主机进不去。本轮把菜单放宽到
`("ssh", "winrm")` —— 判断依据是**桥接层公开面一致**（`app/winrm/bridge.py` 的文件头
docstring：`WinrmBridge` 与 `app/terminal/bridge.py:ShellBridge` 的公开面完全相同，
`session_service.open_session()` 只按 `host.protocol` 选桥），所以网关那条
「`open_session()` → `bridge.feed_input()` → `writer` 回吐字节」的通路不需要为 WinRM 改一行。

这个脚本就是那条通路的真机取证：单元测试只能证明「选桥正确」，证明不了
「真机 PowerShell 在网关里敲得动、退出后能回菜单」。

用法：
    python tools/winrm_gw_check.py                                    # 默认找菜单里的 win-75-winrm
    python tools/winrm_gw_check.py --host win-75-winrm                # 指定主机名（子串匹配）
    python tools/winrm_gw_check.py --admin-password <当前管理员口令>
    python tools/winrm_gw_check.py --expect-user 'win-930nkgjcoed\\administrator'   # 断言 whoami 的内容

断言（9 项）：网关登录 → 下发主机菜单 → 菜单里有这台 WinRM 主机 → 进入会话（出现 WinRM
能力边界提示 + PowerShell 提示符）→ `whoami` 是目标机的 → `hostname` 是目标机的 →
`exit` 回到主机菜单 → 会话收口有「会话已关闭」提示。

需要后端在跑（`python run.py`）、目标机开着 WinRM，并且**会真的在目标机上执行两条只读命令**。
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
    parser = argparse.ArgumentParser(description="真实 SSH 网关里连 Windows（WinRM）主机并执行命令")
    parser.add_argument("--host", default="win-75-winrm", help="菜单里要选的主机名（子串匹配）")
    parser.add_argument("--expect-user", default="", help="断言 whoami 输出包含这个子串（留空只断言非空）")
    parser.add_argument("--admin-user", default=ADMIN_USER, help="网关登录账号（默认 admin，可被 BASTION_ADMIN 覆盖）")
    parser.add_argument("--admin-password", default=ADMIN_PW, help="网关登录口令（默认 admin123，可被 BASTION_ADMIN_PASSWORD 覆盖）")
    parser.add_argument("--idle", type=float, default=6.0, help="每条命令的空闲收手秒数")
    args = parser.parse_args(argv)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    chan = None
    buf = [""]
    try:
        cli.connect(
            GW_HOST,
            port=GW_PORT,
            username=args.admin_user,
            password=args.admin_password,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            banner_timeout=30,
        )
        check(f"SSH 网关登录成功（{args.admin_user}）", True)
        chan = cli.invoke_shell(width=140, height=40)

        def read_until(needle: str, timeout: float = 30.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if needle in ANSI_SGR.sub("", buf[0]):
                    return True
                if chan.recv_ready():
                    buf[0] += chan.recv(65536).decode("utf-8", "replace")
                    continue
                time.sleep(0.15)
            return needle in ANSI_SGR.sub("", buf[0])

        def drain(seconds: float) -> str:
            """读到空闲为止，返回这一轮的纯文本。"""
            deadline = time.time() + seconds
            last = time.time()
            while time.time() < deadline:
                if chan.recv_ready():
                    buf[0] += chan.recv(65536).decode("utf-8", "replace")
                    last = time.time()
                    continue
                if buf[0] and time.time() - last > args.idle:
                    break
                time.sleep(0.15)
            text, buf[0] = ANSI_SGR.sub("", buf[0]), ""
            return text

        check("网关下发主机菜单", read_until("你可访问的主机", 30), repr(ANSI_SGR.sub("", buf[0])[-160:]))

        number = None
        for num, name in MENU_ITEM.findall(ANSI_SGR.sub("", buf[0])):
            if args.host in name:
                number = num.strip()
                break
        menu_text = ANSI_SGR.sub("", buf[0])
        check(
            f"菜单里有 WinRM 主机（{args.host}）",
            number is not None,
            "菜单主机：" + ", ".join(name for _, name in MENU_ITEM.findall(menu_text)),
        )
        if number is None:
            raise SystemExit(1)

        buf[0] = ""
        chan.send(number + "\n")
        entered = read_until("PS C:\\", 60)
        session_text = ANSI_SGR.sub("", buf[0])
        check("进入目标机会话（PowerShell 提示符）", entered, repr(session_text[-160:]))
        check(
            "会话里给出 WinRM 能力边界提示",
            "这是 Windows（WinRM）会话" in session_text and "本会话也不支持文件传输" in session_text,
        )
        buf[0] = ""
        time.sleep(1.0)

        chan.send("whoami\n")
        who = drain(20)
        who_lines = [ln.strip() for ln in who.splitlines() if ln.strip() and ln.strip() != "whoami"]
        who_value = next((ln for ln in reversed(who_lines) if "PS C:" not in ln), "")
        check(
            "whoami 返回目标机账号",
            bool(who_value) and (not args.expect_user or args.expect_user.lower() in who_value.lower()),
            repr(who_value),
        )

        chan.send("hostname\n")
        host_out = drain(20)
        host_lines = [ln.strip() for ln in host_out.splitlines() if ln.strip() and ln.strip() != "hostname"]
        host_value = next((ln for ln in reversed(host_lines) if "PS C:" not in ln), "")
        check("hostname 返回目标机名", bool(host_value), repr(host_value))

        chan.send("exit\n")
        ended = drain(20)
        check("exit 之后回到主机菜单", "选择主机 >" in ended or "你可访问的主机" in ended, repr(ended[-140:]))
        check("会话收口有提示", "会话已关闭" in ended, repr([ln for ln in ended.splitlines() if "会话" in ln][:2]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        check("网关 WinRM 实测", False, f"{type(exc).__name__}: {exc}")
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
