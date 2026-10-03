"""WebRDP（Windows 远程桌面）网关回归测试。

四层都要有真断言，不做「恒真的假绿」：

1. **RDCleanPath 报文**：DER/ASN.1 编解码照 ironrdp-wasm 的 example 实现对齐（版本 3390、
   字段号、错误文案）；
2. **一次性票据**：取用即删、2 分钟过期、绑定目标主机；
3. **隧道**：在进程内起一个**真 TCP + 真 TLS** 的假 RDP 服务器，把 X.224 + TLS + 双向中继
   跑通；并验证「客户端申请的目标与票据不一致」时被拒绝（否则网关就是个任意端口转发器）；
4. **REST 准入**：`rdp:use` 权限、只列 rdp 主机、授权开关与账号校验，以及网页终端入口页
   **把两类主机合并**在一张表里（靠 `protocol` 区分），而 SSH 网关/TUI 菜单里
   **仍然不出现** rdp 主机（Windows 远程桌面不进字符菜单）。
"""

from __future__ import annotations

import datetime
import faulthandler
import socket
import ssl
import sys
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.extensions import db
from app.models import AuditLog, Host
from app.rdp.cleanpath import (
    ERROR_GENERAL,
    FIELD_DESTINATION,
    FIELD_VERSION,
    FIELD_X224_CONNECTION_PDU,
    RDCLEANPATH_VERSION,
    CleanPathError,
    build_error,
    build_response,
    der_context,
    der_integer,
    der_octet_string,
    der_utf8,
    der_wrap,
    parse_destination,
    parse_request,
    parse_response,
    parse_x224_negotiation,
)
from app.rdp.proxy import (
    PROTOCOL_HYBRID,
    PROTOCOL_HYBRID_EX,
    RdpConnectionHooks,
    RdpProxyError,
    RdpTicketStore,
    TICKETS,
    TLS_CAPABLE_PROTOCOLS,
    handle_connection,
    perform_handshake,
    server_cert_info,
)
from tests.conftest import token_of

# 客户端 X.224 Connection Request（请求 PROTOCOL_HYBRID），与真实探测一致
X224_CR = bytes.fromhex("030000130ee000000000000100080002000000")
# 服务器 X.224 Connection Confirm + RDP_NEG_RSP(selectedProtocol=2 HYBRID)
X224_CC_HYBRID = bytes.fromhex("030000130ed00000123400022f080002000000")
# 服务器 X.224 CC + RDP_NEG_RSP(selectedProtocol=8 HYBRID_EX)：浏览器里的 ironrdp-wasm
# 会请求 HYBRID_EX，真实机器（192.168.0.75）就是回这个，网关必须认
X224_CC_HYBRID_EX = bytes.fromhex("030000130ed00000123400022f080008000000")
# 服务器 X.224 CC + RDP_NEG_FAILURE(failureCode=5 HYBRID_REQUIRED_BY_SERVER)
X224_CC_FAILURE = bytes.fromhex("030000130ed000001234000300080005000000")

# 隧道用例的看门狗上限（秒）：正常一次中继是毫秒级，给 30 秒只是「不许无限挂住」
TUNNEL_DEADLINE_SECONDS = 30.0


def build_request(
    destination: str,
    x224: bytes = X224_CR,
    *,
    version: int = RDCLEANPATH_VERSION,
    proxy_auth: str | None = None,
) -> bytes:
    """手搭一个 RDCleanPath 请求报文（网关侧只需要解析，不提供 encoder）。"""
    parts = [
        der_context(FIELD_VERSION, der_integer(version)),
        der_context(FIELD_DESTINATION, der_utf8(destination)),
        der_context(FIELD_X224_CONNECTION_PDU, der_octet_string(x224)),
    ]
    if proxy_auth is not None:
        parts.append(der_context(3, der_utf8(proxy_auth)))
    return der_wrap(0x30, b"".join(parts))


