"""WinRM 桥接：把网页终端的字节流翻译成一条条 WinRM 短命令。

公开面与 `app/terminal/bridge.py:ShellBridge`（SSH）完全一致 —— ``start/stop/
feed_input/resize/stats`` 加上 ``seq/bytes_in/bytes_out/segmented/closed/at_prompt`` ——
所以 `session_service.open_session()` 只按 ``host.protocol`` 选一个桥，网页终端前端、
命令策略、CommandLog、转录、AI 助手与会话登记全部零改动复用。

与 SSH 桥的关键差别
------------------
* **进程内状态不保留**：每条用户输入都是一个新 PowerShell 进程（原因见
  `app/winrm/client.py` 的模块注释）。``cd`` 由本桥跟踪：命令脚本尾部回显
  ``(Get-Location).Path``，桥记下来并在下一条命令开头 ``Set-Location`` 回去。
* **输出由桥打印**：命令的 stdout/stderr 整体取回后按 CRLF 写回浏览器；输入回显、
  行编辑（backspace / 左右 / Home / End / Delete / 历史）与提示符都是本地实现 ——
  WinRM 没有 PTY，目标机不会替我们回显。
* **策略判定点与 SSH 完全一致**：按 Enter 时先 `is_session_exit()`，再
  `evaluate_policy()`；被拒绝的命令不发给目标机，但仍然落一条 ``deny`` 审计。
"""

from __future__ import annotations

import html
import logging
import queue
import re
import threading
import time
import uuid

from ..policy import evaluate_policy, is_session_exit
from ..session_registry import touch as registry_touch
from ..terminal.bridge import ANSI_RED, ANSI_RESET, LineEndingNormalizer
from .client import WinrmConnection, WinrmError, close as close_connection, run_script

log = logging.getLogger(__name__)

ANSI_GREEN = "\x1b[32m"
ANSI_YELLOW = "\x1b[33m"
ANSI_CYAN = "\x1b[36m"
ANSI_DIM = "\x1b[90m"

CR = 0x0D
LF = 0x0A
ESC = 0x1B
BS = 0x08
DEL = 0x7F
CTRL_A = 0x01
CTRL_C = 0x03
CTRL_D = 0x04
CTRL_E = 0x05
CTRL_L = 0x0C
CTRL_U = 0x15
CTRL_W = 0x17
BELL = 0x07

MAX_LINE = 4096
MAX_HISTORY = 200
MAX_CAPTURE = 200_000

#: 清屏控制序列（清整个屏幕并把光标移到左上角），与终端的 `clear` 等价。
CLEAR_SCREEN = "\x1b[2J\x1b[H"

#: 就地处理的清屏命令。
#:
#: WinRM 的 WSMan 会话没有真实控制台：PowerShell 的 `cls` / `Clear-Host` 依赖
#: `$Host.UI.RawUI`，在 `-NonInteractive` + 重定向输出的会话里会直接报错
#: （用户在终端里看到的就是一句 PowerShell 错误，屏幕根本没清）。这几个等价写法
#: 因此在堡垒机这一侧就地处理 —— 只往用户终端写一条清屏序列，不发给目标机。
LOCAL_CLEAR_COMMANDS = frozenset({"cls", "clear", "clear-host", "clearhost"})

ESCAPES = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1bOH": "home",
    b"\x1bOF": "end",
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
    b"\x1b[7~": "home",
    b"\x1b[8~": "end",
    b"\x1b[3~": "delete",
}


# --------------------------------------------------------------------------- 脚本
def ps_literal(text: str) -> str:
    """PowerShell 单引号字符串字面量（内部的 ' 双写转义）。"""
    return "'" + text.replace("'", "''") + "'"


