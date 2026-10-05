"""用真实 Chrome（CDP）逐页截取堡垒机后台 UI 的「现状图」，覆盖 `docs/screenshots/` 里同名的旧图。

和 `tools/ui_check.py`（判据巡检）不同，这个脚本只负责**出图**：登录一次拿到 JWT 写进
`localStorage.bastion_token`，然后按 `SHOTS` 表逐条（路由 + 等待条件 + 可选交互）截图，
分辨率固定，方便 README 里排版对齐。

覆盖的图与用途：
  - `login.png` / `dashboard.png` / `multiproto-launcher.png` / `multiproto-hosts-list.png`
    / `ssh-gateway-menu.png` / `ai-console.png`：纯页面图，随时可重拍；
  - `multiproto-host-form.png`、`multiproto-merge-*.png`：需要点开编辑抽屉 / 合并弹窗（脚本里点了
    `win-75` 那一行的按钮）；
  - `terminal-console.png`、`file-manager.png`、`multiproto-winrm-console.png`、`web-rdp-win75.png`：
    真开会话（Linux 走本机演示目标机 `127.0.0.1:2200`，Windows 走 `192.168.0.75` 的 WinRM / RDP），
    目标机不可达时这几张会失败但**不会覆盖**旧图（先写临时文件，成功才替换）。

用法::

    python tools/ui_shots.py                              # 默认 admin + BASTION_ADMIN_PASSWORD
    python tools/ui_shots.py --admin-password <当前口令>
    python tools/ui_shots.py --only dashboard,hosts-list   # 只重拍几张（名字模糊匹配）
    python tools/ui_shots.py --list                        # 只列出会拍哪些图

前置：后端已 `python run.py`（且已托管 `ant-design-pro/dist`）、Chrome 以
`--remote-debugging-port=9222 --remote-allow-origins=*` 启动。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import websocket  # websocket-client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "docs" / "screenshots"
DEFAULT_CDP = "http://127.0.0.1:9222"
DEFAULT_BASE = "http://127.0.0.1:5000"

# ---------------------------------------------------------------- 截图清单

# 主机列表里按主机名定位一行；「编辑 / 合并」按钮就在该行的操作列里。
# 用「第一列文本完全相等」来认行：列表按 id 倒序，子串匹配会撞上 `win-75-dup` 这类同名前缀。
ROW_OF = (
    "(name) => [...document.querySelectorAll('.ant-table-row')].find((row) => {"
    "  const cell = row.querySelector('td');"
    "  return Boolean(cell) && (cell.innerText || '').trim() === name;"
    "})"
)
CLICK_IN_ROW = """(() => {
  const row = (%s)('win-75');
  if (!row) return 'NO_ROW';
  const btn = [...row.querySelectorAll('button')].find((b) => (b.innerText || '').trim().includes('%s'));
  if (!btn) return 'NO_BUTTON';
  btn.click();
  return 'CLICKED';
})()"""
OPEN_MODAL_SELECT = """(() => {
  const input = document.querySelector('.ant-modal input.ant-select-input');
  if (!input) return 'NO_INPUT';
  input.focus();
  return 'FOCUSED';
})()"""
FIRST_ROW_DETAIL = """(() => {
  const row = document.querySelector('.ant-table-row');
  if (!row) return 'NO_ROW';
  const btn = [...row.querySelectorAll('button, a')]
    .find((b) => /详情|查看|链路|哈希/.test(b.innerText || ''));
  if (!btn) return 'NO_BUTTON';
  btn.click();
  return 'CLICKED';
})()"""
CLICK_FIRST_ROW_CONNECT = """(() => {
  const row = [...document.querySelectorAll('.ant-table-row')].find((r) => (r.innerText || '').includes('win-75'))
    || document.querySelector('.ant-table-row');
  if (!row) return 'NO_ROW';
  const btn = [...row.querySelectorAll('button')].find((b) => /远程桌面/.test(b.innerText || ''));
  if (!btn) return 'NO_BUTTON';
  btn.click();
  return 'CLICKED';
})()"""


@dataclass
class Shot:
    name: str
    route: str
    wait: str
    action: str | None = None
    settle: float = 1.5
    anonymous: bool = False
    keys: list[str] = field(default_factory=list)
    note: str = ""


XTERM_READY = "!!document.querySelector('.xterm-rows')"
CONNECTED = (
    "((document.querySelector('.bastion-statusbar') || {}).innerText || '').includes('已连接')"
)
WINRM_CONNECTED = (
    "((document.querySelector('.bastion-statusbar') || {}).innerText || '').includes('已连接')"
)

SHOTS: list[Shot] = [
    Shot("login.png", "/user/login", "!!document.querySelector('input[type=password]')", settle=2.0, anonymous=True,
         note="登录页（清掉令牌后再进）"),
    Shot("dashboard.png", "/dashboard", "!!document.querySelector('.ant-card')", settle=3.0, note="概览首页"),
    Shot("multiproto-launcher.png", "/terminal", "!!document.querySelector('.ant-table-row')", settle=2.5,
         note="网页终端入口页（一台主机一行、行内每协议一个按钮）"),
    Shot("multiproto-hosts-list.png", "/assets/hosts",
         "document.querySelectorAll('.ant-table-row').length > 0", settle=2.5, note="主机列表（多协议标签 + 合并按钮）"),
    Shot("multiproto-host-form.png", "/assets/hosts",
         "document.querySelectorAll('.ant-table-row').length > 0",
         action=CLICK_IN_ROW % (ROW_OF, "编辑"), settle=2.5, note="编辑主机抽屉（协议多选 + 每协议端口）"),
    Shot("multiproto-merge-modal.png", "/assets/hosts",
         "document.querySelectorAll('.ant-table-row').length > 0",
         action=CLICK_IN_ROW % (ROW_OF, "合并"), settle=2.0, note="合并弹窗"),
    Shot("multiproto-merge-options.png", "/assets/hosts",
         "document.querySelectorAll('.ant-table-row').length > 0",
         action=CLICK_IN_ROW % (ROW_OF, "合并"), settle=2.0,
         keys=["Sleep:1.2", OPEN_MODAL_SELECT, "ArrowDown", "Sleep:1.5"],
         note="合并弹窗（下拉已展开，可选同地址主机）"),
    Shot("terminal-console.png", "/terminal/console?hostId=2&accountId=2&title=e2e-demo-01",
         XTERM_READY, action=CONNECTED, settle=1.0, keys=["whoami", "Enter"], note="Linux 网页终端（真连演示目标机）"),
    Shot("file-manager.png", "/files/console?hostId=2&accountId=2&title=e2e-demo-01",
         "!!document.querySelector('.bastion-files-pathbar')", settle=3.0, note="SFTP 文件管理器（真连演示目标机）"),
    Shot("ssh-gateway-menu.png", "/system/gateway", "!!document.querySelector('.ant-card')", settle=2.0,
         note="SSH 网关页"),
    Shot("ai-console.png", "/ai", "!!document.querySelector('.ant-card, textarea')", settle=2.5, note="AI 运维助手"),
    Shot("audit-chain-detail.png", "/audit/sessions",
         "document.querySelectorAll('.ant-table-row').length > 0",
         action=FIRST_ROW_DETAIL, settle=2.5, note="会话详情（防篡改哈希链）"),
    Shot("multiproto-winrm-console.png",
         "/terminal/console?hostId=5&accountId=5&title=win-75&protocol=winrm",
         XTERM_READY, action=WINRM_CONNECTED, settle=1.0, keys=["hostname", "Enter"],
         note="WinRM 网页终端（真连 192.168.0.75）"),
    Shot("web-rdp-win75.png", "/rdp/console?hostId=5&accountId=5&title=win-75",
         "!!document.querySelector('canvas')", settle=8.0, note="WebRDP（真连 192.168.0.75）"),
]


# ---------------------------------------------------------------- CDP 客户端


class Tab:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.seq = 0
        self.send("Page.enable")
        self.send("Runtime.enable")

    def send(self, method: str, **params):
        self.seq += 1
        msg_id = self.seq
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            data = json.loads(self.ws.recv())
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                raise RuntimeError(f"{method} -> {data['error']}")
            return data.get("result", {})

    def evaluate(self, expr: str, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        return res.get("result", {}).get("value")

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", url=url)
        time.sleep(0.6)

    def wait_for(self, expr: str, timeout: float = 25.0, interval: float = 0.4) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expr):
                    return True
            except Exception:  # noqa: BLE001 - 页面切换期间 evaluate 可能被中断，重试即可
                pass
            time.sleep(interval)
        return False

    def key(self, name: str, code: str | None = None, vk: int = 0, text: str | None = None) -> None:
        for kind in ("keyDown", "keyUp"):
            self.send(
                "Input.dispatchKeyEvent",
                type=kind,
                key=name,
                code=code or name,
                windowsVirtualKeyCode=vk,
                nativeVirtualKeyCode=vk,
                **(  {"text": text} if (kind == "keyDown" and text) else {}  ),
            )

    def type_text(self, text: str) -> None:
        vk_map = {" ": 32, "-": 189, ".": 190, "/": 191, "_": 189, ":": 186}
        for ch in text:
            self.send(
                "Input.dispatchKeyEvent",
                type="keyDown",
                key=ch,
                text=ch,
                unmodifiedText=ch,
                windowsVirtualKeyCode=vk_map.get(ch, ord(ch.upper())),
            )
            self.send("Input.dispatchKeyEvent", type="keyUp", key=ch)
            time.sleep(0.03)

    def click(self, x: float, y: float) -> None:
        """真实鼠标点（antd v6 的 Select 只认真实 mousedown，合成 click() 打不开下拉）。"""
        self.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.send(
                "Input.dispatchMouseEvent",
                type=kind,
                x=x,
                y=y,
                button="left",
                buttons=buttons,
                clickCount=1,
            )

    def click_selector(self, css: str) -> str:
        box = self.evaluate(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return null; const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
            % json.dumps(css)
        )
        if not box:
            return "NO_ELEMENT"
        self.click(box["x"], box["y"])
        return "CLICKED"

    def shot(self, path: Path) -> None:
        res = self.send("Page.captureScreenshot", format="png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(res["data"]))


def http_json(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - 本机固定地址
        return json.loads(resp.read().decode("utf-8"))


def open_tab(cdp: str) -> Tab:
    req = urllib.request.Request(f"{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        info = json.loads(resp.read().decode("utf-8"))
    return Tab(info["webSocketDebuggerUrl"])


def login(base: str, user: str, password: str) -> str:
    res = http_json("POST", f"{base}/api/auth/login", {"username": user, "password": password})
    token = (res.get("data") or {}).get("token")
    if not token:
        raise SystemExit(f"登录失败：{json.dumps(res, ensure_ascii=False)[:300]}")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实 Chrome 截取后台 UI 现状图")
    parser.add_argument("--base", default=os.environ.get("BASTION_BASE", DEFAULT_BASE))
    parser.add_argument("--cdp", default=os.environ.get("BASTION_CDP", DEFAULT_CDP))
    parser.add_argument("--out", default=os.environ.get("BASTION_SHOTS_DIR", str(DEFAULT_OUT)))
    parser.add_argument("--admin-user", default=os.environ.get("BASTION_ADMIN_USERNAME", "admin"))
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("BASTION_ADMIN_PASSWORD", ""),
        help="管理员口令（默认读 BASTION_ADMIN_PASSWORD）",
    )
    parser.add_argument("--only", default="", help="只拍名字包含这些片段（逗号分隔）的图")
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--list", action="store_true", help="只列出会拍哪些图")
    args = parser.parse_args()

    picked = SHOTS
    if args.only:
        wanted = [item.strip().lower() for item in args.only.split(",") if item.strip()]
        picked = [s for s in SHOTS if any(w in s.name.lower() for w in wanted)]
    if args.list:
        for shot in picked:
            print(f"{shot.name:34} {shot.route:52} {shot.note}")
        return 0
    if not picked:
        print("没有匹配的截图项")
        return 2
    if not args.admin_password:
        print("缺少管理员口令：--admin-password 或 BASTION_ADMIN_PASSWORD")
        return 2

    out_dir = Path(args.out)
    token = login(args.base, args.admin_user, args.admin_password)
    tab = open_tab(args.cdp)
    tab.send(
        "Emulation.setDeviceMetricsOverride",
        width=args.width,
        height=args.height,
        deviceScaleFactor=1,
        mobile=False,
    )
    # 先落到同源页面，才能写 localStorage
    tab.navigate(f"{args.base}/user/login")
    tab.evaluate(f"localStorage.setItem('bastion_token', {json.dumps(token)})")

    failures: list[str] = []
    for shot in picked:
        if shot.anonymous:
            tab.evaluate("localStorage.removeItem('bastion_token')")
        else:
            tab.evaluate(f"localStorage.setItem('bastion_token', {json.dumps(token)})")
        tab.navigate(f"{args.base}{shot.route}")
        ready = tab.wait_for(shot.wait, timeout=25.0)
        if shot.action:
            result = tab.evaluate(shot.action)
            if result != "CLICKED" and result != "FOCUSED":
                print(f"  [warn] {shot.name}: 交互未生效（{result}）")
        for item in shot.keys:
            if item.startswith("Sleep:"):
                time.sleep(float(item.split(":", 1)[1]))
            elif item.startswith("ClickSelector:"):
                result = tab.click_selector(item.split(":", 1)[1])
                if result != "CLICKED":
                    print(f"  [warn] {shot.name}: 元素没找到（{item}）")
            elif item.startswith("("):
                tab.evaluate(item)
            elif item == "Enter":
                tab.key("Enter", "Enter", 13, text="\r")
            elif item == "ArrowDown":
                tab.key("ArrowDown", "ArrowDown", 40)
            else:
                tab.evaluate("document.querySelector('.xterm-helper-textarea')?.focus()")
                tab.type_text(item)
        time.sleep(shot.settle)

        target = out_dir / shot.name
        tmp = out_dir / f".{shot.name}.new"
        tab.shot(tmp)
        status = "OK" if ready else "WAIT_TIMEOUT"
        if ready:
            os.replace(tmp, target)
        else:
            failures.append(shot.name)
            tmp.unlink(missing_ok=True)
        print(f"  [{status}] {shot.name}  ({shot.note})")

    tab.ws.close()
    print(f"\n截图目录：{out_dir}")
    if failures:
        print(f"未就绪、保留旧图的：{', '.join(failures)}")
        return 1
    print("全部就绪，已覆盖同名旧图")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