# --------------------------------------------------------------------------- #
# 1. 报文编解码
# --------------------------------------------------------------------------- #
def test_request_pdu_round_trip():
    pdu = build_request("10.0.0.8:3389", proxy_auth="opaque-token")
    request = parse_request(pdu)
    assert request.destination == "10.0.0.8:3389"
    assert request.version == RDCLEANPATH_VERSION
    assert request.x224_connection_pdu == X224_CR
    assert request.proxy_auth == "opaque-token"
    assert request.host_port() == ("10.0.0.8", 3389)


def test_unsupported_version_is_rejected():
    pdu = build_request("10.0.0.8:3389", version=3389)
    with pytest.raises(CleanPathError) as excinfo:
        parse_request(pdu)
    # 文案与 ironrdp-wasm 的参考实现逐字一致，便于对照排查
    assert str(excinfo.value) == "Unsupported RDCleanPath version: 3389"


def test_missing_destination_is_rejected():
    parts = [
        der_context(FIELD_VERSION, der_integer(RDCLEANPATH_VERSION)),
        der_context(FIELD_X224_CONNECTION_PDU, der_octet_string(X224_CR)),
    ]
    with pytest.raises(CleanPathError) as excinfo:
        parse_request(der_wrap(0x30, b"".join(parts)))
    assert str(excinfo.value) == "Missing destination in RDCleanPath request"


def test_missing_x224_pdu_is_rejected():
    parts = [
        der_context(FIELD_VERSION, der_integer(RDCLEANPATH_VERSION)),
        der_context(FIELD_DESTINATION, der_utf8("10.0.0.8")),
    ]
    with pytest.raises(CleanPathError) as excinfo:
        parse_request(der_wrap(0x30, b"".join(parts)))
    assert str(excinfo.value) == "Missing x224_connection_pdu in RDCleanPath request"


def test_non_sequence_request_is_rejected():
    with pytest.raises(CleanPathError):
        parse_request(der_integer(3390))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.0.0.8", ("10.0.0.8", 3389)),
        ("10.0.0.8:3390", ("10.0.0.8", 3390)),
        ("win-01.corp.local:3389", ("win-01.corp.local", 3389)),
        ("[fe80::1]:3389", ("fe80::1", 3389)),
        ("[fe80::1]", ("fe80::1", 3389)),
        ("fe80::1", ("fe80::1", 3389)),
    ],
)
def test_parse_destination_variants(text, expected):
    assert parse_destination(text) == expected


def test_response_round_trip_carries_cert_chain():
    cert = b"\x30\x03\x02\x01\x01"
    pdu = build_response("10.0.0.8:3389", X224_CC_HYBRID, [cert, b"second"])
    parsed = parse_response(pdu)
    assert parsed["version"] == RDCLEANPATH_VERSION
    assert parsed["x224ConnectionPdu"] == X224_CC_HYBRID
    assert parsed["serverCertChain"] == [cert, b"second"]
    assert parsed["serverAddr"] == "10.0.0.8:3389"


def test_error_pdu_carries_code_and_http_status():
    parsed = parse_response(build_error(ERROR_GENERAL, 403))
    assert parsed["version"] == RDCLEANPATH_VERSION
    assert parsed["error"]["errorCode"] == ERROR_GENERAL
    assert parsed["error"]["httpStatusCode"] == 403
    # 不带 http 状态时只留错误码
    plain = parse_response(build_error(2))
    assert plain["error"] == {"errorCode": 2}


def test_parse_x224_negotiation_understands_hybrid_and_failure():
    hybrid = parse_x224_negotiation(X224_CC_HYBRID)
    assert hybrid["type"] == "RDP_NEG_RSP"
    assert hybrid["selectedProtocol"] == PROTOCOL_HYBRID

    failure = parse_x224_negotiation(X224_CC_FAILURE)
    assert failure["type"] == "RDP_NEG_FAILURE"
    assert failure["failureCode"] == 5
    assert failure["failureName"] == "HYBRID_REQUIRED_BY_SERVER"

    assert parse_x224_negotiation(b"\x03\x00")["error"] == "X.224 响应过短"


def test_parse_x224_negotiation_tolerates_big_endian_length():
    """真机回的是小端 `08 00`；历史上也有大端 `00 08` 的写法，两种都要认得。"""
    big_endian = bytes.fromhex("030000130ed000001234000200080002000000")
    parsed = parse_x224_negotiation(big_endian)
    assert parsed["type"] == "RDP_NEG_RSP"
    assert parsed["selectedProtocol"] == PROTOCOL_HYBRID


