"""DeepSeek（OpenAI 兼容）流式客户端。

只做一件事：把上游 SSE 解析成项目内部的事件流，供 :mod:`app.ai.service` 编排。

覆盖的坑：

* ``deepseek-flash`` 是**推理模型**，delta 里会先后出现 ``reasoning_content`` 与 ``content``，
  两者必须分开呈现（前端「思考过程」折叠区 + 正文流式 Markdown）；
* 工具调用参数是**跨多个 chunk 拼字符串**的（``function.arguments`` 增量），
  必须按 ``index`` 累积完再 ``json.loads``；
* ``max_tokens`` 给小了会被思考过程吃光，出现 ``finish_reason=length`` 且正文为空 ——
  这里在解析到「空正文 + length」时给出可读的报错提示。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import requests

DEFAULT_BASE_URL = "https://api.deepseek.com"


class AiError(RuntimeError):
    """AI 上游或编排层面的错误（会被转成 SSE error 事件或 502 响应）。"""

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"message": self.message}
        if self.status is not None:
            out["status"] = self.status
        if self.detail:
            out["detail"] = self.detail
        return out


class DeepSeekClient:
    """最小可用的 DeepSeek 客户端（只实现 chat.completions 流式接口）。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "deepseek-flash",
        timeout: int = 120,
        max_tokens: int = 8192,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or "deepseek-flash"
        self.timeout = timeout
        self.max_tokens = max_tokens

    # -- 对外 -------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def models(self) -> list[str]:
        """拉取可用模型（用于设置页自检）。"""
        if not self.configured:
            raise AiError("未配置 DEEPSEEK_API_KEY（请在 bastion-backend/.env 中填写）", status=503)
        try:
            response = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=min(self.timeout, 20),
            )
        except requests.RequestException as exc:
            raise AiError(f"无法连接 DeepSeek：{exc}", status=502) from exc
        if response.status_code >= 400:
            raise AiError(_http_message(response), status=response.status_code)
        data = response.json() if response.content else {}
        return [item.get("id", "") for item in (data.get("data") or []) if item.get("id")]

    def stream_chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Iterator[dict]:
        """流式对话。产出事件：

        ``{"type": "reasoning"|"content", "text": str}``
        ``{"type": "tool_call", "index": int, "id": str, "name": str, "arguments": str}``
        ``{"type": "usage", "usage": {...}}``
        ``{"type": "finish", "reason": str}``
        """
        if not self.configured:
            raise AiError("未配置 DEEPSEEK_API_KEY（请在 bastion-backend/.env 中填写）", status=503)

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                stream=True,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AiError(f"无法连接 DeepSeek：{exc}", status=502) from exc

        if response.status_code >= 400:
            response.close()
            raise AiError(_http_message(response), status=response.status_code)

        content_chars = 0
        finish_reason = ""
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    yield {"type": "usage", "usage": chunk["usage"]}
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        yield {"type": "reasoning", "text": reasoning}
                    content = delta.get("content")
                    if content:
                        content_chars += len(content)
                        yield {"type": "content", "text": content}
                    for call in delta.get("tool_calls") or []:
                        function = call.get("function") or {}
                        yield {
                            "type": "tool_call",
                            "index": int(call.get("index") or 0),
                            "id": call.get("id") or "",
                            "name": function.get("name") or "",
                            "arguments": function.get("arguments") or "",
                        }
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        finally:
            response.close()

        if finish_reason == "length" and content_chars == 0:
            raise AiError(
                "模型把 token 预算都花在思考上了（finish_reason=length 且正文为空），"
                "请调大 AI_MAX_TOKENS 后重试",
                status=502,
            )
        yield {"type": "finish", "reason": finish_reason or "stop"}


def _http_message(response: requests.Response) -> str:
    """把上游错误压成一句人话（402 余额 / 401 Key / 429 限流 等）。"""
    detail: Any = None
    try:
        detail = response.json()
    except ValueError:
        detail = (response.text or "")[:400]
    message = ""
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        message = message or str(detail.get("message") or "")
    if not message:
        message = str(detail)[:400] if detail else ""
    hints = {
        401: "API Key 无效或已过期",
        402: "DeepSeek 账户余额不足",
        422: "请求参数不被接受",
        429: "触发限流，请稍后重试",
    }
    prefix = hints.get(response.status_code, f"DeepSeek 返回 HTTP {response.status_code}")
    if not message:
        # 上游边缘节点偶尔直接拒请求、响应体为空；这时把「没有详情」说清楚，
        # 否则用户（和排查的人）只能看到一句「HTTP 400」，等于没有线索。
        return f"{prefix}（上游响应体为空，无错误详情）"
    return f"{prefix}：{message}"


def create_client(config: dict) -> DeepSeekClient:
    """从 Flask 配置构造客户端。"""
    return DeepSeekClient(
        config.get("DEEPSEEK_API_KEY", ""),
        base_url=config.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL,
        model=config.get("AI_MODEL") or "deepseek-flash",
        timeout=int(config.get("AI_REQUEST_TIMEOUT") or 120),
        max_tokens=int(config.get("AI_MAX_TOKENS") or 8192),
    )
