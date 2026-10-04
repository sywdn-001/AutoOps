"""Socket.IO 网页终端：把浏览器的按键送进 SSH，把输出推回 xterm.js。

与 SSH 网关共用 ``session_service.open_session``，所以命令拦截、录像、
审计落库的行为完全一致，只是传输层从 paramiko 通道换成了 WebSocket。
"""

from __future__ import annotations

import codecs
import logging
import queue
import threading
import time

from flask import current_app, request
from flask_jwt_extended import decode_token
from flask_socketio import emit

from ..audit import log_event
from ..extensions import db, socketio
from ..models import User
from ..security import has_permission
from ..session_notes import session_note_lines
from ..session_registry import registry
from ..session_service import open_session, teardown_session

log = logging.getLogger(__name__)

#: socketio sid -> 会话上下文
_CLIENTS: dict[str, dict] = {}
_CLIENTS_LOCK = threading.Lock()

#: 事件处理器登记表：装饰器只登记，真正注册发生在 ``register_events(app)``
#: （必须晚于 ``socketio.init_app(app)``，因为 init_app 会重建 server）。
_EVENTS: list[tuple[str, object]] = []


def _event(name: str):
    """登记一个 Socket.IO 事件处理器（代替 socketio.on 的直接注册）。"""

    def decorator(handler):
        _EVENTS.append((name, handler))
        return handler

    return decorator


# ---------------------------------------------------------------------------
# 输出泵：把桥接线程的输出按时间片合并后推送，避免逐字节刷屏
# ---------------------------------------------------------------------------
class OutputPump:
    def __init__(self, sid: str, flush_interval: float = 0.025, max_batch: int = 65536):
        self.sid = sid
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer: list[str] = []
        self._size = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        # 暂停闸门：会话号还没交到客户端之前，目标机输出不能抢跑（否则前端会先收到
        # 输出、后收到 terminal:opened，会话头画在输出下面）。
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"pump-{sid}", daemon=True)
        self._thread.start()

    def push(self, data) -> None:
        if not data:
            return
        if isinstance(data, (bytes, bytearray)):
            text = self._decoder.decode(bytes(data))
        else:
            text = str(data)
        if not text:
            return
        with self._lock:
            self._buffer.append(text)
            self._size += len(text)
            # 保护：极端输出且客户端消费不过来时，丢弃最旧数据而不是把内存打满
            if self._size > 4 * 1024 * 1024:
                dropped = self._size - 2 * 1024 * 1024
                self._buffer = [f"\r\n\033[31m[堡垒机] 输出过快，已丢弃 {dropped} 字符\033[0m\r\n"]
                self._size = len(self._buffer[0])
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.flush_interval)
            self._wake.clear()
            self.flush()

    def pause(self) -> None:
        """暂停推送：缓冲保留、不 emit。会话就绪后再 release()。"""
        self._paused.set()

    def release(self) -> None:
        """恢复推送并立刻把攒下的输出刷给客户端。"""
        self._paused.clear()
        self.flush()

    def flush(self) -> None:
        if self._paused.is_set():
            return
        with self._lock:
            if not self._buffer:
                return
            payload = "".join(self._buffer)
            self._buffer.clear()
            self._size = 0
        try:
            socketio.emit("terminal:output", {"sid": self.sid, "data": payload}, to=self.sid)
        except Exception:  # noqa: BLE001 - 客户端断线时忽略
            log.debug("emit terminal:output failed for %s", self.sid)

    def stop(self) -> None:
        self._stop.set()
        self._paused.clear()
        self._wake.set()
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
def _revoked_check(jti) -> bool:
    try:
        from ..api.auth import is_revoked

        return bool(is_revoked(jti))
    except Exception:  # noqa: BLE001
        return False


def _resolve_token(auth) -> str:
    token = ""
    if isinstance(auth, dict):
        token = auth.get("token") or auth.get("Authorization") or ""
    token = token or request.args.get("token") or ""
    if token.lower().startswith("bearer "):
        token = token[7:]
    return token.strip()


