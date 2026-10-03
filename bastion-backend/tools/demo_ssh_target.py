#!/usr/bin/env python
"""本地演示用的「被管理 Linux 主机」（真实 SSH 协议栈，无需装 Linux）。

用途：没有真实 Linux 机器时，用它来验证 / 演示堡垒机全链路 ——
在后台「资产管理」里把主机地址填成 ``127.0.0.1:2200``、账号填 ``root`` / ``s3cret``，
就能从「网页终端」或 ``ssh -p 2222`` 网关真实连进来，命令与输出照常落审计。

它不是 Linux，只对下列命令给出确定性应答，其它命令回 ``command not found``：

    whoami / id / hostname / uname -a / uptime / who / pwd
    ls [-l] [路径] / cat /etc/os-release / cat /etc/hosts
    df -h / free -m / ps aux / ip addr / echo <文本>
    cat /etc/shadow  （供策略演示：只读/审计策略下应被拦截）

用法::

    python tools/demo_ssh_target.py                    # 127.0.0.1:2200, root/s3cret
    python tools/demo_ssh_target.py --port 2222 --user ops --password ops123

同时提供 ``sftp`` 子系统（文件管理器用），把 ``--sftp-root`` 指向的本地目录当远端根目录
（默认 ``%TEMP%/bastion_sftp_demo``，启动时自动铺一批样例文件与 ``.ssh`` 目录）。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
import threading
import time

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sftp_backend import install_sftp_subsystem, seed_demo_root  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("demo-target")

_HOST_KEY: paramiko.RSAKey | None = None
_HOST_KEY_LOCK = threading.Lock()


def _host_key() -> paramiko.RSAKey:
    global _HOST_KEY
    with _HOST_KEY_LOCK:
        if _HOST_KEY is None:
            _HOST_KEY = paramiko.RSAKey.generate(2048)
        return _HOST_KEY


RESPONSES: dict[str, str] = {
    "whoami": "opsadmin\n",
    "id": "uid=0(root) gid=0(root) groups=0(root)\n",
    "hostname": "demo-linux-01\n",
    "pwd": "/root\n",
    "uname -a": "Linux demo-linux-01 6.1.0-13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.1.55-1 x86_64 GNU/Linux\n",
    "uptime": " 10:24:31 up 12 days,  4:02,  2 users,  load average: 0.08, 0.12, 0.09\n",
    "who": "opsadmin pts/0        2026-02-11 09:58 (10.0.0.5)\n",
    "df -h": (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "/dev/vda1        40G  9.8G   28G  27% /\n"
        "tmpfs           985M     0  985M   0% /dev/shm\n"
    ),
    "free -m": "               total        used        free      shared  buff/cache   available\nMem:            1985         612         284          12        1088        1120\nSwap:           1023           0        1023\n",
    "ps aux": (
        "USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND\n"
        "root         1  0.0  0.6 167412 12388 ?        Ss   Feb02   0:12 /sbin/init\n"
        "root       682  0.0  0.4  18932  9216 ?        Ss   Feb02   0:00 /usr/sbin/sshd -D\n"
    ),
    "ip addr": (
        "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
        "    inet 127.0.0.1/8 scope host lo\n"
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state UP\n"
        "    inet 10.0.0.21/24 brd 10.0.0.255 scope global eth0\n"
    ),
    "cat /etc/os-release": (
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
        'NAME="Debian GNU/Linux"\n'
        "VERSION_ID=\"12\"\n"
    ),
    "cat /etc/hosts": "127.0.0.1\tlocalhost\n10.0.0.21\tdemo-linux-01\n",
    "cat /etc/shadow": "root:$6$demo$fakefakefake:19700:0:99999:7:::\n",
}


def respond(command: str) -> str:
    """把命令行映射成确定性输出。"""
    text = command.strip()
    if not text:
        return ""
    if text.startswith("echo "):
        return f"{text[5:]}\n"
    if text.startswith("sudo "):
        text = text[5:].strip()
        if text in RESPONSES:
            return RESPONSES[text]
    if text in RESPONSES:
        return RESPONSES[text]
    if text == "ls" or text.startswith("ls "):
        target = text[2:].strip().split(" ")[-1] if text[2:].strip() else "/root"
        if target in ("/etc", "/etc/"):
            return "hosts  os-release  passwd  shadow\n"
        return "backup.tar.gz  deploy.sh  logs\n"
    head = text.split(" ")[0]
    return f"-bash: {head}: command not found\n"


class DemoTargetServer(paramiko.ServerInterface):
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.shell_ready = threading.Event()
        self.sftp_ready = threading.Event()
        self.sftp_done = threading.Event()
        self.commands: list[str] = []

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username: str) -> int:
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):  # noqa: ANN001, ARG002
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:  # noqa: ARG002
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:  # noqa: ARG002
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes) -> bool:  # noqa: ANN001, ARG002
        return True

    def check_channel_shell_request(self, channel) -> bool:  # noqa: ANN001
        self.shell_ready.set()
        return True

    def check_channel_exec_request(self, channel, command) -> bool:  # noqa: ANN001
        """开放非交互命令执行（``ssh -p 2200 root@127.0.0.1 "uname -a"``）。

        历史缺陷：本演示目标机早期只实现交互 shell，没实现 exec —— paramiko 的**默认实现**
        直接拒绝 exec 请求，客户端拿到 ``Channel closed.``。后果是堡垒机的「连接测试」
        （跑 ``uname -a``）在演示目标机上 HTTP 500，AI 的 ``run_command`` 这类工具也必然失败，
        只能走交互式 shell。演示环境应当是「真实可用的 Linux 替身」，所以这里补上。
        """
        text = command.decode("utf-8", "replace") if isinstance(command, bytes) else str(command)
        self.commands.append(text)
        logger.info("exec 请求：%s", text)
        threading.Thread(
            target=self._serve_exec, args=(channel, text), name="demo-exec", daemon=True
        ).start()
        return True

    @staticmethod
    def _serve_exec(channel, command: str) -> None:  # noqa: ANN001
        """把应答写回通道后**先发 EOF、让客户端自己关**，然后才关通道。

        注意：服务端 `channel.close()` 会立刻发 CLOSE，而客户端的 `Channel._close_internal()`
        会清空接收缓冲 —— 如果应答还没被读走就关闭，客户端会抛 `Channel closed.`（看起来像
        「exec 被拒绝」）。所以这里用 `shutdown_write()` 发 EOF（客户端读到 EOF 后正常结束读取），
        稍等片刻再真正关闭。
        """
        try:
            channel.send(respond(command).encode("utf-8"))
            channel.send_exit_status(0)
            channel.shutdown_write()
            time.sleep(0.2)
        except Exception:  # noqa: BLE001 - 客户端提前断开
            pass
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass

    def check_channel_window_change_request(self, channel, width, height, pw, ph) -> bool:  # noqa: ANN001, ARG002
        return True

    def check_channel_subsystem_request(self, channel, name: str) -> bool:  # noqa: ANN001
        """开放 sftp 子系统（文件管理器用），其余一律拒绝。

        注意：paramiko 的**默认实现**才是真正把子系统 handler 实例化并起线程的地方
        （见 paramiko/server.py:360-388）。这里必须委托给 ``super()``，否则直接返回
        True 会让客户端在 SFTP 协商阶段拿到 EOF（``EOF during negotiation``）。
        """
        if name == "sftp":
            self.sftp_ready.set()
            return super().check_channel_subsystem_request(channel, name)
        return False


def line_discipline_echo(byte: int, buffer: bytearray, prompt: str) -> bytes:
    """模拟真实 tty 驱动（ECHO 标志）的行规程：返回该字节要回显给客户端的字节。

    真实 Linux 的**逐字符回显由 tty 驱动做**，shell 本身不回显；本演示目标机早期版本
    只在收到 Enter 后整行回显，所以经堡垒机连接时用户敲键期间看不到任何字符（回显缺失）。
    这里把行规程抽成纯函数，方便用单测钉住（见 tests/test_demo_target_echo.py）。

    * 可打印字符与 TAB：追加进 buffer 并原样回显；
    * 退格 0x08/0x7F：``\\b \\b``（退一格、空格覆盖、再退一格）；
    * Ctrl-U 0x15：整行擦除；
    * Ctrl-C 0x03：``^C`` + 重画提示符，并清空行缓冲；
    * 其它控制字符：不回显、不入行缓冲（箭头键等转义序列在演示环境里不处理）。
    """
    if byte in (0x08, 0x7F):
        if buffer:
            buffer.pop()
            return b"\b \b"
        return b""
    if byte == 0x15:
        if buffer:
            out = b"\b \b" * len(buffer)
            buffer.clear()
            return out
        return b""
    if byte == 0x03:
        buffer.clear()
        return b"^C\r\n" + prompt.encode("utf-8", "replace")
    if byte < 0x20 and byte != 0x09:
        return b""
    buffer.append(byte)
    return bytes([byte])


class TtyEscapeFilter:
    """吞掉 tty 转义序列（CSI ``ESC [ …``、SS3 ``ESC O x``），只留下真正要回显/入行缓冲的字节。

    真实终端与行规程不会把转义序列当普通字符回显，也不会塞进行缓冲：方向键、粘贴标记
    （bracketed paste 的 ``ESC[200~`` / ``ESC[201~``）都是「模式信号」，由终端/readline 消费。

    演示目标机早期版本逐字节回显，于是经堡垒机粘贴 ``/ask-ai …`` 时，``[200~`` 与迟到的
    ``[201~`` 被当成普通文本写进行缓冲，用户下一条命令会粘成
    ``-bash: [201~echo: command not found``（真机 bash 有 bracketed paste 支持，不会）。
    这里把过滤抽成有状态的小类，跨 ``recv`` 保持半截序列，方便单测钉住。

    * 终结字节：CSI 的 0x40–0x7E（``~`` / 字母 / ``@`` 等），SS3（``ESC O x``）的 ``x``；
    * ``ESC`` 后直接跟控制字符：放弃序列并把控制字符交回调用方（回车不能被吞掉）；
    * 序列过长（> :attr:`MAX_LEN`）或嗅到不支持的形式（OSC 等）：放弃序列，之后的字节照常放行；
    * 单独的 ESC（后一个字节之前就断流）：留在 pending 里等下一次 ``feed``。
    """

    MAX_LEN = 32

    def __init__(self) -> None:
        self._pending = bytearray()
        self._ss3 = False

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            if not self._pending:
                if byte == 0x1B:
                    self._pending.append(byte)
                    continue
                out.append(byte)
                continue

            if self._ss3:
                # SS3（ESC O x）：x 就是终结字符，吃掉它
                self._pending.clear()
                self._ss3 = False
                continue
            if len(self._pending) == 1:
                if byte in (0x5B, 0x4F):  # '[' 起 CSI、'O' 起 SS3
                    self._pending.append(byte)
                    self._ss3 = byte == 0x4F
                    continue
                if 0x40 <= byte <= 0x7E:  # ESC + 单字符（如 ESC 7 / ESC M）
                    self._pending.clear()
                    continue
                if byte < 0x20:  # ESC 后直接跟控制字符：序列作废，控制字符照常处理
                    self._pending.clear()
                    out.append(byte)
                    continue
                self._pending.append(byte)
                continue

            if 0x40 <= byte <= 0x7E or len(self._pending) >= self.MAX_LEN:
                self._pending.clear()
                continue
            if byte < 0x20:
                self._pending.clear()
                out.append(byte)
                continue
            self._pending.append(byte)
        return bytes(out)


def shell_loop(channel: paramiko.Channel, server: DemoTargetServer) -> None:
    """极简行式 shell，但**实现真实 tty 的行规程（line discipline）**。

    历史缺陷：早期版本只在收到 Enter 后才把整行回显出去（`f"{line}\\r\\n{output}"`）。
    真实 Linux 的**逐字符回显是 tty 驱动（ECHO 标志）做的**，不是 shell 做的；所以经
    堡垒机连接这个演示目标机时，用户敲键期间一个字节都看不到 —— 表现为「输入不回显」。
    行规程的具体回显规则见 :func:`line_discipline_echo`。

    另外**转义序列必须先被 :class:`TtyEscapeFilter` 吃掉**：方向键与粘贴标记
    （``ESC[200~`` / ``ESC[201~``）在真机上由终端/readline 消费，逐字节回显的实现若不
    过滤就会把它们写进行缓冲（粘贴 ``/ask-ai`` 时表现为 ``[201~`` 粘到下一条命令上）。
    """
    banner = (
        "Welcome to demo-linux-01 (AutoOps 演示目标机)\r\n"
        "Last login: Wed Feb 11 09:58:12 2026 from 10.0.0.5\r\n"
    )
    marker = ""
    buffer = bytearray()
    esc_filter = TtyEscapeFilter()

    def prompt() -> str:
        return f"{marker}root@demo-linux-01:~# "

    channel.send(banner + prompt())
    while True:
        try:
            data = channel.recv(4096)
        except Exception:  # noqa: BLE001 - 通道关闭即退出
            return
        if not data:
            return
        for byte in esc_filter.feed(data):
            if byte in (0x0A, 0x0D):
                line = buffer.decode("utf-8", "replace")
                buffer.clear()
                text = line.strip()
                channel.send(b"\r\n")  # 命令行已逐字符回显，这里只补换行
                found = re.search(r"__BASTION_[0-9a-f]{16}__", line)
                if found and "PS1=" in line:
                    marker = found.group(0)
                    channel.send(prompt())
                    continue
                if text in ("exit", "logout"):
                    channel.send("logout\r\n")
                    time.sleep(0.05)
                    channel.close()
                    return
                server.commands.append(text)
                output = respond(text)
                channel.send(f"{output}{prompt()}")
            elif byte == 0x04:
                if not buffer:
                    channel.send("logout\r\n")
                    channel.close()
                    return
            else:
                channel.send(line_discipline_echo(byte, buffer, prompt()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AutoOps 本地演示 SSH 目标机")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=2200, help="监听端口（默认 2200）")
    parser.add_argument("--user", default="root", help="登录用户名（默认 root）")
    parser.add_argument("--password", default="s3cret", help="登录密码（默认 s3cret）")
    parser.add_argument(
        "--sftp-root",
        default="",
        help="SFTP 子系统暴露的本地根目录（默认 %%TEMP%%/bastion_sftp_demo，自动铺样例文件）",
    )
    args = parser.parse_args(argv)

    if not args.sftp_root:
        args.sftp_root = os.path.join(
            os.environ.get("TEMP") or os.environ.get("TMP") or ".", "bastion_sftp_demo"
        )
    seed_demo_root(args.sftp_root)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(16)
    sock.settimeout(0.5)
    logger.info("演示目标机已启动：ssh -p %s %s@%s", args.port, args.user, args.host)
    logger.info("在这台机器上可执行：%s", "、".join(sorted(RESPONSES)[:6]) + " 等")
    logger.info("SFTP 根目录：%s", args.sftp_root)

    try:
        while True:
            try:
                client, addr = sock.accept()
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break
            threading.Thread(
                target=_handle, args=(client, addr, args), name=f"demo-{addr[1]}", daemon=True
            ).start()
    except KeyboardInterrupt:
        logger.info("收到中断，正在退出")
    finally:
        sock.close()
    return 0


def _handle(client: socket.socket, addr, args) -> None:  # noqa: ANN001
    transport = None
    try:
        transport = paramiko.Transport(client)
        transport.add_server_key(_host_key())
        interface = DemoTargetServer(args.user, args.password)
        # sftp 子系统由 paramiko 在独立线程里跑，这里只负责把通道留住
        install_sftp_subsystem(transport, args.sftp_root, on_end=interface.sftp_done.set)
        transport.start_server(server=interface)
        channel = transport.accept(15)
        if channel is None:
            logger.warning("来自 %s 的连接未打开会话通道", addr)
            return
        # 客户端要么要 shell、要么要 sftp 子系统，等到其中一个标志被置位
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if interface.sftp_ready.is_set() or interface.shell_ready.is_set():
                break
            time.sleep(0.02)
        if interface.sftp_ready.is_set():
            logger.info("来自 %s 的 SFTP 会话已建立", addr)
            interface.sftp_done.wait(120)
            logger.info("来自 %s 的 SFTP 会话已结束", addr)
            return
        interface.shell_ready.wait(10)
        logger.info("来自 %s 的会话已建立", addr)
        shell_loop(channel, interface)
    except Exception as exc:  # noqa: BLE001
        logger.debug("连接 %s 处理异常：%s", addr, exc)
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
