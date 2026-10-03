"""终端桥接层：用户按键 <-> 远端 shell，并在 Enter 处完成命令级审计。

设计要点
--------
* 用户按键**原样转发**给远端 shell（保留远端回显、Tab 补全、历史），因此在
  按下回车之前远端只是把字符放在行缓冲区里，并不会执行——这就给堡垒机留出了
  在 ``Enter`` 时刻做策略判定的窗口。
* 远端 ``PS1`` 被临时改写为 ``<唯一标记>\\u@\\h:\\w\\$ ``，读数线程把标记从流中
  剥掉（用户看不到），每次标记出现即一次「提示符边界」：用于结束上一条命令的
  输出捕获、恢复「可输入」状态、并注入堡垒机的本地提示。
* 命令被拒绝时向远端发送 ``\\x03``（Ctrl-C）丢弃已输入内容，提示信息会在下一个
  提示符边界之前插入，保证终端光标不错位。
"""

from __future__ import annotations

import logging
import queue
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..policy import evaluate_policy, is_session_exit
from ..session_registry import touch as registry_touch

log = logging.getLogger(__name__)

CR = 0x0D
LF = 0x0A
ESC = 0x1B
BS = 0x7F
BACKSPACE = 0x08
CTRL_A = 0x01
CTRL_C = 0x03
CTRL_D = 0x04
CTRL_E = 0x05
CTRL_K = 0x0B
CTRL_L = 0x0C
CTRL_R = 0x12
CTRL_U = 0x15
CTRL_W = 0x17

ANSI_RESET = "\x1b[0m"
ANSI_RED = "\x1b[31m"


def make_marker() -> str:
    return f"__BASTION_{secrets.token_hex(8)}__"


class LineEndingNormalizer:
    """把远端输出里的裸 LF 归一化成 CRLF（等价于真实 PTY 的 ``stty onlcr``）。

    为什么必须有：终端里 **LF 只把光标下移一行、不回列**。真实 Linux 的 PTY
    默认开着 ``ONLCR``，shell 打印的 ``\\n`` 会被终端驱动转成 ``\\r\\n``，所以直连
    时看不到问题；但堡垒机是通过 ``invoke_shell`` 拿到的字节流，一旦目标端不做
    这次转换（示例目标机 ``tools/demo_ssh_target.py``、网络设备、部分容器/嵌入式
    shell 都是这样），输出就会一行比一行右移 —— 表现为「提示符没有从头开始」、
    回显错位。实测缺陷：演示目标机连接后 ``root@demo-linux-01:~#`` 缩进到了行中。

    实现要点：
    * 跨 chunk 记住上一个字节是不是 CR，``"\\r"`` + ``"\\n"`` 被拆到两个包时不会
      重复插入 CR（否则会多出空行）。
    * 只转换裸 LF；已经是 ``\\r\\n`` 的原样保留，单独的 ``\\r``（进度条/覆盖重绘）
      不动。
    * 没有 LF 的包走快速路径直接返回，不逐字节拷贝。
    """

    __slots__ = ("_pending_cr",)

    def __init__(self) -> None:
        self._pending_cr = False

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        pending = self._pending_cr
        self._pending_cr = data.endswith(b"\r")
        if b"\n" not in data:
            return data
        out = bytearray()
        for byte in data:
            if byte == LF:
                if not pending:
                    out.append(CR)
                out.append(LF)
                pending = False
            else:
                out.append(byte)
                pending = byte == CR
        return bytes(out)


