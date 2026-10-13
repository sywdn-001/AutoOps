"""Windows-MCP 的 streamable-http 客户端（只讲 JSON-RPC，不碰业务）。

上游把 MCP 服务起在目标机上：``windows-mcp serve --transport streamable-http
--host localhost --port 8000``，端点是 ``http://<目标机>:8000/mcp``。协议是
JSON-RPC 2.0，握手顺序是：

1. ``initialize``（带 ``protocolVersion`` / ``clientInfo``）→ 服务端回 ``Mcp-Session-Id``；
2. ``notifications/initialized``（通知，没有 id，服务端通常回 202）；
3. ``tools/list`` 拿到工具清单；``tools/call`` 执行某个工具。

几个实测/文档里踩过的点：

- 请求头必须同时接受 JSON 与 SSE：``Accept: application/json, text/event-stream``，
  否则部分实现直接 406；
- 开了 ``WINDOWS_MCP_STATELESS_HTTP=true`` 时服务端**不要求** ``Mcp-Session-Id``，
  重连更省事（我们默认建议打开，但客户端两种情况都能跑）；
- 回包可能是 ``text/event-stream``，一行一个 ``data: {...}``，要能挑出属于本次
  请求 id 的那条（进度通知是别的 id，直接忽略）；
- 工具结果在 ``result.content`` 里，元素有 ``type=text`` 与 ``type=image``（base64）。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import urlsplit

import requests

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_CLIENT_NAME = "AutoOps-Bastion"
MCP_CLIENT_VERSION = "1.0.0"

#: 只读工具：看一眼就完事，不改变目标机状态，可以不要求管理员二次确认。
#: 名单按真机（windows-mcp，20 个工具）核对过：曾经写在这里的 ``ControlStatus``
#: 在真机上并不存在，AI 照着调只会拿到 ``Unknown tool``。
OBSERVE_TOOLS = frozenset(
    {
        "Screenshot",
        "Snapshot",
        "DisplayInventory",
        "Wait",
        "WaitFor",
    }
)

#: 常见会改状态/碰数据的工具，用于给前端标红（真正的门禁在 permission + 二次确认上）。
MUTATING_TOOLS = frozenset(
    {
        "Click",
        "Type",
        "Scroll",
        "Move",
        "Shortcut",
        "App",
        "PowerShell",
        "FileSystem",
        "Scrape",
        "MultiSelect",
        "MultiEdit",
        "Clipboard",
        "Process",
        "Notification",
        "Registry",
    }
)


class McpError(RuntimeError):
    """MCP 调用失败（连不上、鉴权失败、协议错误、工具报错），message 是可读中文。"""


def normalize_url(url: str) -> str:
    """把用户填的地址补成 ``http://host:port/mcp``。"""
    base = (url or "").strip().rstrip("/")
    if not base:
        raise McpError("MCP 地址为空")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    if not base.endswith("/mcp"):
        base = f"{base}/mcp"
    return base


def extract_text(result: dict | None) -> str:
    """把 ``tools/call`` 的返回拼成人话文本（text 部分；image 见 ``extract_images``）。"""
    if not result:
        return ""
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif isinstance(item, dict) and item.get("type") == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or ""))
    if not parts and result.get("structuredContent"):
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(part for part in parts if part).strip()


def extract_images(result: dict | None) -> list[tuple[str, bytes]]:
    """取出返回里的图片（``type=image``），返回 ``[(mime, bytes)]``。"""
    images: list[tuple[str, bytes]] = []
    if not result:
        return images
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        raw = item.get("data")
        if not raw:
            continue
        try:
            images.append((item.get("mimeType") or "image/png", base64.b64decode(raw)))
        except (ValueError, TypeError):
            continue
    return images


