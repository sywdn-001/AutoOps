"""用真实 Chrome（CDP）逐路由渲染堡垒机后台 UI，验证页面真的能跑起来。

做法：注入 JWT 到 localStorage（等价于用户登录后的状态）→ 逐个访问 14 个业务路由 →
检查 DOM 里没有 React 崩溃覆盖层 / 没有未捕获异常，并抓取页面可见文本与首页截图。
另外在登录页与概览页断言「无 Ant Design Pro 痕迹（外链/版权文案）」与
「品牌 Logo 是本地 SVG 且真的加载成功」。

用法::

    python tools/ui_check.py                                  # admin / admin123
    python tools/ui_check.py --admin-password <当前口令>        # 首次登录网关强制改密后
    python tools/ui_check.py --token <JWT>                     # 直接用现成令牌
    BASTION_ADMIN_PASSWORD=<口令> python tools/ui_check.py

前置：后端已 `python run.py`（且已托管 `dist`）、Chrome 以
`--remote-debugging-port=9222 --remote-allow-origins=*` 启动。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.request

import websocket  # websocket-client

# 报告里有中文，Windows 控制台/管道默认 GBK（cp936）会让 print 抛
# UnicodeEncodeError 截断报告；统一按 UTF-8 输出并允许替换字符。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDP = "http://127.0.0.1:9222"
BASE = "http://127.0.0.1:5000"
OUT = os.path.join(os.environ.get("TEMP", "."), "bastion_ui", "shots")
ROUTES = [
    ("/dashboard", "概览"),
    ("/terminal", "网页终端"),
    ("/assets/hosts", "主机列表"),
    ("/assets/groups", "主机分组"),
    ("/identity/users", "用户管理"),
    ("/identity/roles", "角色管理"),
    ("/grants", "访问授权"),
    ("/policies", "命令策略"),
    ("/file-policies", "文件策略"),
    ("/audit/sessions", "会话记录"),
    ("/audit/commands", "命令记录"),
    ("/audit/files", "文件记录"),
    ("/audit/logs", "操作日志"),
    ("/system/settings", "参数设置"),
    ("/system/gateway", "SSH 网关"),
    ("/account/settings", "个人设置"),
]
BAD_MARKERS = [
    "Something went wrong",
    "组件渲染错误",
    "Unhandled Runtime Error",
    "This page could not be found",
    # 后端返回的 NOT_FOUND 文案：页面若调用不存在的接口，必须让巡检红掉（模板 /account 页曾踩此坑）
    "接口不存在",
]

# 「模板痕迹」判据：Ant Design Pro 的外链与版权文案。用户要求后台 UI 不留模板痕迹，
# 而且内网根本访问不到这些域名（浏览器要挂满超时）。命中即 FAIL，防止以后再被贴回来。
TRACE_RE = re.compile(
    r"alipayobjects\.com|ant\.design|umijs\.org|utoo\.land|github\.com/ant-design|procomponents"
)
TEMPLATE_PROBE = """
(() => {
  const imgs = [...document.querySelectorAll('img')].map((el) => ({
    src: el.getAttribute('src') || '',
    loaded: el.complete && el.naturalWidth > 0,
    w: el.naturalWidth,
  }));
  const links = [...document.querySelectorAll('a[href]')].map((el) => el.getAttribute('href') || '');
  return JSON.stringify({ imgs, links, text: document.body.innerText });
})()
"""


def token_from_login(admin_user: str = "admin", admin_password: str = "admin123") -> str:
    body = json.dumps(
        {"username": admin_user, "password": admin_password, "type": "account"}
    ).encode()
    req = urllib.request.Request(BASE + "/api/login/account", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    token = ((payload or {}).get("data") or {}).get("token")
    if not token:
        message = (payload or {}).get("message") or "登录失败"
        raise SystemExit(
            f"取令牌失败：{message}\n"
            "管理员首次登录网关会被强制改密（产品行为），`admin123` 随即失效；\n"
            "请用 `--admin-password <当前口令>` / `BASTION_ADMIN_PASSWORD` 传入当前口令，\n"
            "或 `python run.py --reset-admin` 恢复默认值。"
        )
    return token


def _parse_args(argv: list[str]) -> dict:
    """支持 `--token` / `--admin-user` / `--admin-password`（只认命名参数）。

    历史缺陷：早期版本把「第一个不以 `--` 开头的参数」当作令牌，于是
    `--admin-password Wlll___20011211` 里的**口令本身**被当成令牌注入 localStorage：
    每个路由都被弹回登录页，而登录页文本长度/bad 标记都满足判据 → **15/15 全绿却什么
    都没验证**。所以这里只认命名参数，并且 main() 里加了「必须真的处于登录态」的守卫。
    """
    opts = {
        "token": "",
        "admin_user": os.environ.get("BASTION_ADMIN", "admin"),
        "admin_password": os.environ.get("BASTION_ADMIN_PASSWORD", "admin123"),
    }
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("--token", "--admin-user", "--admin-password") and index + 1 < len(argv):
            opts[item.lstrip("-").replace("-", "_")] = argv[index + 1]
            index += 2
            continue
        index += 1
    return opts


class CDPClient:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=30, max_size=None)
        self._id = 0

    def send(self, method: str, params: dict | None = None, timeout: float = 40.0):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expr: str):
        res = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return res.get("result", {}).get("value")

    def navigate(self, url: str):
        self.send("Page.navigate", {"url": url})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def template_probe(cdp: CDPClient) -> dict:
    return json.loads(cdp.evaluate(TEMPLATE_PROBE) or "{}")


def template_offenders(snapshot: dict) -> list[str]:
    """命中的模板痕迹（外链 + 版权文案）。空列表 = 干净。"""
    hits = [u for u in (snapshot.get("links") or []) if TRACE_RE.search(u)]
    hits += [s for s in (i.get("src", "") for i in (snapshot.get("imgs") or [])) if TRACE_RE.search(s)]
    if "Ant Design Pro" in (snapshot.get("text") or ""):
        hits.append("文案：Ant Design Pro")
    return hits


def logo_state(snapshot: dict) -> tuple[bool, str]:
    """品牌 Logo 必须是本地 SVG 且真的加载成功（`naturalWidth > 0`）。"""
    imgs = snapshot.get("imgs") or []
    local_svg = [i for i in imgs if str(i.get("src", "")).endswith(".svg")]
    ok = any(i.get("loaded") for i in local_svg)
    detail = ", ".join(f"{i.get('src')}(w={i.get('w')})" for i in imgs) or "页面无 img"
    return ok, detail


def wait_for_render(cdp: CDPClient, floor: float = 3.0, min_len: int = 120, timeout: float = 20.0) -> str:
    """等页面渲染完成并返回正文。

    判据演进史（两条都是踩过的坑）：
      1. 固定 `time.sleep(5)`：后端同时在跑 pytest（CPU + 同一个 SQLite）时
         `/api/policies` 返回慢过 5 秒，`/policies` 只剩页头几十个字 → 误判成页面坏了。
      2. 只等「正文超过 120 字」：页面骨架一到就返回，读到的是**表格还没数据**的中间态
         （`/dashboard` 只读到 245 字），既弱化证据、又可能漏掉「数据回来后才崩」的错误。

    现在：先给懒加载 chunk + 接口一个 `floor` 秒下限，再等正文**连续 3 次不变**才算渲染完
    （出现崩溃标记则立即返回，交给判据去红）。
    """
    time.sleep(floor)
    prev: str | None = None
    stable = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = cdp.evaluate("document.body.innerText") or ""
        if any(marker in text for marker in BAD_MARKERS):
            return text
        stable = stable + 1 if (len(text) > min_len and text == prev) else 0
        if stable >= 3:
            return text
        prev = text
        time.sleep(0.5)
    return prev or ""


def main() -> int:
    opts = _parse_args(sys.argv[1:])
    token = opts["token"] or token_from_login(opts["admin_user"], opts["admin_password"])
    os.makedirs(OUT, exist_ok=True)
    with urllib.request.urlopen(CDP + "/json/list", timeout=15) as resp:
        targets = json.loads(resp.read().decode())
    page = next(t for t in targets if t.get("type") == "page")
    cdp = CDPClient(page["webSocketDebuggerUrl"])
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")

    errors: list[str] = []
    cdp.evaluate("window.__bastionErrors = []; window.addEventListener('error', e => window.__bastionErrors.push(String(e.message)));")
    cdp.navigate(BASE + "/user/login")
    time.sleep(4)
    # 登录页也要查模板痕迹与 Logo：它是未登录状态唯一能看到的页面。
    login_snapshot = template_probe(cdp)
    login_traces = template_offenders(login_snapshot)
    login_logo_ok, login_logo_detail = logo_state(login_snapshot)
    print(f"{'PASS' if not login_traces else 'FAIL'} | 登录页无 Ant Design Pro 痕迹 | {login_traces or 'clean'}", flush=True)
    print(f"{'PASS' if login_logo_ok else 'FAIL'} | 登录页品牌 Logo 为本地 SVG 且已加载 | {login_logo_detail}", flush=True)
    login_shot = cdp.send("Page.captureScreenshot", {"format": "png"})
    with open(os.path.join(OUT, "login.png"), "wb") as fh:
        fh.write(base64.b64decode(login_shot["data"]))
    pre_checks = [
        ("登录页：无 Ant Design Pro 痕迹", "无模板外链/版权文案", not login_traces, 0, "", login_traces, [], 0),
        ("登录页：品牌 Logo 为本地 SVG 且已加载", "/logo.svg 加载成功", login_logo_ok, 0, "", [] if login_logo_ok else ["logo 未加载"], [], 0),
    ]
    cdp.evaluate(f"localStorage.setItem('bastion_token', {json.dumps(token)});")
    print(f"injected token: len={len(token)}", flush=True)

    # 登录态守卫（防「巡检了 14 次登录页却全绿」这类假通过）：先确认带令牌真的进得去。
    cdp.navigate(BASE + "/dashboard")
    time.sleep(5)
    guard_path = cdp.evaluate("location.pathname") or ""
    guard_text = cdp.evaluate("document.body.innerText") or ""
    if guard_path.startswith("/user/login") or not guard_text.strip():
        print(
            "FAIL | 登录态守卫：带令牌访问 /dashboard 被弹回登录页 —— 令牌无效，后续巡检无意义\n"
            "     请确认 --admin-password 是当前口令（管理员首次登录 SSH 网关会被强制改密），\n"
            "     或 `python run.py --reset-admin` 恢复默认口令，或直接 `--token <JWT>`。",
            flush=True,
        )
        cdp.close()
        return 1

    results = []
    results.extend(pre_checks)
    for path, expect in ROUTES:
        cdp.send("Runtime.evaluate", {"expression": "window.__bastionErrors = []"})
        cdp.navigate(BASE + path)
        text = wait_for_render(cdp)
        pathname = cdp.evaluate("location.pathname") or ""
        heading = cdp.evaluate("(document.querySelector('.ant-pro-page-container-warp-page-header-heading-title')||document.querySelector('h1')||{}).innerText || ''") or ""
        js_errors = cdp.evaluate("JSON.stringify(window.__bastionErrors || [])") or "[]"
        bad = [m for m in BAD_MARKERS if m in text]
        js_bad = json.loads(js_errors)
        seen = sum(1 for kw in ("概览", "主机", "会话", "命令", "策略", "用户", "角色", "授权", "审计", "设置", "网关", "终端", "分组", "个人", "密码") if kw in text)
        # 判据说明：表单/详情类页面（如个人设置）标签很短，硬阈值 len>200 会误伤；
        # 改为「仍在登录态 + 有页面标题 + 命中业务关键词 + 无崩溃标记」，崩溃与 404 仍由
        # BAD_MARKERS 兜住。仍在登录态是硬条件：被弹回登录页的巡检等于没巡检。
        logged_out = pathname.startswith("/user/login")
        if logged_out:
            bad = bad + ["被弹回登录页（令牌失效）"]
        ok = (not bad) and (not js_bad) and (not logged_out) and len(text) > 120 and (seen >= 2 or bool(heading.strip()))
        results.append((path, expect, ok, len(text), heading.strip()[:40], bad, js_bad[:2], seen))
        if path == "/dashboard":
            shot = cdp.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            with open(os.path.join(OUT, "dashboard.png"), "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            with open(os.path.join(OUT, "dashboard.txt"), "w", encoding="utf-8") as fh:
                fh.write(text)
            # 主页（布局 Logo + 页脚）与登录页一样，也必须是自己的品牌、自己的几何 Logo。
            dash_snapshot = template_probe(cdp)
            dash_traces = template_offenders(dash_snapshot)
            dash_logo_ok, dash_logo_detail = logo_state(dash_snapshot)
            print(f"{'PASS' if not dash_traces else 'FAIL'} | 主页无 Ant Design Pro 痕迹 | {dash_traces or 'clean'}", flush=True)
            print(f"{'PASS' if dash_logo_ok else 'FAIL'} | 主页品牌 Logo 为本地 SVG 且已加载 | {dash_logo_detail}", flush=True)
            results.append(("/dashboard：无 Ant Design Pro 痕迹", "无模板外链/版权文案", not dash_traces, 0, "", dash_traces, [], 0))
            results.append(("/dashboard：品牌 Logo 为本地 SVG 且已加载", "/logo.svg 加载成功", dash_logo_ok, 0, "", [] if dash_logo_ok else ["logo 未加载"], [], 0))
        print(f"{'PASS' if ok else 'FAIL'} | {path:22s} | text={len(text):5d} | heading={heading.strip()[:24]!r} | bad={bad} | jserr={js_bad[:1]}", flush=True)

    # 未登录时应被弹回登录页
    cdp.evaluate("localStorage.removeItem('bastion_token')")
    cdp.navigate(BASE + "/audit/sessions")
    time.sleep(4)
    back_text = cdp.evaluate("document.body.innerText") or ""
    back_path = cdp.evaluate("location.pathname + location.search") or ""
    # 判据修正：登录页的「堡垒机账号」是 input 的 placeholder，不在 innerText 里；
    # 正确判据是 URL 被重写到 /user/login 且页面出现登录表单文案。
    back_ok = back_path.startswith("/user/login") and ("登录" in back_text)
    print(f"{'PASS' if back_ok else 'FAIL'} | 未登录访问 /audit/sessions 被弹回登录页 | path={back_path}", flush=True)
    results.append((f"/audit/sessions(未登录) -> {back_path}", "登录页", back_ok, len(back_text), "", [], [], 0))

    cdp.close()
    failed = [r for r in results if not r[2]]
    for r in results:
        print(f"{'PASS' if r[2] else 'FAIL'} | {r[0]:22s} | {r[4]}", flush=True)
    print(f"\n共 {len(results)} 个路由，通过 {len(results) - len(failed)}，失败 {len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