# --------------------------------------------------------------------------- #
# 2. 一次性票据
# --------------------------------------------------------------------------- #
def _issue(**overrides):
    payload = {
        "user_id": 1,
        "username": "admin",
        "role_code": "admin",
        "host_id": 7,
        "host_name": "win-01",
        "host_address": "10.0.0.8",
        "port": 3389,
        "account_id": 3,
        "account_username": "Administrator",
        "grant_id": 5,
        "client_ip": "127.0.0.1",
    }
    payload.update(overrides)
    return payload


def test_ticket_is_single_use():
    store = RdpTicketStore(ttl=120)
    ticket = store.issue(**_issue())
    assert store.size() == 1
    assert store.peek(ticket.id) is not None

    taken = store.consume(ticket.id)
    assert taken is not None and taken.id == ticket.id
    # 取用即删：第二次拿不到
    assert store.consume(ticket.id) is None
    assert store.size() == 0


def test_ticket_expires():
    store = RdpTicketStore(ttl=0)
    ticket = store.issue(**_issue())
    assert ticket.expired() is True
    assert store.consume(ticket.id) is None


def test_ticket_is_bound_to_host_and_port():
    store = RdpTicketStore(ttl=120)
    ticket = store.issue(**_issue())
    assert ticket.matches("10.0.0.8", 3389) is True
    assert ticket.matches("10.0.0.8", 3390) is False
    assert ticket.matches("10.0.0.9", 3389) is False
    # 主机名大小写不敏感（DNS 无大小写语义）
    named = store.issue(**_issue(host_address="WIN-01.corp"))
    assert named.matches("win-01.corp", 3389) is True


# --------------------------------------------------------------------------- #
# 3. 隧道：真 TCP + 真 TLS 的假 RDP 服务器
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    directory = tmp_path_factory.mktemp("rdp-tls")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-rdp-target")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {
        "cert": str(cert_path),
        "key": str(key_path),
        "der": cert.public_bytes(serialization.Encoding.DER),
    }


class FakeRdpServer(threading.Thread):
    """假 RDP 服务器：明文收 X.224 → 回 Connection Confirm → 升 TLS → 回一次声后关闭。"""

    def __init__(self, material, *, cc: bytes = X224_CC_HYBRID, echo: bool = True):
        super().__init__(daemon=True)
        self.material = material
        self.cc = cc
        self.echo = echo
        self.error: Exception | None = None
        self.received = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]

    def run(self):  # pragma: no cover - 线程体
        try:
            conn, _ = self._sock.accept()
            self.received = conn.recv(4096)
            conn.sendall(self.cc)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.material["cert"], self.material["key"])
            tls = context.wrap_socket(conn, server_side=True)
            if self.echo:
                data = tls.recv(4096)
                if data:
                    tls.sendall(b"echo:" + data)
                tls.close()
        except Exception as exc:
            self.error = exc
        finally:
            try:
                self._sock.close()
            except OSError:
                pass


class FakeWebSocket:
    """够用的假 WebSocket：handle_connection 只用到 receive/send/close/mode。"""

    def __init__(self, messages=()):
        self.inbox = list(messages)
        self.sent: list[bytes] = []
        self.closed: list[tuple[int | None, str]] = []
        self.mode = "threading"
        self.connected = True

    def receive(self, timeout=None):
        if not self.inbox:
            # 真实的 simple_websocket.receive(timeout) 会**阻塞**到超时或来消息；
            # 这里必须让出极小一段时间，否则中继主循环会变成不退让的忙等（真机没这问题），
            # 既烧 CPU 又会放大竞态窗口。
            time.sleep(0.005)
            return None
        item = self.inbox.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, data):
        self.sent.append(data if isinstance(data, bytes) else bytes(data))

    def close(self, reason=None, message=""):
        self.closed.append((reason, message))
        self.connected = False