class McpClient:
    """一个目标机的 MCP 连接（无状态，用完即弃；会话 id 自动带上）。"""

    def __init__(
        self,
        url: str,
        *,
        auth_key: str = "",
        timeout: float = 60.0,
        client_name: str = MCP_CLIENT_NAME,
    ) -> None:
        self.url = normalize_url(url)
        self.auth_key = (auth_key or "").strip()
        self.timeout = float(timeout or 60.0)
        self.client_name = client_name
        self.session_id = ""
        self._seq = 0
        self._initialized = False

    # ------------------------------------------------------------------ 底层
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{MCP_CLIENT_NAME}/{MCP_CLIENT_VERSION}",
        }
        loopback = self._loopback_host()
        if loopback:
            headers["Host"] = loopback
        if self.auth_key:
            headers["Authorization"] = f"Bearer {self.auth_key}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _loopback_host(self) -> str:
        """按「本机回环 + 目标端口」写 Host，绕开 MCP 的 DNS-rebinding 防护。

        `mcp` 这个包默认打开 DNS-rebinding 防护，只认 Host 是
        `127.0.0.1:*` / `localhost:*` / `[::1]:*` 的请求
        （`mcp/server/lowlevel/server.py` 里那组 `allowed_hosts`），
        否则回一个 「Invalid Host header」。

        堡垒机是隔着局域网、经目标机上的 portproxy 访问 `http://<目标机IP>:<端口>/mcp`
        的，Host 自然写成目标机 IP，于是被拒。而目标机的 portproxy 本来就把连接终结到
        `127.0.0.1:<端口>`，所以这里如实声明回环 Host 是准确的，不是伪造：
        真正的门禁是「只放行堡垒机 IP 的防火墙规则 + Bearer 访问密钥」。

        目标是本地进程（`127.0.0.1`）时不用改，返回空串。
        """
        parsed = urlsplit(self.url)
        host = (parsed.hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            return ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"127.0.0.1:{port}"

    def _timeout_message(self) -> str:
        return (
            f"MCP 在 {self.timeout:.0f} 秒内没有返回结果：{self.url} —— 目标机的 MCP 服务"
            "可能已经卡住（真机上让 Snapshot 取 UI 元素树就会把它卡死），可以重启目标机上的"
            "MCP 服务后重试"
        )

    @staticmethod
    def _sse_message(line: bytes) -> dict | None:
        """把 SSE 的一行 ``data: {...}`` 还原成一条 JSON-RPC 报文。"""
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith("data:"):
            return None
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _is_reply(message: dict, wanted: Any) -> bool:
        """这条报文是不是「本次请求的回复」（有些实现不给 id，只回 result）。"""
        if "result" in message and "id" not in message:
            return True
        return "id" in message and message.get("id") in (wanted, str(wanted))

    @staticmethod
    def _is_read_timeout(exc: BaseException) -> bool:
        """这个请求异常算不算「读超时」（而不是立刻就被拒/连不上）。"""
        if isinstance(exc, requests.Timeout):
            return True
        text = f"{exc} {getattr(exc, '__cause__', '')}"
        return "Read timed out" in text or "ReadTimeout" in text

    @staticmethod
    def _peek(resp: requests.Response, limit: int = 400) -> str:
        """尽量读一点错误正文，读不到就算了（别为了报错再挂一次）。"""
        try:
            for chunk in resp.iter_content(chunk_size=limit):
                return chunk.decode("utf-8", "replace")[:limit]
        except (requests.RequestException, ValueError):
            return ""
        return ""

    def _read_messages(self, resp: requests.Response, *, wanted: Any, deadline: float) -> list[dict]:
        """增量读响应：拿到本次请求的回复就立刻收工，超过 deadline 就给可读错误。

        为什么不能一次 ``resp.text`` 读完：目标机的 SSE 会一直保持连接（心跳 + 进度
        通知），而 ``requests`` 的 per-read 超时会被这些心跳不断刷新，于是永远不触发。
        真机上 ``Snapshot(use_ui_tree=True)`` 卡死时，后端就这么一直挂着（实测 90 秒
        以上），前端只看到转圈，最后变成 5xx。
        """
        ctype = (resp.headers.get("Content-Type") or "").lower()
        sse = "text/event-stream" in ctype
        raw = b""
        messages: list[dict] = []
        chunks = resp.iter_content(chunk_size=8192)
        while True:
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            except requests.Timeout:
                if time.monotonic() >= deadline:
                    raise McpError(self._timeout_message()) from None
                continue
            except requests.RequestException as exc:
                # 目标机 MCP 卡死时连接会「读到一半」被掐断；真机上 requests 把 urllib3 的
                # ReadTimeoutError 包成 ConnectionError（实测就是 500 的来源之一），
                # 这里统一翻成可读的 McpError（对外是 502 + 中文提示，不是 500）。
                message = self._timeout_message() if self._is_read_timeout(exc) else (
                    f"读 MCP 响应时连接断了（{self.url}）：{exc}"
                )
                raise McpError(message) from exc
            if not chunk:
                continue
            raw += chunk
            if sse:
                while b"\n" in raw:
                    line, _, raw = raw.partition(b"\n")
                    message = self._sse_message(line)
                    if message is None:
                        continue
                    messages.append(message)
                    if self._is_reply(message, wanted):
                        return messages
            else:
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    data = None
                if data is not None:
                    return data if isinstance(data, list) else [data]
            if time.monotonic() >= deadline:
                raise McpError(self._timeout_message())
        if sse:
            for line in raw.split(b"\n"):
                message = self._sse_message(line)
                if message is not None:
                    messages.append(message)
            return messages
        text = raw.decode("utf-8", "replace")
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise McpError(f"MCP 返回了无法解析的内容：{text[:200]}") from exc
        return data if isinstance(data, list) else [data]

    def _post(self, payload: dict, *, expect_reply: bool = True) -> list[dict]:
        wanted = payload.get("id")
        deadline = time.monotonic() + max(self.timeout, 1.0)
        read_timeout = min(3.0, max(self.timeout, 1.0))
        try:
            resp = requests.post(
                self.url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=self._headers(),
                timeout=(15.0, read_timeout),
                stream=True,
            )
        except requests.Timeout as exc:
            raise McpError(self._timeout_message()) from exc
        except requests.RequestException as exc:
            raise McpError(
                f"连不上目标机的 MCP 服务（{self.url}）：{exc} —— 确认 MCP 进程在跑、"
                "portproxy 与防火墙已放行、地址端口没写错"
            ) from exc

        try:
            session = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            if session:
                self.session_id = session

            if resp.status_code in (401, 403):
                raise McpError(
                    f"MCP 拒绝访问（HTTP {resp.status_code}）：这台机器的 MCP 开了访问密钥，"
                    "请确认部署时生成的密钥与当前一致"
                )
            if resp.status_code == 404:
                raise McpError(f"MCP 端点不存在（HTTP 404）：{self.url}（注意要带 /mcp）")
            if resp.status_code >= 400:
                raise McpError(f"MCP 返回 HTTP {resp.status_code}：{self._peek(resp)}")
            if not expect_reply:
                return []
            return self._read_messages(resp, wanted=wanted, deadline=deadline)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 — 关连接失败不该盖住真正的错误
                pass

    def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict:
        self._seq += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._seq
        messages = self._post(payload, expect_reply=not notify)
        if notify:
            return {}
        wanted = self._seq
        for message in messages:
            if message.get("id") not in (wanted, str(wanted)):
                continue
            if "error" in message:
                error = message["error"] or {}
                raise McpError(
                    f"MCP 报错（{error.get('code', '?')}）：{error.get('message') or error}"
                )
            return message.get("result") or {}
        # 有些实现只在 SSE 里回一条不带 id 的结果
        for message in messages:
            if "result" in message and "id" not in message:
                return message["result"] or {}
        raise McpError(f"MCP 没有返回本次请求的结果（method={method}）")

    # ------------------------------------------------------------------ 公开接口
    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": MCP_CLIENT_VERSION},
            },
        )
        try:
            self._rpc("notifications/initialized", {}, notify=True)
        except McpError:
            # 有些实现不接受这条通知，不影响后续 tools/list
            pass
        self._initialized = True
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            self.initialize()
        result = self._rpc("tools/list", {})
        tools = (result or {}).get("tools") if isinstance(result, dict) else None
        if tools is None and isinstance(result, list):
            tools = result
        return [tool for tool in (tools or []) if isinstance(tool, dict)]

    def call_tool(
        self, name: str, arguments: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        if not self._initialized:
            self.initialize()
        if timeout:
            self.timeout = float(timeout)
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def probe(self) -> dict:
        """探活：握手 + 列工具，返回工具数量与耗时（失败返回 ok=False 与可读原因）。"""
        started = time.time()
        info: dict[str, Any] = {"endpoint": self.url, "authKey": bool(self.auth_key)}
        try:
            handshake = self.initialize()
            tools = self.list_tools()
        except McpError as exc:
            return {
                **info,
                "ok": False,
                "error": str(exc),
                "latencyMs": int((time.time() - started) * 1000),
                "toolCount": 0,
                "tools": [],
            }
        server = (handshake or {}).get("serverInfo") or {}
        return {
            **info,
            "ok": True,
            "error": "",
            "latencyMs": int((time.time() - started) * 1000),
            "toolCount": len(tools),
            "tools": [
                {
                    "name": tool.get("name", ""),
                    "description": (tool.get("description") or "").strip(),
                    "mutating": tool.get("name") in MUTATING_TOOLS,
                    "observeOnly": tool.get("name") in OBSERVE_TOOLS,
                }
                for tool in tools
            ],
            "server": {
                "name": server.get("name", ""),
                "version": server.get("version", ""),
                "protocolVersion": (handshake or {}).get("protocolVersion", ""),
            },
            "stateless": not bool(self.session_id),
        }