def _authenticate(auth):
    """返回 (user_id, username, error)。"""
    token = _resolve_token(auth)
    if not token:
        return None, None, "缺少登录令牌"
    try:
        claims = decode_token(token)
    except Exception:  # noqa: BLE001
        return None, None, "登录令牌无效或已过期"
    if claims.get("jti") and _revoked_check(claims.get("jti")):
        return None, None, "登录令牌已注销"
    try:
        user_id = int(claims.get("sub"))
    except (TypeError, ValueError):
        return None, None, "登录令牌缺少用户标识"
    user = db.session.get(User, user_id)
    if user is None or not user.is_active():
        return None, None, "账号不存在或已被停用"
    if not user.webterm_enabled:
        return None, None, "你的账号已被禁止使用网页终端"
    if not has_permission(user, "terminal:use"):
        return None, None, "你没有网页终端的使用权限"
    return user.id, user.username, ""


def _client_ctx() -> dict | None:
    with _CLIENTS_LOCK:
        return _CLIENTS.get(request.sid)


def _app():
    return current_app._get_current_object()


# ---------------------------------------------------------------------------
# /ask-ai：按键分流 + AI worker 线程 + 逐字符读行
# ---------------------------------------------------------------------------
#: 连接建立后清屏用的控制序列（清屏 + 光标归位），与 SSH 网关观感一致
CLEAR_SCREEN = "\x1b[2J\x1b[H"

#: 等管理员输入（敏感操作确认）的超时秒数：超时即放弃该次确认，不让 worker 线程永久挂住
AI_INPUT_TIMEOUT = 120.0

#: AI 单行输入缓冲上限（防御畸形输入把内存撑爆）
_AI_LINE_LIMIT = 512


def _line_split():
    """延迟导入 ``app.ai.line_split``（``/ask-ai`` 按键分流，与 SSH 网关共用）。

    ``app.ai`` 包（client / service / tools）很重，所以只在真正用到 ``/ask-ai``
    时导入，别拖慢应用启动。
    """
    from ..ai import line_split

    return line_split


def _ai_shell():
    """延迟导入 ``app.gateway.ai_shell``（AI 问答实现，与 SSH 网关共用）。

    该模块顶层会 ``from .server import color ...``，两边模块级互相导入会成环，
    所以只能在调用时导入（网关那边也是同样的做法）。
    """
    from ..gateway import ai_shell

    return ai_shell


def _drain_queue(q: queue.Queue) -> None:
    """丢掉队列里残留的按键/中止标记（上一次问答留下的）。"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class AiTurnState:
    """网页终端会话里的 AI 状态（跨多次 ``/ask-ai`` 反复使用）。

    ``awaiting`` 是关键闸门：worker 线程在 ``read_line`` 里阻塞等管理员输入时，
    ``terminal:input`` 必须把按键送进 ``queue`` 而不是转发给目标机——否则管理员
    密码会被目标机 bash 回显，还会进审计录像。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.active = False  # 是否有 AI 问答正在跑
        self.awaiting = False  # worker 正在 read_line 里等输入
        self.mask = False  # 当前提示是密码（回显用 *）
        self.prompt = ""  # 当前提示文本（Ctrl-U 重画用）
        self.buf: list[str] = []  # 正在敲的这一行
        self.queue: queue.Queue = queue.Queue()
        self.abort = False
        self.conversation_id: int | None = None


class WebTermWriter:
    """把 AI 侧的输出写进网页终端会话既有的推送通道（``OutputPump`` → xterm.js）。

    只需实现 ``ai_shell.run_ask_ai`` / ``TerminalMarkdown`` 用到的最小接口：
    ``write(str)``、``write_bytes(bytes)``、``line(str)``。桥接线程与 AI worker
    线程会并发写，这里再加一把锁保证「一行」不会被拆开（``OutputPump.push``
    自己也是线程安全的，但那是数据块级的）。
    """

    def __init__(self, pump: OutputPump):
        self.pump = pump
        self._lock = threading.Lock()
        self._stopped = False

    def stop(self) -> None:
        """会话结束后让写入变成空操作（pump 已经停了，别再往里堆数据）。"""
        self._stopped = True

    def write(self, text) -> None:
        if not text or self._stopped:
            return
        with self._lock:
            self.pump.push(str(text))

    def write_bytes(self, data) -> None:
        if not data or self._stopped:
            return
        with self._lock:
            self.pump.push(bytes(data))

    def line(self, text: str = "") -> None:
        self.write(f"{text}\r\n")


def _abort_ai(ctx: dict) -> None:
    """中止当前 AI 问答：叫醒阻塞在 ``read_line`` 里的 worker 线程。"""
    ai = ctx.get("ai")
    if ai is None:
        return
    with ai.lock:
        ai.abort = True
        ai.awaiting = False
        ai.mask = False
        ai.prompt = ""
        ai.buf = []
    ai.queue.put(None)  # read_line 收到 None → 返回 aborted=True