def test_perform_handshake_completes_x224_and_tls(tls_material):
    server = FakeRdpServer(tls_material)
    server.start()
    try:
        response, chain, tls = perform_handshake("127.0.0.1", server.port, X224_CR, timeout=5)
    finally:
        server.join(timeout=5)
    assert response == X224_CC_HYBRID
    assert server.received == X224_CR  # 网关是把客户端的 X.224 原样转发过去的
    assert chain and chain[0].startswith(b"\x30")  # DER SEQUENCE
    info = server_cert_info(chain)
    assert info["subject"].endswith("CN=fake-rdp-target")
    assert info["selfSigned"] is True
    assert tls.version() is not None
    tls.close()


def test_perform_handshake_accepts_hybrid_ex(tls_material):
    """真实机器回的是 selectedProtocol=8（HYBRID_EX）——浏览器端的 ironrdp-wasm 请求它，
    所以网关必须把它当成「可走 TLS 的 NLA」而不是未知安全层（曾经的线上故障：
    真实机器一律在协商阶段被判成 0x00000008 未知安全层，WebRDP 永远连不上）。"""
    assert PROTOCOL_HYBRID_EX in TLS_CAPABLE_PROTOCOLS
    server = FakeRdpServer(tls_material, cc=X224_CC_HYBRID_EX)
    server.start()
    try:
        response, chain, tls = perform_handshake("127.0.0.1", server.port, X224_CR, timeout=5)
    finally:
        server.join(timeout=5)
    assert parse_x224_negotiation(response)["selectedProtocol"] == PROTOCOL_HYBRID_EX
    assert chain
    tls.close()


def test_perform_handshake_reports_nla_required(tls_material):
    server = FakeRdpServer(tls_material, cc=X224_CC_FAILURE)
    server.start()
    try:
        with pytest.raises(RdpProxyError) as excinfo:
            perform_handshake("127.0.0.1", server.port, X224_CR, timeout=5)
    finally:
        server.join(timeout=5)
    message = str(excinfo.value)
    assert "HYBRID_REQUIRED_BY_SERVER" in message
    assert "NLA" in message  # 报错要能直接告诉运维「目标机要求 NLA」


def test_perform_handshake_reports_unreachable_host():
    # 关掉的端口：必须抛 RdpProxyError（而不是裸 OSError 冒到 WSGI）
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(RdpProxyError) as excinfo:
        perform_handshake("127.0.0.1", port, X224_CR, timeout=2)
    assert "失败" in str(excinfo.value)


def test_tunnel_relays_bytes_and_calls_hooks(tls_material):
    """双向中继真的把字节送到对端，并且开 / 关会话的钩子都要被调到。

    这里用**工作线程 + `join(timeout)`** 兜底：仓库里没有 pytest-timeout，中继一旦卡死，
    整个测试会话会被无限挂住（CI 里最难查的一种失败）。超时就带着现场证据失败：
    目标机收到了什么、报了什么错、WebSocket 发过什么，并把**所有线程的栈**打到 stderr
    （"谁在等谁"一眼可见，不用再靠运气复现第二次）。
    """
    server = FakeRdpServer(tls_material)
    server.start()
    TICKETS.clear()
    box: dict = {}
    try:
        ticket = TICKETS.issue(**_issue(host_address="127.0.0.1", port=server.port))
        ws = FakeWebSocket([build_request(f"127.0.0.1:{server.port}"), b"PING"])
        opened: list = []
        closed: list = []
        hooks = RdpConnectionHooks(
            open_session=lambda item, meta: opened.append((item.host_name, meta)) or {"sid": "s-1"},
            close_session=lambda record, counters: closed.append((record, counters)),
            audit=lambda *a, **k: None,
        )

        def handle() -> None:
            box["result"] = handle_connection(ws, ticket.id, hooks=hooks)

        worker = threading.Thread(target=handle, name="rdp-handle-connection", daemon=True)
        worker.start()
        worker.join(timeout=TUNNEL_DEADLINE_SECONDS)
        if worker.is_alive():
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            pytest.fail(
                f"中继卡死：handle_connection 在 {TUNNEL_DEADLINE_SECONDS}s 内没有返回。"
                f" 目标机收到={getattr(server, 'received', None)!r}"
                f" 目标机错误={getattr(server, 'error', None)!r}"
                f" WebSocket 已发={ws.sent!r}"
                f" WebSocket 已关={ws.closed!r}"
            )
        result = box["result"]
    finally:
        server.join(timeout=5)

    assert result["ok"] is True
    assert result["negotiation"]["selectedProtocol"] == PROTOCOL_HYBRID
    assert result["serverCert"]["subject"].endswith("CN=fake-rdp-target")
    # 第一条必须是引导响应，第二条是目标机回声（证明中继真的在跑）
    assert parse_response(ws.sent[0])["serverCertChain"], "引导响应必须带服务器证书"
    assert ws.sent[1] == b"echo:PING"
    assert result["bytesFromClient"] == 4
    assert result["bytesFromServer"] == 9
    assert opened and opened[0][0] == "win-01"
    assert opened[0][1]["certificate"]["selfSigned"] is True
    assert closed and closed[0][0] == {"sid": "s-1"}
    assert closed[0][1]["fromClient"] == 4
    # 票据用完即焚
    assert TICKETS.peek(ticket.id) is None


