"""RDCleanPath 协议编解码（DER/ASN.1）。

RDCleanPath 是 IronRDP 的浏览器客户端用来跟「网关」建立 RDP 隧道的引导协议：
客户端先把 TCP+X.224+TLS 这段握手委托给网关做，网关把服务器的 X.224 Connection Confirm
与服务器证书链回给客户端，之后这条 WebSocket 就只做 TLS 字节的双向中继，NLA/CredSSP
由浏览器端的 WASM 客户端在隧道里自己跑。

本模块只做编解码，不碰网络（对照 D:\\Desktop\\WebRemote 与 ironrdp-wasm 的
example/lib/rdp-proxy.js 实现，字段编号与 3390 版本号完全一致）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

#: RDP 默认端口 + 1，IronRDP 用这个魔数作为协议版本，必须原样回填
RDCLEANPATH_VERSION = 3390

TAG_SEQUENCE = 0x30
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_UTF8_STRING = 0x0C

#: 请求字段（上下文标签）
FIELD_VERSION = 0
FIELD_ERROR = 1
FIELD_DESTINATION = 2
FIELD_PROXY_AUTH = 3
FIELD_PRECONNECTION_BLOB = 5
FIELD_X224_CONNECTION_PDU = 6
FIELD_SERVER_CERT_CHAIN = 7
FIELD_SERVER_ADDR = 9

#: build_error() 的错误码
ERROR_GENERAL = 1
ERROR_NEGOTIATION = 2

#: RDP_NEG_FAILURE 的 failureCode（服务器拒绝协商时用得上，便于给出人话）
NEG_FAILURE_NAMES = {
    0x00000001: "SSL_REQUIRED_BY_SERVER",
    0x00000002: "SSL_NOT_ALLOWED_BY_SERVER",
    0x00000003: "SSL_CERT_NOT_ON_SERVER",
    0x00000004: "INCONSISTENT_FLAGS",
    0x00000005: "HYBRID_REQUIRED_BY_SERVER",
    0x00000006: "SSL_WITH_USER_AUTH_REQUIRED_BY_SERVER",
}


class CleanPathError(ValueError):
    """RDCleanPath 报文不合法（或版本不受支持）。"""


# --------------------------------------------------------------------------- #
# DER 编码
# --------------------------------------------------------------------------- #
def encode_length(length: int) -> bytes:
    if length < 0:
        raise CleanPathError("DER 长度不能为负")
    if length < 0x80:
        return bytes([length])
    encoded = b""
    value = length
    while value:
        encoded = bytes([value & 0xFF]) + encoded
        value >>= 8
    return bytes([0x80 | len(encoded)]) + encoded


def der_wrap(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(content)) + content


def der_integer(value: int) -> bytes:
    if value < 0:
        raise CleanPathError("只支持非负整数")
    if value == 0:
        return der_wrap(TAG_INTEGER, b"\x00")
    encoded = b""
    remaining = value
    while remaining:
        encoded = bytes([remaining & 0xFF]) + encoded
        remaining >>= 8
    if encoded[0] & 0x80:  # 最高位为 1 要补 0x00，否则被当成负数
        encoded = b"\x00" + encoded
    return der_wrap(TAG_INTEGER, encoded)


def der_utf8(text: str) -> bytes:
    return der_wrap(TAG_UTF8_STRING, text.encode("utf-8"))


def der_octet_string(data: bytes) -> bytes:
    return der_wrap(TAG_OCTET_STRING, data)


def der_context(tag_num: int, content: bytes) -> bytes:
    """上下文标签（显式）：内容本身要已经是完整的 DER 元素。"""
    return der_wrap(0xA0 + tag_num, content)


# --------------------------------------------------------------------------- #
# DER 解码
# --------------------------------------------------------------------------- #
def decode_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise CleanPathError("DER 长度字节缺失")
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0 or count > 4:
        raise CleanPathError(f"不支持的 DER 长度编码：0x{first:02x}")
    if offset + count > len(data):
        raise CleanPathError("DER 长格式长度字节不足")
    length = int.from_bytes(data[offset : offset + count], "big")
    return length, offset + count


def decode_tlv(data: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    """返回 (tag, content, 下一个位置)。"""
    if offset >= len(data):
        raise CleanPathError("DER 元素缺失")
    tag = data[offset]
    length, after_length = decode_length(data, offset + 1)
    end = after_length + length
    if end > len(data):
        raise CleanPathError("DER 元素长度超出报文")
    return tag, data[after_length:end], end


def decode_children(content: bytes) -> list[tuple[int, bytes]]:
    children: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(content):
        tag, inner, offset = decode_tlv(content, offset)
        children.append((tag, inner))
    return children


def decode_integer(content: bytes) -> int:
    if not content:
        raise CleanPathError("DER 整数内容为空")
    return int.from_bytes(content, "big", signed=bool(content[0] & 0x80))


def _context_value(content: bytes) -> tuple[int, bytes]:
    """上下文标签内的第一个元素，返回 (tag, content)。"""
    tag, inner, _ = decode_tlv(content, 0)
    return tag, inner


def _context_sequence_children(content: bytes) -> list[tuple[int, bytes]]:
    tag, inner, _ = decode_tlv(content, 0)
    if tag != TAG_SEQUENCE:
        raise CleanPathError(f"期望 SEQUENCE，实际 tag=0x{tag:02x}")
    return decode_children(inner)


# --------------------------------------------------------------------------- #
# RDCleanPath 请求 / 响应 / 错误
# --------------------------------------------------------------------------- #
@dataclass
class CleanPathRequest:
    """客户端发来的引导报文。"""

    destination: str
    x224_connection_pdu: bytes
    proxy_auth: str | None = None
    preconnection_blob: str | None = None
    version: int = RDCLEANPATH_VERSION
    fields: dict[int, bytes] = field(default_factory=dict)

    def host_port(self) -> tuple[str, int]:
        return parse_destination(self.destination)


def parse_request(data: bytes) -> CleanPathRequest:
    """解析客户端首条二进制消息（外层 SEQUENCE，子元素是上下文标签）。"""
    if len(data) < 2:
        raise CleanPathError("RDCleanPath 请求为空")
    tag, content, _ = decode_tlv(data, 0)
    if tag != TAG_SEQUENCE:
        raise CleanPathError(f"RDCleanPath 请求必须是 SEQUENCE，实际 tag=0x{tag:02x}")

    version = RDCLEANPATH_VERSION
    destination: str | None = None
    proxy_auth: str | None = None
    preconnection_blob: str | None = None
    x224: bytes | None = None
    raw_fields: dict[int, bytes] = {}

    for child_tag, child_content in decode_children(content):
        if not 0xA0 <= child_tag <= 0xBF:
            continue
        number = child_tag - 0xA0
        raw_fields[number] = child_content
        inner_tag, inner = _context_value(child_content)
        if number == FIELD_VERSION:
            if inner_tag != TAG_INTEGER:
                raise CleanPathError("RDCleanPath 版本字段不是 INTEGER")
            version = decode_integer(inner)
        elif number == FIELD_DESTINATION:
            if inner_tag != TAG_UTF8_STRING:
                raise CleanPathError("destination 不是 UTF8String")
            destination = inner.decode("utf-8", errors="replace")
        elif number == FIELD_PROXY_AUTH:
            if inner_tag == TAG_UTF8_STRING:
                proxy_auth = inner.decode("utf-8", errors="replace")
            elif inner_tag == TAG_OCTET_STRING:
                proxy_auth = inner.decode("utf-8", errors="replace")
        elif number == FIELD_PRECONNECTION_BLOB:
            if inner_tag == TAG_UTF8_STRING:
                preconnection_blob = inner.decode("utf-8", errors="replace")
        elif number == FIELD_X224_CONNECTION_PDU:
            if inner_tag != TAG_OCTET_STRING:
                raise CleanPathError("x224_connection_pdu 不是 OCTET STRING")
            x224 = inner

    if version != RDCLEANPATH_VERSION:
        raise CleanPathError(f"Unsupported RDCleanPath version: {version}")
    if not destination:
        raise CleanPathError("Missing destination in RDCleanPath request")
    if not x224:
        raise CleanPathError("Missing x224_connection_pdu in RDCleanPath request")
    return CleanPathRequest(
        destination=destination,
        x224_connection_pdu=x224,
        proxy_auth=proxy_auth,
        preconnection_blob=preconnection_blob,
        version=version,
        fields=raw_fields,
    )


def build_response(server_addr: str, x224_response: bytes, cert_chain: list[bytes]) -> bytes:
    """网关回给客户端的引导响应（含服务器证书链）。"""
    parts = [
        der_context(FIELD_VERSION, der_integer(RDCLEANPATH_VERSION)),
        der_context(FIELD_X224_CONNECTION_PDU, der_octet_string(x224_response)),
        der_context(
            FIELD_SERVER_CERT_CHAIN,
            der_wrap(TAG_SEQUENCE, b"".join(der_octet_string(cert) for cert in cert_chain)),
        ),
        der_context(FIELD_SERVER_ADDR, der_utf8(server_addr)),
    ]
    return der_wrap(TAG_SEQUENCE, b"".join(parts))


def build_error(error_code: int = ERROR_GENERAL, http_status_code: int | None = None) -> bytes:
    """网关回给客户端的错误响应。"""
    inner = [der_context(0, der_integer(error_code))]
    if http_status_code is not None:
        inner.append(der_context(1, der_integer(http_status_code)))
    return der_wrap(
        TAG_SEQUENCE,
        b"".join(
            [
                der_context(FIELD_VERSION, der_integer(RDCLEANPATH_VERSION)),
                der_context(FIELD_ERROR, der_wrap(TAG_SEQUENCE, b"".join(inner))),
            ]
        ),
    )


def parse_response(data: bytes) -> dict:
    """仅供测试/排查：把响应或错误报文解回字典。"""
    tag, content, _ = decode_tlv(data, 0)
    if tag != TAG_SEQUENCE:
        raise CleanPathError("RDCleanPath 响应必须是 SEQUENCE")
    out: dict = {}
    for child_tag, child_content in decode_children(content):
        if not 0xA0 <= child_tag <= 0xBF:
            continue
        number = child_tag - 0xA0
        if number == FIELD_VERSION:
            _, inner = _context_value(child_content)
            out["version"] = decode_integer(inner)
        elif number == FIELD_X224_CONNECTION_PDU:
            _, inner = _context_value(child_content)
            out["x224ConnectionPdu"] = inner
        elif number == FIELD_SERVER_CERT_CHAIN:
            chain = []
            for cert_tag, cert_content in _context_sequence_children(child_content):
                if cert_tag == TAG_OCTET_STRING:
                    chain.append(cert_content)
            out["serverCertChain"] = chain
        elif number == FIELD_SERVER_ADDR:
            _, inner = _context_value(child_content)
            out["serverAddr"] = inner.decode("utf-8", errors="replace")
        elif number == FIELD_ERROR:
            error: dict = {}
            for err_tag, err_content in _context_sequence_children(child_content):
                if not 0xA0 <= err_tag <= 0xBF:
                    continue
                err_number = err_tag - 0xA0
                _, inner = _context_value(err_content)
                if err_number == 0:
                    error["errorCode"] = decode_integer(inner)
                elif err_number == 1:
                    error["httpStatusCode"] = decode_integer(inner)
            out["error"] = error
    return out


def parse_destination(destination: str) -> tuple[str, int]:
    """`host[:port]` → (host, port)；支持 IPv6 的 `[::1]:3389` 写法。"""
    text = (destination or "").strip()
    if not text:
        raise CleanPathError("destination 为空")
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise CleanPathError(f"IPv6 地址缺少右括号：{destination}")
        host = text[1:end]
        rest = text[end + 1 :]
        if rest.startswith(":"):
            return host, _port(rest[1:])
        return host, 3389
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        return host, _port(port_text)
    if ":" in text:  # 未加方括号的 IPv6，按默认端口处理
        return text, 3389
    return text, 3389


def _port(text: str) -> int:
    text = text.strip()
    if not text:
        return 3389
    try:
        port = int(text)
    except ValueError as exc:
        raise CleanPathError(f"端口不是数字：{text}") from exc
    if not 1 <= port <= 65535:
        raise CleanPathError(f"端口超出范围：{port}")
    return port


def parse_x224_negotiation(pdu: bytes) -> dict:
    """从 X.224 Connection Confirm 里读出 RDP 协商结果（用于人话报错）。

    PDU = TPKT(4) + X.224 CC(7) + 可选的 RDP_NEG_RSP / RDP_NEG_FAILURE(8)。
    """
    result: dict = {}
    if len(pdu) < 11:
        result["error"] = "X.224 响应过短"
        return result
    body = pdu[11:]
    # length 是小端（[MS-RDPBCGR] 的 0x0008 在线上是 08 00）。真机实测 192.168.0.75:3389
    # 的 CC body 就是 `022f080002000000`：type=02、flags=2f、length=08 00(=8)、selected=2。
    # 这里同时容忍大端写法（历史上的解析器两种都有），避免把合法的 CC 判成「没有协商结果」。
    length = struct.unpack("<H", body[2:4])[0] if len(body) >= 8 else 0
    if len(body) >= 8 and body[0] in (0x02, 0x03) and (length == 8 or body[2:4] == b"\x00\x08"):
        value = struct.unpack("<I", body[4:8])[0]
        if body[0] == 0x02:
            result["type"] = "RDP_NEG_RSP"
            result["selectedProtocol"] = value
        else:
            result["type"] = "RDP_NEG_FAILURE"
            result["failureCode"] = value
            result["failureName"] = NEG_FAILURE_NAMES.get(value, f"0x{value:08x}")
    else:
        result["type"] = "NO_NEGOTIATION"
        result["tail"] = body.hex()
    return result