def build_script(line: str, cwd: str, marker: str) -> str:
    """把一行用户输入包成「设目录 → 执行 → 回显退出码与当前目录」的脚本。

    尾部回显是「每条命令一个新进程」的补偿：``Set-Location`` 的效果（cd）靠它带回
    桥里，下一条命令再设回去；退出码优先取 ``$LASTEXITCODE``（原生程序），拿不到就
    用 ``$?``（cmdlet 成功与否）。
    """
    parts = [
        "$ErrorActionPreference = 'Continue'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    ]
    if cwd:
        parts.append(f"Set-Location -LiteralPath {ps_literal(cwd)} -ErrorAction SilentlyContinue")
    if line.strip():
        parts.append(line)
    parts.append("$__bastion_ok = $?")
    parts.append(
        "$__bastion_code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } "
        "else { if ($__bastion_ok) { 0 } else { 1 } }"
    )
    parts.append(f'Write-Output ("{marker}{{0}}|{{1}}{marker}" -f $__bastion_code, (Get-Location).Path)')
    return "\n".join(parts)


def split_trailer(text: str, marker: str) -> tuple[str, int, str]:
    """从输出里剥掉尾部回显，返回 (正文, 退出码, 当前目录)；没有回显时退出码为 -1。"""
    # 回显形如 `…<marker>0|C:\Windows<marker>`：**必须从前面的那个标记开始找**，
    # 用 rfind 会先命中结尾那个标记，结果把整段回显当成正文留在输出里。
    index = text.find(marker)
    if index < 0:
        return text, -1, ""
    body = text[:index]
    end = text.find(marker, index + len(marker))
    if end < 0:
        return body, -1, ""
    payload = text[index + len(marker) : end].strip()
    code_text, _, cwd = payload.partition("|")
    try:
        code = int(code_text.strip())
    except ValueError:
        code = -1
    return body, code, cwd.strip()


_CLIXML_ERROR = re.compile(r'<S S="Error">(.*?)</S>', re.S)


def decode_clixml(text: str) -> str:
    """把 WinRS 下 PowerShell 错误流的 CLIXML 还原成人能读的文本。

    用 ``-EncodedCommand`` 跑脚本时，目标机把 ps1 的错误流序列化成 CLIXML
    （形如 ``#< CLIXML\\r\\n<Objs …><S S="Error">出错信息_x000D__x000A_</S>…``）。
    直接把这段 XML 打到终端上是没法看的，这里抽出所有错误条目并对齐换行。
    """
    if not text or "#< CLIXML" not in text:
        return text
    body = text.split("#< CLIXML", 1)[1]
    parts = [match.group(1) for match in _CLIXML_ERROR.finditer(body)]
    if not parts:
        return ""
    decoded = "".join(parts)
    for escaped, real in (("_x000D_", "\r"), ("_x000A_", "\n"), ("_x0009_", "\t")):
        decoded = decoded.replace(escaped, real)
    return html.unescape(decoded).strip("\r\n")


# --------------------------------------------------------------------------- 桥
class WinrmBridge:
    """一条 WinRM 会话：持久 WSMan shell + 每条命令一个短命 PowerShell 进程。"""

    def __init__(self, connection: WinrmConnection, config, session=None) -> None:
        self.conn = connection
        self.cfg = config
        self.session = session

        self.seq = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.closed = False
        # WinRM 天然「逐条命令」：命令边界清晰、可以逐条过策略，与 SSH 的分段模式等价。
        self.segmented = True
        self.at_prompt = True
        self.cwd = ""

        self.history: list[str] = []
        self._hist_index: int | None = None
        self._draft = b""
        self._buf = bytearray()
        self._cursor = 0
        self._esc = b""

        self.out_normalizer = LineEndingNormalizer()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._worker: threading.Thread | None = None
        self._started_at = time.time()
        self._cols = 120
        self._rows = 32
        self._pending: dict | None = None
        self._pending_notice = ""
        self._capture: list[str] = []
        self._capture_truncated = False

        token = uuid.uuid4().hex[:8]
        self._marker = f"@@BASTION-{token}@@"

    # ------------------------------------------------------------ 生命周期
    def start(self, arm_timeout: float = 15.0) -> bool:
        self._worker = threading.Thread(target=self._worker_loop, name=f"ww-{self.cfg.sid}", daemon=True)
        self._worker.start()
        self._banner()
        try:
            self.cwd = self._sync_cwd(timeout=max(float(arm_timeout), 10.0)) or self.cwd
        except WinrmError as exc:
            self._emit_output(f"{ANSI_RED}[堡垒机] 读取目标机当前目录失败：{exc}{ANSI_RESET}\r\n".encode())
        self._prompt()
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.record(
                    "session_start",
                    {
                        "sid": self.cfg.sid,
                        "host": self.cfg.host_label,
                        "channel": self.cfg.channel_kind,
                        "segmented": True,
                        "shell": "winrm",
                        "endpoint": self.conn.target.address,
                    },
                    flush=True,
                )
            except Exception:  # noqa: BLE001
                log.exception("写转录失败（忽略）")
        return True

    def stop(self, reason: str = "用户断开") -> None:
        """外部收尾（session_service.teardown_session 调用）：不再回调 on_close。"""
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        self._interrupt.set()
        self._queue.put(None)
        try:
            close_connection(self.conn)
        except Exception:  # noqa: BLE001
            log.debug("关闭 WinRM 连接失败（忽略）", exc_info=True)
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.close(reason, bytes_in=self.bytes_in, bytes_out=self.bytes_out)
            except Exception:  # noqa: BLE001
                log.exception("关闭转录失败（忽略）")

    def _finish(self, reason: str) -> None:
        """会话自己结束（用户输入 exit / 目标机不可用）：回调 on_close 让上层收口。"""
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        self._interrupt.set()
        self._queue.put(None)
        try:
            close_connection(self.conn)
        except Exception:  # noqa: BLE001
            log.debug("关闭 WinRM 连接失败（忽略）", exc_info=True)
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.close(reason, bytes_in=self.bytes_in, bytes_out=self.bytes_out)
            except Exception:  # noqa: BLE001
                log.exception("关闭转录失败（忽略）")
        cb = self.cfg.on_close
        if cb is not None:
            try:
                cb(reason)
            except Exception:  # noqa: BLE001
                log.exception("WinRM on_close 回调异常")

    # ------------------------------------------------------------ 输出（桥 -> 用户）
    def _emit_output(self, data: bytes) -> None:
        if not data or self.closed:
            return
        data = self.out_normalizer.feed(data)
        if not data:
            return
        self.bytes_out += len(data)
        # 有输出 = 会话活着：喂给空闲清理器，否则「空闲超时」会把正在干活的会话误杀。
        try:
            registry_touch(self.cfg.sid)
        except Exception:  # noqa: BLE001
            log.debug("touch 会话失败（忽略）", exc_info=True)
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.record_io("out", data)
            except Exception:  # noqa: BLE001
                log.exception("记录输出流失败（忽略）")
        cb = self.cfg.on_output
        if cb is not None:
            try:
                cb(data)
            except Exception:  # noqa: BLE001
                log.exception("WinRM on_output 回调异常")

    def _publish(self, text: str, *, capture: bool = True) -> None:
        if not text:
            return
        if capture:
            room = MAX_CAPTURE - sum(len(chunk) for chunk in self._capture)
            if room > 0:
                self._capture.append(text[:room])
            if len(text) > max(room, 0):
                self._capture_truncated = True
        self._emit_output(text.encode("utf-8", "replace"))

    # ------------------------------------------------------------ 提示符与首屏
    def _banner(self) -> None:
        target = self.conn.target
        transport = f"WinRM {target.transport.upper()}"
        if target.use_ssl:
            transport += "/HTTPS"
        lines = [
            f"{ANSI_GREEN}已连接到 Windows 目标机{ANSI_RESET} {self.cfg.host_label} ({target.address})",
            f"账号：{target.username} ｜ 通道：{transport}",
            (
                f"{ANSI_DIM}每条命令在目标机上单独执行（PowerShell）；cd 会保留，环境变量/自定义"
                f"变量等进程内状态不保留；more / pause / Read-Host 这类交互式程序不可用；"
                f"输入 exit 结束会话。{ANSI_RESET}"
            ),
            "",
        ]
        self._publish("\n".join(lines) + "\n", capture=False)

    def _prompt_text(self) -> str:
        return f"{ANSI_CYAN}PS {self.cwd or 'C:'}>{ANSI_RESET} "

    def _clear_screen(self) -> None:
        """就地清屏（`cls` / `clear`）：只写清屏序列并重画提示符，不碰目标机。"""
        self._emit_output(CLEAR_SCREEN.encode())
        self._prompt()

    def _prompt(self) -> None:
        self.at_prompt = True
        notice = self._pending_notice
        self._pending_notice = ""
        if notice:
            self._emit_output(f"{ANSI_RED}[堡垒机] {notice}{ANSI_RESET}\r\n".encode())
        self._emit_output(self._prompt_text().encode())
        cb = self.cfg.on_prompt
        if cb is not None:
            try:
                cb("")
            except Exception:  # noqa: BLE001
                log.exception("WinRM on_prompt 回调异常")

    # ------------------------------------------------------------ 用户 -> 目标机
    def feed_input(self, data: bytes) -> None:
        if self.closed or not data:
            return
        self.bytes_in += len(data)
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.record_io("in", data)
            except Exception:  # noqa: BLE001
                log.exception("记录输入流失败（忽略）")
        for byte in data:
            self._feed_byte(byte)

    def _feed_byte(self, byte: int) -> None:
        if self._esc:
            self._esc += bytes([byte])
            if self._handle_escape():
                self._esc = b""
            elif not any(key.startswith(self._esc) for key in ESCAPES):
                self._esc = b""
            return
        if byte == ESC:
            self._esc = bytes([byte])
            return
        if byte in (CR, LF):
            self._submit()
            return
        if byte in (DEL, BS):
            self._backspace()
            return
        if byte == CTRL_C:
            self._cancel_or_interrupt()
            return
        if byte == CTRL_D:
            if not self._buf:
                self._emit_output(b"exit\r\n")
                self._finish("用户在终端里退出了 Windows 会话")
            return
        if byte == CTRL_L:
            self._emit_output(b"\x1b[2J\x1b[H")
            self._repaint()
            return
        if byte == CTRL_U:
            self._buf.clear()
            self._cursor = 0
            self._repaint()
            return
        if byte == CTRL_W:
            self._delete_word()
            return
        if byte == CTRL_A:
            self._cursor = 0
            self._repaint()
            return
        if byte == CTRL_E:
            self._cursor = len(self._buf)
            self._repaint()
            return
        if byte < 0x20 or byte == BELL:
            return
        if len(self._buf) >= MAX_LINE:
            return
        if self._cursor == len(self._buf):
            # 快路径：在行尾直接回显这一个字节，不整行重画。
            self._buf.append(byte)
            self._cursor += 1
            self._emit_output(bytes([byte]))
            return
        self._buf[self._cursor : self._cursor] = bytes([byte])
        self._cursor += 1
        self._repaint()

    def _handle_escape(self) -> bool:
        action = ESCAPES.get(self._esc)
        if action is None:
            return False
        if action == "left":
            if self._cursor > 0:
                self._cursor -= 1
                self._repaint()
        elif action == "right":
            if self._cursor < len(self._buf):
                self._cursor += 1
                self._repaint()
        elif action == "home":
            self._cursor = 0
            self._repaint()
        elif action == "end":
            self._cursor = len(self._buf)
            self._repaint()
        elif action == "delete":
            if self._cursor < len(self._buf):
                del self._buf[self._cursor]
                self._repaint()
        elif action == "up":
            self._history_move(-1)
        elif action == "down":
            self._history_move(1)
        return True

    def _repaint(self) -> None:
        tail = max(len(self._buf) - self._cursor, 0)
        data = b"\r" + self._prompt_text().encode() + bytes(self._buf) + b"\x1b[K"
        if tail:
            data += f"\x1b[{tail}D".encode()
        self._emit_output(data)

    def _backspace(self) -> None:
        if not self._buf:
            return
        if self._cursor == len(self._buf):
            self._buf.pop()
            self._cursor -= 1
            self._emit_output(b"\b \b")
            return
        if self._cursor > 0:
            del self._buf[self._cursor - 1]
            self._cursor -= 1
            self._repaint()

    def _delete_word(self) -> None:
        if not self._buf:
            return
        index = self._cursor
        while index > 0 and self._buf[index - 1 : index] == b" ":
            index -= 1
        while index > 0 and self._buf[index - 1 : index] != b" ":
            index -= 1
        del self._buf[index : self._cursor]
        self._cursor = index
        self._repaint()

    def _history_move(self, step: int) -> None:
        if not self.history:
            return
        if self._hist_index is None:
            if step > 0:
                return
            self._draft = bytes(self._buf)
            index = len(self.history) - 1
        else:
            index = self._hist_index + step
        if index < 0:
            index = 0
        if index >= len(self.history):
            self._hist_index = None
            self._buf = bytearray(self._draft)
        else:
            self._hist_index = index
            self._buf = bytearray(self.history[index].encode("utf-8", "replace"))
        self._cursor = len(self._buf)
        self._repaint()

    def _submit(self) -> None:
        line = self._buf.decode("utf-8", "replace")
        self._buf.clear()
        self._cursor = 0
        self._hist_index = None
        self._draft = b""
        self._emit_output(b"\r\n")
        self._handle_enter(line)

    def _cancel_or_interrupt(self) -> None:
        if self._pending is not None:
            self._interrupt.set()
            self._pending_notice = "已按 Ctrl-C 请求中断当前命令（等目标机结束）"
            self._emit_output(b"^C")
            return
        self._buf.clear()
        self._cursor = 0
        self._emit_output(b"^C\r\n")
        self._prompt()

    def _handle_enter(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            self._prompt()
            return
        if not self.at_prompt:
            self._pending_notice = "命令正在执行，暂不支持交互输入（Ctrl-C 可请求中断）"
            self._prompt()
            return
        if is_session_exit(stripped):
            self._record_control(line)
            self._publish(f"{ANSI_DIM}会话结束。{ANSI_RESET}\n", capture=False)
            self._finish("用户在终端里退出了 Windows 会话")
            return
        decision = evaluate_policy(self.cfg.policy, stripped)
        if not decision.allowed:
            self._record_denied(line, decision)
            self._pending_notice = f"命令被拒绝 —— {decision.reason}"
            self._prompt()
            return
        if stripped.lower() in LOCAL_CLEAR_COMMANDS:
            self.history.append(line)
            if len(self.history) > MAX_HISTORY:
                del self.history[0]
            self._record_control(
                line, reason="本地清屏指令（WinRM 会话没有真实控制台，不发给目标机）"
            )
            self._clear_screen()
            return
        self.history.append(line)
        if len(self.history) > MAX_HISTORY:
            del self.history[0]
        self._start_command(
            line,
            risk_level=decision.risk_level,
            reason=decision.reason,
            matched_rule_id=decision.rule_id,
            matched_rule_pattern=decision.rule_pattern,
        )

    def _start_command(
        self,
        line: str,
        *,
        risk_level: str,
        reason: str,
        matched_rule_id: int | None = None,
        matched_rule_pattern: str = "",
        uncertain: bool = False,
    ) -> None:
        self.seq += 1
        self.at_prompt = False
        self._pending = {
            "seq": self.seq,
            "command": line,
            "started_at": time.monotonic(),
            "action": "allow",
            "risk_level": risk_level,
            "reason": reason,
            "matched_rule_id": matched_rule_id,
            "matched_rule_pattern": matched_rule_pattern,
            "uncertain": uncertain,
            "timed_out": False,
        }
        self._interrupt.clear()
        self._queue.put(("run", line))

    # ------------------------------------------------------------ 执行线程
    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            kind, line = item
            if kind != "run":
                continue
            try:
                self._run_line(line)
            except Exception:  # noqa: BLE001
                log.exception("WinRM 命令执行异常")
                self._emit_output(f"{ANSI_RED}[堡垒机] 执行命令时出错，会话仍可继续使用{ANSI_RESET}\r\n".encode())
                self._finalize_pending(force_action="error", reason="堡垒机执行命令时出错")
                self._prompt()

    def _run_line(self, line: str) -> None:
        self._capture = []
        self._capture_truncated = False
        timeout = float(self.cfg.command_timeout or 0)
        try:
            result = run_script(
                self.conn,
                build_script(line, self.cwd, self._marker),
                timeout=timeout if timeout > 0 else None,
                cancel=self._interrupt,
            )
        except WinrmError as exc:
            self._publish(f"{ANSI_RED}[堡垒机] {exc}{ANSI_RESET}\n", capture=False)
            self._finalize_pending(force_action="error", reason=str(exc))
            self._prompt()
            return

        body, code, cwd = split_trailer(decode_clixml(result.stdout or ""), self._marker)
        if cwd:
            self.cwd = cwd
        self._publish(body)
        stderr = decode_clixml(result.stderr or "")
        if stderr.strip():
            if body and not body.endswith(("\n", "\r")):
                self._publish("\n")
            self._publish(stderr)

        if result.timed_out:
            self._finalize_pending(
                force_action="timeout",
                reason=f"命令执行超过 {int(timeout)} 秒，已请求中断",
            )
            self._pending_notice = f"命令执行超时（>{int(timeout)}s），已请求中断"
        elif result.cancelled:
            self._finalize_pending(force_action="interrupt", reason="用户按 Ctrl-C 中断了命令")
            self._pending_notice = "已按 Ctrl-C 请求中断当前命令"
        else:
            self._finalize_pending()
            exit_code = code if code >= 0 else result.exit_code
            if exit_code != 0:
                self._publish(f"{ANSI_DIM}[退出码 {exit_code}]{ANSI_RESET}\n", capture=False)
        self._prompt()

    def _sync_cwd(self, timeout: float) -> str:
        result = run_script(
            self.conn,
            build_script("", self.cwd, self._marker),
            timeout=timeout,
            cancel=self._interrupt,
        )
        if result.timed_out:
            raise WinrmError("目标机没有在超时前响应 WinRM 命令")
        _, _, cwd = split_trailer(decode_clixml(result.stdout or ""), self._marker)
        return cwd

    # ------------------------------------------------------------ 审计事件
    def _finalize_pending(self, force_action: str | None = None, reason: str | None = None) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        output = "".join(self._capture).lstrip("\r\n")
        event = {
            "seq": pending["seq"],
            "command": pending["command"],
            "output": output,
            "action": force_action or pending.get("action") or "allow",
            "risk_level": pending.get("risk_level") or "low",
            "reason": reason if reason is not None else (pending.get("reason") or ""),
            "matched_rule_id": pending.get("matched_rule_id"),
            "matched_rule_pattern": pending.get("matched_rule_pattern") or "",
            "duration_ms": int((time.monotonic() - pending["started_at"]) * 1000),
            "truncated": bool(self._capture_truncated),
            "uncertain": bool(pending.get("uncertain")),
            "channel": self.cfg.channel_kind,
            "ts": time.time(),
        }
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.record(
                    "command",
                    {
                        "seq": event["seq"],
                        "command": event["command"],
                        "output": event["output"][:20000],
                        "output_bytes": len(event["output"].encode("utf-8", "replace")),
                        "action": event["action"],
                        "risk_level": event["risk_level"],
                        "duration_ms": event["duration_ms"],
                        "truncated": event["truncated"],
                        "uncertain": event["uncertain"],
                    },
                    flush=False,
                )
            except Exception:  # noqa: BLE001
                log.exception("写转录失败（忽略）")
        cb = self.cfg.on_command
        if cb is not None:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("WinRM on_command 回调异常")

    def _record_control(self, line: str, *, reason: str | None = None) -> None:
        """会话控制指令（exit 等）：不受命令策略限制，但也留一条审计。"""
        self.seq += 1
        event = {
            "seq": self.seq,
            "command": line,
            "output": "",
            "action": "allow",
            "risk_level": "low",
            "reason": reason or "会话控制指令（退出/登出，不受命令策略限制）",
            "matched_rule_id": None,
            "matched_rule_pattern": "",
            "duration_ms": 0,
            "truncated": False,
            "uncertain": False,
            "channel": self.cfg.channel_kind,
            "ts": time.time(),
        }
        cb = self.cfg.on_command
        if cb is not None:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("WinRM on_command 回调异常")

    def _record_denied(self, line: str, decision) -> None:
        event = {
            "seq": self.seq + 1,
            "command": line,
            "output": "",
            "action": "deny",
            "risk_level": decision.risk_level,
            "reason": decision.reason,
            "matched_rule_id": decision.rule_id,
            "matched_rule_pattern": decision.rule_pattern,
            "duration_ms": 0,
            "truncated": False,
            "uncertain": False,
            "channel": self.cfg.channel_kind,
            "ts": time.time(),
        }
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.record(
                    "deny",
                    {
                        "command": line,
                        "reason": decision.reason,
                        "risk_level": decision.risk_level,
                        "rule_id": decision.rule_id,
                    },
                    flush=True,
                )
            except Exception:  # noqa: BLE001
                log.exception("写转录失败（忽略）")
        cb = self.cfg.on_command
        if cb is not None:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("WinRM on_command 回调异常")

    # ------------------------------------------------------------ 其它
    def resize(self, cols: int, rows: int) -> None:
        """WinRM 没有 PTY 可调；记一笔尺寸用于审计还原。"""
        try:
            self._cols = max(int(cols or 120), 20)
            self._rows = max(int(rows or 32), 5)
        except (TypeError, ValueError):
            return
        if self.cfg.recorder is not None:
            try:
                self.cfg.recorder.record("resize", {"cols": self._cols, "rows": self._rows}, flush=False)
            except Exception:  # noqa: BLE001
                log.exception("记录 resize 失败（忽略）")

    def stats(self) -> dict:
        return {
            "sid": self.cfg.sid,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "commands": self.seq,
            "segmented": self.segmented,
            "duration": int(time.time() - self._started_at),
        }