def test_handle_connection_rejects_unknown_ticket():
    TICKETS.clear()
    ws = FakeWebSocket([build_request("127.0.0.1:3389")])
    result = handle_connection(ws, "not-a-ticket")
    assert result["ok"] is False
    assert parse_response(ws.sent[0])["error"] == {"errorCode": 2, "httpStatusCode": 401}
    assert ws.closed[-1][0] == 1008


def test_handle_connection_rejects_destination_mismatch():
    """关键安全约束：票据只能连它授权的那台机器，否则网关就成了任意端口转发器。"""
    TICKETS.clear()
    ticket = TICKETS.issue(**_issue(host_address="10.0.0.8", port=3389))
    audits: list = []
    ws = FakeWebSocket([build_request("127.0.0.1:445")])
    result = handle_connection(
        ws,
        ticket.id,
        hooks=RdpConnectionHooks(
            audit=lambda item, action, message, **kw: audits.append((action, kw))
        ),
    )
    assert result["ok"] is False
    assert "不一致" in result["reason"]
    assert parse_response(ws.sent[0])["error"] == {"errorCode": 1, "httpStatusCode": 403}
    assert ws.closed[-1][0] == 1008
    assert audits and audits[0][0] == "rdp_denied"
    assert audits[0][1]["result"] == "denied"


def test_handle_connection_rejects_non_binary_first_message():
    TICKETS.clear()
    ticket = TICKETS.issue(**_issue())
    ws = FakeWebSocket(["I am not a binary PDU"])
    result = handle_connection(ws, ticket.id)
    assert result["ok"] is False
    assert parse_response(ws.sent[0])["error"]["httpStatusCode"] == 400
    assert ws.closed[-1][0] == 1003


def test_handle_connection_rejects_broken_pdu():
    TICKETS.clear()
    ticket = TICKETS.issue(**_issue())
    ws = FakeWebSocket([b"\x30\x03\x02\x01\x01"])
    result = handle_connection(ws, ticket.id)
    assert result["ok"] is False
    assert parse_response(ws.sent[0])["error"]["httpStatusCode"] == 400


def test_handle_connection_audits_handshake_failure(tls_material):
    server = FakeRdpServer(tls_material, cc=X224_CC_FAILURE)
    server.start()
    TICKETS.clear()
    try:
        ticket = TICKETS.issue(**_issue(host_address="127.0.0.1", port=server.port))
        audits: list = []
        ws = FakeWebSocket([build_request(f"127.0.0.1:{server.port}")])
        result = handle_connection(
            ws,
            ticket.id,
            hooks=RdpConnectionHooks(
                audit=lambda item, action, message, **kw: audits.append((action, message, kw))
            ),
        )
    finally:
        server.join(timeout=5)
    assert result["ok"] is False
    assert parse_response(ws.sent[0])["error"]["httpStatusCode"] == 502
    assert audits and audits[0][0] == "rdp_failed"
    assert "HYBRID_REQUIRED_BY_SERVER" in audits[0][1]


