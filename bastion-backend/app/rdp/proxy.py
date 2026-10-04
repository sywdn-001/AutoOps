"""WebRDP 网关：浏览器 ←WebSocket→ 本模块 ←TLS→ Windows 目标机。

实现的是 IronRDP 的 **RDCleanPath** 引导协议（与 ironrdp-wasm 的 `lib/rdp-proxy.js` 同语义）：

1. 游客（浏览器）先 `POST /api/rdp/sessions` 换一张**一次性票据**（短 TTL、取用即删），
   WebSocket 只带票据 id，不带任何凭据；
2. 连上 `/api/rdp/ws?ticket=…` 后，客户端发一条 RDCleanPath 请求 PDU；
3. 本模块替它跟目标机做完 **TCP + X.224 + TLS**，把 X.224 Connection Confirm 与服务器
   证书链包成响应 PDU 回给客户端；
4. 之后这条 WebSocket 只做 TLS 字节的双向中继 —— **NLA/CredSSP 由浏览器端客户端在
   这条隧道里自己跑**，所以账号口令在 CredSSP 里已经被目标机公钥加密，网关（我们）
   在链路上只看得到 TLS 密文，既不需要也不应该持有 RDP 口令。

选这个方案而不是 Node + node-rdpjs（参考项目 D:\\Desktop\\WebRemote 的路子）是因为
实测目标机 192.168.0.75:3389 对 `PROTOCOL_SSL` / `PROTOCOL_RDP` 一律回
`RDP_NEG_FAILURE(5) = HYBRID_REQUIRED_BY_SERVER`，只有 NLA-capable 的客户端能连上，
而 node-rdpjs 不支持 NLA。

另外：浏览器里的 ironrdp-wasm 会请求 `PROTOCOL_HYBRID_EX`，目标机也照实回
`selectedProtocol = 0x08`，所以 `TLS_CAPABLE_PROTOCOLS` 必须把 HYBRID_EX 一起认下来，
否则真实机器一律在协商阶段就被我们自己的网关否掉。
"""

from __future__ import annotations

import logging
import select
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import simple_websocket

from ..audit import close_session, log_event, new_session
from .cleanpath import (
    ERROR_GENERAL,
    ERROR_NEGOTIATION,
    CleanPathError,
    build_error,
    build_response,
    parse_request,
    parse_x224_negotiation,
)

logger = logging.getLogger(__name__)

#: 票据有效期（秒）：从换票到 WebSocket 连上之间的窗口，越短越安全
TICKET_TTL_SECONDS = 120
#: 与目标机握手（TCP + X.224 + TLS）的超时
HANDSHAKE_TIMEOUT = 15
#: 等待客户端首条 RDCleanPath 请求的超时
REQUEST_TIMEOUT = 15
#: 单次中继读取的字节数
RELAY_BUFFER = 65536
#: 中继里「等目标机来数据」的轮询粒度（秒）。见 `relay()` 的线程安全说明：
#: 读线程在**锁外**用 select 等可读，只在真正 recv 的瞬间持锁，所以这里取小值
#: 只影响「目标机静默时」的唤醒频率（20 次/秒），不会给界面输入引入延迟。
RELAY_POLL_SECONDS = 0.05
#: X.224 协商选中的协议号
PROTOCOL_SSL = 0x00000001
PROTOCOL_HYBRID = 0x00000002
PROTOCOL_HYBRID_EX = 0x00000008
PROTOCOL_RDP = 0x00000000

NEGOTIATED_NAMES = {
    PROTOCOL_RDP: "PROTOCOL_RDP（标准 RDP 安全层，非 TLS）",
    PROTOCOL_SSL: "PROTOCOL_SSL（TLS）",
    PROTOCOL_HYBRID: "PROTOCOL_HYBRID（NLA / CredSSP）",
    PROTOCOL_HYBRID_EX: "PROTOCOL_HYBRID_EX（NLA / CredSSP + 登录前结果报错，Windows 8+）",
}

#: 允许继续走 TLS 隧道中继的安全层。HYBRID_EX 只是 HYBRID 的超集（多一个
#: Early User Authorization Result），TLS 之后的一切仍由浏览器端客户端自己完成，
#: 网关不需要、也不应该关心 CredSSP 细节。
TLS_CAPABLE_PROTOCOLS = (PROTOCOL_SSL, PROTOCOL_HYBRID, PROTOCOL_HYBRID_EX)