def _read_ai_line(
    ai: AiTurnState, writer: WebTermWriter, prompt: str = "", mask: bool = False
) -> tuple[str, bool]:
    """worker 线程里的阻塞读行，返回 ``(这一行, 是否中止)``（契约同 SSH 网关）。

    按键由 Socket.IO 事件回调通过 ``ai.queue`` 送进来——worker **绝不**碰 Socket.IO
    的事件循环：threading 模式下事件回调一旦被阻塞，同一条连接上后续的按键（包括
    管理员密码）永远到不了，直接死锁。
    """
    ai_shell = _ai_shell()
    with ai.lock:
        if ai.abort:
            return "", True
        ai.awaiting = True
        ai.mask = bool(mask)
        ai.prompt = prompt
        ai.buf = []
    writer.write(ai_shell.color(prompt, ai_shell.CLR_CYAN))
    try:
        item = ai.queue.get(timeout=AI_INPUT_TIMEOUT)
    except queue.Empty:
        writer.line(
            ai_shell.color(
                f"[堡垒机] 等待输入超过 {int(AI_INPUT_TIMEOUT)} 秒，已放弃本次操作。",
                ai_shell.CLR_RED,
            )
        )
        return "", True
    finally:
        with ai.lock:
            ai.awaiting = False
            ai.mask = False
            ai.prompt = ""
            ai.buf = []
    if item is None:  # 会话结束 / 被打断
        return "", True
    return str(item), False


def _handle_ai_input(ctx: dict, text: str, ai: AiTurnState) -> None:
    """AI 正在等管理员输入：按键只走 AI 这一侧，一个字节都不许转发给目标机。"""
    writer = ctx.get("writer")
    if writer is None:
        return
    ai_shell = _ai_shell()
    for ch in text:
        if ch in ("\r", "\n"):  # 提交这一行
            line = "".join(ai.buf)
            ai.buf = []
            with ai.lock:
                ai.awaiting = False
            writer.write("\r\n")
            ai.queue.put(line)
            continue
        if ch in ("\x7f", "\x08"):  # Backspace
            if ai.buf:
                ai.buf.pop()
                writer.write("\b \b")
            continue
        if ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D：中止本次确认
            ai.buf = []
            with ai.lock:
                ai.awaiting = False
            writer.write("^C\r\n")
            ai.queue.put(None)
            return
        if ch == "\x15":  # Ctrl-U：清掉这一行并重画提示符
            ai.buf = []
            writer.write("\r\x1b[K")
            if ai.prompt:
                writer.write(ai_shell.color(ai.prompt, ai_shell.CLR_CYAN))
            continue
        if ch == "\x1b" or ord(ch) < 32:  # 转义序列 / 其余控制字符：丢弃
            continue
        ai.buf.append(ch)
        if len(ai.buf) > _AI_LINE_LIMIT:
            ai.buf = []
        writer.write("*" if ai.mask else ch)


