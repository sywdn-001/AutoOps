"""把「目标机的一次 mstsc 会话」桥进 RDP 网关（实验分支用的过渡实现）。

为什么要这么绕：RDP 网关本身是 RDP **服务端**（`bastion-rdp-gateway`，Rust），
它要「把目标机的画面接进这条会话」就必须做 RDP **客户端** —— 那需要完整的
CredSSP/NLA + 图形解码 + 输入编码。为了先把功能跑通，这里用 Windows 自带能力搭一座桥：

1. `cmdkey` 把该账号的口令存成凭据，`mstsc` 用它连目标机（1280×800，免证书提示）；
2. 按 **64×64 分块**比对前后两帧，只回传变化的块（裸 RGBA，二进制协议，见 `frame()`）；
3. 输入事件（键盘/鼠标）用 `SendInput` 转发到那个 mstsc 窗口。

限制（写清楚，别当成正式实现）：
- 依赖**有交互桌面**的 Windows 会话（服务方式跑起来没有桌面，桥就用不了）；
- 同一时刻只能桥一个目标（mstsc 窗口是全局的）；
- 目标机口令会通过 `cmdkey` 落到本机凭据管理器（会话结束会删掉）。
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes

import logging

import win32api
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image

TILE = 64
USER32 = ctypes.windll.user32
LOG = logging.getLogger("bastion.rdp.bridge")


class BridgeError(RuntimeError):
    """桥启不来时抛这个（错误信息直接回给用户看）。"""


def _find_window(pid: int, timeout: float = 25.0) -> int:
    """等 mstsc 的会话窗口出现，返回 hwnd。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[int] = []

        def _cb(hwnd, _acc):
            try:
                _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:  # noqa: BLE001
                return True
            if win_pid == pid and win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd) or ""
                cls = win32gui.GetClassName(hwnd) or ""
                if "TscShellContainerClass" in cls or title:
                    found.append(hwnd)
            return True

        win32gui.EnumWindows(_cb, None)
        for title in _dismiss_dialogs(pid):
            LOG.info("已自动关闭 mstsc 弹窗：%s", title)
        for hwnd in found:
            cls = win32gui.GetClassName(hwnd) or ""
            rect = win32gui.GetClientRect(hwnd)
            # 只认真正的 RDP 会话窗口；mstsc 报「无法连接」时是别的对话框，不能当画面源
            if "TscShellContainerClass" in cls and rect[2] > 400 and rect[3] > 300:
                return hwnd
        time.sleep(0.4)
    raise BridgeError("等 mstsc 窗口超时（目标机可能连不上或凭据被拒）")


def _dismiss_dialogs(pid: int) -> list[str]:
    """把 mstsc 的证书/安全警告弹窗按「连接」「是」关掉，返回关掉的标题（便于日志）。"""
    targets: list[int] = []

    def _cb(hwnd, _acc):
        try:
            _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            return True
        if win_pid != pid or not win32gui.IsWindowVisible(hwnd):
            return True
        if (win32gui.GetClassName(hwnd) or "") != "#32770":
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if any(word in title for word in ("安全警告", "远程桌面连接安全", "证书")):
            targets.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    closed: list[str] = []
    for hwnd in targets:
        title = win32gui.GetWindowText(hwnd) or ""
        clicked = False
        buttons: list[tuple[int, str]] = []

        def _child(child, _acc):
            cls = win32gui.GetClassName(child) or ""
            if cls.lower() == "button":
                buttons.append((child, win32gui.GetWindowText(child) or ""))
            return True

        try:
            win32gui.EnumChildWindows(hwnd, _child, None)
        except Exception:  # noqa: BLE001
            pass
        for child, text in buttons:
            if any(word in text for word in ("连接", "是", "继续")):
                try:
                    win32gui.SendMessage(child, 0x00F5, 0, 0)  # BM_CLICK
                    clicked = True
                except Exception:  # noqa: BLE001
                    pass
                break
        if not clicked:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_COMMAND, 1, 0)  # IDOK
                clicked = True
            except Exception:  # noqa: BLE001
                pass
        if clicked:
            closed.append(title)
    return closed