# --------------------------------------------------------------------------- #
# 4. REST 准入与终端菜单过滤
# --------------------------------------------------------------------------- #
def test_rdp_targets_requires_the_permission(client, admin_headers, make_user):
    make_user("no-rdp", role_code="viewer")
    visitor = token_of(client, "no-rdp", "User1234")
    denied = client.get("/api/rdp/targets", headers={"Authorization": f"Bearer {visitor}"})
    assert denied.status_code == 403
    assert denied.get_json()["message"] == "权限不足，需要：rdp:use"

    allowed = client.get("/api/rdp/targets", headers=admin_headers)
    assert allowed.status_code == 200
    assert allowed.get_json()["success"] is True


def test_rdp_targets_only_lists_rdp_hosts(client, admin_headers, make_host):
    ssh_id = make_host(name="linux-01", address="10.0.0.1", port=22, protocol="ssh")
    rdp_id = make_host(
        name="win-01", address="10.0.0.75", port=3389, protocol="rdp", os_type="windows"
    )
    body = client.get("/api/rdp/targets", headers=admin_headers).get_json()
    ids = [item["hostId"] for item in body["data"]]
    assert rdp_id in ids
    assert ssh_id not in ids
    entry = next(item for item in body["data"] if item["hostId"] == rdp_id)
    assert entry["protocol"] == "rdp"
    assert entry["osType"] == "windows"


def test_create_session_issues_a_ticket_and_audits_it(
    app, client, admin_headers, make_host, make_account
):
    host_id = make_host(
        name="win-02", address="10.0.0.86", port=3389, protocol="rdp", os_type="windows"
    )
    account_id = make_account(host_id, name="Administrator", username="Administrator")
    resp = client.post("/api/rdp/sessions", headers=admin_headers, json={"hostId": host_id})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["protocol"] == "rdp"
    assert data["expiresIn"] > 0
    assert data["wsPath"] == f"/api/rdp/ws?ticket={data['ticket']}"
    assert data["account"]["id"] == account_id
    assert data["host"]["address"] == "10.0.0.86"
    # 口令随票据下发（CredSSP 必须在客户端算），这条通道有独立的审计留痕
    assert data["credential"]["username"] == "Administrator"
    assert data["credential"]["password"] == "s3cret"

    ticket = TICKETS.consume(data["ticket"])
    assert ticket is not None
    assert (ticket.host_address, ticket.port) == ("10.0.0.86", 3389)
    assert ticket.account_username == "Administrator"

    with app.app_context():
        assert AuditLog.query.filter_by(action="rdp_ticket").count() == 1
        reveal = AuditLog.query.filter_by(action="rdp_credential_reveal").one()
        assert "口令已下发到浏览器" in reveal.message
        assert reveal.detail["account"] == "Administrator"


def test_create_session_rejects_key_account(client, admin_headers, make_host, make_account):
    """Windows 远程桌面走 NTLM，密钥账号给不了口令 —— 必须明确拒绝而不是发一张空口令的票。"""
    host_id = make_host(
        name="win-key", address="10.0.0.88", port=3389, protocol="rdp", os_type="windows"
    )
    make_account(host_id, name="key-only", username="Administrator", auth_type="key")
    resp = client.post("/api/rdp/sessions", headers=admin_headers, json={"hostId": host_id})
    assert resp.status_code == 400
    assert "密钥" in resp.get_json()["message"]


def test_create_session_rejects_ssh_host(client, admin_headers, make_host, make_account):
    host_id = make_host(name="linux-02", address="10.0.0.2", port=22, protocol="ssh")
    make_account(host_id, name="root", username="root")
    resp = client.post("/api/rdp/sessions", headers=admin_headers, json={"hostId": host_id})
    assert resp.status_code == 403
    assert "远程桌面" in resp.get_json()["message"]


def test_create_session_rejects_host_without_accounts(
    client, admin_headers, make_host
):
    host_id = make_host(name="win-03", address="10.0.0.87", port=3389, protocol="rdp")
    resp = client.post("/api/rdp/sessions", headers=admin_headers, json={"hostId": host_id})
    assert resp.status_code == 400
    assert "账号" in resp.get_json()["message"]


