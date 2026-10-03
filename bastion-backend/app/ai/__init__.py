"""AI 运维（AIOps）子系统。

* :mod:`app.ai.client`  —— DeepSeek 流式客户端（解析 reasoning / content / tool_calls）
* :mod:`app.ai.tools`   —— 90+ 工具注册表，声明式映射到本项目自己的 REST API
* :mod:`app.ai.prompt`  —— 系统提示词（含卡片协议）
* :mod:`app.ai.service` —— 对话编排、权限判定、敏感操作确认、审计落库
"""

from .client import AiError, DeepSeekClient, create_client
from .service import (
    Caller,
    add_message,
    approve_tool_call,
    build_messages,
    create_conversation,
    execute_tool,
    extract_cards,
    pending_tool_calls,
    reject_tool_call,
    run_turn,
    strip_cards,
)
from .tools import TOOLS, TOOL_INDEX, grouped_catalog, openai_schemas, tools_for_permission

__all__ = [
    "AiError",
    "Caller",
    "DeepSeekClient",
    "TOOLS",
    "TOOL_INDEX",
    "add_message",
    "approve_tool_call",
    "build_messages",
    "create_client",
    "create_conversation",
    "execute_tool",
    "extract_cards",
    "grouped_catalog",
    "openai_schemas",
    "pending_tool_calls",
    "reject_tool_call",
    "run_turn",
    "strip_cards",
    "tools_for_permission",
]