#: 目标机主动断开时各平台给的错误码（Windows WinError / POSIX errno）
_REMOTE_CLOSED_CODES = frozenset({32, 10053, 10054, 104})
#: 端口没人听：远程桌面服务没启动 / 被防火墙拒了
_REMOTE_REFUSED_CODES = frozenset({10061, 111})
#: 连不上又没被明确拒绝：网络不通或对方没响应
_REMOTE_TIMEOUT_CODES = frozenset({10060, 110})


def humanize_socket_error(exc: BaseException) -> str:
    """把套接字异常翻成人话 —— 这段文字会直接出现在「会话记录」的结束原因和审计里。"""
    text = str(exc).strip()
    code = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
    lowered = text.lower()
    if code in _REMOTE_CLOSED_CODES or any(
        key in lowered
        for key in (
            "forcibly closed",
            "broken pipe",
            "reset by peer",
            "connection reset",
            "connection aborted",
        )
    ):
        return "目标主机断开了连接（对方可能重启、注销或网络中断）"
    if code in _REMOTE_REFUSED_CODES or "refused" in lowered:
        return "目标主机拒绝了连接（远程桌面服务可能没启动，或端口不通）"
    if code in _REMOTE_TIMEOUT_CODES or "timed out" in lowered:
        return "连接目标主机超时（网络不通或对方没有响应）"
    if "certificate" in lowered:
        return "目标主机的证书校验没通过"
    return text or exc.__class__.__name__


class RdpProxyError(RuntimeError):
    """网关侧可预期的失败（要变成人话报错 + 审计）。"""


# --------------------------------------------------------------------------- #
# 一次性票据
# --------------------------------------------------------------------------- #
@dataclass
class RdpTicket:
    """一次 WebRDP 连接的授权凭据（不含任何目标机口令）。"""

    id: str
    user_id: int
    username: str
    role_code: str
    host_id: int
    host_name: str
    host_address: str
    port: int
    account_id: int | None
    account_username: str
    grant_id: int | None
    client_ip: str
    created_at: float
    expires_at: float
    used: bool = False
    used_at: float | None = None
    meta: dict = field(default_factory=dict)

    def matches(self, host: str, port: int) -> bool:
        # 主机名大小写不敏感（DNS 语义如此）：客户端把 destination 原样回传，
        # 大小写差异不该把合法连接判成越权；IP 字面量走同一路径也不受影响。
        return (
            host.strip().lower() == self.host_address.strip().lower()
            and int(port) == int(self.port)
        )

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at


class RdpTicketStore:
    """内存票据表：单次使用 + TTL。进程内足够（WebRDP 会话不跨进程）。"""

    def __init__(self, ttl: int = TICKET_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, RdpTicket] = {}
        self.ttl = ttl

    def issue(self, **kwargs: Any) -> RdpTicket:
        now = time.time()
        ticket = RdpTicket(
            id=uuid.uuid4().hex,
            created_at=now,
            expires_at=now + self.ttl,
            **kwargs,
        )
        with self._lock:
            self._purge_locked(now)
            self._tickets[ticket.id] = ticket
        return ticket

    def consume(self, ticket_id: str) -> RdpTicket | None:
        """取用即删：同一张票只能换一条 WebSocket。"""
        if not ticket_id:
            return None
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            ticket = self._tickets.pop(ticket_id, None)
            if ticket is None:
                return None
            ticket.used = True
            ticket.used_at = now
            return ticket

    def peek(self, ticket_id: str) -> RdpTicket | None:
        with self._lock:
            self._purge_locked(time.time())
            return self._tickets.get(ticket_id)

    def _purge_locked(self, now: float) -> None:
        dead = [key for key, item in self._tickets.items() if item.expires_at <= now]
        for key in dead:
            self._tickets.pop(key, None)

    def size(self) -> int:
        with self._lock:
            self._purge_locked(time.time())
            return len(self._tickets)

    def clear(self) -> None:
        with self._lock:
            self._tickets.clear()


#: 进程级票据表（测试里可以直接 clear()）
TICKETS = RdpTicketStore()


