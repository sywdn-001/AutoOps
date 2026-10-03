"""用真实 Chrome（CDP）验证「一连接一弹窗」的网页终端与「审计清除」UI 真的能用。

覆盖：
  1. 资产列表页（可访问资产表格 + 页面内不再内嵌终端、不放说明性提示条）
  2. 点「连接」真的**用一个带 features 的 `window.open()` 弹出独立窗口**（URL 带 /terminal/console，一个窗口一条会话）
  3. 控制台页骨架：工具条 / 终端 / 底部状态条；**没有标签栏、没有实时审计面板**
  4. 终端内真敲命令（逐键派发）与回显、状态条显示资产/协议/已连接/连接时长
  5. Ctrl/Cmd+F 搜索浮层、终端右键菜单、工作区全屏（真进 `document.fullscreenElement`）
  6. 点「断开」后终端内出现关窗倒计时（逐秒递减），数到 0 弹窗**自动关闭**
  7. 重开一个窗口，点工具条「资产列表」关闭弹窗
  8. 审计页「删除选中 / 清除筛选结果」入口与二次确认文案（不真的确认删除）
  9. 全程无未捕获 JS 异常

用法::

    python tools/console_check.py                       # admin / admin123
    python tools/console_check.py --admin-password <当前口令>
    python tools/console_check.py --token <JWT>

环境变量：`BASTION_CONSOLE_HOST`（默认 e2e-demo-01）、`BASTION_CONSOLE_WHOAMI`（默认 opsadmin）。

前置：后端已 `python run.py`（托管 dist）、演示目标机在 2200、Chrome 以
`--remote-debugging-port=9222 --remote-allow-origins=*` 启动。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

import websocket  # websocket-client  # noqa: F401  (CDPClient 依赖)

# 报告里有中文，Windows 控制台/管道默认 GBK（cp936）会让 print 抛
# UnicodeEncodeError 截断报告；统一按 UTF-8 输出并允许替换字符。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui_check import CDP, BASE, CDPClient, _parse_args, token_from_login  # noqa: E402

HOST_LABEL = os.environ.get("BASTION_CONSOLE_HOST", "e2e-demo-01")
WHOAMI_EXPECT = os.environ.get("BASTION_CONSOLE_WHOAMI", "opsadmin")

KEYS = {
    "Enter": ("Enter", 13),
    "Escape": ("Escape", 27),
    "f": ("KeyF", 70),
    "w": ("KeyW", 87),
    "h": ("KeyH", 72),
    "o": ("KeyO", 79),
    "a": ("KeyA", 65),
    "m": ("KeyM", 77),
    "i": ("KeyI", 73),
}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}", flush=True)
    return bool(ok)


# --------------------------------------------------------------------------- #
# CDP 基础设施
# --------------------------------------------------------------------------- #

def list_targets() -> list[dict]:
    with urllib.request.urlopen(CDP + "/json/list", timeout=15) as resp:
        return json.loads(resp.read().decode())


def close_target(target_id: str) -> None:
    """关掉一个浏览器窗口/标签页（Chrome 的 HTTP 端点）。"""
    if not target_id:
        return
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{target_id}", timeout=10).read()
    except Exception:  # noqa: BLE001 - 关不掉不影响结论
        pass


def find_rect(cdp: CDPClient, selector: str) -> dict | None:
    """按 CSS 选择器找可见元素，返回中心点坐标（不可见/不存在返回 None）。"""
    expr = f"""
    (() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      return JSON.stringify({{x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
                              text: (el.innerText || el.value || '').trim().slice(0, 40)}});
    }})()
    """
    raw = cdp.evaluate(expr)
    return json.loads(raw) if raw else None


def click_point(cdp: CDPClient, point: dict, button: str = "left") -> None:
    buttons = 1 if button == "left" else 2
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
    cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": point["x"],
            "y": point["y"],
            "button": button,
            "buttons": buttons,
            "clickCount": 1,
        },
    )
    cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": point["x"],
            "y": point["y"],
            "button": button,
            "buttons": 0,
            "clickCount": 1,
        },
    )


def js_click_text(cdp: CDPClient, text: str, selector: str = "button") -> bool:
    """用元素自身的 click() 触发。

    antd 6 会在中文按钮字之间插空格（「连 接」），所以文本比较必须先去空白；
    另外鼠标坐标点击 Modal / 工具条里的 antd 按钮偶发不生效，按钮类目标一律走 JS click。
    """
    expr = f"""
    (() => {{
      const compact = (value) => (value || '').replace(/\\s+/g, '');
      const needle = compact({json.dumps(text)});
      const el = [...document.querySelectorAll({json.dumps(selector)})]
        .find((node) => compact(node.innerText).includes(needle));
      if (!el) return 'NOT_FOUND';
      el.click();
      return 'CLICKED:' + (el.innerText || '').trim().slice(0, 20);
    }})()
    """
    raw = cdp.evaluate(expr) or ""
    return raw.startswith("CLICKED")


def js_click_selector(cdp: CDPClient, selector: str, index: int = 0) -> bool:
    expr = f"""
    (() => {{
      const list = [...document.querySelectorAll({json.dumps(selector)})];
      const el = list[{index}];
      if (!el) return 'NOT_FOUND';
      el.click();
      return 'CLICKED:' + (el.innerText || '').trim().slice(0, 20);
    }})()
    """
    raw = cdp.evaluate(expr) or ""
    return raw.startswith("CLICKED")


def press_key(cdp: CDPClient, name: str, modifiers: int = 0) -> None:
    code, vk = KEYS[name]
    for kind in ("keyDown", "keyUp"):
        cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": kind,
                "key": name,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "modifiers": modifiers,
            },
        )


def type_text(cdp: CDPClient, text: str) -> None:
    for char in text:
        code, vk = KEYS.get(char, (f"Key{char.upper()}", ord(char.upper())))
        cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": char,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "text": char,
                "unmodifiedText": char,
            },
        )
        cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": char,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            },
        )


# 可见终端输出：按 .xterm-rows 的可见性判定（隐藏的终端是 display:none）
VISIBLE_ROWS = "[...document.querySelectorAll('.xterm-rows')].filter((el) => el.offsetParent !== null)"


def terminal_text(cdp: CDPClient) -> str:
    return (
        cdp.evaluate(
            f"(() => {{ const rows = {VISIBLE_ROWS}; return rows.length ? rows[rows.length - 1].innerText : ''; }})()"
        )
        or ""
    )


def status_text(cdp: CDPClient) -> str:
    return cdp.evaluate("(document.querySelector('.bastion-statusbar') || {}).innerText || ''") or ""


def countdown_remaining(cdp: CDPClient) -> int | None:
    """读终端里「本窗口将在 N 秒后自动关闭…」的当前秒数（没在倒数返回 None）。"""
    match = re.search(r"本窗口将在 (\d+) 秒后自动关闭", terminal_text(cdp))
    return int(match.group(1)) if match else None


def console_windows_open() -> int:
    """当前还开着的终端弹窗数量（关窗后 target 会消失，据此断言「自动关闭」）。"""
    return sum(
        1
        for t in list_targets()
        if t.get("type") == "page" and "/terminal/console" in (t.get("url") or "")
    )


def js_errors(cdp: CDPClient) -> list[str]:
    raw = cdp.evaluate("JSON.stringify(window.__bastionErrors || [])") or "[]"
    return json.loads(raw)


def arm_error_capture(cdp: CDPClient) -> None:
    cdp.evaluate(
        "window.__bastionErrors = [];"
        " window.addEventListener('error', (e) => window.__bastionErrors.push(String(e.message)));"
    )


def wait_until(fn, timeout: float = 15.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001 - 轮询期容忍瞬时错误
            pass
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# 业务动作
# --------------------------------------------------------------------------- #

def find_connect_button(cdp: CDPClient, host_label: str, scroll: bool = True) -> dict | None:
    """在资产列表里找到该主机那一行「连接」按钮的中心点（找不到/被禁用/不可见返回 None）。

    `scroll=True` 时先 `scrollIntoView`——浏览器窗口可能比表格矮（实测 764×485），
    第 4 行会落在视口外，此时 CDP 按视口坐标点击会打空（`elementFromPoint` 返回 null）。
    """
    expr = f"""
    (() => {{
      const compact = (value) => (value || '').replace(/\\s+/g, '');
      const needle = compact({json.dumps(host_label)});
      const rows = [...document.querySelectorAll('.ant-table-row')];
      const row = rows.find((item) => compact(item.innerText).includes(needle));
      if (!row) return null;
      const btn = [...row.querySelectorAll('button')].find((node) => compact(node.innerText).includes('连接'));
      if (!btn || btn.disabled) return null;
      if ({str(scroll).lower()}) btn.scrollIntoView({{block: 'center', inline: 'nearest'}});
      const rect = btn.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return null;
      return JSON.stringify({{x, y, text: (btn.innerText || '').trim().slice(0, 20)}});
    }})()
    """
    raw = cdp.evaluate(expr)
    return json.loads(raw) if raw else None


def open_console_tab(cdp: CDPClient, host_label: str, wait: float = 20.0):
    """点「连接」→ 等弹出窗口出现 → 返回 (控制台 CDPClient, target)。

    两个坑：
      1. 必须用**真实鼠标事件**点击——`el.click()` 不产生用户激活（user activation），
         `window.open` 会被 Chrome 当弹窗拦掉，窗口根本不会出现。
      2. 点击前必须把按钮滚进视口，否则坐标落在视口外，点击打空。
    """
    before = {t.get("id") for t in list_targets() if t.get("type") == "page"}
    if not find_connect_button(cdp, host_label):
        print(f"  [diag] 没找到可点击的「连接」按钮（host={host_label}）", flush=True)
        return None, None
    time.sleep(0.5)
    point = find_connect_button(cdp, host_label, scroll=False)
    if not point:
        print("  [diag] 滚动后按钮仍不在视口内", flush=True)
        return None, None
    click_point(cdp, point)
    found: list[dict] = []
    wait_until(
        lambda: bool(
            found.extend(
                t
                for t in list_targets()
                if t.get("type") == "page"
                and t.get("id") not in before
                and "/terminal/console" in (t.get("url") or "")
            )
        ),
        timeout=wait,
        interval=0.4,
    )
    if not found:
        after = [t.get("url") for t in list_targets() if t.get("type") == "page"]
        popup = cdp.evaluate("(document.querySelector('.ant-message') || {}).innerText || ''") or ""
        print(f"  [diag] 未出现终端窗口，当前标签：{after} message={popup!r}", flush=True)
        return None, None
    target = found[0]
    client = CDPClient(target["webSocketDebuggerUrl"])
    client.send("Page.enable")
    client.send("Runtime.enable")
    time.sleep(1.0)
    arm_error_capture(client)
    return client, target


def open_file_console_tab(console_cdp: CDPClient, wait: float = 25.0):
    """点终端工具条的「文件管理」→ 等文件管理器窗口出现 → 返回 (CDPClient, target)。

    与 `open_console_tab` 同一套坑：必须用真实鼠标事件点击（`el.click()` 没有用户激活，
    `window.open` 会被 Chrome 当弹窗拦掉），且按钮必须在视口内。
    """
    before = {t.get("id") for t in list_targets() if t.get("type") == "page"}
    point = find_rect(console_cdp, ".bastion-toolbar-files")
    if not point:
        print("  [diag] 终端工具条上没有可点的「文件管理」按钮（未渲染/被禁用）", flush=True)
        return None, None
    click_point(console_cdp, point)
    found: list[dict] = []
    wait_until(
        lambda: bool(
            found.extend(
                t
                for t in list_targets()
                if t.get("type") == "page"
                and t.get("id") not in before
                and "/files/console" in (t.get("url") or "")
            )
        ),
        timeout=wait,
        interval=0.4,
    )
    if not found:
        after = [t.get("url") for t in list_targets() if t.get("type") == "page"]
        popup = console_cdp.evaluate("(document.querySelector('.ant-message') || {}).innerText || ''") or ""
        print(f"  [diag] 未出现文件管理器窗口，当前标签：{after} message={popup!r}", flush=True)
        return None, None
    target = found[0]
    client = CDPClient(target["webSocketDebuggerUrl"])
    client.send("Page.enable")
    client.send("Runtime.enable")
    time.sleep(1.5)
    arm_error_capture(client)
    return client, target


def main() -> int:
    opts = _parse_args(sys.argv[1:])
    token = opts["token"] or token_from_login(opts["admin_user"], opts["admin_password"])

    page = next(t for t in list_targets() if t.get("type") == "page")
    cdp = CDPClient(page["webSocketDebuggerUrl"])
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    cdp.send("Page.navigate", {"url": BASE + "/user/login"})
    time.sleep(3)
    cdp.evaluate(f"localStorage.setItem('bastion_token', {json.dumps(token)});")
    arm_error_capture(cdp)
    print(f"injected token: len={len(token)} host={HOST_LABEL}", flush=True)

    console_cdp = None
    console_target = None
    try:
        # ---------- 1. 资产列表页 ----------
        cdp.send("Page.navigate", {"url": BASE + "/terminal"})
        time.sleep(8)
        launcher = json.loads(
            cdp.evaluate(
                """JSON.stringify({
                     path: location.pathname,
                     rows: document.querySelectorAll('.ant-table-row').length,
                     hostCell: !!document.querySelector('.bastion-launcher-host'),
                     xterm: document.querySelectorAll('.xterm').length,
                     infoAlerts: document.querySelectorAll('.ant-alert-info').length,
                     hintText: (document.body.innerText || '').includes('一个弹窗'),
                   })"""
            )
            or "{}"
        )
        # 用户明确要求：这个页面不要再放「一个弹窗 = 一条审计会话」这类说明性提示条，
        # 以后写代码也别加。所以这里反过来守：资产列表页必须没有 info 提示条。
        check(
            "资产列表页渲染（资产表格，页面内不再内嵌终端、不放说明性提示条）",
            launcher.get("rows", 0) >= 1
            and launcher.get("hostCell")
            and launcher.get("xterm") == 0
            and launcher.get("infoAlerts") == 0
            and not launcher.get("hintText"),
            f"path={launcher.get('path')} rows={launcher.get('rows')} xterm={launcher.get('xterm')} "
            f"infoAlerts={launcher.get('infoAlerts')} hintText={launcher.get('hintText')}",
        )

        # ---------- 2. 弹出窗口建连 ----------
        # 先给 window.open 埋点：要断言的是「第三参数带 features 的弹出式窗口」，
        # 而不是「不给 features 的新标签页」——两者在 CDP 里都是 type=page 的 target，看不出来。
        cdp.evaluate(
            """(() => {
                 const original = window.open;
                 window.__bastionOpenArgs = [];
                 window.open = function (url, name, features) {
                   window.__bastionOpenArgs.push({ url: url, name: name, features: features });
                   return original.apply(window, arguments);
                 };
                 return true;
               })()"""
        )
        console_cdp, console_target = open_console_tab(cdp, HOST_LABEL)
        check(
            f"点「连接」弹出独立终端窗口（{HOST_LABEL}，/terminal/console）",
            console_cdp is not None,
            (console_target or {}).get("url", ""),
        )
        open_args = json.loads(cdp.evaluate("JSON.stringify(window.__bastionOpenArgs || [])") or "[]")
        first_open = open_args[0] if open_args else {}
        features = first_open.get("features") or ""
        check(
            "弹窗走 window.open 第三参数（popup=yes + 尺寸），不是普通新标签页",
            "popup=yes" in features and "width=" in features and "height=" in features,
            f"name={(first_open.get('name') or '')[:44]} features={features[:72]}",
        )
        check(
            "弹窗保留 opener（弹窗内「资产列表」能聚焦回原窗口）",
            bool(console_cdp and console_cdp.evaluate("!!window.opener")),
            "",
        )
        if console_cdp is None:
            return 1

        connected = wait_until(
            lambda: "已进入审计会话" in terminal_text(console_cdp)
            or "已连接" in status_text(console_cdp),
            timeout=25.0,
        )
        check("弹出窗口内会话建立成功", connected, status_text(console_cdp).replace("\n", " ")[:80])
        if not connected:
            return 1

        # ---------- 3. 控制台骨架：无标签栏、无实时审计面板 ----------
        layout = json.loads(
            console_cdp.evaluate(
                """JSON.stringify({
                     path: location.pathname,
                     toolbar: !!document.querySelector('.bastion-toolbar'),
                     asset: (document.querySelector('.bastion-toolbar-asset') || {}).innerText || '',
                     statusbar: !!document.querySelector('.bastion-statusbar'),
                     status: (document.querySelector('.bastion-statusbar') || {}).innerText || '',
                     tabs: document.querySelectorAll('.bastion-tab').length,
                     infoPanel: !!document.querySelector('.bastion-panel'),
                     auditWord: (document.body.innerText || '').includes('命令审计'),
                     duration: !!document.querySelector('.bastion-status-duration'),
                   })"""
            )
            or "{}"
        )
        check(
            "控制台页骨架（工具条 + 终端 + 底部状态条）",
            layout.get("toolbar") and layout.get("statusbar") and layout.get("path") == "/terminal/console",
            f"path={layout.get('path')}",
        )
        check(
            "同一页不再有多标签栏与实时审计面板（实时审计改去审计中心看）",
            layout.get("tabs") == 0 and not layout.get("infoPanel") and not layout.get("auditWord"),
            f"tabs={layout.get('tabs')} infoPanel={layout.get('infoPanel')}",
        )
        status = layout.get("status") or status_text(console_cdp)
        check(
            "状态条显示资产 / SSH / 已连接 / 连接时长",
            HOST_LABEL in status and "SSH" in status and "已连接" in status,
            status.replace("\n", " ")[:80],
        )
        check("工具条显示当前资产名", HOST_LABEL in (layout.get("asset") or ""), layout.get("asset") or "")

        # ---------- 4. 真敲命令 ----------
        console_cdp.evaluate("document.querySelector('.xterm-helper-textarea')?.focus()")
        type_text(console_cdp, "whoami")
        press_key(console_cdp, "Enter")
        time.sleep(4.0)
        terminal = terminal_text(console_cdp)
        check(
            "终端内逐键输入回显并返回命令结果",
            WHOAMI_EXPECT in terminal and "whoami" in terminal,
            terminal.splitlines()[-1][:60] if terminal else "",
        )
        check(
            "状态条连接时长开始走动（不再是 -）",
            ":" in (console_cdp.evaluate("(document.querySelector('.bastion-status-duration') || {}).innerText || ''") or ""),
            console_cdp.evaluate("(document.querySelector('.bastion-status-duration') || {}).innerText || ''") or "",
        )

        # ---------- 5. 搜索 / 右键菜单 / 全屏 ----------
        console_cdp.evaluate("document.querySelector('.xterm-helper-textarea')?.focus()")
        press_key(console_cdp, "f", modifiers=2)
        time.sleep(1.5)
        has_search = bool(find_rect(console_cdp, ".bastion-searchbar"))
        check("Ctrl+F 打开终端搜索浮层", has_search, "")
        if has_search:
            console_cdp.evaluate("document.querySelector('.bastion-searchbar input')?.focus()")
            type_text(console_cdp, "ops")
            js_click_text(console_cdp, "下一个", ".bastion-searchbar button")
            time.sleep(0.8)
            js_click_text(console_cdp, "关闭", ".bastion-searchbar button")
            time.sleep(0.8)
        check("关闭搜索浮层", not find_rect(console_cdp, ".bastion-searchbar"), "")

        pane_point = find_rect(console_cdp, ".bastion-terminal-area")
        if pane_point:
            click_point(console_cdp, pane_point, button="right")
            time.sleep(1.2)
        menu = console_cdp.evaluate("(document.querySelector('.ant-dropdown-menu') || {}).innerText || ''") or ""
        check(
            "终端右键菜单（复制/粘贴/全选/清屏/字号）",
            "复制" in menu and "清空屏幕" in menu,
            menu.replace("\n", " ")[:60],
        )
        press_key(console_cdp, "Escape")
        time.sleep(0.8)

        # 全屏：用户投诉「点了全屏没反应」——旧实现只切 `.bastion-console-fullscreen`
        # （`position: fixed; inset: 0`），而弹窗本来就铺满视口，视觉上零变化。
        # 现在必须真的进浏览器全屏，所以断言 `document.fullscreenElement`。
        # 工具条按钮用真实鼠标事件点（`el.click()` 没有用户激活，requestFullscreen 会被拒）。
        # 注意：进/出全屏会改变视口尺寸（784x519 ↔ 800x600），工具条按钮位置随之位移，
        # 所以每一次点击前都必须重新定位，不能复用上一次的坐标。
        def _is_fullscreen() -> bool:
            return bool(console_cdp.evaluate("!!document.fullscreenElement"))

        full_btn = find_rect(console_cdp, ".bastion-toolbar-fullscreen")
        if full_btn:
            click_point(console_cdp, full_btn)
            time.sleep(1.5)
        check(
            "点工具条「全屏」真的进入全屏（document.fullscreenElement）",
            _is_fullscreen(),
            f"button={bool(full_btn)}",
        )
        full_btn = find_rect(console_cdp, ".bastion-toolbar-fullscreen")
        if full_btn:
            click_point(console_cdp, full_btn)
            time.sleep(1.5)
        check("再点一次退出全屏", not _is_fullscreen(), f"button={bool(full_btn)}")

        press_key(console_cdp, "f", modifiers=10)  # Ctrl + Shift + F
        time.sleep(1.5)
        check("Ctrl+Shift+F 与按钮同一套全屏逻辑", _is_fullscreen(), "")
        press_key(console_cdp, "f", modifiers=10)
        time.sleep(1.5)
        check("再次 Ctrl+Shift+F 退出全屏", not _is_fullscreen(), "")

        # ---------- 5.5 工具条「文件管理」→ 独立窗口（SFTP 可视化） ----------
        # 需求原话：终端弹窗工具条里加「文件管理」，点开后像终端一样再弹一个独立窗口，
        # 用 SFTP 可视化浏览远端文件，每个操作都要过访问控制与审计。
        file_cdp, file_target = open_file_console_tab(console_cdp)
        if file_cdp:
            url = file_target.get("url") or ""
            check("点工具条「文件管理」弹出独立窗口（新标签页）", "/files/console" in url, url[-70:])
            rendered = wait_until(
                lambda: "readme.txt" in (file_cdp.evaluate("document.body.innerText") or ""),
                timeout=25.0,
                interval=0.6,
            )
            body = file_cdp.evaluate("document.body.innerText") or ""
            check(
                "文件管理器用 SFTP 真列出远端目录（可视化浏览器）",
                rendered and all(name in body for name in ("readme.txt", "docs", "data")),
                body.replace("\n", " ")[:90],
            )
            path_text = (
                file_cdp.evaluate("(document.querySelector('.bastion-files-pathbar') || {}).innerText || ''") or ""
            )
            check(
                "路径条显示当前目录（面包屑可跳转，已回到顶层 /）",
                "/" in path_text,
                path_text.replace("\n", " ")[:60],
            )
            toolbar = (
                file_cdp.evaluate("(document.querySelector('.bastion-files-toolbar') || {}).innerText || ''") or ""
            )
            check(
                "工具条给出上传 / 新建等文件操作入口",
                "上传" in toolbar and "新建目录" in toolbar,
                toolbar.replace("\n", " ")[:80],
            )
            check(
                "窗口带出该授权绑定的文件策略（访问控制上下文可见）",
                "策略" in (file_cdp.evaluate("document.body.innerText") or ""),
                "",
            )
            file_errors = js_errors(file_cdp)
            check(
                "文件管理器窗口全程无未捕获 JS 异常",
                not file_errors,
                json.dumps(file_errors[:2], ensure_ascii=False),
            )
            close_target(file_target.get("id", ""))
            time.sleep(1.2)
        else:
            check("点工具条「文件管理」弹出独立窗口（新标签页）", False, "未找到按钮或窗口未出现")
            check("文件管理器用 SFTP 真列出远端目录（可视化浏览器）", False, "窗口未打开")

        # ---------- 6. 断开会话 → 终端内 10 秒关窗倒计时 ----------
        # 用户需求：断开会话后终端里显示「本窗口将在 10s 后关闭」并逐秒倒数，数到 0 关窗。
        errors = js_errors(console_cdp)
        check("控制台窗口全程无未捕获 JS 异常", not errors, json.dumps(errors[:2], ensure_ascii=False))

        disc_btn = find_rect(console_cdp, ".bastion-toolbar-disconnect")
        if disc_btn:
            click_point(console_cdp, disc_btn)
        started = wait_until(lambda: countdown_remaining(console_cdp) is not None, timeout=8.0)
        first = countdown_remaining(console_cdp)
        check(
            "点「断开」后终端出现关窗倒计时",
            bool(disc_btn) and started and first is not None,
            f"button={bool(disc_btn)} 剩余={first}",
        )
        status = status_text(console_cdp)
        check("断开会话后状态条显示已断开", "已断开" in status or "断开" in status, status.replace("\n", " ")[:80])

        # 倒数在动：隔 2.5 秒再读一次，秒数必须变小（不是写死一行文本）
        time.sleep(2.5)
        second = countdown_remaining(console_cdp)
        check(
            "倒计时逐秒递减",
            first is not None and second is not None and second < first,
            f"{first} → {second}",
        )

        # 数到 0 后 `window.close()`：弹窗 target 必须消失
        closed = wait_until(lambda: console_windows_open() == 0, timeout=25.0, interval=0.5)
        check("倒计时结束后弹窗自动关闭", closed, f"remaining={console_windows_open()}")
        console_target = None

        # ---------- 6.5 工具条「资产列表」：关掉本窗口 ----------
        # 旧实现只调 `opener.focus()`（跨窗口聚焦被浏览器忽略）→ 用户点了「完全没用」。
        # 现在语义是「回到资产列表」= 关掉弹窗；原窗口本来就在资产列表页。
        console_cdp, console_target = open_console_tab(cdp, HOST_LABEL)
        back_btn = find_rect(console_cdp, ".bastion-toolbar-back") if console_cdp else None
        if back_btn:
            click_point(console_cdp, back_btn)
            time.sleep(2.5)
        still_open = console_windows_open() > 0
        check(
            "点工具条「资产列表」关闭终端弹窗",
            bool(back_btn) and not still_open,
            f"button={bool(back_btn)} remaining={still_open}",
        )
    finally:
        if console_target:
            close_target(console_target.get("id", ""))
            time.sleep(1.5)

    # ---------- 7. 审计清除 UI ----------
    cdp.send("Page.navigate", {"url": BASE + "/audit/logs"})
    time.sleep(6)
    logs_text = cdp.evaluate("document.body.innerText") or ""
    toolbar_ok = "删除选中" in logs_text and "清除筛选结果" in logs_text
    selection_ok = bool(find_rect(cdp, ".ant-table-selection-column"))
    check(
        "审计页出现「删除选中 / 清除筛选结果」入口与多选列",
        toolbar_ok and selection_ok,
        f"toolbar={toolbar_ok} selection={selection_ok}",
    )
    if toolbar_ok and js_click_text(cdp, "清除筛选结果", ".ant-pro-table-list-toolbar button, button"):
        time.sleep(1.2)
        popover = cdp.evaluate("(document.querySelector('.ant-popover') || {}).innerText || ''") or ""
        check(
            "清除操作有二次确认且说明留痕/级联影响",
            ("留痕" in popover) or ("确认" in popover and "清除" in popover),
            popover.replace("\n", " ")[:80],
        )
        press_key(cdp, "Escape")
        time.sleep(0.6)

    # ---------- 8. JS 异常 ----------
    errors = js_errors(cdp)
    check("资产列表页全程无未捕获 JS 异常", not errors, json.dumps(errors[:2], ensure_ascii=False))

    cdp.close()
    failed = [item for item in results if not item[1]]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
