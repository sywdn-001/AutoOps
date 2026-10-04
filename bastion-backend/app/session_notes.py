"""会话「能力边界」提示语（连接横幅 / 连接后清屏重打共用一份措辞）。

为什么要单独一个模块：同一条提示要出现在两个入口 —— 网页终端
（``app/webterm/events.py`` 的 ``_clear_screen_after_connect``）与 SSH 网关
（``app/gateway/server.py`` 的 ``_write_session_context``）。两处各写一遍文案，
改一处忘一处就会出现「网页终端说了、网关没说」的不一致。

目前只有 Windows（WinRM）会话需要额外声明限制：它的设计与 Linux（SSH）
不同——**每条命令在目标机上单独起一个 PowerShell 进程**，所以进程内状态
（环境变量、``$ErrorActionPreference`` 之外的自定义变量、函数）不跨命令保留，
``more`` / ``pause`` / ``Read-Host`` 这类需要 stdin 常驻的交互式程序也无法工作。
"""

#: WinRM（Windows）会话的能力边界：逐条写给用户，不含颜色（由调用方着色）
WINRM_LIMIT_LINES: tuple[str, ...] = (
    "[堡垒机] 这是 Windows（WinRM）会话：每条命令在目标机上单独执行，cd 会保留；"
    "环境变量、自定义变量等进程内状态不保留。",
    "[堡垒机] more / pause / Read-Host 等交互式程序不可用，本会话也不支持文件传输；"
    "需要这些能力时请改用远程桌面。",
)


def session_note_lines(protocol: str | None) -> list[str]:
    """返回该协议会话需要额外提示的行（ssh 等无额外限制时返回空列表）。"""
    if (protocol or "").strip().lower() == "winrm":
        return list(WINRM_LIMIT_LINES)
    return []
