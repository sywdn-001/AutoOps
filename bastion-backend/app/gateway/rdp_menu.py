"""RDP 网关（`mstsc` 连 4489）的选单模型 —— 与渲染/传输无关的纯逻辑。

背景（为什么要单独一个模块）：
`mstsc` 只会说 RDP 协议（X.224 → MCS → 安全协商 → 图形通道），它**不会**给你一个
"先登录堡垒机、再挑机器"的机会 —— 这个"菜单"必须由**服务端**画成一帧一帧的位图
发下去，再把鼠标点击 / 上下键 + Enter 当作输入收上来。

真正的 RDP 服务端（收 mstsc 的 TCP、跑完整协议栈、把目标机画面编码下发）不在这个
模块里：Python 生态没有可用的 RDP server 实现，详见 `docs/rdp-gateway-experiment.md`。
本模块只做**选单本身**——条目从哪来、光标怎么动、点哪一行算选中、空列表怎么提示——
这样无论将来前面接 IronRDP 的 Rust 服务端，还是先在测试里跑通交互，逻辑都是同一份。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: 一屏最多显示多少台机器（再多的分页，用 PgUp/PgDn 翻）
DEFAULT_PAGE_SIZE = 12


@dataclass(frozen=True)
class MenuItem:
    """选单里的一行：一台主机 + 一个堡垒机账号（与 SSH 网关的条目同构）。"""

    host_id: int
    host_name: str
    address: str
    port: int
    account_id: int
    account_username: str
    os_type: str = ""
    remark: str = ""

    def title(self) -> str:
        return f"{self.host_name} ({self.address}:{self.port})"


@dataclass
class RdpMenu:
    """可授权的 RDP 主机选单：支持键盘上下、翻页、鼠标点击。"""

    items: list[MenuItem] = field(default_factory=list)
    page_size: int = DEFAULT_PAGE_SIZE
    cursor: int = 0  # 指向 items 的下标
    page: int = 0  # 当前页（0 起）

    # -- 基本状态 ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self.items)

    @property
    def pages(self) -> int:
        if not self.items:
            return 1
        return max(1, math.ceil(len(self.items) / self.page_size))

    @property
    def empty(self) -> bool:
        return not self.items

    def visible(self) -> list[MenuItem]:
        start = self.page * self.page_size
        return self.items[start : start + self.page_size]

    def selected(self) -> MenuItem | None:
        if self.empty:
            return None
        return self.items[self.cursor]

    # -- 键盘 -------------------------------------------------------------
    def move(self, delta: int) -> MenuItem | None:
        """↑ / ↓：跨页时自动翻页（首尾环绕）。"""
        if self.empty:
            return None
        self.cursor = (self.cursor + delta) % len(self.items)
        self.page = self.cursor // self.page_size
        return self.selected()

    def page_move(self, delta: int) -> MenuItem | None:
        """PgUp / PgDn：整页翻，光标落到该页第一行。"""
        if self.empty:
            return None
        self.page = (self.page + delta) % self.pages
        self.cursor = min(self.page * self.page_size, len(self.items) - 1)
        return self.selected()

    def home(self) -> MenuItem | None:
        if self.empty:
            return None
        self.cursor, self.page = 0, 0
        return self.selected()

    def end(self) -> MenuItem | None:
        if self.empty:
            return None
        self.cursor = len(self.items) - 1
        self.page = self.cursor // self.page_size
        return self.selected()

    # -- 鼠标 -------------------------------------------------------------
    def row_of(self, index: int) -> int:
        """条目在当前页里的行号（0 起），不在当前页返回 -1。"""
        start = self.page * self.page_size
        if start <= index < start + len(self.visible()):
            return index - start
        return -1

    def hit_test(self, row: int) -> MenuItem | None:
        """鼠标点到「当前页第 row 行」：选中并返回该条；行号越界返回 None。"""
        if self.empty or row < 0:
            return None
        index = self.page * self.page_size + row
        if index >= len(self.items):
            return None
        self.cursor = index
        return self.items[index]

    # -- 渲染（给服务端画位图用；纯文本，便于测试与日志） ------------------
    def render(self, width: int = 78) -> list[str]:
        lines: list[str] = []
        if self.empty:
            lines.append("当前账号没有可用的 RDP 主机（请联系管理员在「访问授权」里开通）")
            lines.append("No RDP hosts are authorized for this account yet.")
            return lines
        start = self.page * self.page_size
        for offset, item in enumerate(self.visible()):
            index = start + offset
            mark = ">" if index == self.cursor else " "
            text = f"{mark} {index + 1:2d}. {item.title()}  账号 {item.account_username}"
            lines.append(text[:width])
        lines.append(f"— 第 {self.page + 1}/{self.pages} 页 · 共 {len(self.items)} 台 · ↑↓ 选择，Enter 连接 —")
        return lines


def build_menu_items(user, *, protocols: tuple[str, ...] = ("rdp",)) -> list[MenuItem]:
    """按当前账号的授权，构造 RDP 选单条目（失败一律返回空列表，不让网关 500）。"""
    try:
        from ..access import accessible_targets, serialize_target

        raw = accessible_targets(user, protocols=protocols)
        serialized = [serialize_target(entry) for entry in raw]
    except Exception:  # noqa: BLE001 - 授权表异常时按「没有可连主机」处理
        return []
    items: list[MenuItem] = []
    for entry in serialized:
        for account in entry.get("accounts") or []:
            items.append(
                MenuItem(
                    host_id=int(entry.get("id") or 0),
                    host_name=str(entry.get("name") or entry.get("hostName") or ""),
                    address=str(entry.get("address") or ""),
                    port=int(entry.get("port") or 3389),
                    account_id=int(account.get("id") or 0),
                    account_username=str(account.get("username") or ""),
                    os_type=str(entry.get("osType") or ""),
                    remark=str(entry.get("remark") or ""),
                )
            )
    items.sort(key=lambda item: (item.host_name.lower(), item.account_username.lower()))
    return items
