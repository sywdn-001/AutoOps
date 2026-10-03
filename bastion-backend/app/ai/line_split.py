"""``/ask-ai`` 命令行识别与按键分流（SSH 网关与网页终端共用）。

为什么单独成模块
----------------
网关（``app/gateway/server.py``）和网页终端（``app/webterm/events.py``）都要在**按键流**里
判断「这一行是不是 ``/ask-ai``」，而且必须共用同一套转义序列处理，否则：

* 粘贴（bracketed paste，``ESC[200~`` / ``ESC[201~``）、方向键、功能键这些多字节 CSI 序列
  会被当成正文写进影子行，``/ask-ai`` 就漏过去被目标机 bash 执行（实测现象：
  ``-bash: /ask-ai: 没有那个文件或目录``）；
* 两边各写一份，行为迟早不一致。

放在 ``app/ai/`` 下是因为它不依赖网关（避免 ``gateway.server`` ↔ ``gateway.ai_shell`` 的循环导入），
两边都可以安全导入。
"""

from __future__ import annotations

#: 支持的命令前缀（``/ask`` 是顺手给的别名）
AI_PREFIXES = ("/ask-ai", "/ask")

#: 影子行缓冲上限：超过就丢弃，避免异常输入把内存撑爆
MAX_SHADOW = 512

#: 单个转义序列的参数字节上限（防御畸形输入）
MAX_ESCAPE_PARAMS = 32

# 转义序列解析状态（必须跨 chunk 保持，粘贴的序列可能被 TCP 分段）
_ESC_NONE = 0
_ESC_SEEN = 1  # 刚收到 ESC
_ESC_CSI = 2  # ESC [ … （直到 0x40~0x7E 的终结字节）
_ESC_SS3 = 3  # ESC O … （再吞一个字节）
_ESC_OSC = 4  # ESC ] … （直到 BEL 或 ST）
_ESC_OSC_ST = 5  # OSC 里遇到 ESC，等一个 '\' 结束

#: 不影响行内文本的转义序列（粘贴标记、私有模式）：**不能再清空影子行**，
#: 否则粘贴的 ``/ask-ai`` 会被 ``ESC[201~`` 抹掉，白白漏检。
_BENIGN_CSI_PARAMS = (b"200", b"201")


def extract_question(line: str) -> str | None:
    """``/ask-ai 帮我看看磁盘`` → ``"帮我看看磁盘"``；不是 AI 命令则返回 ``None``。

    只敲命令本身返回空串（调用方据此打印用法）。
    """
    text = (line or "").strip()
    for prefix in AI_PREFIXES:
        if text == prefix:
            return ""
        if text.startswith(prefix) and text[len(prefix) : len(prefix) + 1] in (" ", "\t"):
            return text[len(prefix) :].strip()
    return None


def is_ai_command(line: str) -> bool:
    """这一行是不是 ``/ask-ai`` 命令（含 ``/ask`` 别名）。"""
    return extract_question(line) is not None