# --------------------------------------------------------------------------- 过滤
class PromptFilter:
    """从远端字节流中剥离提示符标记，按顺序抛出 data / prompt 事件。"""

    def __init__(self, marker: str):
        self.marker = marker.encode()
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        events: list[tuple[str, bytes]] = []
        self._buf.extend(data)
        while True:
            idx = self._buf.find(self.marker)
            if idx == -1:
                held = self._partial_tail()
                cut = len(self._buf) - held
                if cut > 0:
                    events.append(("data", bytes(self._buf[:cut])))
                    del self._buf[:cut]
                break
            if idx:
                events.append(("data", bytes(self._buf[:idx])))
            del self._buf[: idx + len(self.marker)]
            events.append(("prompt", b""))
        return events

    def _partial_tail(self) -> int:
        """缓冲区尾部可能是标记的前缀，先留一手避免把标记切碎发给用户。"""
        limit = min(len(self._buf), len(self.marker) - 1)
        for size in range(limit, 0, -1):
            if self._buf[-size:] == self.marker[:size]:
                return size
        return 0

    def flush(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


# --------------------------------------------------------------------------- 行追踪
class LineTracker:
    """尽最大努力还原用户输入的命令行（含行内编辑）。"""

    def __init__(self):
        self.buffer = bytearray()
        self.cursor = 0
        self.uncertain = False
        self._esc = bytearray()
        self._in_escape = False

    def reset(self) -> None:
        self.buffer.clear()
        self.cursor = 0
        self.uncertain = False
        self._esc.clear()
        self._in_escape = False

    def text(self) -> str:
        return self.buffer.decode("utf-8", "replace")

    def feed(self, data: bytes) -> None:
        for byte in data:
            self.feed_byte(byte)

    def feed_byte(self, byte: int) -> None:  # noqa: C901 - 终端状态机
        if self._in_escape:
            self._esc.append(byte)
            if self._escape_complete():
                self._apply_escape(bytes(self._esc))
                self._esc = bytearray()
                self._in_escape = False
            elif len(self._esc) > 32:
                self.uncertain = True
                self._esc = bytearray()
                self._in_escape = False
            return

        if byte == ESC:
            self._in_escape = True
            self._esc = bytearray([ESC])
            return
        if byte in (CR, LF):
            return
        if byte in (BS, BACKSPACE):
            if self.cursor > 0:
                del self.buffer[self.cursor - 1]
                self.cursor -= 1
            return
        if byte == CTRL_U:
            del self.buffer[: self.cursor]
            self.cursor = 0
            return
        if byte == CTRL_K:
            del self.buffer[self.cursor:]
            return
        if byte == CTRL_W:
            cut = self.cursor
            while cut > 0 and self.buffer[cut - 1:cut] == b" ":
                cut -= 1
            while cut > 0 and self.buffer[cut - 1:cut] != b" ":
                cut -= 1
            del self.buffer[cut : self.cursor]
            self.cursor = cut
            return
        if byte == CTRL_A:
            self.cursor = 0
            return
        if byte == CTRL_E:
            self.cursor = len(self.buffer)
            return
        if byte == CTRL_C:
            self.reset()
            return
        if byte == CTRL_D:
            if self.buffer:
                if self.cursor < len(self.buffer):
                    del self.buffer[self.cursor]
            return
        if byte == CTRL_L or byte == CTRL_R:
            if byte == CTRL_R:
                self.uncertain = True
            return
        if byte < 0x20:
            return
        # 按 UTF-8 字节流追加，光标按字节推进
        self.buffer.insert(self.cursor, byte)
        self.cursor += 1

    def _escape_complete(self) -> bool:
        seq = self._esc
        if len(seq) < 2:
            return False
        second = seq[1]
        if second == ord("["):
            if len(seq) < 3:
                return False
            return 0x40 <= seq[-1] <= 0x7E
        if second == ord("O"):
            return len(seq) >= 3
        return True

    def _apply_escape(self, seq: bytes) -> None:
        body = seq[1:]
        if body.startswith(b"["):
            body = body[1:]
        if body.endswith(b"~") or (body and 0x40 <= body[-1] <= 0x7E and body[-1] != ord("~")):
            pass
        key = body.rstrip(b"~").decode("latin-1", "replace")
        final = chr(seq[-1])
        mapping = {
            "D": "left",
            "C": "right",
            "H": "home",
            "F": "end",
            "A": "up",
            "B": "down",
        }
        tilde = {"1": "home", "2": "insert", "3": "delete", "4": "end", "7": "home", "8": "end"}
        if final in ("D", "C") and key and key[:-1] and key[:-1].isdigit():
            # 形如 ESC[3D / ESC[2C 的批量移动
            try:
                steps = int(key[:-1])
            except ValueError:
                steps = 1
            if final == "D":
                self.cursor = max(0, self.cursor - steps)
            else:
                self.cursor = min(len(self.buffer), self.cursor + steps)
            return
        if final == "~":
            action = tilde.get(key)
            if action == "home":
                self.cursor = 0
            elif action == "end":
                self.cursor = len(self.buffer)
            elif action == "delete":
                if self.cursor < len(self.buffer):
                    del self.buffer[self.cursor]
            else:
                self.uncertain = True
            return
        if final in ("H", "F") or key in ("1", "4", "7", "8"):
            self.cursor = 0 if final in ("H", "1", "7") else len(self.buffer)
            return
        if final in ("D", "C"):
            self.cursor = max(0, self.cursor - 1) if final == "D" else min(len(self.buffer), self.cursor + 1)
            return
        if final in ("3",) or key == "3":
            if self.cursor < len(self.buffer):
                del self.buffer[self.cursor]
            return
        if final in ("A", "B"):
            # 历史回溯：无法还原被召回的内容
            self.uncertain = True
            return
        if seq.startswith(b"\x1b[200~") or seq.startswith(b"\x1b[201~"):
            return
        if mapping.get(final):
            return
        if tilde.get(key):
            return
        # Alt+键、F1-F12、未知序列
        self.uncertain = True


# --------------------------------------------------------------------------- 配置
@dataclass
class BridgeConfig:
    sid: str
    marker: str = ""
    policy: object | None = None  # FrozenPolicy 或任何具备 rules/default_action 的对象
    recorder: object | None = None
    host_label: str = ""
    channel_kind: str = "ssh"
    command_timeout: int = 60
    max_output_bytes: int = 256 * 1024
    interactive: bool = False  # 交互式命令（如 top）是否需要放行（一般 False）
    on_output: Callable[[bytes], None] | None = None
    on_command: Callable[[dict], None] | None = None
    on_notice: Callable[[str], None] | None = None
    on_prompt: Callable[[str], None] | None = None
    on_close: Callable[[str], None] | None = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- 桥
class ShellBridge:
    """一条远端 shell 通道 <-> 一个用户终端的双向桥。"""

    def __init__(self, channel, config: BridgeConfig, session=None):
        self.channel = channel
        self.cfg = config
        self.session = session
        self.marker = config.marker or make_marker()
        self.filter = PromptFilter(self.marker)
        self.tracker = LineTracker()
        self.out_normalizer = LineEndingNormalizer()

        self.seq = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.segmented = False
        self.at_prompt = False
        self.closed = False

        self._stop = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._pending: dict | None = None
        self._capture = bytearray()
        self._capture_truncated = False
        self._pending_notice = ""
        self._lock = threading.Lock()
        self._started_at = time.time()

    # ------------------------------------------------------------ 生命周期
    def start(self, arm_timeout: float = 15.0) -> bool:
        try:
            self.channel.settimeout(0.1)
        except Exception:  # noqa: BLE001
            pass
        self._writer = threading.Thread(target=self._writer_loop, name=f"bw-{self.cfg.sid}", daemon=True)
        self._writer.start()
        self.segmented = self._arm(arm_timeout)
        self.at_prompt = self.segmented
        self._reader = threading.Thread(target=self._reader_loop, name=f"br-{self.cfg.sid}", daemon=True)
        self._reader.start()
        if self.cfg.recorder is not None:
            self.cfg.recorder.record(
                "session_start",
                {
                    "sid": self.cfg.sid,
                    "host": self.cfg.host_label,
                    "channel": self.cfg.channel_kind,
                    "segmented": self.segmented,
                },
                flush=True,
            )
        if not self.segmented:
            self._notify("目标主机提示符不可控，已切换为『原始录制』模式：命令仍全程录制，但无法逐条拦截")
        return self.segmented

    def stop(self, reason: str = "用户断开") -> None:
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        self._queue.put(None)
        try:
            self.channel.close()
        except Exception:  # noqa: BLE001
            pass
        self._finalize_pending(force_action=None)
        if self.cfg.recorder is not None:
            self.cfg.recorder.close(reason, bytes_in=self.bytes_in, bytes_out=self.bytes_out)

    def _finish(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self._stop.set()
        self._queue.put(None)
        self._finalize_pending(force_action=None)
        if self.cfg.recorder is not None:
            self.cfg.recorder.close(reason, bytes_in=self.bytes_in, bytes_out=self.bytes_out)
        if self.cfg.on_close is not None:
            try:
                self.cfg.on_close(reason)
            except Exception:  # noqa: BLE001
                log.exception("bridge on_close 回调异常")

    # ------------------------------------------------------------ 输出（远端 -> 用户）
    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                self.channel.sendall(item)
            except Exception:  # noqa: BLE001
                break

    def _write(self, data: bytes) -> None:
        if data and not self.closed:
            self._queue.put(data)

    def _emit_output(self, data: bytes) -> None:
        if not data:
            return
        # 客户端看到的一切输出都在这里过一遍 ONLCR 归一化：裸 LF 会让终端
        # 只下行不回列，目标机不转 CRLF 时提示符/回显会一行比一行右移。
        data = self.out_normalizer.feed(data)
        if not data:
            return
        # 有输出 = 会话活着：喂给空闲清理器，否则「空闲超时」会把正在干活的会话误杀。
        registry_touch(self.cfg.sid)
        cb = self.cfg.on_output
        if cb is not None:
            try:
                cb(data)
            except Exception:  # noqa: BLE001
                log.exception("bridge on_output 回调异常")

    def _notify(self, message: str) -> None:
        cb = self.cfg.on_notice
        if cb is not None:
            try:
                cb(message)
            except Exception:  # noqa: BLE001
                log.exception("bridge on_notice 回调异常")

    # ------------------------------------------------------------ 交互模式确认
    def _arm_commands(self) -> list[bytes]:
        title = f"{self.cfg.host_label}" if self.cfg.host_label else "bastion"
        keep = f"[{title}]"
        full = f"export PS1='{self.marker}\\u@\\h:\\w\\$ '\n"
        plain = f"export PS1='{self.marker}'\n"
        _ = keep
        return [full.encode(), plain.encode()]

    def _arm(self, timeout: float) -> bool:
        for index, cmd in enumerate(self._arm_commands()):
            self._write(cmd)
            data = self._read_until_marker(timeout if index == 0 else min(timeout, 8.0))
            if data is None:
                continue
            self._emit_output(self._strip_echo(data, cmd))
            tail = self._read_for(0.25)
            if tail:
                self._emit_output(tail)
            return True
        return False

    def _read_until_marker(self, timeout: float) -> bytes | None:
        buf = bytearray()
        marker = self.marker.encode()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                chunk = self.channel.recv(65536)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                return None
            if not chunk:
                return None
            buf.extend(chunk)
            idx = buf.find(marker)
            if idx >= 0:
                del buf[idx : idx + len(marker)]
                return bytes(buf)
            if len(buf) > 4 * 1024 * 1024:
                return None
        return None

    def _read_for(self, seconds: float) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                chunk = self.channel.recv(65536)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            buf.extend(chunk)
            if buf:
                break
        return bytes(buf)

    @staticmethod
    def _strip_echo(data: bytes, cmd: bytes) -> bytes:
        needles = (b"export PS1=", b"PS1=")
        out = bytearray()
        for line in data.replace(b"\r\n", b"\n").split(b"\n"):
            if any(n in line for n in needles):
                continue
            out.extend(line + b"\n")
        if len(out) > 1 and data.endswith((b"\r\n", b"\n")):
            pass
        return bytes(out).replace(b"\n", b"\r\n")

    # ------------------------------------------------------------ 读数线程
    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.channel.recv(65536)
            except socket.timeout:
                self._check_timeout()
                continue
            except Exception as exc:  # noqa: BLE001
                self._flush_filter()
                self._finish(f"远端通道异常：{exc}")
                return
            if not chunk:
                self._flush_filter()
                self._finish("远端会话已结束")
                return
            self.bytes_out += len(chunk)
            if self.cfg.recorder is not None:
                self.cfg.recorder.record_io("out", chunk)
            for kind, payload in self.filter.feed(chunk):
                if kind == "data":
                    self._on_remote_data(payload)
                else:
                    self._on_prompt()
            self._check_timeout()

    def _flush_filter(self) -> None:
        rest = self.filter.flush()
        if rest:
            self._on_remote_data(rest)

    def _on_remote_data(self, payload: bytes) -> None:
        if self._pending is not None:
            room = self.cfg.max_output_bytes - len(self._capture)
            if room > 0:
                self._capture.extend(payload[:room])
            if len(payload) > max(room, 0):
                self._capture_truncated = True
        self._emit_output(payload)

    def _on_prompt(self) -> None:
        self._finalize_pending()
        self.at_prompt = True
        notice = self._pending_notice
        self._pending_notice = ""
        if notice:
            self._emit_output(f"{ANSI_RED}[堡垒机] {notice}{ANSI_RESET}\r\n".encode())
        cb = self.cfg.on_prompt
        if cb is not None:
            try:
                cb("")
            except Exception:  # noqa: BLE001
                log.exception("bridge on_prompt 回调异常")

    def _check_timeout(self) -> None:
        pending = self._pending
        if pending is None or self.cfg.command_timeout <= 0:
            return
        if time.monotonic() - pending["started_at"] < self.cfg.command_timeout:
            return
        if pending.get("timed_out"):
            return
        pending["timed_out"] = True
        pending["action"] = "timeout"
        pending["reason"] = f"命令执行超过 {self.cfg.command_timeout} 秒，已强制中断"
        self._write(b"\x03")
        self._pending_notice = f"命令执行超时（>{self.cfg.command_timeout}s），已发送 Ctrl-C 中断"

    # ------------------------------------------------------------ 用户 -> 远端
    def feed_input(self, data: bytes) -> None:
        if not data or self.closed:
            return
        # 用户按键 = 会话活着（有人在敲字但远端暂时没有输出，不能算空闲）。
        registry_touch(self.cfg.sid)
        self.bytes_in += len(data)
        if self.cfg.recorder is not None:
            self.cfg.recorder.record_io("in", data)

        buf = bytearray()
        index = 0
        total = len(data)
        while index < total:
            byte = data[index]
            if byte in (CR, LF):
                if byte == LF and index > 0 and data[index - 1] == CR:
                    index += 1
                    continue
                self._handle_enter(bytes(buf))
                buf.clear()
                index += 1
                continue
            self.tracker.feed_byte(byte)
            buf.append(byte)
            index += 1
        if buf:
            self._write(bytes(buf))

    def _start_command(
        self,
        raw: bytes,
        line: str,
        *,
        risk_level: str,
        reason: str,
        matched_rule_id: int | None = None,
        matched_rule_pattern: str | None = None,
        uncertain: bool = False,
    ) -> None:
        """把一条命令置为「执行中」并把原始按键（含回车）发给目标机。

        抽出来是为了让「策略放行」与「会话控制指令放行」共用同一段落库/采集逻辑，
        避免两处 pending 字段漂移（曾经就会话控制指令漏掉一次 capture 重置）。
        """
        self.seq += 1
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
        }
        self._capture = bytearray()
        self._capture_truncated = False
        self.at_prompt = False
        self._write(raw + b"\r")

    def _handle_enter(self, raw: bytes) -> None:
        line = self.tracker.text().strip()
        uncertain = self.tracker.uncertain
        self.tracker.reset()

        if not self.segmented:
            self._write(raw + b"\r")
            if line:
                self._record_raw(line)
            return

        if not self.at_prompt:
            # 命令执行期间的交互输入：原样透传，仅记录
            self._write(raw + b"\r")
            if line:
                self._record_raw(line)
            return

        if not line:
            self._write(raw + b"\r")
            return

        if is_session_exit(line):
            # 会话控制指令（exit/quit/logout/bye）先于策略判定放行：只读白名单策略会把
            # `exit` 判成「白名单外命令」而拒绝，用户就困在目标机里出不去了（横幅却承诺
            # 「输入 exit 返回主机菜单」）。仍然走 allow 通道，命令与输出照常落库审计。
            self._start_command(
                raw,
                line,
                risk_level="low",
                reason="会话控制指令（退出/登出，不受命令策略限制）",
            )
            return

        decision = evaluate_policy(self.cfg.policy, line)
        if decision.allowed:
            self._start_command(
                raw,
                line,
                risk_level=decision.risk_level,
                reason=decision.reason,
                matched_rule_id=decision.rule_id,
                matched_rule_pattern=decision.rule_pattern,
                uncertain=uncertain,
            )
            return

        # ---- 拒绝：丢弃远端行缓冲，并预约在下一个提示符处给出提示
        # 注意 at_prompt 必须保持 True：远端收到 \x03 后会丢掉这一行并重画提示符，
        # 它本来就还在提示符上。如果置 False，用户下一条命令会被当成“执行期间的交互输入”
        # 直接透传到目标机 —— 命令跑了却没有 CommandLog，等于审计漏一条。
        self._write(b"\x03")
        self._pending = None
        self._capture = bytearray()
        self._capture_truncated = False
        self._pending_notice = f"命令被拒绝 —— {decision.reason}"
        self._record_denied(line, decision, uncertain)

    def _record_raw(self, line: str) -> None:
        if self.cfg.recorder is not None:
            self.cfg.recorder.record("interactive_input", {"data": line})

    def send_raw(self, data: bytes) -> None:
        """发送非阻塞 / 带外数据（例如 SFTP 之外的场景）。"""
        self._write(data)

    def resize(self, cols: int, rows: int) -> None:
        try:
            self.channel.resize_pty(width=max(int(cols), 20), height=max(int(rows), 5))
            if self.cfg.recorder is not None:
                self.cfg.recorder.record("resize", {"cols": cols, "rows": rows})
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 命令落库
    def _finalize_pending(self, force_action: str | None = None) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        started = pending["started_at"]
        duration_ms = int((time.monotonic() - started) * 1000)
        raw_output = bytes(self._capture)
        self._capture = bytearray()
        output = raw_output.decode("utf-8", "replace")
        if output.startswith("\r\n"):
            output = output[2:]
        elif output.startswith("\n"):
            output = output[1:]
        action = force_action or pending.get("action") or "allow"
        event = {
            "seq": pending["seq"],
            "command": pending["command"],
            "output": output,
            "action": action,
            "risk_level": pending.get("risk_level", "low"),
            "reason": pending.get("reason", ""),
            "matched_rule_id": pending.get("matched_rule_id"),
            "matched_rule_pattern": pending.get("matched_rule_pattern", ""),
            "duration_ms": duration_ms,
            "truncated": self._capture_truncated,
            "uncertain": pending.get("uncertain", False),
            "channel": self.cfg.channel_kind,
            "ts": time.time(),
        }
        if self.cfg.recorder is not None:
            self.cfg.recorder.record(
                "command",
                {
                    "seq": event["seq"],
                    "command": event["command"],
                    "output": output[:20000],
                    "output_bytes": len(raw_output),
                    "action": action,
                    "risk_level": event["risk_level"],
                    "duration_ms": duration_ms,
                    "truncated": event["truncated"],
                    "uncertain": event["uncertain"],
                },
                flush=False,
            )
        cb = self.cfg.on_command
        if cb is not None:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("bridge on_command 回调异常")

    def _record_denied(self, line: str, decision, uncertain: bool) -> None:
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
            "uncertain": uncertain,
            "channel": self.cfg.channel_kind,
            "ts": time.time(),
        }
        if self.cfg.recorder is not None:
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
        cb = self.cfg.on_command
        if cb is not None:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("bridge on_command 回调异常")

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        return {
            "sid": self.cfg.sid,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "commands": self.seq,
            "segmented": self.segmented,
            "duration": int(time.time() - self._started_at),
        }