# --------------------------------------------------------------------------- #
# 握手：TCP + X.224 + TLS
# --------------------------------------------------------------------------- #
def perform_handshake(
    host: str,
    port: int,
    x224_request: bytes,
    *,
    timeout: int = HANDSHAKE_TIMEOUT,
) -> tuple[bytes, list[bytes], ssl.SSLSocket]:
    """替客户端跟目标机做 TCP + X.224 + TLS，返回 (X.224 响应, 证书链, TLS 套接字)。"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise RdpProxyError(
            f"连接目标主机 {host}:{port} 失败：{humanize_socket_error(exc)}"
        ) from exc
    try:
        sock.settimeout(timeout)
        sock.sendall(x224_request)
        response = sock.recv(4096)
        if not response:
            raise RdpProxyError(
                f"目标主机 {host}:{port} 没有回应协商请求（连接被对方关闭，端口可能不通）"
            )
        negotiation = parse_x224_negotiation(response)
        if negotiation.get("type") == "RDP_NEG_FAILURE":
            name = negotiation.get("failureName", "")
            hint = ""
            if name == "HYBRID_REQUIRED_BY_SERVER":
                hint = "（目标机要求 NLA/CredSSP，浏览器端 WASM 客户端支持；若仍失败请检查账号口令）"
            raise RdpProxyError(
                f"目标主机 {host}:{port} 不接受当前安全层（{name}，失败码 {negotiation.get('failureCode')}）{hint}"
            )
        selected = negotiation.get("selectedProtocol")
        if selected is None:
            raise RdpProxyError(
                f"目标主机 {host}:{port} 的协商响应里没有安全层结果（{negotiation.get('type')}）"
            )
        if selected == PROTOCOL_RDP:
            raise RdpProxyError(
                f"目标主机 {host}:{port} 要求不带 TLS 的标准 RDP 安全层（{NEGOTIATED_NAMES[PROTOCOL_RDP]}），"
                "网页远程桌面只能走 TLS；请在目标机上启用「仅允许运行使用网络级别身份验证的远程桌面的计算机连接」或改用 SSL"
            )
        if selected not in TLS_CAPABLE_PROTOCOLS:
            raise RdpProxyError(f"目标主机 {host}:{port} 返回了无法识别的安全层（0x{int(selected):08x}）")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # RDP 目标机普遍自签，证书由客户端校验
        try:
            tls = context.wrap_socket(sock, server_hostname=host)
        except ssl.SSLError as exc:
            raise RdpProxyError(
                f"与目标主机 {host}:{port} 的 TLS 握手失败：{humanize_socket_error(exc)}"
            ) from exc
        der = tls.getpeercert(binary_form=True)
        chain = [der] if der else []
        tls.settimeout(None)
        return response, chain, tls
    except Exception:
        try:
            sock.close()
        except OSError:  # pragma: no cover
            pass
        raise


def server_cert_info(chain: list[bytes]) -> dict:
    """尽力解析服务器证书（只为审计/排障，失败不影响连接）。"""
    if not chain:
        return {}
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(chain[0])
        return {
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "serial": hex(cert.serial_number),
            "notAfter": cert.not_valid_after_utc.isoformat(),
            "selfSigned": cert.subject == cert.issuer,
        }
    except Exception as exc:  # pragma: no cover - 只影响审计细节
        logger.debug("解析服务器证书失败：%s", exc)
        return {}


# --------------------------------------------------------------------------- #
# 双向中继
# --------------------------------------------------------------------------- #
def relay(
    ws: simple_websocket.Server,
    tls_socket: ssl.SSLSocket,
    *,
    on_error: Callable[[str], None] | None = None,
    stop: threading.Event | None = None,
    on_activity: Callable[[], None] | None = None,
) -> dict:
    """把 WebSocket 与 TLS 套接字对接起来，返回双向字节数。

    **`ssl.SSLSocket` 不能被两个线程同时碰**：底下的 OpenSSL `SSL` 对象不是线程安全的，
    读线程与写线程并发进入会踩坏内部状态 —— 表现是「`sendall()` 明明返回成功、字节却没
    上线」，目标机永远等不到数据。所以这里**只用当前这一个线程**管两个方向，一把锁都不加。

    中间还试过「读线程 + `select()` 等可读 + `io_lock` 串行化」：真机正常，但测试抓到了
    死锁 —— **`select()` 说「可读」并不代表 `recv()` 不会阻塞**：TLS 里还会有握手后的记录
    （`NewSessionTicket` 之类）等着 OpenSSL 处理，把这些记录吃掉之后如果还没有应用数据，
    阻塞模式下的 `recv()` 就挂住了；而此时读线程**正握着 `io_lock`**，写线程永远等不到锁，
    于是两个方向一起死。`tests/test_rdp_gateway.py` 的看门狗打出的线程栈就是铁证：
    `rdp-relay` 线程停在 `recv` 里、主线程停在抢锁那一行。

    所以这里把 TLS 套接字切到**非阻塞**：`recv()` 只认 `SSLWantReadError`（没数据就先回去
    处理 WebSocket 方向），`send()` 遇到 `SSLWantWriteError` 就 `select` 等可写再接着写。
    全程没有任何一处会长时间阻塞，也就不需要锁；`ws.receive(timeout=…)` 是唯一会等待的调用，
    但它最多等 `RELAY_POLL_SECONDS`。

    两个可选参数：
    * ``stop``：外部（管理员在「会话记录」里点中断、空闲清理线程）置位后，中继会在
      一个轮询周期内退出，结束原因由调用方改写成「管理员强制中断」这类人话；
    * ``on_activity``：有字节流动时回调一次，用来喂在线会话表的 ``last_active``，
      否则一条正在被人使用的远程桌面会被空闲清理当成僵尸会话掐掉。
    """
    stop = stop or threading.Event()
    counters = {"fromClient": 0, "fromServer": 0, "reason": ""}
    previous_timeout = tls_socket.gettimeout()
    tls_socket.setblocking(False)

    def read_from_target() -> bytes | None:
        """非阻塞读一次目标机：返回 bytes（`b""` = 对端已关闭）或 None（暂时没数据）。"""
        try:
            return tls_socket.recv(RELAY_BUFFER)
        except (ssl.SSLWantReadError, BlockingIOError):
            return None

    def write_to_target(payload: bytes) -> None:
        """把客户端的字节写完（非阻塞套接字上续写，遇忙就等可写）。"""
        view = memoryview(payload)
        while view and not stop.is_set():
            try:
                written = tls_socket.send(view)
            except (ssl.SSLWantWriteError, BlockingIOError):
                select.select([], [tls_socket], [], RELAY_POLL_SECONDS)
                continue
            view = view[written:]

    try:
        while not stop.is_set():
            # 客户端 → 目标机：receive(timeout) 到点就返回 None，不会无限阻塞
            message = ws.receive(timeout=RELAY_POLL_SECONDS)
            if message is not None:
                if isinstance(message, str):
                    message = message.encode("utf-8")
                if message:
                    try:
                        write_to_target(message)
                    except OSError as exc:
                        counters["reason"] = counters["reason"] or (
                            f"向目标主机发送数据失败：{humanize_socket_error(exc)}"
                        )
                        break
                    counters["fromClient"] += len(message)
                    if on_activity:
                        on_activity()
            # 目标机 → 客户端：先把已经解密好的数据搬完，再回去看 WebSocket 方向
            while not stop.is_set():
                try:
                    data = read_from_target()
                except OSError as exc:
                    counters["reason"] = counters["reason"] or (
                            f"与目标主机的连接中断：{humanize_socket_error(exc)}"
                        )
                    stop.set()
                    break
                if data is None:
                    break
                if not data:
                    counters["reason"] = counters["reason"] or "目标主机结束了远程桌面会话"
                    stop.set()
                    break
                ws.send(data)
                counters["fromServer"] += len(data)
                if on_activity:
                    on_activity()
    except simple_websocket.ConnectionClosed:
        counters["reason"] = counters["reason"] or "浏览器侧关闭了窗口"
    except OSError as exc:
        counters["reason"] = counters["reason"] or (
                            f"远程桌面通道异常：{humanize_socket_error(exc)}"
                        )
    except Exception as exc:  # pragma: no cover - 保底
        counters["reason"] = counters["reason"] or (
                            f"远程桌面通道异常：{humanize_socket_error(exc)}"
                        )
        logger.debug("RDP 中继异常", exc_info=True)
    finally:
        stop.set()
        try:
            tls_socket.settimeout(previous_timeout)
        except OSError:  # pragma: no cover - 套接字已经关了
            pass
        try:
            tls_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            tls_socket.close()
        except OSError:
            pass
        if on_error and counters["reason"]:
            on_error(counters["reason"])
    return counters


# --------------------------------------------------------------------------- #
# 连接主流程
# --------------------------------------------------------------------------- #
@dataclass
class RdpConnectionHooks:
    """把「写库/审计」这类需要 app context 的动作注入进来，方便单测替换。"""

    open_session: Callable[[RdpTicket, dict], Any] | None = None
    close_session: Callable[[Any, dict], None] | None = None
    audit: Callable[..., None] | None = None


class _StopRequest:
    """外部要求断开这条隧道时的开关（管理员点「中断」/ 空闲清理线程调用）。

    `session_registry.close()` 会调 `request(reason)`：置位事件让中继循环在一个轮询周期内
    退出，并顺手关掉 WebSocket，让浏览器那边立刻显示「会话已被中断」。
    """

    def __init__(self, ws: simple_websocket.Server) -> None:
        self.event = threading.Event()
        self.requested = False
        self.reason = ""
        self._ws = ws

    def request(self, reason: str = "") -> None:
        self.requested = True
        self.reason = (reason or "").strip() or "会话已被中断"
        self.event.set()
        _safe_close(self._ws, 1000, self.reason)


def _session_sid(session: Any) -> str:
    """从 `open_session()` 的返回值里取会话 sid（兼容历史上只回行 id 的写法）。"""
    if isinstance(session, dict):
        return str(session.get("sid") or "")
    return str(getattr(session, "sid", "") or "")


def _register_live_session(sid: str, ticket: RdpTicket, stop_request: _StopRequest) -> None:
    """登记到在线会话表 —— 这样管理员才能从「会话记录」里把这条远程桌面踢掉。"""
    from ..session_registry import register

    register(
        sid,
        kind="rdp",
        user_id=ticket.user_id,
        username=ticket.username,
        host_id=ticket.host_id,
        host_name=ticket.host_name,
        host_address=f"{ticket.host_address}:{ticket.port}",
        account_username=ticket.account_username,
        grant_id=ticket.grant_id,
        client_ip=ticket.client_ip,
        protocol="rdp",
        stop=stop_request.request,
    )


def _forget_live_session(sid: str) -> None:
    from ..session_registry import unregister

    unregister(sid)


def _activity_toucher(sid: str) -> Callable[[], None] | None:
    """把「隧道里有字节流动」喂给在线会话表的 `last_active`（一秒最多碰一次锁）。

    不喂的话，`last_active` 会永远停在注册那一刻，一条正在被人用的远程桌面会被空闲清理
    当成僵尸会话掐掉。
    """
    if not sid:
        return None
    last = [0.0]

    def touch() -> None:
        now = time.monotonic()
        if now - last[0] < 1.0:
            return
        last[0] = now
        from ..session_registry import touch as registry_touch

        registry_touch(sid)

    return touch


def handle_connection(
    ws: simple_websocket.Server,
    ticket_id: str,
    *,
    hooks: RdpConnectionHooks | None = None,
) -> dict:
    """一条 WebRDP WebSocket 的完整生命周期。返回统计信息（便于测试断言）。"""
    hooks = hooks or RdpConnectionHooks()
    result: dict = {"ok": False, "ticket": ticket_id[:12], "reason": ""}

    ticket = TICKETS.consume(ticket_id)
    if ticket is None:
        result["reason"] = "连接票据无效或已过期（请回资产列表重新连接）"
        _safe_error(ws, ERROR_NEGOTIATION, 401)
        _safe_close(ws, 1008, "Invalid ticket")
        return result

    result["host"] = f"{ticket.host_address}:{ticket.port}"
    session = None
    try:
        message = ws.receive(timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        result["reason"] = f"浏览器没有把连接请求发上来（{humanize_socket_error(exc)}）"
        _safe_close(ws, 1002, "No request")
        return result
    if not isinstance(message, (bytes, bytearray)):
        result["reason"] = "浏览器发来的连接请求格式不对（不是二进制 RDCleanPath）"
        _safe_error(ws, ERROR_NEGOTIATION, 400)
        _safe_close(ws, 1003, "Binary request expected")
        return result

    try:
        request = parse_request(bytes(message))
    except CleanPathError as exc:
        result["reason"] = f"浏览器发来的连接请求无法解析（{exc}）"
        _safe_error(ws, ERROR_NEGOTIATION, 400)
        _safe_close(ws, 1003, "Bad request")
        return result

    host, port = request.host_port()
    result["destination"] = f"{host}:{port}"
    # 关键安全约束：客户端只能在票据授权的那台主机上开隧道，防止被当成任意端口转发器
    if not ticket.matches(host, port):
        result["reason"] = f"浏览器请求的目标 {host}:{port} 与票据授权的主机不一致"
        if hooks.audit:
            hooks.audit(ticket, "rdp_denied", result["reason"], result="denied")
        _safe_error(ws, ERROR_GENERAL, 403)
        _safe_close(ws, 1008, "Destination not allowed")
        return result

    try:
        x224_response, chain, tls = perform_handshake(host, port, request.x224_connection_pdu)
    except RdpProxyError as exc:
        result["reason"] = str(exc)
        if hooks.audit:
            hooks.audit(ticket, "rdp_failed", str(exc), result="failed")
        _safe_error(ws, ERROR_NEGOTIATION, 502)
        _safe_close(ws, 1011, str(exc)[:120])
        return result

    info = server_cert_info(chain)
    result["negotiation"] = parse_x224_negotiation(x224_response)
    result["serverCert"] = info

    try:
        ws.send(build_response(f"{host}:{port}", x224_response, chain))
    except Exception as exc:
        result["reason"] = f"把协商结果回给浏览器失败（{humanize_socket_error(exc)}）"
        _safe_close(ws, 1011, "Send failed")
        return result

    session = None
    sid = ""
    stop_request = _StopRequest(ws)
    if hooks.open_session:
        try:
            session = hooks.open_session(ticket, {"certificate": info, "negotiation": result["negotiation"]})
            sid = _session_sid(session)
        except Exception:  # pragma: no cover - 审计不能挡住业务
            logger.exception("写 RDP 会话记录失败 ticket=%s", ticket.id[:12])

    if sid:
        _register_live_session(sid, ticket, stop_request)

    try:
        counters = relay(
            ws,
            tls,
            stop=stop_request.event,
            on_activity=_activity_toucher(sid),
        )
    finally:
        if sid:
            _forget_live_session(sid)

    if stop_request.requested:
        # 管理员点中断 / 空闲清理：原因以调用方的说法为准 ——
        # 中继那边往往只看到「连接被重置」，说出来不像人话
        counters["reason"] = stop_request.reason
        counters["forced"] = True

    result["bytesFromClient"] = counters["fromClient"]
    result["bytesFromServer"] = counters["fromServer"]
    result["reason"] = counters["reason"]
    result["ok"] = True
    if hooks.close_session and session is not None:
        try:
            hooks.close_session(session, counters)
        except Exception:  # pragma: no cover
            logger.exception("收口 RDP 会话记录失败")
    return result


def _safe_error(ws: simple_websocket.Server, code: int, http_status: int | None = None) -> None:
    try:
        ws.send(build_error(code, http_status))
    except Exception:  # pragma: no cover - 对端可能已经走了
        pass


def _safe_close(ws: simple_websocket.Server, code: int = 1000, message: str = "") -> None:
    try:
        ws.close(reason=code, message=message[:120])
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# WSGI 中间件：把 /api/rdp/ws 的升级请求截下来
# --------------------------------------------------------------------------- #
class RdpWebSocketMiddleware:
    """在 WSGI 层面接住 `/api/rdp/ws` 的 WebSocket 升级。

    写法对照 engineio 的 `async_drivers/_websocket_wsgi.py`：握手由 simple_websocket
    直接写进 socket，**返回空列表**即可，绝不能再自己 start_response。
    必须挂在 socketio 的 WSGI 中间件**外面**（后包一层），否则 engine.io 会先判定路径。
    """

    def __init__(self, app, *, path: str = "/api/rdp/ws", hooks_factory=None) -> None:
        self.app = app
        self.path = path
        self.hooks_factory = hooks_factory

    def __call__(self, environ, start_response):
        if environ.get("HTTP_UPGRADE", "").lower() != "websocket":
            return self.app(environ, start_response)
        if (environ.get("PATH_INFO") or "") != self.path:
            return self.app(environ, start_response)

        ticket_id = _query_value(environ.get("QUERY_STRING", ""), "ticket")
        try:
            ws = simple_websocket.Server(environ)
        except Exception:  # pragma: no cover - 握手失败（非 WS 客户端）
            logger.warning("RDP WebSocket 握手失败", exc_info=True)
            start_response("400 Bad Request", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"WebSocket handshake failed"]

        hooks = self.hooks_factory() if self.hooks_factory else None
        try:
            handle_connection(ws, ticket_id or "", hooks=hooks)
        except Exception:  # pragma: no cover - 保底，别把异常冒到 WSGI
            logger.exception("RDP WebSocket 会话异常")
            _safe_close(ws, 1011, "Internal error")
        if ws.mode == "gunicorn":  # pragma: no cover - 部署在 gunicorn 下才走这里
            raise StopIteration()
        return []


def _query_value(query: str, key: str) -> str:
    from urllib.parse import parse_qs

    values = parse_qs(query or "")
    items = values.get(key) or []
    return items[0] if items else ""
