"""ComputerUse（Windows-MCP）运维能力。

这个子包把「让 AI 真的去操作一台 Windows 机器」拆成三件事：

- ``mirror.py``：华为云 Python 镜像的「可下载 Windows amd64 安装包」清单，以及把安装包
  缓存到堡垒机本地（跨网段部署时先落在堡垒机，再由目标机拉走）。
- ``mcp.py``：Windows-MCP 的 streamable-http 客户端（``initialize`` → ``tools/list`` /
  ``tools/call``），只做一件事 —— 把 JSON-RPC 讲明白，解析 SSE 与 JSON 两种回包。
- ``deploy.py``：阶段式部署编排器。真机上要做的每一步（上传安装包 → 静默安装 Python →
  ``pip install windows-mcp`` → 写开机自启 VBS → 起服务 → 端口代理 + 防火墙 → 探活）
  都走 WinRM，并把每一行日志推给前端进度条。

设计约束（来自真机实测与上游文档）：

- Windows-MCP 要求 **Python ≥ 3.13**；
- 它的命令行是 ``windows-mcp serve --transport streamable-http --host localhost --port 8000``，
  只监听回环，所以堡垒机要访问就得在目标机加一层 portproxy（LAN:8000 → 127.0.0.1:8000）
  并把防火墙放行范围收窄到堡垒机 IP；
- UIA / 截图是**按登录会话隔离**的：开机自启的 MCP 跑在控制台会话，而 WebRDP 是另一个
  RDP 会话，所以「操作员在自己的远程桌面窗口里看不到 AI 的动作」是正常现象 ——
  侧边栏必须把 MCP ``Screenshot`` 的结果直接渲染出来给操作员看。
"""

from __future__ import annotations

from .deploy import (
    STAGES,
    DeployError,
    DeploymentRunner,
    deployment_snapshot,
    get_runner,
    start_deployment,
)
from .mcp import McpClient, McpError, OBSERVE_TOOLS, extract_images, extract_text
from .mirror import (
    MirrorError,
    cache_installer,
    list_versions,
    local_installer_path,
    prewarm,
)

__all__ = [
    "STAGES",
    "DeployError",
    "DeploymentRunner",
    "McpClient",
    "McpError",
    "MirrorError",
    "OBSERVE_TOOLS",
    "cache_installer",
    "deployment_snapshot",
    "extract_images",
    "extract_text",
    "get_runner",
    "list_versions",
    "local_installer_path",
    "prewarm",
    "start_deployment",
]