class LineShadow:
    """影子行缓冲：**只用来判断这一行是不是 ``/ask-ai``**，不影响任何字节转发。

    本地回显、补全、Ctrl-C 依旧是目标机 bash 的行为——我们只旁路观察，不改写输入。
    命中 ``/ask-ai`` 时：回车**不转发**（调用方改发 Ctrl-U 把该行从 bash 里抹掉），
    所以 ``/ask-ai`` 绝不会被目标机当成命令执行。

    转义序列（ESC 开头）原样转发、但不进影子缓冲；序列状态跨 ``feed()`` 调用保持，
    因此粘贴、方向键、跨 TCP 分段的序列都不会污染影子行。光标/删改类序列（方向键、
    Home/End、Delete）会让行内容与屏幕不一致，遇到时**清空影子行**保守处理；
    粘贴标记与私有模式不影响文本，遇到时保持影子行不变。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._params = bytearray()
        self._esc = _ESC_NONE

    def clear(self) -> None:
        """清空行状态（例如目标机提示符重画、会话重连）。"""
        self._buf.clear()
        self._params.clear()
        self._esc = _ESC_NONE

    @property
    def pending(self) -> str:
        """当前影子行内容（调试与测试用）。"""
        return self._buf.decode("utf-8", "replace")

    def _finish_escape(self, benign: bool) -> None:
        """结束一个转义序列；非无害序列保守清空影子行。"""
        self._esc = _ESC_NONE
        self._params.clear()
        if not benign:
            self._buf.clear()

    def _consume_escape(self, byte: int) -> None:
        """推进转义序列状态机（不写影子行）。"""
        if self._esc == _ESC_SEEN:
            if byte == 0x5B:  # '['
                self._esc = _ESC_CSI
                self._params.clear()
                return
            if byte == 0x4F:  # 'O'（SS3，F1~F4 / 小键盘）
                self._esc = _ESC_SS3
                return
            if byte == 0x5D:  # ']'（OSC，设置标题之类；正常不会出现在输入里）
                self._esc = _ESC_OSC
                return
            if 0x20 <= byte <= 0x2F:  # 中间字节，序列还没完
                return
            self._finish_escape(False)  # 两字节转义（如 Alt+键）→ 保守清行
            return
        if self._esc == _ESC_CSI:
            if 0x40 <= byte <= 0x7E:  # 终结字节，'~' 也算
                params = bytes(self._params)
                benign = params in _BENIGN_CSI_PARAMS or params.startswith(b"?")
                self._finish_escape(benign)
            elif 0x20 <= byte <= 0x3F:  # 参数/中间字节
                if len(self._params) < MAX_ESCAPE_PARAMS:
                    self._params.append(byte)
            else:  # 非法序列：放弃解析
                self._finish_escape(False)
            return
        if self._esc == _ESC_SS3:
            self._finish_escape(False)
            return
        if self._esc == _ESC_OSC:
            if byte == 0x07:  # BEL 结束
                self._finish_escape(True)
            elif byte == 0x1B:  # 可能是 ST（ESC \）
                self._esc = _ESC_OSC_ST
            return
        if self._esc == _ESC_OSC_ST:
            self._esc = _ESC_OSC if byte != 0x5C else _ESC_NONE
            if byte == 0x5C:
                self._params.clear()

    def feed(self, raw: bytes) -> tuple[bytes, str | None]:
        """吞一段按键，返回 ``(要转发给目标机的字节, 命中的 /ask-ai 命令行)``。

        **命中之后不会中断这一批按键的处理**：只把命中那一行的回车吞掉，同一批里剩下的字节
        （典型是粘贴的结束标记 ``ESC[201~``）照常转发。否则 readline 的括号粘贴状态永远关不掉，
        用户下一次按键会被当成粘贴内容插进行里。
        """
        out = bytearray()
        hit: str | None = None
        for byte in raw:
            if self._esc != _ESC_NONE:
                out.append(byte)
                self._consume_escape(byte)
                continue
            if byte == 27:  # ESC：先不下结论，等序列走完再决定要不要清影子行
                self._esc = _ESC_SEEN
                self._params.clear()
                out.append(byte)
                continue
            if byte in (10, 13):  # Enter
                line = self._buf.decode("utf-8", "replace")
                self._buf.clear()
                if hit is None and is_ai_command(line):
                    hit = line.strip()
                    continue  # 命中那一行的回车不转发（调用方改发 Ctrl-U）
                out.append(byte)
                continue
            if byte in (8, 127):  # Backspace
                if self._buf:
                    self._buf.pop()
            elif byte in (3, 4, 21):  # Ctrl-C / Ctrl-D / Ctrl-U：行状态未知
                self._buf.clear()
            elif byte >= 32:
                self._buf.append(byte)
                if len(self._buf) > MAX_SHADOW:
                    self._buf.clear()
            out.append(byte)
        return bytes(out), hit
