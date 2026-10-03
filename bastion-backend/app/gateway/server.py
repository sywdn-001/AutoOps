"""SSH 审计网关：账号登录 → 主机菜单 → 审计 shell。

设计要点
--------
* 复用 paramiko 的服务器端实现（Transport + ServerInterface），不从零写协议。
* 认证走堡垒机自己的账号库（与 Web 端同一套），并受锁定策略、网关开关约束。
* 登录后进入「主机菜单」，只列出该账号**有权访问**且当前在时间窗内的主机。
* 选中主机后复用 ``session_service.open_session`` + ``ShellBridge``，
  因此网关与网页终端共享同一套命令拦截与审计落库逻辑。
* 会话结束回到菜单，可以继续连别的机器；退出菜单才断开连接。
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import logging
import os
import re
import socket
import threading
import time
import unicodedata

import paramiko

from ..ai.line_split import LineShadow
from ..extensions import db
from ..models import User, utcnow
from ..security import verify_password
from ..session_service import open_session, teardown_session
from ..settings_store import get_int, get_setting

logger = logging.getLogger(__name__)

DEFAULT_ROOM_WIDTH = 120


# ---------------------------------------------------------------------------
# 终端排版工具
#
# 血泪教训（真实缺陷）：菜单里「可用命令」整块曾经是裸 LF（"\n"）换行的字符串，
# 而菜单其它部分用 CRLF。PTY 里 **裸 LF 只把光标下移一行、不回列**，于是每一行都从
# 上一行结束的那一列开始打印，整块被排成阶梯状 —— 用户看到的就是「提示文字错位」。
# 所以：任何写进通道的多行文本，换行一律走 to_crlf() 归一化。
#
# 同理，CJK 字符在终端占 **2 列**，用 Python 的 ljust/rjust（按字符个数）补齐会让
# 中文主机名/账号名把后面的列顶歪，必须用 pad_display() 按显示列宽补齐。
# ---------------------------------------------------------------------------
#: 基础 8 色 SGR —— PuTTY / Xshell / Windows Terminal / iTerm / Linux console 通吃。
#: 颜色序列是「零宽」的：任何按列宽补齐、居中、断言的地方都必须先 strip_ansi()，
#: 否则 "\x1b[96m" 这 5 个字节会被当成 5 列宽，中英混排立刻歪。display_width() 已内置。
CLR_RESET = "\x1b[0m"
CLR_BOLD = "\x1b[1m"
CLR_DIM = "\x1b[90m"
CLR_RED = "\x1b[91m"
CLR_GREEN = "\x1b[92m"
CLR_YELLOW = "\x1b[93m"
CLR_BLUE = "\x1b[94m"
CLR_MAGENTA = "\x1b[95m"
CLR_CYAN = "\x1b[96m"
CLR_WHITE = "\x1b[97m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色序列，只留可见字符（宽度计算与字符串断言都用它）。"""
    return ANSI_RE.sub("", text or "")


def color(text: str, *codes: str) -> str:
    """给文本上色；空文本不加序列，避免输出里飘着无意义的转义。"""
    if not text:
        return text
    return "".join(codes) + text + CLR_RESET


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """终端显示列宽：CJK 宽字符记 2 列，组合字符记 0 列，ANSI 颜色序列不计宽。"""
    return sum(_char_width(char) for char in strip_ansi(text))


def pad_display(text: str, width: int, align: str = "left") -> str:
    """按显示列宽补空格（不是按字符个数），保证中英文混排的列对齐。"""
    text = text or ""
    spaces = max(0, width - display_width(text))
    if align == "right":
        return " " * spaces + text
    if align == "center":
        left = spaces // 2
        return " " * left + text + " " * (spaces - left)
    return text + " " * spaces