def test_create_session_requires_host_id(client, admin_headers):
    resp = client.post("/api/rdp/sessions", headers=admin_headers, json={})
    assert resp.status_code == 400


def test_create_session_respects_grant_switch_and_scope(
    app, client, make_user, make_host, make_account, make_grant
):
    """非超管走授权：没授权连不上、`can_webterm=False` 也不给开、指定越权账号被拒。"""
    host_id = make_host(name="win-04", address="10.0.0.88", port=3389, protocol="rdp")
    account_id = make_account(host_id, name="Administrator", username="Administrator")
    other_id = make_account(host_id, name="ops", username="ops")
    user_id = make_user("rdp-ops", role_code="ops")
    headers = {"Authorization": f"Bearer {token_of(client, 'rdp-ops', 'User1234')}"}

    # 1) 没有任何授权 → 403（超管之外不存在「默认可连」）
    assert client.post("/api/rdp/sessions", headers=headers, json={"hostId": host_id}).status_code == 403

    # 2) 授权关闭交互式登录开关 → 403
    grant_id = make_grant(user_id, host_id, account_id=account_id, can_webterm=False)
    denied = client.post("/api/rdp/sessions", headers=headers, json={"hostId": host_id})
    assert denied.status_code == 403
    assert "交互式登录" in denied.get_json()["message"]

    # 3) 打开开关 → 拿到票据
    with app.app_context():
        from app.models import Grant

        Grant.query.filter_by(id=grant_id).update({"can_webterm": True})
        db.session.commit()
    ok = client.post("/api/rdp/sessions", headers=headers, json={"hostId": host_id})
    assert ok.status_code == 200
    assert ok.get_json()["data"]["account"]["id"] == account_id

    # 4) 指定一个不在自己授权里的账号 → 403（绝不静默换账号）
    swapped = client.post(
        "/api/rdp/sessions", headers=headers, json={"hostId": host_id, "accountId": other_id}
    )
    assert swapped.status_code == 403
    assert "授权范围" in swapped.get_json()["message"]


def test_terminal_targets_merge_ssh_and_rdp_hosts(client, admin_headers, make_host, make_account):
    """需求⑥：网页终端列表把 Linux 与 Windows 主机**合并**在一张表里，靠 `protocol` 区分。

    合并只发生在网页终端入口页；准入校验（`/check`）仍然只认字符会话，
    对 rdp 主机必须明确拒绝并说清「去资产列表点连接」。
    """
    ssh_id = make_host(name="linux-03", address="10.0.0.3", port=22, protocol="ssh")
    rdp_id = make_host(name="win-05", address="10.0.0.89", port=3389, protocol="rdp", os_type="windows")
    make_account(rdp_id, name="Administrator", username="Administrator")

    body = client.get("/api/terminal/targets", headers=admin_headers).get_json()
    entries = {item["hostId"]: item for item in body["data"]}
    assert ssh_id in entries
    assert rdp_id in entries
    assert entries[ssh_id]["protocol"] == "ssh"
    assert entries[rdp_id]["protocol"] == "rdp"
    assert entries[rdp_id]["osType"] == "windows"
    # 账号里带上 authType：前端据此把密钥账号从「可选资产账号」里滤掉（远程桌面只认口令）
    assert all("authType" in account for account in entries[rdp_id]["accounts"])

    # 直接对整个 rdp 主机做准入校验：必须明确拒绝并给出正确原因
    checked = client.post(f"/api/terminal/targets/{rdp_id}/check", headers=admin_headers)
    assert checked.status_code == 200
    payload = checked.get_json()["data"]
    assert payload["allowed"] is False
    assert "远程桌面" in payload["reason"]