class TargetBridge:
    """一次目标机连接的桥：起 mstsc、抓帧、转发输入。"""

    def __init__(self, host: str, port: int, username: str, password: str, width: int = 1280, height: int = 800):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.width = width
        self.height = height
        self.hwnd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._prev: bytes | None = None
        self._lock = threading.Lock()
        self._stopped = False
        self._credfile = os.path.join(tempfile.gettempdir(), "bastion-rdp-bridge.rdp")

    # ---------------------------------------------------------------- start
    def start(self) -> None:
        target = f"{self.host}:{self.port}"
        try:
            subprocess.run(
                ["cmdkey", f"/generic:TERMSRV/{self.host}", f"/user:{self.username}", f"/pass:{self.password}"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"cmdkey 存凭据失败：{exc}") from exc

        rdp = "\n".join(
            [
                f"full address:s:{target}",
                f"username:s:{self.username}",
                "prompt for credentials:i:0",
                "authentication level:i:0",  # 不弹「证书不受信任」
                "enablecredsspsupport:i:1",
                "screen mode id:i:1",
                f"desktopwidth:i:{self.width}",
                f"desktopheight:i:{self.height}",
                "session bpp:i:32",
                "compression:i:1",
                "audiomode:i:2",
                "",
            ]
        )
        with open(self._credfile, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(rdp)

        self.proc = subprocess.Popen(
            ["mstsc.exe", self._credfile],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.hwnd = _find_window(self.proc.pid)
        # 让窗口在前台，好抓屏 + 收输入
        try:
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:  # noqa: BLE001
            pass
        self._bring_to_front()
        time.sleep(2.0)  # 等第一帧画面出来

    def _bring_to_front(self) -> None:
        if not self.hwnd:
            return
        try:
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- frame
    def _grab(self) -> Image.Image:
        """从屏幕 DC 上把 mstsc 窗口的客户区抠下来。

        mstsc 的画面是 DirectX 混合渲染，PrintWindow 抓出来几乎全黑，
        所以这里直接从桌面 DC BitBlt 该窗口的屏幕矩形 —— 代价是窗口必须可见、没被完全遮住。
        """
        assert self.hwnd
        _left, _top, right, bottom = win32gui.GetClientRect(self.hwnd)
        width, height = right, bottom
        screen_x, screen_y = win32gui.ClientToScreen(self.hwnd, (0, 0))
        screen_dc = win32gui.GetDC(0)
        src_dc = win32ui.CreateDCFromHandle(screen_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bitmap)
        mem_dc.BitBlt((0, 0), (width, height), src_dc, (screen_x, screen_y), win32con.SRCCOPY)
        raw = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bitmap.GetHandle())
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(0, screen_dc)
        return image

    def frame(self, full: bool = False) -> bytes | None:
        """返回「变化的块」：`[u32 块数][每块: u16 x, u16 y, u16 w, u16 h, RGBA 像素]`。

        没有变化返回 None（网关侧收到 204 就不重绘）。
        """
        with self._lock:
            if self._stopped or not self.hwnd:
                return None
            # mstsc 必须在前台才抓得到真实画面（抓的是屏幕区域），所以每秒顶一次前台
            if time.time() - getattr(self, "_front_at", 0.0) > 1.0:
                self._bring_to_front()
                self._front_at = time.time()
            raw_image = self._grab()
            if not raw_image.getbbox():
                time.sleep(0.12)
                raw_image = self._grab()
            image = raw_image.convert("RGBA").resize((self.width, self.height), Image.BILINEAR)
            data = image.tobytes()
            prev = self._prev
            self._prev = data
            if full:
                prev = None
            if prev is None or len(prev) != len(data):
                return self._encode_tiles(data, None)
            return self._encode_tiles(data, prev)

    def _encode_tiles(self, data: bytes, prev: bytes | None) -> bytes | None:
        stride = self.width * 4
        tiles: list[bytes] = []
        for y in range(0, self.height, TILE):
            for x in range(0, self.width, TILE):
                w = min(TILE, self.width - x)
                h = min(TILE, self.height - y)
                tile = b"".join(
                    data[(yy * stride + x * 4) : (yy * stride + (x + w) * 4)] for yy in range(y, y + h)
                )
                if prev is not None:
                    old = b"".join(
                        prev[(yy * stride + x * 4) : (yy * stride + (x + w) * 4)] for yy in range(y, y + h)
                    )
                    if tile == old:
                        continue
                tiles.append(struct.pack("<HHHH", x, y, w, h) + tile)
        if not tiles:
            return None
        return struct.pack("<I", len(tiles)) + b"".join(tiles)

    # ----------------------------------------------------------------- input
    def send_input(self, events: list[dict]) -> None:
        """把网关收到的输入转给 mstsc：`key`（vk + down）与 `mouse`（x,y,action）。"""
        if not self.hwnd:
            return
        with self._lock:
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:  # noqa: BLE001
                pass
            for event in events:
                kind = event.get("kind")
                if kind == "key":
                    vk = int(event.get("vk") or 0)
                    if not vk:
                        continue
                    win32api.keybd_event(vk, 0, 0 if event.get("down") else win32con.KEYEVENTF_KEYUP, 0)
                elif kind == "mouse":
                    x = int(event.get("x") or 0)
                    y = int(event.get("y") or 0)
                    left, top = win32gui.ClientToScreen(self.hwnd, (x, y))
                    win32api.SetCursorPos((left, top))
                    action = event.get("action")
                    if action == "down":
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    elif action == "up":
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    elif action == "right":
                        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

    # ------------------------------------------------------------------ stop
    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        subprocess.run(
            ["cmdkey", f"/delete:TERMSRV/{self.host}"], capture_output=True, timeout=10, check=False
        )
        try:
            os.remove(self._credfile)
        except OSError:
            pass


_BRIDGES: dict[str, TargetBridge] = {}
_BRIDGES_LOCK = threading.Lock()


def bridge_for(sid: str) -> TargetBridge | None:
    with _BRIDGES_LOCK:
        return _BRIDGES.get(sid)


def open_bridge(sid: str, host: str, port: int, username: str, password: str) -> TargetBridge:
    with _BRIDGES_LOCK:
        if sid in _BRIDGES:
            return _BRIDGES[sid]
    bridge = TargetBridge(host, port, username, password)
    bridge.start()
    with _BRIDGES_LOCK:
        _BRIDGES[sid] = bridge
    return bridge


def close_bridge(sid: str) -> bool:
    with _BRIDGES_LOCK:
        bridge = _BRIDGES.pop(sid, None)
    if bridge is None:
        return False
    bridge.stop()
    return True