def to_crlf(text: str) -> str:
    """把任意换行统一成 CRLF —— PTY 下裸 LF 只下移不回列，会把整块排版排成阶梯。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


HELP_ITEMS: tuple[tuple[str, str], ...] = (
    ("<编号>", "连接对应主机"),
    ("l / list", "重新显示主机列表"),
    ("i / info", "查看本次登录与会话信息"),
    ("/ask-ai", "会话里问 AI（如 /ask-ai 这台机器磁盘满了吗）"),
    ("h / help", "显示本帮助"),
    ("q / quit", "退出堡垒机网关"),
)


def _help_text() -> str:
    """按显示列宽排版帮助块：所有说明文字必须从同一列开始（说明列固定第 16 显示列）。"""
    lines = ["  " + color("可用命令：", CLR_BOLD, CLR_YELLOW)]
    for keys, description in HELP_ITEMS:
        lines.append("    " + color(pad_display(keys, 12), CLR_CYAN) + color(description, CLR_WHITE))
    return "\r\n".join(lines)


HELP_TEXT = _help_text()


# ---------------------------------------------------------------------------
# 进站字符画（CLI 侧的 logo）
#
# 只用 ▄▀█ 这类块字符 + 基础 8 色：不依赖字体、图片或真彩（24bit），老旧 SSH 客户端
# 上也不会变成乱码。形状与网页端同一个「盾牌 + >_ 提示符」+ AutoOps 字标，
# 保证命令行和 Web 是同一个品牌。
# ---------------------------------------------------------------------------
BANNER_SHIELD: tuple[str, ...] = (
    " ███████████ ",
    "█████████████",
    "███  >_   ███",
    "█████████████",
    " ███████████ ",
    "   ███████   ",
)
#: 盾牌右侧的品牌字标：字母间距拉开的品牌名 + 一条细横线。
#: **刻意不用「块字符拼字母」**：█ 在不同字体/行高下上下不一定严丝合缝，一旦有缝，
#: 竖笔画就断成一串短横（真实截图里踩过，5 行点阵字母糊成一片虚线）；纯文本则
#: 任何客户端、任何字体都清晰，也不依赖真彩。
BANNER_WORDMARK: tuple[str, ...] = (
    "A u t o O p s",
    "─────────────",
)
BANNER_WORDMARK_COLORS = (CLR_BOLD + CLR_WHITE, CLR_CYAN)
BANNER_SUBTITLE = "统一运维入口 · 全程操作审计 · 命令级策略管控"
BANNER_GAP = "   "
#: 字标相对盾牌顶部的行偏移（2 行字标在 6 行盾牌里垂直居中）
BANNER_WORDMARK_OFFSET = 2

_ART_BODY_CHARS = "█▀▄▌▐"
_ART_EDGE_CHARS = "╔╗╚╝║═"
_ART_ACCENT_CHARS = ">_"


def _paint_art_row(row: str, body_codes: str, edge_codes: str, accent_codes: str) -> str:
    """逐字符给字符画上色：块=body、边框=edge、提示符=accent、空格不上色。

    按「同色连续段」合并转义序列 —— 每个字符都包一层 \x1b[ 会把 SSH 流量翻好几倍。
    """
    out: list[str] = []
    current = ""
    for char in row:
        if char == " ":
            wanted = ""
        elif char in _ART_ACCENT_CHARS:
            wanted = accent_codes
        elif char in _ART_EDGE_CHARS:
            wanted = edge_codes
        elif char in _ART_BODY_CHARS:
            wanted = body_codes
        else:
            wanted = ""
        if wanted != current:
            out.append(CLR_RESET if not wanted else wanted)
            current = wanted
        out.append(char)
    if current:
        out.append(CLR_RESET)
    return "".join(out)


def _banner_rows() -> list[str]:
    """盾牌 + AutoOps 字标（已上色）。行宽按**可见图形**补齐，不留多余空隙。"""
    shield_width = max(display_width(row.rstrip()) for row in BANNER_SHIELD)
    wordmark_width = max(display_width(row.rstrip()) for row in BANNER_WORDMARK)
    rows = []
    for index, shield_row in enumerate(BANNER_SHIELD):
        painted = _paint_art_row(
            pad_display(shield_row.rstrip(), shield_width),
            CLR_BOLD + CLR_CYAN,
            CLR_BLUE,
            CLR_BOLD + CLR_YELLOW,
        )
        word_index = index - BANNER_WORDMARK_OFFSET
        if 0 <= word_index < len(BANNER_WORDMARK):
            painted += BANNER_GAP + color(
                pad_display(BANNER_WORDMARK[word_index].rstrip(), wordmark_width),
                BANNER_WORDMARK_COLORS[word_index % len(BANNER_WORDMARK_COLORS)],
            )
        rows.append(painted)
    return rows


def _render_banner(width: int = DEFAULT_ROOM_WIDTH) -> str:
    """进站字符画：整体居中、彩色、绝不折行；终端放不下就返回空串由调用方跳过。"""
    rows = _banner_rows()
    if not rows:
        return ""
    art_width = max(display_width(row) for row in rows)
    terminal_width = int(width or DEFAULT_ROOM_WIDTH)
    if terminal_width < art_width + 6:
        return ""
    indent = " " * max(0, (terminal_width - art_width) // 2)
    lines = [""]
    lines.extend(indent + row for row in rows)
    lines.append("")
    subtitle = BANNER_SUBTITLE
    lines.append(" " * max(0, (terminal_width - display_width(subtitle)) // 2) + color(subtitle, CLR_DIM))
    lines.append("")
    return to_crlf("\r\n".join(lines)) + "\r\n"


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _iso(dt) -> str:
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
def _authenticate(app, username: str, password: str, ip: str):
    """返回 (user_id, username, error_message)。error_message 为空即成功。"""
    from ..audit import log_event

    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user is None:
            log_event(
                "auth",
                "gateway_login",
                result="failure",
                actor_username=username,
                ip=ip,
                message="SSH 网关登录失败：账号不存在",
            )
            return None, None, "用户名或密码错误"
        if not user.is_active():
            log_event(
                "auth",
                "gateway_login",
                result="failure",
                actor_username=username,
                actor_id=user.id,
                ip=ip,
                message="SSH 网关登录失败：账号已停用",
            )
            return None, None, "该账号已被停用，请联系管理员"
        if user.is_locked():
            log_event(
                "auth",
                "gateway_login",
                result="failure",
                actor_username=username,
                actor_id=user.id,
                ip=ip,
                message="SSH 网关登录失败：账号处于锁定状态",
            )
            return None, None, f"账号已锁定，请于 {_iso(user.locked_until)} 后重试"
        if not user.gateway_enabled:
            log_event(
                "auth",
                "gateway_login",
                result="failure",
                actor_username=username,
                actor_id=user.id,
                ip=ip,
                message="SSH 网关登录失败：账号未开放网关访问",
            )
            return None, None, "该账号未被授权使用 SSH 网关"

        if not verify_password(password, user.password_hash):
            user.failed_attempts = (user.failed_attempts or 0) + 1
            max_failures = get_int("login_max_failures", app.config.get("LOGIN_MAX_FAILURES", 5)) or 5
            lock_minutes = get_int("login_lock_minutes", app.config.get("LOGIN_LOCK_MINUTES", 15)) or 15
            locked = False
            if user.failed_attempts >= max_failures:
                user.locked_until = utcnow() + _dt.timedelta(minutes=lock_minutes)
                locked = True
            db.session.commit()
            log_event(
                "auth",
                "login_locked" if locked else "gateway_login",
                result="failure",
                actor_username=username,
                actor_id=user.id,
                ip=ip,
                message=(
                    f"SSH 网关密码错误达 {user.failed_attempts} 次，账号锁定 {lock_minutes} 分钟"
                    if locked
                    else f"SSH 网关登录失败：密码错误（第 {user.failed_attempts} 次）"
                ),
            )
            if locked:
                return None, None, f"密码错误次数过多，账号已锁定 {lock_minutes} 分钟"
            return None, None, "用户名或密码错误"

        user.failed_attempts = 0
        user.locked_until = None
        user.last_login_at = utcnow()
        user.last_login_ip = ip
        db.session.commit()
        log_event(
            "auth",
            "gateway_login",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
            actor_id=user.id,
            actor_username=user.username,
            actor_role=user.role_code,
            ip=ip,
            message="SSH 网关登录成功",
        )
        return user.id, user.username, ""


class BastionServerInterface(paramiko.ServerInterface):
    """把 paramiko 的认证回调接到堡垒机账号库上。"""

    def __init__(self, app, client_ip: str, client_port: int, allow_password=True, allow_pubkey=True):
        self.app = app
        self.client_ip = client_ip
        self.client_port = client_port
        self.allow_password = allow_password
        self.allow_pubkey = allow_pubkey
        self.event = threading.Event()
        self.user_id = None
        self.username = None
        self.auth_method = None
        self.error = ""
        self.window = (DEFAULT_ROOM_WIDTH, 32)
        self.window_changed = threading.Event()

    # -- 认证 ---------------------------------------------------------------
    def check_auth_password(self, username, password):
        if not self.allow_password:
            self.error = "该堡垒机未开放口令登录"
            return paramiko.AUTH_FAILED
        user_id, name, error = _authenticate(self.app, username, password, self.client_ip)
        if user_id is None:
            self.error = error
            return paramiko.AUTH_FAILED
        self.user_id, self.username, self.auth_method = user_id, name, "password"
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        if not self.allow_pubkey:
            return paramiko.AUTH_FAILED
        fingerprint = key.get_fingerprint().hex()
        with self.app.app_context():
            user = User.query.filter_by(username=username).first()
            if user is None or not user.is_active() or not user.gateway_enabled:
                self.error = "该账号未授权使用 SSH 网关"
                return paramiko.AUTH_FAILED
            stored = (user.public_key or "").strip()
            if not stored:
                self.error = "该账号未登记公钥，请使用密码登录"
                return paramiko.AUTH_FAILED
            if fingerprint not in stored.replace(":", "").lower() and stored.lower() not in (
                key.get_name().lower(),
                fingerprint.lower(),
            ):
                self.error = "公钥不匹配"
                return paramiko.AUTH_FAILED
        self.user_id, self.username, self.auth_method = user.id, user.username, "publickey"
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        methods = []
        if self.allow_password:
            methods.append("password")
        if self.allow_pubkey:
            methods.append("publickey")
        return ",".join(methods) or "password"

    # -- 通道 ---------------------------------------------------------------
    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_exec_request(self, channel, command):
        # 支持 `ssh -p 2222 user@host` 直接带命令：只允许列出主机菜单，其余拒绝。
        self.event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.window = (int(width) or DEFAULT_ROOM_WIDTH, int(height) or 32)
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        self.window = (int(width) or DEFAULT_ROOM_WIDTH, int(height) or 32)
        self.window_changed.set()
        return True


# ---------------------------------------------------------------------------
# 终端输出小工具
# ---------------------------------------------------------------------------
class ChannelWriter:
    """给通道加一把锁，避免菜单线程与会话输出线程交叉写花屏。"""

    def __init__(self, channel):
        self.channel = channel
        self._lock = threading.Lock()

    def write(self, text: str):
        if not text:
            return
        data = text.encode("utf-8", "replace")
        with self._lock:
            try:
                self.channel.sendall(data)
            except Exception:  # noqa: BLE001 - 客户端断开时忽略
                pass

    def write_bytes(self, data: bytes):
        if not data:
            return
        with self._lock:
            try:
                self.channel.sendall(data)
            except Exception:  # noqa: BLE001
                pass

    def line(self, text: str = ""):
        self.write(text + "\r\n")


def _read_key(channel, timeout=None):
    """读一个字节。返回 None 表示本轮超时（没有数据），b'' 表示对端已关闭。"""
    channel.settimeout(timeout)
    try:
        data = channel.recv(1)
    except socket.timeout:
        return None
    except Exception:  # noqa: BLE001 - 对端断开
        return b""
    return data if data else b""


def _read_line(channel, writer: ChannelWriter, prompt: str = "", mask: bool = False, allow_empty=True):
    """逐字符读取一行；返回 (text, aborted)。aborted 表示客户端断开或 Ctrl-C/Ctrl-D。"""
    if prompt:
        writer.write(prompt)
    buf: list[str] = []
    while True:
        raw = _read_key(channel)
        if raw is None:
            continue
        if raw == b"":
            return "", True
        byte = raw[0]
        if byte in (10, 13):  # Enter
            writer.write("\r\n")
            return "".join(buf).strip(), False
        if byte in (3, 4):  # Ctrl-C / Ctrl-D
            writer.write("^C\r\n")
            return "", True
        if byte in (8, 127):  # Backspace
            if buf:
                buf.pop()
                writer.write("\b \b")
            continue
        if byte == 27:  # ESC：吞掉常见转义序列
            for _ in range(2):
                if _read_key(channel, timeout=0.05) in (None, b""):
                    break
            continue
        if byte < 32:
            continue
        char = raw.decode("utf-8", "ignore")
        if not char or char == "\ufffd":
            continue
        buf.append(char)
        writer.write("*" if mask else char)
        if len(buf) > 512:
            break
    return "".join(buf), False


# ---------------------------------------------------------------------------
# 菜单渲染
# ---------------------------------------------------------------------------
#: 终端窄于该列数时，主机信息改用两行式布局（单行完整版约 95 列，80 列终端会折行）
COMPACT_MENU_WIDTH = 96


def _render_target_lines(entries, width: int = DEFAULT_ROOM_WIDTH) -> list[str]:
    """渲染可访问主机列表（每行一个元素，不含换行）。

    `width` 是客户端 PTY 的列数：窄终端（< COMPACT_MENU_WIDTH）改用「主机行 + 明细行」
    两行式布局 —— 完整单行有 95 列左右，80 列的默认终端会把它折行，看起来又像排版错乱。

    配色只加在**取值**上，标签（` 策略: ` / ` 账号: `）保持纯文本：既好看，也让
    「列是否对齐」的断言可以直接对可见文本做判断。
    """
    if not entries:
        return ["  " + color("（当前没有你可访问的主机，请联系管理员授权）", CLR_YELLOW)]
    compact = bool(width) and int(width) < COMPACT_MENU_WIDTH
    lines = []
    for index, entry in enumerate(entries, start=1):
        accounts = "、".join(item["name"] for item in entry["accounts"]) or "无可用账号"
        policy = entry["policyName"] or "未绑定策略"
        flag = color(" [仅命令行]", CLR_DIM) if not entry["canWebterm"] else ""
        if compact:
            lines.append(
                "  "
                + color(pad_display(f"[{index:>2}]", 5), CLR_BOLD, CLR_YELLOW)
                + color(pad_display(entry["hostName"], 18), CLR_BOLD, CLR_WHITE)
                + color(pad_display(f"{entry['address']}:{entry['port']}", 20), CLR_CYAN)
                + flag
            )
            detail = "      策略: " + color(policy, CLR_YELLOW)
            if entry["groupName"] and entry["groupName"] != "-":
                detail += "    分组: " + color(str(entry["groupName"]), CLR_MAGENTA)
            lines.append(detail + "    账号: " + color(accounts, CLR_GREEN))
            continue
        lines.append(
            "  "
            + color(pad_display(f"[{index:>2}]", 5), CLR_BOLD, CLR_YELLOW)
            + color(pad_display(entry["hostName"], 18), CLR_BOLD, CLR_WHITE)
            + color(pad_display(f"{entry['address']}:{entry['port']}", 20), CLR_CYAN)
            + color(pad_display(entry["groupName"] or "-", 10), CLR_DIM)
            + " 策略: "
            + color(pad_display(policy, 22), CLR_YELLOW)
            + " 账号: "
            + color(accounts, CLR_GREEN)
            + flag
        )
    return lines


def _render_targets(entries, width: int = DEFAULT_ROOM_WIDTH) -> str:
    """兼容旧接口：主机列表 + 结尾 CRLF。"""
    return to_crlf("\r\n".join(_render_target_lines(entries, width))) + "\r\n"


def _menu_blocks(entries, username, display_name, role_name, ip, live_count, width=DEFAULT_ROOM_WIDTH) -> list[list[str]]:
    """菜单的三段内容：身份信息 / 可访问主机 / 可用命令。分隔线宽度由这三段内容算出来。

    关键取舍：颜色只包裹**取值**，标签与分隔线长度都按 `strip_ansi` 后的可见文本计算，
    所以「= 号跟随文字长度」这件事在任何终端宽度与任何配色下都成立。
    """
    newline = "\r\n"
    return [
        [
            "  "
            + color("堡垒机审计网关", CLR_BOLD, CLR_CYAN)
            + color(" · ", CLR_DIM)
            + color(get_setting("site_name", "Bastion"), CLR_BOLD, CLR_WHITE),
            "  "
            + color("登录账号：", CLR_DIM)
            + color(display_name or username, CLR_BOLD, CLR_GREEN)
            + color(f"（{username}）", CLR_GREEN)
            + "    "
            + color("角色：", CLR_DIM)
            + color(role_name or "-", CLR_MAGENTA),
            "  "
            + color("来源 IP：", CLR_DIM)
            + color(str(ip), CLR_CYAN)
            + "    "
            + color("登录时间：", CLR_DIM)
            + color(_now_text(), CLR_DIM)
            + "    "
            + color("在线会话：", CLR_DIM)
            + color(str(live_count), CLR_BOLD, CLR_YELLOW),
        ],
        ["  " + color("你可访问的主机：", CLR_BOLD, CLR_YELLOW)] + _render_target_lines(entries, width),
        _help_text().split(newline),
    ]


def _menu_rules(blocks: list[list[str]], limit: int) -> list[str]:
    """蓝色分隔线：每条长度 = 紧邻内容块的显示宽度（跟随文字长度变化，不再固定 78 列）。

    长度序列 = [首块宽] + [相邻两块取宽者 …] + [末块宽]，保证每段内容都被恰好包住的线分开。
    """
    widths = [min(limit, max((display_width(line) for line in block), default=0)) for block in blocks]
    lengths = [widths[0]]
    lengths.extend(max(widths[index], widths[index + 1]) for index in range(len(widths) - 1))
    lengths.append(widths[-1])
    return [color("=" * max(20, length), CLR_BLUE) for length in lengths]


def _render_menu(entries, username, display_name, role_name, ip, live_count, width: int = DEFAULT_ROOM_WIDTH) -> str:
    # 分隔线跟随内容宽度（至少 20 列），并留 1 列余量避免顶到最后一列时自动折行
    limit = max(20, int(width or DEFAULT_ROOM_WIDTH) - 1)
    blocks = _menu_blocks(entries, username, display_name, role_name, ip, live_count, width)
    rules = _menu_rules(blocks, limit)
    parts: list[str] = ["", rules[0]]
    for index, block in enumerate(blocks):
        parts.extend(block)
        parts.append(rules[index + 1])
    return to_crlf("\r\n".join(parts)) + "\r\n"


# ---------------------------------------------------------------------------
# 连接处理
# ---------------------------------------------------------------------------
def _select_account(channel, writer: ChannelWriter, entry) -> dict | None:
    accounts = entry["accounts"]
    if not accounts:
        writer.line("\r\n" + color("[堡垒机] 该主机没有你可用的登录账号，请联系管理员。", CLR_RED))
        return None
    if len(accounts) == 1:
        return accounts[0]
    writer.line()
    writer.line("  " + color("该主机有多个登录账号，请选择：", CLR_BOLD, CLR_YELLOW))
    for index, account in enumerate(accounts, start=1):
        writer.line(
            "    "
            + color(f"[{index}]", CLR_BOLD, CLR_YELLOW)
            + " "
            + color(account["name"], CLR_BOLD, CLR_WHITE)
            + color(f"（{account['username']}）", CLR_DIM)
        )
    while True:
        text, aborted = _read_line(channel, writer, color("选择账号编号（回车用第 1 个）> ", CLR_BOLD, CLR_CYAN))
        if aborted:
            return None
        if text == "":
            return accounts[0]
        try:
            index = int(text)
        except ValueError:
            writer.line("  " + color("请输入编号数字。", CLR_RED))
            continue
        if 1 <= index <= len(accounts):
            return accounts[index - 1]
        writer.line("  " + color("编号超出范围，请重新输入。", CLR_RED))


#: 清屏 = 擦全屏 + 光标归位。只写给**客户端**，绝不在目标机上执行 ``clear`` 命令：
#: 那会多出一条用户没敲过的命令，既过命令策略又落审计。
CLEAR_SCREEN = "\x1b[2J\x1b[H"

#: 连接后等目标机第一个提示符的最长时间（秒）；超时就放弃清屏，绝不拖慢会话
CLEAR_SCREEN_TIMEOUT = 2.0


def _write_session_context(writer: ChannelWriter, entry, account, sid: str) -> None:
    """打印紧凑的会话上下文。

    连接横幅（``on_ready``）与「连接后清屏」重打共用这一份措辞与配色，
    免得两处文案各自漂移。
    """
    writer.line()
    writer.line(
        color("[堡垒机]", CLR_GREEN)
        + f" 已连接 {entry['hostName']}（{entry['address']}），"
        f"账号 {account['username'] if account else '-'}，会话号 {sid}"
    )
    writer.line(color("[堡垒机] 输入的命令与输出都会被审计记录。输入 exit 返回主机菜单。", CLR_DIM))
    writer.line(
        color("[堡垒机] 想随时问 AI：", CLR_DIM)
        + color("/ask-ai <问题>", CLR_CYAN)
        + color("（AI 的答案流式输出，操作同样入审计）", CLR_DIM)
    )
    writer.line()


def _bridge_at_prompt(bridge) -> bool:
    """桥接层是否已经「站在提示符上」，即目标机的开机输出（MOTD）已经回流完毕。

    分段模式下 ``at_prompt`` 由 ``bridge.start()`` 在读到第一个 ``__BASTION_*__`` 标记时
    置位，而 MOTD 是在该标记之前回流的——所以它一为真，就说明 MOTD 已经转发给客户端，
    此时清屏不会把 MOTD 之后的提示符擦掉。非分段（原始录制）模式拿不到标记，
    ``_arm`` 已经把开机输出排空，也算就绪。
    """
    return bool(getattr(bridge, "at_prompt", False) or not getattr(bridge, "segmented", False))


def _clear_screen_after_prompt(
    bridge, writer: ChannelWriter, entry, account, sid: str, timeout: float = CLEAR_SCREEN_TIMEOUT
) -> bool:
    """等目标机第一个提示符出现后清屏，重打上下文并让 bash 重画 PS1；返回是否真的清了屏。

    用户嫌「连上机器后 MOTD + 堡垒机横幅太乱」，这里只做三件事，全在客户端一侧：

    1. 写 ``\\x1b[2J\\x1b[H`` 擦屏——**不执行 ``clear`` 命令**，否则审计里会凭空多出
       一条用户没敲过的命令；
    2. 重打紧凑上下文（与连接横幅同一份措辞/配色）；
    3. ``bridge.feed_input(b"\\n")``：空行在 ``ShellBridge._handle_enter`` 里只往目标机回写
       一个 ``\\r``，不建 pending、不进命令策略、不落审计，但足以让 bash 重画 PS1
       （含 ``__BASTION_*__`` 标记），用户可以立刻继续敲命令。

    超过 ``timeout`` 还没等到提示符（或会话已经关闭）就静默放弃并返回 ``False``：
    清屏只是体验优化，绝不能卡住会话循环。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while not _bridge_at_prompt(bridge):
        if getattr(bridge, "closed", False) or time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    writer.write_bytes(CLEAR_SCREEN.encode())
    _write_session_context(writer, entry, account, sid)
    bridge.feed_input(b"\n")
    return True


def _run_session(app, channel, writer: ChannelWriter, user_id: int, entry, account, client_ip, server):
    """打开一条审计会话并接管终端，直到会话结束。"""
    cols, rows = server.window
    closed_reason: list[str] = []
    done = threading.Event()
    session_state: dict[str, str] = {}

    def on_closed(reason: str):
        closed_reason.append(reason or "会话结束")
        done.set()

    def on_notice(message: str):
        writer.line("\r\n" + color(f"[堡垒机] {message}", CLR_RED))

    def on_ready(ready_sid: str, ready_info: dict):
        """目标机输出回流之前先把堡垒机横幅打出去（否则 MOTD 会抢先）。"""
        session_state["sid"] = str(ready_sid or "")
        _write_session_context(writer, entry, account, ready_sid)

    try:
        opened = open_session(
            app,
            user_id=user_id,
            host_id=entry["hostId"],
            account_id=account["id"] if account else None,
            source="gateway",
            client_ip=client_ip,
            client_port=0,
            cols=cols,
            rows=rows,
            send_output=lambda data: writer.write_bytes(data),
            notify=on_notice,
            on_closed=on_closed,
            on_ready=on_ready,
        )
    except Exception as exc:  # noqa: BLE001 - 用中文原因回显给用户
        writer.line("\r\n" + color(f"[堡垒机] 无法连接 {entry['hostName']}：{exc}", CLR_RED))
        logger.warning("gateway open_session failed: %s", exc)
        return

    # 降级提示由桥接层统一发出（bridge._notify → on_notice 红字一行），这里不再重复打印。

    bridge = opened.bridge
    server.window_changed.clear()
    # 影子行缓冲必须在整条会话里长期存活：转义序列的解析状态要跨 feed 保持
    # （TCP 会把 ESC[200~ 之类切碎），每次按键新建会把粘贴标记误当正文。
    shadow = LineShadow()
    ai_conversation_id: int | None = None
    try:
        # 连上客户机后 MOTD + 堡垒机横幅太乱：等第一个提示符出现（说明 MOTD 已回流）
        # 就擦屏、重打紧凑上下文，再让 bash 重画 PS1。失败只记日志，绝不影响会话。
        try:
            if not _clear_screen_after_prompt(bridge, writer, entry, account, session_state.get("sid", "")):
                logger.debug("gateway clear screen skipped (sid=%s)", session_state.get("sid", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("gateway clear screen failed: %s", exc)
        while not bridge.closed and not done.is_set():
            if server.window_changed.is_set():
                server.window_changed.clear()
                cols, rows = server.window
                try:
                    bridge.resize(cols, rows)
                except Exception:  # noqa: BLE001
                    pass
            raw = _read_key(channel, timeout=0.4)
            if raw is None:
                continue
            if raw == b"":
                break
            forward, ai_line = _split_ai_command(raw, shadow)
            if forward:
                bridge.feed_input(forward)
            if ai_line is None:
                continue
            # 命中 /ask-ai：先让目标机丢弃这一行（readline 的 Ctrl-U 只清行不执行），
            # 再把问题交给 AI——目标是目标机，AI 的输出只打印在堡垒机这一侧。
            bridge.feed_input(b"\x15")
            # AI 侧任何异常都不允许掀掉整条 SSH 会话：打完错误行继续留在 shell 里。
            try:
                ai_conversation_id = _ask_ai_in_session(
                    app,
                    channel,
                    writer,
                    user_id,
                    entry,
                    account,
                    session_state.get("sid", ""),
                    _ai_shell().extract_question(ai_line) or "",
                    cols,
                    ai_conversation_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("gateway /ask-ai failed: %s", exc)
                writer.line()
                writer.line(color(f"[堡垒机] AI 调用失败：{exc}", CLR_RED))
                writer.line()
    finally:
        try:
            teardown_session(opened, reason="用户断开" if not bridge.closed else "会话结束")
        except Exception as exc:  # noqa: BLE001
            logger.warning("gateway teardown_session failed: %s", exc)

    writer.line()
    reason = closed_reason[0] if closed_reason else "会话已结束"
    writer.line(color(f"[堡垒机] 会话已关闭：{reason}", CLR_YELLOW))


def _channel_alive(channel) -> bool:
    try:
        return not (channel.closed or channel.eof_received)
    except Exception:  # noqa: BLE001
        return False


def _ai_shell():
    """延迟导入 ``ai_shell``。

    它需要 server 的配色与宽度工具（``color`` / ``display_width``），模块级互相导入会成环，
    所以只在会话里真正用到 ``/ask-ai`` 时导入。
    """
    from . import ai_shell

    return ai_shell


def _split_ai_command(raw: bytes, shadow: LineShadow) -> tuple[bytes, str | None]:
    """把一次按键输入拆成 ``(要转发给目标机的字节, 命中的 /ask-ai 命令行)``。

    分流逻辑的唯一真源是 :class:`app.ai.line_split.LineShadow`（网页终端也用它），
    这里保留同名薄包装，只是为了不动调用点与既有测试的形状。

    ``shadow`` 是影子行缓冲：**只用来判断这一行是不是 ``/ask-ai``**，
    不影响任何字节转发——所以本地回显、补全、Ctrl-C 依旧是目标机 bash 的行为。
    命中时不转发回车（改由调用方发 Ctrl-U 把该行从 bash 里抹掉），因此
    ``/ask-ai`` 绝不会被目标机当成命令执行。

    它必须**在整条会话里长期存活**（跨 ``feed`` 保持转义序列解析状态）：旧实现把
    转义序列长度硬编码成 2 字节，粘贴包裹 ``ESC[200~`` / ``ESC[201~``（5 字节）时
    会把 ``00~`` 当正文写进影子行，``/ask-ai`` 漏检后被目标机 bash 执行——
    用户看到的正是 ``-bash: /ask-ai: 没有那个文件或目录``。
    """
    return shadow.feed(raw)


def _ask_ai_in_session(
    app, channel, writer: ChannelWriter, user_id: int, entry, account, sid: str, question: str, width: int, conversation_id: int | None
) -> int | None:
    """在会话里跑一次 ``/ask-ai``（由 :mod:`app.gateway.ai_shell` 实现）。"""

    def read_line(prompt: str, mask: bool = False) -> tuple[str, bool]:
        return _read_line(channel, writer, color(prompt, CLR_CYAN), mask=mask)

    writer.line()
    return _ai_shell().run_ask_ai(
        app,
        writer,
        user_id=user_id,
        entry=entry,
        account=account,
        sid=sid,
        question=question,
        width=width,
        read_line=read_line,
        conversation_id=conversation_id,
    )


def handle_client(app, sock: socket.socket, addr, host_key):
    client_ip, client_port = addr[0], addr[1]
    transport = None
    try:
        transport = paramiko.Transport(sock)
        transport.local_version = app.config.get("GATEWAY_SERVER_VERSION", "SSH-2.0-BastionGW_1.0")
        transport.add_server_key(host_key)
        transport.set_keepalive(int(app.config.get("SSH_KEEPALIVE", 30) or 30))

        server = BastionServerInterface(
            app,
            client_ip,
            client_port,
            allow_password=bool(app.config.get("GATEWAY_ALLOW_PASSWORD", True)),
            allow_pubkey=bool(app.config.get("GATEWAY_ALLOW_PUBKEY", True)),
        )
        try:
            transport.start_server(server=server)
        except paramiko.SSHException as exc:
            logger.info("gateway handshake failed from %s: %s", client_ip, exc)
            return

        auth_timeout = int(app.config.get("GATEWAY_AUTH_TIMEOUT", 120) or 120)
        channel = transport.accept(timeout=auth_timeout)
        if channel is None:
            logger.info("gateway client %s did not open a channel in time", client_ip)
            return

        # 用长生命周期 app context 支撑整个交互过程（同一线程内反复查库）
        ctx = app.app_context()
        ctx.push()
        try:
            _serve_channel(app, transport, channel, server, client_ip, host_key)
        finally:
            try:
                ctx.pop()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - 单个连接失败不能拖垮网关
        logger.warning("gateway client %s error: %s", client_ip, exc)
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass


def _serve_channel(app, transport, channel, server, client_ip, host_key):
    writer = ChannelWriter(channel)
    user_id = server.user_id
    if user_id is None:
        writer.line("认证未完成，连接关闭。")
        return

    user = db.session.get(User, user_id)
    if user is None:
        writer.line("账号状态异常，连接关闭。")
        return

    # 进站先亮相：字符画 logo（盾牌 + AutoOps 字标）。终端太窄时 _render_banner 返回空串。
    cols, rows = server.window
    art = _render_banner(cols)
    if art:
        writer.write(art)

    banner = (get_setting("gateway_banner", "") or app.config.get("GATEWAY_BANNER", "") or "").strip()
    if banner:
        for line in banner.splitlines():
            writer.line(line)

    if user.must_change_password:
        if not _force_password_change(app, channel, writer, user, client_ip):
            return
        user = db.session.get(User, user_id)
        if user is None:
            return

    writer.line()
    writer.line(
        "  "
        + color("欢迎使用堡垒机审计网关，", CLR_WHITE)
        + color(user.display_name or user.username, CLR_BOLD, CLR_GREEN)
        + color("。", CLR_WHITE)
    )
    writer.line("  " + color("本网关只允许访问你被授权的主机，所有命令与输出都会被完整审计。", CLR_DIM))

    # accessible_targets 返回的是带 ORM 对象的内部条目（address 含端口、没有 port/osType），
    # 菜单渲染吃的是对外结构 —— 必须经过 serialize_target，否则会 KeyError: 'port'。
    from ..access import accessible_targets, serialize_target

    while True:
        if not _channel_alive(channel):
            return
        entries = [serialize_target(entry) for entry in accessible_targets(user)]
        live_count = _live_count(user.id)
        # 终端可能在菜单停留期间被拖动改变宽度，每轮都取最新的 PTY 列数
        menu_width = server.window[0] or cols
        writer.write(
            _render_menu(
                entries,
                user.username,
                user.display_name,
                user.role_name,
                client_ip,
                live_count,
                menu_width,
            )
        )
        if not entries:
            writer.line("  " + color("（没有可访问的主机，按回车重新检查，或输入 q 退出）", CLR_YELLOW))

        text, aborted = _read_line(channel, writer, color("选择主机 > ", CLR_BOLD, CLR_CYAN))
        if aborted:
            writer.line(color("连接已断开，再见。", CLR_DIM))
            return

        lowered = text.lower()
        if lowered in ("", "l", "list"):
            continue
        if lowered in ("q", "quit", "exit", "logout", "bye"):
            writer.line(color("已退出堡垒机网关，再见。", CLR_DIM))
            return
        if lowered in ("h", "help", "?"):
            writer.line(_help_text())
            _read_line(channel, writer, color("按回车继续 > ", CLR_DIM))
            continue
        if lowered in ("i", "info"):
            writer.line(
                "  "
                + color("账号：", CLR_DIM)
                + color(user.username, CLR_BOLD, CLR_GREEN)
                + "  "
                + color("角色：", CLR_DIM)
                + color(user.role_name or "-", CLR_MAGENTA)
                + "  "
                + color("来源：", CLR_DIM)
                + color(str(client_ip), CLR_CYAN)
            )
            writer.line(
                "  "
                + color("认证方式：", CLR_DIM)
                + color(str(server.auth_method), CLR_WHITE)
                + "  "
                + color("在线会话：", CLR_DIM)
                + color(str(_live_count(user.id)), CLR_BOLD, CLR_YELLOW)
                + color(" 条", CLR_DIM)
            )
            _read_line(channel, writer, color("按回车继续 > ", CLR_DIM))
            continue

        try:
            index = int(text)
        except ValueError:
            writer.line("  " + color("无效输入，请输入主机编号，或输入 h 查看帮助。", CLR_RED))
            continue
        if not (1 <= index <= len(entries)):
            writer.line("  " + color("编号超出范围，请输入列表中的主机编号。", CLR_RED))
            continue

        entry = entries[index - 1]
        if not entry["canWebterm"]:
            writer.line("  " + color("该主机未开放交互式终端，请联系管理员。", CLR_YELLOW))
            continue
        account = _select_account(channel, writer, entry)
        if account is None:
            continue
        if not _check_quota(app, user, entry, writer):
            continue

        _run_session(
            app,
            channel,
            writer,
            user.id,
            entry,
            account,
            client_ip,
            server,
        )


def _live_count(user_id: int) -> int:
    from ..models import SessionRecord

    try:
        return int(
            SessionRecord.query.filter_by(user_id=user_id, status="active").count() or 0
        )
    except Exception:  # noqa: BLE001
        return 0


def _check_quota(app, user, entry, writer: ChannelWriter) -> bool:
    from ..access import check_session_quota, find_access

    resolved = find_access(user, host_id=entry["hostId"], require_login=True)
    if resolved is None:
        writer.line("  " + color("你没有该主机的访问权限（可能刚被回收或超出时间窗）。", CLR_RED))
        return False
    allowed, reason = check_session_quota(user, resolved.grant)
    if not allowed:
        writer.line("  " + color(str(reason), CLR_RED))
        return False
    max_per_user = get_int("max_sessions_per_user", app.config.get("GATEWAY_MAX_SESSIONS_PER_USER", 5))
    if max_per_user and _live_count(user.id) >= max_per_user:
        writer.line("  " + color(f"你已达到并发会话上限（{max_per_user} 条），请先关闭其他会话。", CLR_RED))
        return False
    return True


def _force_password_change(app, channel, writer: ChannelWriter, user, client_ip: str) -> bool:
    from ..api.auth import validate_password_strength
    from ..audit import log_event
    from ..security import hash_password

    writer.line()
    writer.line(color("[堡垒机] 这是你的首次登录（或管理员已重置密码），必须先设置新密码。", CLR_YELLOW))
    for _ in range(3):
        new_password, aborted = _read_line(channel, writer, "新密码（至少 8 位）> ", mask=True)
        if aborted:
            return False
        error = validate_password_strength(new_password)
        if error:
            writer.line("  " + color(f"密码不符合要求：{error}", CLR_RED))
            continue
        confirm, aborted = _read_line(channel, writer, "再次输入新密码 > ", mask=True)
        if aborted:
            return False
        if confirm != new_password:
            writer.line("  " + color("两次输入不一致，请重新设置。", CLR_RED))
            continue
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        db.session.commit()
        log_event(
            "auth",
            "change_password",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
            actor_id=user.id,
            actor_username=user.username,
            ip=client_ip,
            message="SSH 网关首次登录强制修改密码",
        )
        writer.line(color("[堡垒机] 密码已更新。", CLR_GREEN))
        return True
    writer.line("  " + color("连续 3 次输入不合法，连接关闭。", CLR_RED))
    return False


def fingerprint_of(key) -> str:
    """返回 paramiko 密钥对象的 OpenSSH 风格 SHA256 指纹：`SHA256:<base64 无填充>`。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def host_key_fingerprint(path: str) -> str:
    """读取网关主机密钥文件并返回 SHA256 指纹；文件缺失/损坏返回空串。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        key = paramiko.RSAKey.from_private_key_file(path)
    except Exception:  # noqa: BLE001 - 指纹拿不到不该影响主流程
        return ""
    try:
        return fingerprint_of(key)
    except Exception:  # noqa: BLE001 - 同上
        return ""


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------
class GatewayServer:
    """一个线程内的 SSH 监听器。"""

    def __init__(self, app):
        self.app = app
        self.host = app.config.get("GATEWAY_HOST", "0.0.0.0")
        self.port = int(app.config.get("GATEWAY_PORT", 2222))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._host_key = None
        self._started_at: _dt.datetime | None = None
        self.last_error = ""
        self.accepted = 0

    # -- 主机密钥 -----------------------------------------------------------
    @property
    def fingerprint(self) -> str:
        """网关主机密钥的 SHA256 指纹（OpenSSH 风格），未加载密钥时返回空串。

        与 `ssh-keyscan -p <port> <host> | ssh-keygen -lf -` 的输出一致，便于用户核对。
        （旧实现用 paramiko 的 MD5 hex 且写在 status() 里，run.py 取 `gateway.fingerprint`
        永远拿到空串，启动横幅不显示指纹。）
        """
        if self._host_key is None:
            return ""
        return fingerprint_of(self._host_key)

    def load_host_key(self):
        path = self.app.config.get("GATEWAY_HOST_KEY") or os.path.join(
            self.app.instance_path, "gateway_host_rsa.key"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            try:
                return paramiko.RSAKey.from_private_key_file(path)
            except paramiko.SSHException:
                logger.warning("invalid gateway host key at %s, regenerating", path)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("generated new gateway host key at %s", path)
        return key

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._host_key = self.load_host_key()
        self._thread = threading.Thread(target=self._serve_forever, name="bastion-gateway", daemon=True)
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.last_error or (self._sock is not None):
                break
            time.sleep(0.05)
        return self._sock is not None

    def _serve_forever(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(64)
            sock.settimeout(0.5)
            self._sock = sock
            self._started_at = utcnow()
            logger.info("bastion gateway listening on %s:%s", self.host, self.port)
        except OSError as exc:
            self.last_error = f"网关端口 {self.host}:{self.port} 监听失败：{exc}"
            logger.error(self.last_error)
            self._sock = None
            return

        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            thread = threading.Thread(
                target=handle_client,
                args=(self.app, client, addr, self._host_key),
                name=f"bastion-gw-{addr[0]}",
                daemon=True,
            )
            thread.start()
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._sock = None
        logger.info("bastion gateway stopped")

    @property
    def running(self) -> bool:
        return bool(self._sock is not None and self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "accepted": self.accepted,
            "startedAt": _iso(self._started_at),
            "lastError": self.last_error,
            "fingerprint": self.fingerprint,
        }


_server: GatewayServer | None = None
_server_lock = threading.Lock()


def get_server(app=None) -> GatewayServer | None:
    global _server
    with _server_lock:
        if _server is None and app is not None:
            _server = GatewayServer(app)
        return _server


def start_gateway(app) -> GatewayServer | None:
    if not app.config.get("GATEWAY_ENABLED", True):
        logger.info("gateway disabled by config, skip start")
        return None
    server = get_server(app)
    assert server is not None
    server.host = app.config.get("GATEWAY_HOST", "0.0.0.0")
    server.port = int(app.config.get("GATEWAY_PORT", 2222))
    server.start()
    return server


def stop_gateway():
    global _server
    with _server_lock:
        if _server is not None:
            _server.stop()
        _server = None


def is_running() -> bool:
    with _server_lock:
        return bool(_server is not None and _server.running)