def test_terminal_targets_follow_the_callers_permissions(app, client, make_host, make_grant):
    """同一个入口页，按权限码决定列哪几类协议：
    只有 `terminal:use` 的人看不到 Windows 主机，只有 `rdp:use` 的人看不到 Linux 主机。"""
    ssh_id = make_host(name="linux-07", address="10.0.0.7", port=22, protocol="ssh")
    rdp_id = make_host(name="win-07", address="10.0.0.97", port=3389, protocol="rdp")

    with app.app_context():
        from app.models import Role, User
        from app.security import hash_password

        for code, perms in (
            ("rdp-test-ssh-only", ["dashboard:view", "terminal:use"]),
            ("rdp-test-rdp-only", ["dashboard:view", "rdp:use"]),
        ):
            if Role.query.filter_by(code=code).first() is None:
                db.session.add(
                    Role(code=code, name=code, description="测试用最小角色", permissions=perms)
                )
        db.session.commit()
        ids: dict[str, int] = {}
        for username, code in (
            ("rdp-test-ssh-only", "rdp-test-ssh-only"),
            ("rdp-test-rdp-only", "rdp-test-rdp-only"),
        ):
            role = Role.query.filter_by(code=code).one()
            user = User(
                username=username,
                password_hash=hash_password("User1234"),
                display_name=username,
                role_id=role.id,
                status="active",
            )
            db.session.add(user)
            db.session.commit()
            ids[username] = user.id
    make_grant(ids["rdp-test-ssh-only"], ssh_id)
    make_grant(ids["rdp-test-rdp-only"], rdp_id)

    ssh_body = client.get(
        "/api/terminal/targets",
        headers={"Authorization": f"Bearer {token_of(client, 'rdp-test-ssh-only', 'User1234')}"},
    ).get_json()
    ssh_ids = [item["hostId"] for item in ssh_body["data"]]
    assert ssh_id in ssh_ids
    assert rdp_id not in ssh_ids

    rdp_body = client.get(
        "/api/terminal/targets",
        headers={"Authorization": f"Bearer {token_of(client, 'rdp-test-rdp-only', 'User1234')}"},
    ).get_json()
    rdp_ids = [item["hostId"] for item in rdp_body["data"]]
    assert rdp_id in rdp_ids
    assert ssh_id not in rdp_ids



def test_hosts_api_accepts_rdp_protocol(app, client, admin_headers):
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={
            "name": "win-06",
            "address": "10.0.0.90",
            "port": 3389,
            "protocol": "rdp",
            "osType": "windows",
            "description": "Windows 测试机",
        },
    )
    assert resp.status_code in (200, 201), resp.get_json()
    with app.app_context():
        host = Host.query.filter_by(name="win-06").first()
        assert host is not None
        assert host.protocol == "rdp"
        assert host.os_type == "windows"


def test_hosts_api_still_rejects_unknown_protocol(client, admin_headers):
    resp = client.post(
        "/api/hosts",
        headers=admin_headers,
        json={"name": "warp-01", "address": "10.0.0.91", "protocol": "vnc"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 5. 会话记录与审计（hooks 跨 app context 的收口）
# --------------------------------------------------------------------------- #
def test_hooks_open_and_close_session(app):
    """线上丢过一次 `rdp_session_close`：hooks 把 ORM 实例交给网关，回到新 app context 时已 detach。

    契约：`open_session()` 只回会话行 id，`close_session()` 拿 id 重新取记录再收口。
    """
    from app.models import SessionRecord
    from app.rdp.hooks import build_hooks

    ticket = RdpTicketStore(ttl=120).issue(**_issue())
    hooks = build_hooks(app)

    session_id = hooks.open_session(
        ticket,
        {
            "negotiation": {"type": "RDP_NEG_RSP", "selectedProtocol": PROTOCOL_HYBRID_EX},
            "certificate": {"subject": "CN=WIN-930NKGJCOED", "selfSigned": True},
        },
    )
    assert isinstance(session_id, int)

    hooks.close_session(
        session_id,
        {"fromClient": 1234, "fromServer": 5678, "reason": "客户端已断开"},
    )

    with app.app_context():
        record = db.session.get(SessionRecord, session_id)
        assert record is not None
        assert record.protocol == "rdp"
        assert record.source == "web"
        assert record.status == "closed"
        assert record.bytes_in == 1234
        assert record.bytes_out == 5678
        assert record.end_reason == "客户端已断开"
        assert record.ended_at is not None
        actions = [row.action for row in AuditLog.query.order_by(AuditLog.id).all()]
        assert "rdp_session_open" in actions
        assert "rdp_session_close" in actions

