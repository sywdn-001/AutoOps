"""WinRM（Windows 远程管理）支持：让 Windows 主机也有网页终端里的 shell。

Linux 走 SSH（`app/terminal/bridge.py:ShellBridge`），Windows 走 WinRM
（`WinrmBridge`）—— 两者公开面一致，上层的网页终端前端、命令策略、审计与 AI
助手完全复用。
"""

from .bridge import WinrmBridge
from .client import (
    WINRM_DEFAULT_PORT,
    WINRM_DEFAULT_SSL_PORT,
    WINRM_TRANSPORTS,
    ScriptResult,
    WinrmConnection,
    WinrmError,
    WinrmTarget,
    build_target,
    close,
    connect,
    run_script,
    test_connection,
)

__all__ = [
    "WINRM_DEFAULT_PORT",
    "WINRM_DEFAULT_SSL_PORT",
    "WINRM_TRANSPORTS",
    "ScriptResult",
    "WinrmBridge",
    "WinrmConnection",
    "WinrmError",
    "WinrmTarget",
    "build_target",
    "close",
    "connect",
    "run_script",
    "test_connection",
]