def _handle_session_input(ctx: dict, text: str) -> None:
    """把一次 ``terminal:input`` 的按键路由给 bridge 或正在等输入的 AI。

    普通按键**原样**转发（``LineShadow`` 只是旁路观察，不改写输入）；命中
    ``/ask-ai`` 时转发字节里不含回车，接着补一个 Ctrl-U 把该行从目标机 bash 的
    行缓冲里抹掉——``/ask-ai`` 绝不会被目标机当成命令执行。
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not text:
        return
    ai = ctx.get("ai")
    opened = ctx.get("opened")
    # AI 正在等管理员输入：这一行绝不能漏给目标机（密码会被 bash 回显、进录像）
    if ai is not None and ai.awaiting and opened is not None:
        _handle_ai_input(ctx, text, ai)
        return
    if opened is None:
        # 会话还在建立中（open_session 未返回）：按键先攒着，等 on_open 就绪后按序回放。
        # 历史缺陷：这里原来直接 return，客户端收到 terminal:opened 立刻发来的第一条
        # 命令会凭空消失（E2E 里表现为 web 终端 whoami 不回显且不落库）。
        early = ctx.get("early_input")
        if early is not None:
            early.append(text)
            log.debug("buffered early input while opening: %r", text[:32])
        return
    shadow = ctx.get("shadow")
    if shadow is None:
        shadow = ctx["shadow"] = _line_split().LineShadow()
    forward, ai_line = shadow.feed(text.encode("utf-8", "replace"))
    if forward:
        try:
            opened.bridge.feed_input(forward)
        except Exception:  # noqa: BLE001
            log.debug("feed_input failed sid=%s", getattr(opened, "sid", ""))
    if ai_line is None:
        return
    # 命中 /ask-ai：先用 Ctrl-U 让目标机丢弃这一行（readline 只清行、不执行），
    # 再在独立 worker 线程里跑一次问答——AI 的输出只打印在堡垒机这一侧。
    try:
        opened.bridge.feed_input(b"\x15")
    except Exception:  # noqa: BLE001
        log.debug("ctrl-u failed sid=%s", getattr(opened, "sid", ""))
    _start_ai_turn(ctx, ai_line)


def _start_ai_turn(ctx: dict, line: str) -> None:
    """在独立线程里启动一次 ``/ask-ai``（不阻塞 Socket.IO 事件回调）。"""
    ai = ctx.get("ai")
    writer = ctx.get("writer")
    if ai is None:
        return
    ai_shell = _ai_shell()
    with ai.lock:
        if ai.active:
            if writer is not None:
                writer.line(ai_shell.color("[堡垒机] 上一个 AI 问答还没结束，请等它答完再问。", ai_shell.CLR_RED))
            return
        ai.active = True
        ai.abort = False
        ai.awaiting = False
        ai.mask = False
        ai.prompt = ""
        ai.buf = []
    # 清掉上一次可能残留的按键/中止标记，别让新的 read_line 误吃
    _drain_queue(ai.queue)
    question = _line_split().extract_question(line) or ""
    sid = getattr(ctx.get("opened"), "sid", "") or ""
    thread = threading.Thread(
        target=_ai_worker,
        args=(ctx, question),
        name=f"webterm-ai-{sid or 'session'}",
        daemon=True,
    )
    ai.thread = thread
    thread.start()


def _ai_worker(ctx: dict, question: str) -> None:
    """AI worker 主体：跑一轮 ``run_ask_ai``，异常只打印红字、绝不掀掉终端会话。"""
    ai = ctx.get("ai")
    writer = ctx.get("writer")
    app = ctx.get("app")
    opened = ctx.get("opened")
    try:
        if ai is None or writer is None or app is None:
            return
        ai_shell = _ai_shell()
        conversation_id = ai_shell.run_ask_ai(
            app,
            writer,
            user_id=int(ctx.get("user_id") or 0),
            entry=ctx.get("ai_entry"),
            account=ctx.get("ai_account"),
            sid=str(getattr(opened, "sid", "") or ""),
            question=question,
            width=int(ctx.get("cols") or 80),
            read_line=lambda prompt, mask=False: _read_ai_line(ai, writer, prompt, mask),
            conversation_id=ai.conversation_id,
        )
        if conversation_id:
            ai.conversation_id = conversation_id
    except Exception:  # noqa: BLE001 - shell 里必须给可读原因，且不能带走会话
        log.exception("webterm /ask-ai failed")
        if writer is not None:
            try:
                ai_shell = _ai_shell()
                writer.line(ai_shell.color("[堡垒机] AI 调用失败，请稍后再试。", ai_shell.CLR_RED))
            except Exception:  # noqa: BLE001
                pass
    finally:
        if ai is not None:
            with ai.lock:
                ai.active = False
                ai.awaiting = False
                ai.mask = False
                ai.prompt = ""
                ai.buf = []
                ai.thread = None
            _drain_queue(ai.queue)


def _clear_screen_after_connect(ctx: dict, opened, ready: dict, cols: int) -> None:
    """会话就绪后清屏，并重画紧凑的堡垒机上下文行（措辞与 SSH 网关一致）。

    为什么不用 ``clear`` 命令：那会在目标机上执行一条用户没敲过的命令，多一条审计
    记录、还污染录像。这里只写控制序列，再用 ``bridge.feed_input(b"\\n")`` 发一个
    空行让 bash 重画 PS1 —— 提示符下的空行不进命令库、不评估策略（见
    ``bridge._handle_enter`` 里 ``if not line`` 那条分支）。
    """
    writer = ctx.get("writer")
    if writer is None:
        return
    ai_shell = _ai_shell()
    color = ai_shell.color
    host = ready.get("hostName") or getattr(opened, "host_name", "") or ""
    address = ready.get("address") or ""
    account = ready.get("accountUsername") or getattr(opened, "account_username", "") or "-"
    sid_text = ready.get("sid") or getattr(opened, "sid", "")
    lines = [
        color("[堡垒机]", ai_shell.CLR_GREEN)
        + f" 已连接 {host}（{address}），账号 {account}，会话号 {sid_text}",
        color("[堡垒机] 输入的命令与输出都会被审计记录。输入 exit 返回主机菜单。", ai_shell.CLR_DIM),
        color("[堡垒机] 想随时问 AI：", ai_shell.CLR_DIM)
        + color("/ask-ai <问题>", ai_shell.CLR_CYAN)
        + color("（AI 的答案流式输出，操作同样入审计）", ai_shell.CLR_DIM),
    ]
    # 协议专属的能力边界（如 WinRM 的「进程内状态不保留 / 交互式程序不可用」）：
    # 桥接层自己在 start() 里也打印过一份，但紧接着这句清屏会把它擦掉，所以必须在这里重打。
    protocol = (getattr(opened, "meta", None) or {}).get("protocol") or "ssh"
    lines.extend(
        color(line, ai_shell.CLR_DIM) for line in session_note_lines(protocol)
    )
    writer.write(CLEAR_SCREEN + "\r\n".join(lines) + "\r\n")
    try:
        opened.bridge.feed_input(b"\n")
    except Exception:  # noqa: BLE001
        log.debug("redraw prompt after clear failed sid=%s", sid_text)


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
@_event("connect")
def on_connect(auth=None):
    user_id, username, error = _authenticate(auth)
    if user_id is None:
        log.info("webterm connection rejected: %s", error)
        emit("terminal:error", {"message": error})
        return False
    with _CLIENTS_LOCK:
        _CLIENTS[request.sid] = {
            "user_id": user_id,
            "username": username,
            "opened": None,
            "pump": None,
            "seq": 0,
            # 会话就绪前到达的按键缓冲（握手期间用户已经在敲键盘 → 不能丢）
            "early_input": [],
            # /ask-ai 的影子行缓冲（只旁路观察，不改写转发字节）
            "shadow": _line_split().LineShadow(),
            # AI 问答状态：worker 线程 + 喂行队列
            "ai": AiTurnState(),
            "writer": None,
            "app": None,
            "cols": 80,
            "ai_entry": None,
            "ai_account": None,
        }
    emit("terminal:ready", {"username": username})
    return True


@_event("disconnect")
def on_disconnect(reason=None):
    with _CLIENTS_LOCK:
        ctx = _CLIENTS.pop(request.sid, None)
    if ctx is None:
        return
    _close_session(ctx, reason="浏览器断开连接")


def _close_session(ctx: dict, reason: str = "会话关闭"):
    opened = ctx.get("opened")
    pump = ctx.get("pump")
    writer = ctx.get("writer")
    ctx["opened"] = None
    # 先叫醒可能正阻塞在 read_line 里的 AI worker，再关推送通道
    _abort_ai(ctx)
    if writer is not None:
        try:
            writer.stop()
        except Exception:  # noqa: BLE001
            pass
    if pump is not None:
        try:
            pump.stop()
        except Exception:  # noqa: BLE001
            pass
    ctx["pump"] = None
    if opened is not None:
        try:
            teardown_session(opened, reason=reason)
        except Exception:  # noqa: BLE001
            log.exception("webterm teardown failed sid=%s", opened.sid)


@_event("terminal:targets")
def on_targets(data=None):
    ctx = _client_ctx()
    if ctx is None:
        emit("terminal:error", {"message": "会话未认证，请重新登录"})
        return
    from ..access import accessible_targets, serialize_target

    user = db.session.get(User, ctx["user_id"])
    if user is None:
        emit("terminal:error", {"message": "账号状态异常"})
        return
    # 网页终端列 ssh（Linux shell）与 winrm（Windows shell）主机；Windows 远程桌面
    # （protocol="rdp"）走 /api/rdp/* 独立入口。
    entries = [
        serialize_target(entry)
        for entry in accessible_targets(user, protocols=("ssh", "winrm"))
    ]
    emit(
        "terminal:targets",
        {
            "list": [
                {
                    "hostId": item["hostId"],
                    "hostName": item["hostName"],
                    "address": item["address"],
                    "port": item["port"],
                    "groupName": item["groupName"],
                    "description": item["description"],
                    "policyName": item["policyName"],
                    "canWebterm": item["canWebterm"],
                    "protocol": item.get("protocol") or "ssh",
                    "accounts": item["accounts"],
                }
                for item in entries
            ]
        },
    )


@_event("terminal:open")
def on_open(data=None):
    payload = data or {}
    ctx = _client_ctx()
    if ctx is None:
        emit("terminal:error", {"message": "会话未认证，请重新登录"})
        return
    if ctx.get("opened") is not None:
        _close_session(ctx, reason="切换到新的主机")

    host_id = payload.get("hostId")
    account_id = payload.get("accountId")
    cols = int(payload.get("cols") or 80)
    rows = int(payload.get("rows") or 24)
    if not host_id:
        emit("terminal:error", {"message": "请先选择要连接的主机"})
        return

    app = _app()
    # 这些回调运行在**桥接线程**里，不在 Socket.IO 请求上下文中；必须在事件处理器内
    # 先把 sid 取出来捕获进闭包，回调里只用这个字符串。
    socket_sid = request.sid
    pump = OutputPump(socket_sid)
    # 先关上闸门：目标机可能在 open_session 返回前就把 MOTD 推上来。
    pump.pause()
    # 会话就绪（ctx["opened"] 写入）之前到达的按键先攒在这里，就绪后按序回放。
    # 静默丢弃按键是真实缺陷：客户端收到 terminal:opened 立刻发 terminal:input 时，
    # 服务端可能还在 open_session 里，ctx 里没有会话 → 第一条命令凭空消失。
    early_input: list[str] = ctx.setdefault("early_input", [])

    def on_notice(message: str):
        socketio.emit("terminal:notice", {"message": message}, to=socket_sid)

    def on_command(event: dict):
        socketio.emit(
            "terminal:command",
            {
                "seq": event.get("seq"),
                "command": event.get("command"),
                "action": event.get("action"),
                "riskLevel": event.get("risk_level"),
                "reason": event.get("reason"),
                "durationMs": event.get("duration_ms"),
                "ts": event.get("ts"),
            },
            to=socket_sid,
        )

    def on_closed(reason: str):
        socketio.emit("terminal:closed", {"reason": reason or "会话已结束"}, to=socket_sid)
        with _CLIENTS_LOCK:
            live = _CLIENTS.get(socket_sid)
        if live is not None and live.get("pump") is pump:
            # 会话已经结束：叫醒可能还在等管理员输入的 AI worker，别让它干等超时
            _abort_ai(live)
            try:
                pump.stop()
            except Exception:  # noqa: BLE001
                pass

    ready: dict = {}

    def on_ready(ready_sid: str, info: dict):
        """桥接线程在 bridge.start() 之前回调：这里只记录会话信息。

        真正的 terminal:opened 由事件处理器在本函数返回、ctx["opened"] 写好后统一发出
        —— 顺序上仍然先于目标机输出（输出被 pump 闸门攒着），但客户端绝不会在服务端
        还没存好会话时就开始打字。
        """
        ready.update(info or {})
        ready["sid"] = ready_sid

    try:
        opened = open_session(
            app,
            user_id=ctx["user_id"],
            host_id=int(host_id),
            account_id=int(account_id) if account_id else None,
            source="web",
            client_ip=request.environ.get("REMOTE_ADDR", "") or "",
            client_port=int(request.environ.get("REMOTE_PORT") or 0),
            cols=max(cols, 20),
            rows=max(rows, 5),
            send_output=pump.push,
            notify=on_notice,
            on_closed=on_closed,
            on_command=on_command,
            on_ready=on_ready,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成前端可读提示
        pump.stop()
        ctx.pop("early_input", None)
        message = str(exc) or "建立会话失败"
        log.warning("webterm open failed: %s", message)
        emit("terminal:error", {"message": message})
        try:
            log_event(
                "session",
                "webterm_open_failed",
                result="failure",
                actor_id=ctx["user_id"],
                actor_username=ctx["username"],
                message=message,
                ip=request.environ.get("REMOTE_ADDR", "") or "",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    ctx["pump"] = pump
    ctx["opened"] = opened
    ctx["writer"] = WebTermWriter(pump)
    ctx["app"] = app
    ctx["cols"] = max(cols, 20)
    # 新会话 = 新的行状态：上一次会话残留的影子行/对话号不能带过来
    ctx["shadow"] = _line_split().LineShadow()
    ctx["ai_entry"] = {
        "hostId": int(host_id),
        "hostName": ready.get("hostName") or opened.host_name or "",
        "address": ready.get("address") or "",
    }
    ctx["ai_account"] = {
        "username": ready.get("accountUsername") or opened.account_username or "-"
    }
    ai = ctx.get("ai")
    if ai is not None:
        ai.conversation_id = None
    # 会话已经真正就绪，现在才把会话号交给客户端：先 opened，再回放就绪前的按键，
    # 最后放行缓冲的目标机输出 —— 顺序与「前端先画会话头」一致，也不丢任何按键。
    emit(
        "terminal:opened",
        {
            "sid": ready.get("sid") or opened.sid,
            "hostName": ready.get("hostName") or "",
            "address": ready.get("address") or "",
            "accountUsername": ready.get("accountUsername") or "",
            "policyName": ready.get("policyName") or "",
            "segmented": opened.segmented,
            # 协议随会话下发，前端状态条据此显示 SSH / WINRM（同一个终端窗口两种通道）
            "protocol": (opened.meta or {}).get("protocol") or "ssh",
        },
    )
    # 放行缓冲的目标机输出（MOTD + 第一个提示符），随后清屏并重画堡垒机上下文行：
    # 用户一进来就看到干净的终端，而不是滚了一屏的 MOTD。清屏只写控制序列、
    # 不用 clear 命令，避免多一条用户没敲过的审计。
    pump.release()
    _clear_screen_after_connect(ctx, opened, ready, ctx["cols"])
    # 就绪前的按键按序回放（走同一套分流，握手期间敲的 /ask-ai 也拦得住）
    for chunk in ctx.pop("early_input", None) or []:
        try:
            _handle_session_input(ctx, chunk)
        except Exception:  # noqa: BLE001
            log.debug("replay early input failed sid=%s", opened.sid)
    # 降级提示只由桥接层发一次（bridge._notify → terminal:notice）：原来这里又发一条
    # 内容几乎相同的提示，加上前端按 opened.segmented 自己再提示一次，用户会看到三遍。


@_event("terminal:input")
def on_input(data=None):
    ctx = _client_ctx()
    if ctx is None:
        return
    text = (data or {}).get("data") or ""
    if not text:
        return
    _handle_session_input(ctx, text)


@_event("terminal:resize")
def on_resize(data=None):
    ctx = _client_ctx()
    if ctx is None:
        return
    opened = ctx.get("opened")
    if opened is None:
        return
    payload = data or {}
    cols = int(payload.get("cols") or 80)
    rows = int(payload.get("rows") or 24)
    # AI 的 Markdown 按终端宽度重排，所以记住当前列数
    ctx["cols"] = max(cols, 20)
    try:
        opened.bridge.resize(cols, rows)
    except Exception:  # noqa: BLE001
        pass


@_event("terminal:close")
def on_close(data=None):
    ctx = _client_ctx()
    if ctx is None:
        return
    _close_session(ctx, reason="用户主动关闭终端")
    emit("terminal:closed", {"reason": "已断开"})


@_event("terminal:ping")
def on_ping(data=None):
    emit("terminal:pong", {"ts": time.time()})


def register_events(app):
    """把 webterm 事件处理器挂到**当前** socketio server 上，并返回注册数量。

    为什么不能只在模块顶层用 socketio.on 装饰器：flask_socketio 的
    ``SocketIO.on`` 在 ``self.server`` 已存在时会**直接注册到那个 server 对象**上，
    而 ``init_app()`` 每次调用都会重建 ``socketio.server``。结果是同一个进程里
    第二次 ``create_app``（测试、脚本、多 app 部署）拿到的 server 上
    **一个 webterm 处理器都没有**：连接能建立（manager 层握手成功），
    但 ``terminal:ready`` 等事件永远不会下发，表现为「终端连上了却什么都不响」。
    所以装饰器只负责登记，真正注册统一放在这里，由 ``create_app`` 在
    ``socketio.init_app(app)`` 之后调用。
    """
    for name, handler in _EVENTS:
        socketio.on(name)(handler)
    log.debug("webterm events registered for %s (%d handlers)", app.name, len(_EVENTS))
    return len(_EVENTS)


def online_sids() -> dict:
    with _CLIENTS_LOCK:
        return {sid: {"username": ctx["username"], "sid": (ctx.get("opened").sid if ctx.get("opened") else "")} for sid, ctx in _CLIENTS.items()}


_ = registry
