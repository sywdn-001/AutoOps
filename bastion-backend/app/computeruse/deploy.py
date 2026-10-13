"""ComputerUse（Windows-MCP）部署编排：把一台 Windows 目标机变成「AI 能用鼠标键盘操作」的机器。

一次部署分 7 个阶段（前端向导的阶段式进度条直接读 `STAGES`）：

1. ``prepare``  连接目标机、环境体检（OS / 架构 / PowerShell / 磁盘 / 已有 Python 与 MCP）
2. ``upload``   把堡垒机缓存好的 Python 安装包推到目标机 ``C:\\Installer\\``
3. ``install``  静默安装 Python（``/quiet InstallAllUsers=1 PrependPath=1``）
4. ``pip``      ``pip install windows-mcp``（装在这个刚装好的 Python 上）
5. ``vbs``      写开机自启脚本并**立刻**启动 MCP（``serve --transport streamable-http``）
6. ``expose``   开 ``netsh portproxy`` + 防火墙规则，让堡垒机连得上只监听回环的 MCP
7. ``verify``   用 MCP 客户端探活（``tools/list``），把工具清单写回数据库

四条设计约束（每条都吃过亏，别改）：

* Windows-MCP 要求 Python **3.13+**，安装完成后必须回读实际版本确认。
* MCP 默认只监听 ``localhost``：堡垒机要访问就得靠 portproxy 把 LAN 地址转发到回环，
  再只放行堡垒机自己的 IP（``bindAll`` 打开时则直接监听 0.0.0.0，不需要 portproxy）。
* 目标机没有 JWT，下载安装包靠**带 HMAC 签名的一次性地址**（`signed_download_url`），
  签名不对就退回让目标机自己去华为云镜像拉。
* UIA / 截图按**登录会话**隔离：VBS 自启的 MCP 在控制台会话里，WebRDP 是另一个会话，
  所以侧边栏必须把 ``Screenshot`` 的结果内联渲染出来，操作员才看得到 AI 在做什么。

进度与日志都落在 `ComputerUseDeployment` 行上（`stage` / `progress` / `log_tail`），
进程重启后前端仍能回放最后 40 行；运行中的实时事件在内存里（`DeploymentRunner.events`）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..audit import log_event
from ..crypto import decrypt, encrypt
from ..extensions import db
from ..models import ComputerUseDeployment, Host, HostAccount, to_naive_utc, utcnow
from ..winrm import client as winrm_client
from .mcp import McpClient, McpError
from .mirror import MCP_MIN_VERSION, local_installer_path, read_sha256

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 阶段表：前端进度条与后端执行顺序都读它
# --------------------------------------------------------------------------- #
STAGES: tuple[tuple[str, str, int], ...] = (
    ("prepare", "连接目标机 / 环境体检", 6),
    ("upload", r"把 Python 安装包传到 C:\Installer", 28),
    ("install", "静默安装 Python（/quiet InstallAllUsers=1 PrependPath=1）", 48),
    ("pip", "安装 windows-mcp（pip）", 68),
    ("vbs", "写开机自启 VBS 并立即启动 MCP", 82),
    ("expose", "开 portproxy + 防火墙（只放行堡垒机）", 92),
    ("verify", "探活 MCP（tools/list）", 100),
)
STAGE_LABELS = {key: label for key, label, _ in STAGES}
STAGE_PROGRESS = {key: pct for key, _, pct in STAGES}

MAX_EVENTS = 2000  # 内存里最多留多少条日志事件（前端按 seq 续传，不需要无限留）
MAX_CONCURRENT = 2  # 同时最多几个部署在跑（WinRM 连接 + pip 都很吃目标机）
_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT)

TARGET_DIR = r"C:\Installer"
VBS_NAME = "AutoOps-ComputerUse.vbs"
CMD_NAME = "AutoOps-ComputerUse.cmd"
TAKEOVER_NAME = "AutoOps-ComputerUse-takeover.ps1"
VBS_LOCAL = rf"{TARGET_DIR}\{VBS_NAME}"
CMD_LOCAL = rf"{TARGET_DIR}\{CMD_NAME}"
TAKEOVER_LOCAL = rf"{TARGET_DIR}\{TAKEOVER_NAME}"
STARTUP_DIR = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp"
VBS_STARTUP = rf"{STARTUP_DIR}\{VBS_NAME}"
DOWNLOADER = rf"{TARGET_DIR}\download-installer.ps1"
DOWNLOAD_DONE = rf"{TARGET_DIR}\download.done"
DEFAULT_MCP_PORT = 8000
TASK_DOWNLOAD = "AutoOps-ComputerUse-Download"
TASK_MCP = "AutoOps-ComputerUse-MCP"

# 每个 PowerShell 片段都带上它：报错即停、不要进度条、控制台按 UTF-8 输出
PS_PREAMBLE = (
    "$ErrorActionPreference='Stop'; "
    "$ProgressPreference='SilentlyContinue'; "
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
)

SIGNED_DOWNLOAD_TTL = 7200  # 目标机下载安装包用的签名有效期（秒）


class DeployError(RuntimeError):
    """部署过程中的可预期失败：消息会直接展示给用户，所以必须是中文人话。"""


_CLIXML_ERROR_RE = re.compile(r'<S S="Error">(.*?)</S>', re.DOTALL)


def clean_remote_stderr(text: str) -> str:
    """WinRM 给的 stderr 多数是 PowerShell 的 CLIXML 包装。

    真机第一次跑时，服务器日志里出现的是 ``#< CLIXML`` 加一长串 XML（真正的错误文本埋在
    ``<S S="Error">`` 里），用户完全看不懂。这里只挑出错误文本，进度噪音直接丢掉；
    不是 CLIXML 的普通 stderr 原样返回。
    """
    if not text or not text.strip():
        return ""
    if "CLIXML" not in text and "<Objs" not in text:
        return text.strip()
    errors = [html.unescape(item).strip() for item in _CLIXML_ERROR_RE.findall(text)]
    return "\n".join(item for item in errors if item)


def mcp_listen_wait_script(port: int, old_pid: str = "") -> str:
    """生成「等新 MCP 真的接手端口」的 PowerShell。

    真机踩过的坑：接管脚本会先把老会话里那个 MCP 停掉再起新的，中间有几秒空档。
    如果只判断「端口有人在听」，就会把**还没被杀掉的老进程**误判成新的已经起来了，
    紧接着探活就被连接重置（实测 verify 阶段就是这么失败的）。
    所以记下老进程号，要求监听者换人。
    """
    expect_new = f"$new = ($owner -ne '{old_pid}')" if old_pid else "$new = $true"
    return (
        "$ErrorActionPreference='SilentlyContinue';$ok = $false;$who = '';"
        "for ($i = 0; $i -lt 40; $i++) {"
        f"  $c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -First 1;"
        "  $t = $false;"
        f"  if ($c) {{ $t = (Test-NetConnection -ComputerName 127.0.0.1 -Port {port} "
        "-WarningAction SilentlyContinue).TcpTestSucceeded };"
        "  if ($c -and $t) {"
        "    $owner = [string]$c.OwningProcess;"
        f"    {expect_new};"
        "    if ($new) { $ok = $true; $who = $owner; break }"
        "  };"
        "  Start-Sleep -Seconds 2 };"
        '"LISTEN=$ok WHO=$who"'
    )


# --------------------------------------------------------------------------- #
# 下载安装包的签名地址（目标机没有 JWT）
# --------------------------------------------------------------------------- #
def _sign_download(filename: str, expires: str, secret: str) -> str:
    payload = f"{filename}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def signed_download_url(
    bastion_url: str, filename: str, *, secret: str, ttl: int = SIGNED_DOWNLOAD_TTL
) -> str:
    """给目标机用的下载地址：带上过期时间与 HMAC，不需要登录态。"""
    expires = str(int(time.time()) + int(ttl))
    signature = _sign_download(filename, expires, secret)
    base = bastion_url.rstrip("/")
    return f"{base}/api/computer-use/installers/{quote(filename)}?e={expires}&s={signature}"


def verify_download_signature(filename: str, expires: str, signature: str, secret: str) -> bool:
    """校验签名与有效期；任何一项不对都当没签名。"""
    if not filename or not expires or not signature or not secret:
        return False
    try:
        if int(expires) < int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    expected = _sign_download(filename, str(expires), secret)
    return hmac.compare_digest(expected, str(signature))


# --------------------------------------------------------------------------- #
# 上下文与事件
# --------------------------------------------------------------------------- #
@dataclass
class DeployContext:
    """一次部署要用到的所有输入 + 运行期探到的结果。"""

    deployment_id: int
    host_id: int
    account_id: int | None = None
    account_username: str = ""
    account_password: str = ""
    python_version: str = ""
    installer_name: str = ""
    options: dict = field(default_factory=dict)
    bastion_url: str = ""
    bastion_ip: str = ""
    host_name: str = ""
    host_address: str = ""
    deployed_by: str = ""

    # 运行期
    target: Any = None
    connection: Any = None
    python_path: str = ""
    python_version_actual: str = ""
    mcp_exe: str = ""
    mcp_version: str = ""
    auth_key: str = ""
    mcp_url: str = ""
    lan_ip: str = ""
    installer_size: int = 0
    installer_sha256: str = ""

    @property
    def mcp_port(self) -> int:
        return int(self.options.get("mcpPort") or DEFAULT_MCP_PORT)

    @property
    def bind_all(self) -> bool:
        return bool(self.options.get("bindAll"))

    @property
    def host_for_url(self) -> str:
        """写进数据库的 MCP 地址用哪个主机名：绑 0.0.0.0 就直接用工单里的地址。"""
        if self.bind_all:
            return self.host_address
        return self.lan_ip or self.host_address


@dataclass
class Event:
    seq: int
    ts: float
    level: str
    stage: str
    message: str
    progress: int | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).strftime("%H:%M:%S"),
            "level": self.level,
            "stage": self.stage,
            "stageLabel": STAGE_LABELS.get(self.stage, self.stage),
            "message": self.message,
            "progress": self.progress,
        }


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class DeploymentRunner:
    """跑一次部署：逐阶段执行、把日志同时写内存（给 SSE）与数据库（给回放）。"""

    def __init__(self, deployment_id: int, ctx: DeployContext) -> None:
        self.deployment_id = int(deployment_id)
        self.ctx = ctx
        self.status = "running"
        self.stage = "queued"
        self.progress = 0
        self.error = ""
        self.started_at = utcnow()
        self.finished_at: datetime | None = None
        self.events: list[Event] = []
        self._seq = 0
        self._log_tail: list[str] = []
        self._cond = threading.Condition()
        self._cancel = threading.Event()
        self._app = None

    # ---------------------------------------------------------------- 日志
    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(
        self,
        message: str,
        *,
        level: str = "info",
        stage: str | None = None,
        progress: int | None = None,
    ) -> Event:
        with self._cond:
            self._seq += 1
            if progress is not None:
                self.progress = max(0, min(100, int(progress)))
            event = Event(
                seq=self._seq,
                ts=time.time(),
                level=level,
                stage=stage or self.stage,
                message=str(message),
                progress=self.progress,
            )
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            for line in str(message).splitlines() or [""]:
                self._log_tail.append(f"[{event.stage}] {line}")
            self._log_tail = self._log_tail[-40:]
            self._cond.notify_all()
        return event

    def emit_output(self, text: str, *, stage: str | None = None, limit: int = 4000) -> None:
        """把命令输出按行发成日志（太长的只留尾巴，别把前端刷爆）。"""
        body = (text or "").strip()
        if not body:
            return
        if len(body) > limit:
            body = "…（前面省略）\n" + body[-limit:]
        for line in body.splitlines():
            self.emit(line, stage=stage)

    def wait_for_events(self, since: int = 0, timeout: float = 20.0) -> list[Event]:
        """等 ``since`` 之后的新事件；没有新事件就等到超时（SSE 用它发心跳）。"""
        since = int(since or 0)
        deadline = time.time() + timeout
        with self._cond:
            while True:
                pending = [event for event in self.events if event.seq > since]
                if pending or self.status != "running":
                    return pending
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    def snapshot(self) -> dict:
        return {
            "id": self.deployment_id,
            "status": self.status,
            "stage": self.stage,
            "stageLabel": STAGE_LABELS.get(self.stage, self.stage),
            "progress": self.progress,
            "error": self.error,
            "lastSeq": self._seq,
            "live": self.status == "running",
        }

    # ---------------------------------------------------------------- 取消
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> bool:
        if self.status != "running":
            return False
        self._cancel.set()
        self.emit("收到取消请求，正在收尾…", level="warning")
        return True

    # ---------------------------------------------------------------- 数据库
    def _update_row(self, **fields) -> None:
        """把状态写回数据库（同时刷新 log_tail，供进程重启后回放）。"""
        if self._app is None:
            return
        with self._app.app_context():
            row = db.session.get(ComputerUseDeployment, self.deployment_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)
            row.log_tail = "\n".join(self._log_tail[-40:])
            db.session.commit()

    # ---------------------------------------------------------------- 执行
    def run(self, app) -> None:
        self._app = app
        self._update_row(
            status="running",
            stage="prepare",
            progress=0,
            error="",
            started_at=to_naive_utc(self.started_at),
        )
        log_event(
            "computeruse",
            "deploy_start",
            message=(
                f"{self.ctx.deployed_by or 'admin'} 开始部署 ComputerUse："
                f"{self.ctx.host_name}（{self.ctx.host_address}），"
                f"Python {self.ctx.python_version}"
            ),
            target_type="host",
            target_id=self.ctx.host_id,
            target_name=self.ctx.host_name,
            actor_username=self.ctx.deployed_by,
            detail={
                "deploymentId": self.deployment_id,
                "hostId": self.ctx.host_id,
                "installer": self.ctx.installer_name,
                "options": self.ctx.options,
                "bastionUrl": self.ctx.bastion_url,
            },
        )
        try:
            for key, label, pct in STAGES:
                if self.cancelled:
                    raise DeployError("部署已取消")
                self.stage = key
                start_pct = max(self.progress, min(100, pct - 20)) if pct > 20 else self.progress
                self.emit(f"▶ {label}", stage=key, progress=start_pct)
                self._update_row(stage=key, progress=start_pct)
                getattr(self, f"_stage_{key}")(app)
                self.emit(f"✓ {label} 完成", level="success", stage=key, progress=pct)
                self.progress = pct
                self._update_row(stage=key, progress=pct)
            self.status = "success"
        except DeployError as exc:
            self.status = "cancelled" if self.cancelled else "failed"
            self.error = str(exc)
            self.emit(str(exc), level="warning" if self.cancelled else "error")
        except Exception as exc:  # noqa: BLE001 - 任何意外都要落到记录里，不能让线程静默死掉
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            self.emit(f"部署过程中出现意外错误：{self.error}", level="error")
            logger.exception("ComputerUse 部署 #%s 失败", self.deployment_id)
        finally:
            self._close_connection()
            self.finished_at = utcnow()
            self._update_row(
                status=self.status,
                error=self.error,
                finished_at=to_naive_utc(self.finished_at),
            )
            if self.status == "success":
                self.emit("部署完成，ComputerUse 侧边栏已可用", level="success", progress=100)
            with self._cond:
                self._cond.notify_all()
            log_event(
                "computeruse",
                "deploy_success" if self.status == "success" else "deploy_failed",
                result="success" if self.status == "success" else "failure",
                message=(
                    f"{self.ctx.host_name} 的 ComputerUse 部署"
                    + ("成功" if self.status == "success" else f"失败：{self.error}")
                ),
                target_type="host",
                target_id=self.ctx.host_id,
                target_name=self.ctx.host_name,
                actor_username=self.ctx.deployed_by,
                detail={
                    "deploymentId": self.deployment_id,
                    "stage": self.stage,
                    "mcpUrl": self.ctx.mcp_url,
                    "python": self.ctx.python_path,
                    "mcpVersion": self.ctx.mcp_version,
                },
            )

    # ---------------------------------------------------------------- 远程执行
    def _ps(
        self, script: str, *, timeout: int = 120, stage: str | None = None, label: str = ""
    ) -> tuple[int, str, str]:
        """在目标机上跑一段 PowerShell，返回 (exit_code, stdout, stderr)。"""
        if self.cancelled:
            raise DeployError("部署已取消")
        if self.ctx.connection is None:
            raise DeployError("与目标机的连接已经断开，请重新部署")
        body = PS_PREAMBLE + script
        if label:
            self.emit(f"$ {label}", stage=stage)
        try:
            result = winrm_client.run_script(
                self.ctx.connection, body, timeout=timeout, cancel=self._cancel
            )
        except winrm_client.WinrmError as exc:
            raise DeployError(f"在目标机上执行命令失败：{exc}") from exc
        if result.cancelled or self.cancelled:
            raise DeployError("部署已取消")
        if result.timed_out:
            raise DeployError(f"目标机命令超时（{timeout} 秒）：{label or 'PowerShell'}")
        noise = clean_remote_stderr(result.stderr)
        if noise:
            self.emit_output(noise, stage=stage)
        return result.exit_code, result.stdout, result.stderr

    def _write_remote_file(self, path: str, data: bytes, *, stage: str, label: str = "") -> None:
        """把字节写进目标机文件（base64 走命令行，避免引号与编码问题）。"""
        encoded = base64.b64encode(data).decode("ascii")
        parent = path.rsplit("\\", 1)[0] if "\\" in path else ""
        prefix = f"New-Item -ItemType Directory -Force -Path '{parent}' | Out-Null;" if parent else ""
        script = (
            f"{prefix}"
            f"$bytes = [System.Convert]::FromBase64String('{encoded}');"
            f"[System.IO.File]::WriteAllBytes('{path}', $bytes);"
            f"(Get-Item '{path}').Length"
        )
        code, out, _ = self._ps(script, timeout=180, stage=stage, label=label or f"写入 {path}")
        if code != 0:
            raise DeployError(f"写入目标机文件失败（exit={code}）：{path}")
        try:
            if int(out.strip().splitlines()[-1]) != len(data):
                raise DeployError(f"写入目标机文件后大小不一致：{path}")
        except (ValueError, IndexError):
            self.emit(f"（未能回读 {path} 的大小，继续）", level="warning", stage=stage)

    def _close_connection(self) -> None:
        if self.ctx.connection is not None:
            try:
                winrm_client.close(self.ctx.connection)
            except Exception:  # noqa: BLE001 - 关连接失败不该影响部署结论
                pass
            self.ctx.connection = None

    # ------------------------------------------------------------- 长任务启动
    # WinRM 的坑（win-75 真机实测）：命令一结束，WinRS 会把这条命令拉起来的子进程
    # 一起杀掉。所以「后台下载」「立刻启动 MCP」都不能用 Start-Process，
    # 必须交给计划任务（schtasks）去跑——计划任务的生命周期不挂在 WinRM 会话上。
    def _run_detached(
        self,
        task_name: str,
        program: str,
        arguments: str,
        *,
        stage: str,
        label: str,
        interactive: bool = False,
    ) -> str:
        """用计划任务在目标机上跑一个「活过 WinRM 命令」的进程，返回目标机回显。"""
        user = self.ctx.account_username or ""
        if not user:
            raise DeployError("不知道用哪个 Windows 账号跑计划任务：账号信息缺失")
        # 账号里没写域就用目标机自己的计算机名（堡垒机上的主机名跟 Windows 计算机名不是一回事）
        user_ps = f"'{user}'" if "\\" in user else f"($env:COMPUTERNAME + '\\{user}')"
        # Interactive = 只在有人登录桌面时跑（进程落在那个桌面会话里，截图才不是黑的）；
        # S4U = 不需要口令的「不存储密码」方式，会话 0 也能跑（适合纯后台下载）。
        want = "Interactive" if interactive else "S4U"
        script = (
            "$ErrorActionPreference='Continue';"
            "$active = $false;"
            "try { $active = -not ((quser 2>&1 | Out-String) -match 'No User exists') } catch {};"
            f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false "
            "-ErrorAction SilentlyContinue | Out-Null;"
            f"$action = New-ScheduledTaskAction -Execute '{program}' -Argument '{arguments}';"
            # 没人在桌面登录时 Interactive 任务会被挂住不跑 —— 自动退回 S4U（会话 0）
            f"$logon = if ($active) {{ '{want}' }} else {{ 'S4U' }};"
            f"$principal = New-ScheduledTaskPrincipal -UserId {user_ps} "
            "-LogonType $logon -RunLevel Highest;"
            f"Register-ScheduledTask -TaskName '{task_name}' -Action $action "
            "-Principal $principal -Force | Out-Null;"
            f"Start-ScheduledTask -TaskName '{task_name}';"
            "Start-Sleep -Seconds 3;"
            f"$t = Get-ScheduledTask -TaskName '{task_name}';"
            f"$i = Get-ScheduledTaskInfo -TaskName '{task_name}';"
            '"TASK=$($t.State) LOGON=$logon ACTIVE=$active RESULT=$($i.LastTaskResult)"'
        )
        code, out, _ = self._ps(script, timeout=180, stage=stage, label=label)
        summary = ""
        for line in (out or "").splitlines():
            if line.startswith("TASK="):
                summary = line.strip()
        if code != 0 and not summary:
            raise DeployError(f"目标机上创建计划任务失败（exit={code}）：{task_name}")
        if summary:
            self.emit_output(summary, stage=stage)
        return summary

    def _delete_task(self, task_name: str, *, stage: str) -> None:
        try:
            self._ps(
                f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false "
                "-ErrorAction SilentlyContinue | Out-Null; 'CLEANED'",
                timeout=60,
                stage=stage,
            )
        except DeployError:
            pass

    # ---------------------------------------------------------------- 阶段 1
    def _stage_prepare(self, app) -> None:
        """连上目标机，先看看这台机器是什么状况。"""
        with app.app_context():
            host = db.session.get(Host, self.ctx.host_id)
            if host is None:
                raise DeployError("主机已不存在，请刷新页面后重试")
            account = (
                db.session.get(HostAccount, self.ctx.account_id) if self.ctx.account_id else None
            )
            if account is None:
                account = next(
                    (item for item in host.accounts if (item.auth_type or "password") == "password"),
                    None,
                )
            if account is None:
                raise DeployError(
                    f"{host.name} 没有可用的口令账号：WinRM 不接受私钥登录，"
                    "请先加一个「用户名 + 口令」账号"
                )
            endpoint = host.endpoint_for("winrm")
            if endpoint is None:
                raise DeployError(
                    f"{host.name} 没有 WinRM 端点：请先在「资产管理 → 编辑主机」里勾上"
                    " Windows 命令行（WinRM）"
                )
            self.ctx.host_name = host.name
            self.ctx.host_address = host.address
            self.ctx.account_username = account.username or ""
            # 计划任务要以这个账号跑（Interactive/S4U 两种登录类型都不需要口令，
            # 但 S4U 需要账号名；口令留着是为了在 S4U 不可用时退回带口令的 /ru /rp 方式）
            if (account.auth_type or "password") == "password" and account.secret_enc:
                self.ctx.account_password = decrypt(account.secret_enc)
            secret = str(app.config.get("SECRET_KEY") or "")
            # 目标机没有登录态，用签名地址从堡垒机拉安装包
            if self.ctx.bastion_url and secret and self.ctx.installer_name:
                self.ctx.options["downloadUrl"] = signed_download_url(
                    self.ctx.bastion_url, self.ctx.installer_name, secret=secret
                )
            self.ctx.target = winrm_client.build_target(
                host,
                account,
                port=endpoint.port,
                winrm_transport=endpoint.winrm_transport,
                connect_timeout=15,
                command_timeout=900,
            )

        self.emit(
            f"连接 {self.ctx.host_address}"
            f"（{self.ctx.target.endpoint}，账号 {self.ctx.target.username}）…"
        )
        try:
            self.ctx.connection = winrm_client.connect(self.ctx.target)
        except winrm_client.WinrmError as exc:
            raise DeployError(f"连接目标机失败：{exc}") from exc
        self.emit("已连上目标机，开始环境体检", level="success")

        script = (
            "$os = (Get-CimInstance Win32_OperatingSystem);"
            "\"OS=$($os.Caption) $($os.Version)\";"
            "\"ARCH=$env:PROCESSOR_ARCHITECTURE\";"
            "\"PS=$($PSVersionTable.PSVersion)\";"
            "$drive = Get-PSDrive -Name C;"
            "\"FREE_GB={0:N1}\" -f ($drive.Free / 1GB);"
            "$py = Get-Command python.exe -ErrorAction SilentlyContinue;"
            "if ($py) { \"PYTHON=$($py.Source)\"; \"PYVER=$(& $py.Source -c 'import platform;print(platform.python_version())')\" } else { 'PYTHON=' };"
            "$mcp = Get-Command windows-mcp.exe -ErrorAction SilentlyContinue;"
            "if ($mcp) { \"MCP=$($mcp.Source)\" } else { 'MCP=' }"
        )
        code, out, _ = self._ps(script, timeout=180, stage="prepare", label="环境体检")
        if code != 0:
            raise DeployError("目标机环境体检失败，请确认这台机器能正常执行 PowerShell")
        info: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip()
        for key in ("OS", "ARCH", "PS", "FREE_GB"):
            if info.get(key):
                self.emit(f"{key}: {info[key]}", stage="prepare")
        if info.get("ARCH") and "64" not in info["ARCH"]:
            raise DeployError("目标机是 32 位系统：Windows-MCP 需要 64 位 Windows")
        if info.get("PYTHON"):
            self.emit(
                f"目标机已有 Python（{info.get('PYVER') or '版本未知'}，{info['PYTHON']}），"
                "本次仍会按选择的版本装一份新的（互不影响）",
                level="warning",
                stage="prepare",
            )
        if info.get("MCP"):
            self.emit(
                f"目标机已经在跑 windows-mcp（{info['MCP']}），部署完成后会被新的自启脚本接管",
                level="warning",
                stage="prepare",
            )
        self.emit("环境体检通过", level="success", stage="prepare")

    # ---------------------------------------------------------------- 阶段 2
    def _stage_upload(self, app) -> None:
        """把 Python 安装包推到目标机（优先从堡垒机带签名下载）。"""
        local = local_installer_path(self.ctx.installer_name)
        if not local.exists():
            raise DeployError(
                f"堡垒机上没有 {self.ctx.installer_name}：请回到第 1 步先缓存这个安装包"
            )
        local_size = local.stat().st_size
        local_sha = read_sha256(self.ctx.installer_name)
        self.ctx.installer_size = local_size
        self.ctx.installer_sha256 = local_sha
        self.emit(
            f"堡垒机上的安装包：{self.ctx.installer_name}（{local_size / 1024 / 1024:.1f} MB）",
            stage="upload",
        )

        remote_exe = rf"{TARGET_DIR}\{self.ctx.installer_name}"
        url = self.ctx.options.get("downloadUrl") or ""
        fallback = (
            f"https://mirrors.huaweicloud.com/python/{self.ctx.python_version}/"
            f"{self.ctx.installer_name}"
        )
        if url:
            self.emit("目标机会从堡垒机下载安装包（带签名的临时地址）", stage="upload")
        else:
            url = fallback
            self.emit(
                "没有可用的堡垒机地址，目标机改为直接从华为云镜像下载", level="warning", stage="upload"
            )

        lines = [
            "$ErrorActionPreference='Stop'",
            "$ProgressPreference='SilentlyContinue'",
            f"$dst = '{remote_exe}'",
            f"$tmp = '{remote_exe}.part'",
            f"$done = '{DOWNLOAD_DONE}'",
            "Remove-Item $done,$tmp -Force -ErrorAction SilentlyContinue",
            "function Report([string]$text) { Set-Content -Path $done -Value $text -Encoding ascii }",
            "try {",
            "  $size = 0",
            "  $sha = ''",
            "  try {",
            f"    Invoke-WebRequest -Uri '{url}' -OutFile $tmp -UseBasicParsing -TimeoutSec 3600",
            "  } catch {",
            f"    if ('{fallback}' -ne '{url}') {{",
            f"      Invoke-WebRequest -Uri '{fallback}' -OutFile $tmp -UseBasicParsing -TimeoutSec 3600",
            "    } else { throw }",
            "  }",
            "  Move-Item $tmp $dst -Force",
            "  $size = (Get-Item $dst).Length",
            "  $sha = (Get-FileHash $dst -Algorithm SHA256).Hash.ToLower()",
            "  Report \"OK $size $sha\"",
            "} catch {",
            "  Report \"FAIL $($_.Exception.Message)\"",
            "}",
        ]
        self._write_remote_file(
            DOWNLOADER,
            ("\r\n".join(lines) + "\r\n").encode("utf-8-sig"),
            stage="upload",
            label=f"下发下载脚本 {DOWNLOADER}",
        )
        self._run_detached(
            TASK_DOWNLOAD,
            "powershell.exe",
            f"-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {DOWNLOADER}",
            stage="upload",
            label="交给计划任务在目标机后台下载安装包",
        )

        deadline = time.time() + 3600
        poll = (
            f"$done='{DOWNLOAD_DONE}';$part='{remote_exe}.part';"
            "if (Test-Path $done) { Get-Content -Raw $done } "
            "elseif (Test-Path $part) { 'PART ' + (Get-Item $part).Length } else { 'PENDING' }"
        )
        last_report = 0.0
        glitches = 0
        expected = local_size or 1
        while True:
            if self.cancelled:
                raise DeployError("部署已取消（下载安装包阶段）")
            if time.time() > deadline:
                raise DeployError("目标机下载安装包超时（超过 60 分钟）")
            try:
                _, out, _ = self._ps(poll, timeout=60, stage="upload")
                glitches = 0
            except DeployError as exc:
                # 轮询期间 WinRM 抖一下（实测见过「拒绝了连接」）不该把整个部署判死
                glitches += 1
                if glitches >= 6:
                    raise
                self.emit(
                    f"读取下载进度失败（第 {glitches} 次，稍后重试）：{exc}",
                    level="warning",
                    stage="upload",
                )
                time.sleep(5)
                continue
            text = (out or "").strip()
            if text.startswith("OK "):
                parts = text.split()
                size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                sha = parts[2].lower() if len(parts) > 2 else ""
                self.emit(f"目标机已下载完成：{size / 1024 / 1024:.1f} MB", stage="upload")
                if size != local_size:
                    raise DeployError(
                        f"下载到的安装包大小不对（目标机 {size} 字节，堡垒机 {local_size} 字节）"
                    )
                if local_sha and sha and sha != local_sha.lower():
                    raise DeployError("下载到的安装包 SHA256 与堡垒机上的不一致，已中止部署")
                self.emit("大小与 SHA256 校验通过", level="success", stage="upload")
                self._delete_task(TASK_DOWNLOAD, stage="upload")
                return
            if text.startswith("FAIL "):
                self._delete_task(TASK_DOWNLOAD, stage="upload")
                raise DeployError(f"目标机下载安装包失败：{text[5:].strip()[:400]}")
            if text.startswith("PART ") and time.time() - last_report > 10:
                last_report = time.time()
                try:
                    done = int(text.split()[1])
                except (IndexError, ValueError):
                    done = 0
                self.emit(
                    f"已下载 {done / 1024 / 1024:.1f} MB / {expected / 1024 / 1024:.1f} MB"
                    f"（{min(99, int(done * 100 / expected))}%）",
                    stage="upload",
                )
            time.sleep(4)

    # ---------------------------------------------------------------- 阶段 3
    def _stage_install(self, app) -> None:
        """静默安装 Python，并回读实际版本确认满足 Windows-MCP 的要求。"""
        remote_exe = rf"{TARGET_DIR}\{self.ctx.installer_name}"
        self.emit("开始静默安装（可能要几分钟，请勿关闭页面）", stage="install")
        script = (
            f"$p = Start-Process -FilePath '{remote_exe}' -Wait -PassThru -ArgumentList "
            "'/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0','Include_launcher=1';"
            "\"EXIT=$($p.ExitCode)\""
        )
        code, out, _ = self._ps(script, timeout=3600, stage="install", label="运行 Python 安装包")
        exit_code = None
        for line in out.splitlines():
            if line.startswith("EXIT="):
                try:
                    exit_code = int(line.split("=", 1)[1])
                except ValueError:
                    exit_code = None
        if exit_code is None:
            exit_code = code
        if exit_code not in (0, 3010):
            raise DeployError(
                f"Python 安装失败（退出码 {exit_code}）："
                "0 表示成功、3010 表示成功但需要重启，其它值请到目标机上看安装日志"
            )
        self.emit(f"安装程序返回 {exit_code}（0/3010 都算成功）", level="success", stage="install")

        find = (
            "$cands = @();"
            "foreach ($root in @('C:\\Program Files','C:\\Program Files (x86)')) {"
            "  $cands += Get-ChildItem -Path (Join-Path $root 'Python*\\python.exe') -ErrorAction SilentlyContinue"
            "};"
            "$onPath = Get-Command python.exe -ErrorAction SilentlyContinue;"
            "if ($onPath) { $cands += Get-Item $onPath.Source };"
            "$best = $null; $bestVer = $null; $bestText = '';"
            "foreach ($c in ($cands | Sort-Object -Property FullName -Unique)) {"
            "  try { $v = & $c.FullName -c 'import platform;print(platform.python_version())' } catch { continue };"
            "  $t = [version]($v.Trim());"
            "  if ($null -eq $bestVer -or $t -gt $bestVer) { $bestVer = $t; $best = $c.FullName; $bestText = $v.Trim() }"
            "};"
            "if ($best) { \"PYTHON=$best\"; \"PYVER=$bestText\" } else { 'PYTHON=' }"
        )
        _, out2, _ = self._ps(find, timeout=180, stage="install", label="查找刚装好的 Python")
        found: dict[str, str] = {}
        for line in out2.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                found[key.strip()] = value.strip()
        if not found.get("PYTHON"):
            raise DeployError("安装程序说成功了，但没找到 python.exe：请到目标机确认安装是否真的完成")
        self.ctx.python_path = found["PYTHON"]
        self.ctx.python_version_actual = found.get("PYVER", "")
        self.emit(
            f"新 Python：{self.ctx.python_path}（{self.ctx.python_version_actual}）",
            level="success",
            stage="install",
        )
        try:
            actual = tuple(int(part) for part in self.ctx.python_version_actual.split(".")[:2])
        except ValueError:
            actual = MCP_MIN_VERSION
        if actual < MCP_MIN_VERSION:
            raise DeployError(
                f"Python {self.ctx.python_version_actual} 太旧了："
                f"Windows-MCP 需要 {'.'.join(str(p) for p in MCP_MIN_VERSION)}+，"
                "请回到第 1 步换一个更新的版本"
            )

    # ---------------------------------------------------------------- 阶段 4
    def _stage_pip(self, app) -> None:
        """装 windows-mcp（先升 pip，装完回读版本与可执行文件路径）。"""
        python = self.ctx.python_path
        index = (self.ctx.options.get("pipIndex") or "").strip()
        self.emit("升级 pip…", stage="pip")
        _, out, _ = self._ps(
            f"& '{python}' -m pip install --disable-pip-version-check --no-warn-script-location "
            "--upgrade pip 2>&1 | Select-Object -Last 5",
            timeout=900,
            stage="pip",
        )
        self.emit_output(out, stage="pip")

        self.emit("安装 windows-mcp（PyPI）…", stage="pip")
        install_cmd = (
            f"& '{python}' -m pip install --disable-pip-version-check --no-warn-script-location "
            + (f"-i '{index}' " if index else "")
            + "windows-mcp 2>&1 | Select-Object -Last 25"
        )
        _, out2, _ = self._ps(install_cmd, timeout=1800, stage="pip")
        self.emit_output(out2, stage="pip")
        if "Successfully installed" not in out2 and "already satisfied" not in out2.lower():
            raise DeployError(
                "pip 安装 windows-mcp 没有成功：请检查目标机能不能访问 PyPI"
                + ("（当前使用自定义镜像源，可在部署选项里清空）" if index else "")
            )

        probe = (
            f"$py = '{python}';"
            "$ver = (& $py -m pip show windows-mcp 2>$null | Select-String '^Version:' |"
            " ForEach-Object { $_.Line.Split(':')[1].Trim() });"
            "\"MCPVER=$ver\";"
            "$exe = Join-Path (Split-Path $py) 'Scripts\\windows-mcp.exe';"
            "if (Test-Path $exe) { \"MCPEXE=$exe\" } else { 'MCPEXE=' }"
        )
        _, out3, _ = self._ps(probe, timeout=180, stage="pip", label="确认 windows-mcp 安装结果")
        info: dict[str, str] = {}
        for line in out3.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip()
        self.ctx.mcp_version = info.get("MCPVER", "")
        self.ctx.mcp_exe = info.get("MCPEXE", "")
        if not self.ctx.mcp_exe:
            raise DeployError(
                "装完了但找不到 windows-mcp.exe：请手动执行 "
                f"\"{python}\" -m windows_mcp --help 确认这个包能不能跑"
            )
        self.emit(
            f"windows-mcp {self.ctx.mcp_version or '(版本未知)'} → {self.ctx.mcp_exe}",
            level="success",
            stage="pip",
        )

    # ---------------------------------------------------------------- 阶段 5
    def _stage_vbs(self, app) -> None:
        """写自启脚本（无窗口）并立刻启动，确认端口真的监听起来。"""
        port = self.ctx.mcp_port
        host_arg = "0.0.0.0" if self.ctx.bind_all else "localhost"
        auth_key = (self.ctx.options.get("authKey") or "").strip()
        self.ctx.auth_key = auth_key

        exe = self.ctx.mcp_exe or "windows-mcp.exe"
        takeover_lines = [
            "# AutoOps ComputerUse - 端口接管：把别个会话里残留的 MCP 停掉，让新会话的 MCP 能监听到端口。",
            "# 由堡垒机生成。Windows-MCP 的截图/界面自动化只认「自己所在的那个登录会话」，",
            "# 所以用户 RDP 登录时，开机自启脚本会用它把会话 0 里那份接管过来。",
            "param([int]$Port = 8000)",
            "$ErrorActionPreference = 'SilentlyContinue'",
            "$logDir = Join-Path $env:USERPROFILE '.windows-mcp'",
            "$log = Join-Path $logDir 'launcher.log'",
            "$listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port "
            "-WarningAction SilentlyContinue).TcpTestSucceeded",
            "if ($listening) {",
            "  Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |",
            "    ForEach-Object {",
            "      $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue",
            "      if ($p -and $p.ProcessName -match 'python|windows-mcp') {",
            "        Add-Content -Path $log -Value ('[{0}] 端口 {1} 被旧会话的 {2}（pid {3}）占着，先停掉它' "
            "-f (Get-Date).ToString('HH:mm:ss'), $Port, $p.ProcessName, $p.Id)",
            "        Stop-Process -Id $p.Id -Force",
            "      }",
            "    }",
            "  Start-Sleep -Seconds 2",
            "}",
            "",
        ]
        self._write_remote_file(
            TAKEOVER_LOCAL,
            # 用 utf-8-sig：PowerShell 5.1 只有看到 BOM 才会把无扩展名之外的 .ps1 当 UTF-8 读，
            # 否则脚本里的中文注释会被按 ANSI 解码成乱码。
            ("\r\n".join(takeover_lines)).encode("utf-8-sig"),
            stage="vbs",
            label=f"写入端口接管脚本 {TAKEOVER_LOCAL}",
        )
        cmd_lines = [
            "@echo off",
            "rem AutoOps ComputerUse - silent autostart of Windows-MCP (generated by Bastion, do not edit)",
            'if not exist "%USERPROFILE%\\.windows-mcp" mkdir "%USERPROFILE%\\.windows-mcp"',
            f'powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{TAKEOVER_LOCAL}" -Port {port} >> "%USERPROFILE%\\.windows-mcp\\launcher.log" 2>&1',
            "set ANONYMIZED_TELEMETRY=false",
            "set WINDOWS_MCP_DISABLE_FLASH=1",
            "set WINDOWS_MCP_STATELESS_HTTP=true",
        ]
        if auth_key:
            cmd_lines.append(f"set WINDOWS_MCP_AUTH_KEY={auth_key}")
        cmd_lines += [
            f'"{exe}" serve --transport streamable-http --host {host_arg} --port {port} '
            '>> "%USERPROFILE%\\.windows-mcp\\server.log" 2>&1',
            "",
        ]
        # .cmd 与 .vbs 一律保持纯 ASCII（cmd.exe / wscript 对 BOM 的处理不统一），
        # 中文注释只写在用 utf-8-sig 落盘的 .ps1 里。
        self._write_remote_file(
            CMD_LOCAL,
            ("\r\n".join(cmd_lines)).encode("ascii"),
            stage="vbs",
            label=f"写入启动脚本 {CMD_LOCAL}",
        )

        vbs_lines = [
            "' AutoOps ComputerUse - silent logon autostart (no window). Generated by Bastion; do not edit.",
            "Option Explicit",
            "Dim sh",
            "Set sh = CreateObject(\"WScript.Shell\")",
            f'sh.Run "cmd /c ""{CMD_LOCAL}""", 0, False',
            "",
        ]
        self._write_remote_file(
            VBS_LOCAL,
            ("\r\n".join(vbs_lines)).encode("ascii"),
            stage="vbs",
            label=f"写入 VBS {VBS_LOCAL}",
        )
        self._ps(
            f"New-Item -ItemType Directory -Force -Path '{STARTUP_DIR}' | Out-Null;"
            f"Copy-Item -Path '{VBS_LOCAL}' -Destination '{VBS_STARTUP}' -Force;"
            f"Test-Path '{VBS_STARTUP}'",
            timeout=120,
            stage="vbs",
            label="把 VBS 放进开机自启文件夹",
        )
        # 先把「现在占着端口的是谁」记下来：接管脚本会把它杀掉再起新的，
        # 之后必须等到监听者换人，才算「新的 MCP 真的起来了」（否则会误判成上一个进程还在）。
        old_pid = ""
        try:
            _, out, _ = self._ps(
                f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
                "-ErrorAction SilentlyContinue | Select-Object -First 1;"
                "if ($c) { \"PID=\" + $c.OwningProcess } else { 'PID=' }",
                timeout=60,
                stage="vbs",
            )
            for line in (out or "").splitlines():
                line = line.strip()
                if line.startswith("PID="):
                    old_pid = line[4:].strip()
        except DeployError:
            old_pid = ""
        if old_pid:
            self.emit(
                f"端口 {port} 现在被进程 {old_pid} 占着（大概是上一个会话里的 MCP），"
                "接管脚本会先停掉它再起新的",
                stage="vbs",
            )

        summary = self._run_detached(
            TASK_MCP,
            "wscript.exe",
            f'"{VBS_LOCAL}"',
            stage="vbs",
            label="用计划任务立刻启动一次（不必等重启）",
            interactive=True,
        )
        if "ACTIVE=False" in summary:
            self.emit(
                "目标机当前没有交互式登录：MCP 先跑在后台会话（会话 0）——PowerShell、文件、进程类工具"
                "都能用，但截图和界面自动化会是空的。你 RDP 登录这台机器后，开机自启脚本会把 MCP 接管到"
                "你的桌面会话（同一端口，侧边栏不用改地址）。",
                level="warning",
                stage="vbs",
            )

        ready = mcp_listen_wait_script(port, old_pid)
        _, out, _ = self._ps(ready, timeout=180, stage="vbs", label=f"等待 MCP 监听 127.0.0.1:{port}")
        if "LISTEN=True" not in out:
            tail = self._read_log_tail()
            if old_pid:
                raise DeployError(
                    f"MCP 起了新的进程但没有接手 127.0.0.1:{port}（端口上一直是 {old_pid}）。"
                    f"目标机日志（~/.windows-mcp/server.log）尾部：\n{tail}"
                )
            raise DeployError(
                f"MCP 启动后没有监听 127.0.0.1:{port}。目标机日志（~/.windows-mcp/server.log）尾部："
                f"\n{tail}"
            )
        who = out.split("WHO=", 1)[1].strip().split()[0] if "WHO=" in out else ""
        self.emit(
            f"MCP 已在目标机内监听 127.0.0.1:{port}"
            + (f"（新进程 {who}，旧进程 {old_pid} 已被接管脚本停掉）" if old_pid and who else f"（进程 {who}）"),
            level="success",
            stage="vbs",
        )

    def _read_log_tail(self, lines: int = 15) -> str:
        script = (
            "$p = Join-Path $env:USERPROFILE '.windows-mcp\\server.log';"
            f"if (Test-Path $p) {{ Get-Content $p -Tail {lines} }} else {{ '(没有日志文件)' }}"
        )
        try:
            _, out, _ = self._ps(script, timeout=60, stage=self.stage)
            return (out or "").strip()[:1500]
        except DeployError:
            return "(读取日志失败)"

    # ---------------------------------------------------------------- 阶段 6
    def _stage_expose(self, app) -> None:
        """让堡垒机连得上：只监听回环时开 portproxy，并只放行堡垒机 IP。"""
        port = self.ctx.mcp_port
        if self.ctx.bind_all:
            self.ctx.mcp_url = f"http://{self.ctx.host_address}:{port}/mcp"
            self.emit(
                f"已按选项让 MCP 监听 0.0.0.0，直接使用 {self.ctx.mcp_url}（不开 portproxy）",
                level="warning",
                stage="expose",
            )
        else:
            script = (
                "$lan = (Get-NetIPAddress -AddressFamily IPv4 |"
                "  Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |"
                "  Select-Object -First 1).IPAddress;"
                f"netsh interface portproxy delete v4tov4 listenaddress=$lan listenport={port} 2>$null | Out-Null;"
                f"netsh interface portproxy add v4tov4 listenaddress=$lan listenport={port} "
                f"connectaddress=127.0.0.1 connectport={port};"
                "\"LAN=$lan\""
            )
            code, out, _ = self._ps(script, timeout=120, stage="expose", label="配置 portproxy")
            if code != 0:
                raise DeployError(
                    "配置 portproxy 失败：需要管理员权限，且目标机的 IP Helper 服务要能启动"
                )
            for line in out.splitlines():
                if line.startswith("LAN="):
                    self.ctx.lan_ip = line.split("=", 1)[1].strip()
            if not self.ctx.lan_ip:
                raise DeployError("没能确定目标机的内网地址，无法建立从堡垒机到 MCP 的通道")
            self.ctx.mcp_url = f"http://{self.ctx.lan_ip}:{port}/mcp"
            self.emit(f"portproxy 已建立：{self.ctx.lan_ip}:{port} → 127.0.0.1:{port}", stage="expose")

        remote_ip = self.ctx.bastion_ip or "LocalSubnet"
        if self.ctx.bastion_ip:
            self.emit(f"防火墙只放行堡垒机 {remote_ip} 访问 {port} 端口", stage="expose")
        else:
            self.emit(
                f"没识别出堡垒机 IP，防火墙改为只放行本网段（LocalSubnet）访问 {port} 端口",
                level="warning",
                stage="expose",
            )
        self._ps(
            "netsh advfirewall firewall delete rule name='AutoOps-ComputerUse' 2>$null | Out-Null;"
            "netsh advfirewall firewall add rule name='AutoOps-ComputerUse' dir=in action=allow "
            f"protocol=TCP localport={port} remoteip={remote_ip} | Out-Null;"
            "'FIREWALL=OK'",
            timeout=120,
            stage="expose",
            label="添加防火墙放行规则",
        )
        self.emit(f"MCP 地址：{self.ctx.mcp_url}", level="success", stage="expose")

    # ---------------------------------------------------------------- 阶段 7
    def _stage_verify(self, app) -> None:
        """从堡垒机侧探活，确认「AI 真的能用这台机器」。"""
        self.emit(f"探活 {self.ctx.mcp_url} …", stage="verify")
        client = McpClient(self.ctx.mcp_url, auth_key=self.ctx.auth_key, timeout=45.0)
        # MCP 刚被接管重启时，前面几次连接可能被重置（portproxy 还在往老进程上转），
        # 给它几次机会，别让一次抖动把整场部署判死。
        attempts = 6
        probe: dict[str, Any] = {}
        for index in range(attempts):
            if self.cancelled:
                raise DeployError("部署已取消（探活阶段）")
            try:
                probe = client.probe()
            except McpError as exc:
                probe = {"ok": False, "error": str(exc)}
            if probe.get("ok"):
                break
            if index < attempts - 1:
                self.emit(
                    f"探活第 {index + 1} 次没成功（{probe.get('error') or '没有响应'}），10 秒后重试",
                    level="warning",
                    stage="verify",
                )
                time.sleep(10)
        if not probe.get("ok"):
            raise DeployError(
                f"MCP 探活失败（重试 {attempts} 次）：{probe.get('error') or '目标机没有响应'}"
                "（可以检查目标机防火墙、portproxy 规则与 server.log）"
            )
        tools = probe.get("tools") or []
        server = probe.get("server") or {}
        self.emit(
            f"探活成功：{len(tools)} 个工具，延迟 {probe.get('latencyMs')} ms"
            f"（服务端 {server.get('name', 'MCP')}）",
            level="success",
            stage="verify",
        )
        with app.app_context():
            row = db.session.get(ComputerUseDeployment, self.deployment_id)
            if row is None:
                return
            row.status = "success"
            row.stage = "verify"
            row.progress = 100
            row.python_path = self.ctx.python_path
            row.python_version = self.ctx.python_version_actual or self.ctx.python_version
            row.mcp_version = self.ctx.mcp_version
            row.mcp_exe = self.ctx.mcp_exe
            row.mcp_host = self.ctx.host_for_url
            row.mcp_port = self.ctx.mcp_port
            row.mcp_url = self.ctx.mcp_url
            row.mcp_tool_count = probe.get("toolCount") or len(tools)
            row.mcp_tools = tools
            row.mcp_auth_key_enc = encrypt(self.ctx.auth_key) if self.ctx.auth_key else ""
            row.vbs_path = VBS_LOCAL
            row.startup_path = VBS_STARTUP
            row.portproxy = not self.ctx.bind_all
            row.error = ""
            db.session.commit()
        log_event(
            "computeruse",
            "mcp_probe",
            message=(
                f"{self.ctx.host_name} 的 ComputerUse 部署完成并探活成功："
                f"{probe.get('toolCount') or len(tools)} 个工具"
            ),
            target_type="host",
            target_id=self.ctx.host_id,
            target_name=self.ctx.host_name,
            detail={
                "mcpUrl": self.ctx.mcp_url,
                "python": self.ctx.python_path,
                "mcpVersion": self.ctx.mcp_version,
                "authKey": bool(self.ctx.auth_key),
                "portproxy": not self.ctx.bind_all,
                "latencyMs": probe.get("latencyMs"),
            },
        )


# --------------------------------------------------------------------------- #
# 运行器注册表
# --------------------------------------------------------------------------- #
_RUNNERS: dict[int, DeploymentRunner] = {}
_RUNNERS_LOCK = threading.Lock()


def get_runner(deployment_id: int) -> DeploymentRunner | None:
    with _RUNNERS_LOCK:
        return _RUNNERS.get(int(deployment_id))


def _run_guarded(app, runner: DeploymentRunner) -> None:
    """并发上限由信号量控制：超出的部署先排队（状态仍是 running，日志里会说）。

    这一层还必须做两件事 —— 后台线程里写库与写审计都要 app context（真机第一次跑就是
    因为缺它，``log_event`` 里的 rollback 抛 ``Working outside of application context``，
    线程静默死掉、记录永远停在 running）；以及兜住所有异常，免得再留下一具「僵尸 running」。
    """
    with _SEMAPHORE:
        try:
            with app.app_context():
                runner.run(app)
        except Exception as exc:  # noqa: BLE001 - 线程不能静默死掉
            logger.exception("ComputerUse 部署 #%s 线程异常退出", runner.deployment_id)
            runner.emit(f"部署线程异常退出：{type(exc).__name__}: {exc}", level="error")
            try:
                runner.status = "failed"
                runner.error = f"{type(exc).__name__}: {exc}"
                runner.finished_at = utcnow()
                runner._update_row(
                    status=runner.status,
                    error=runner.error,
                    finished_at=to_naive_utc(runner.finished_at),
                )
            except Exception:  # noqa: BLE001 - 连失败状态都写不进去时只留日志
                logger.exception("ComputerUse 部署 #%s 写入失败状态时又出错", runner.deployment_id)


def start_deployment(app, deployment_id: int, ctx: DeployContext) -> DeploymentRunner:
    """起一个后台线程跑部署；同一个 deployment_id 不会起第二个线程。"""
    with _RUNNERS_LOCK:
        existing = _RUNNERS.get(int(deployment_id))
        if existing is not None and existing.status == "running":
            return existing
        runner = DeploymentRunner(deployment_id, ctx)
        _RUNNERS[int(deployment_id)] = runner
    thread = threading.Thread(
        target=_run_guarded,
        args=(app, runner),
        name=f"computeruse-deploy-{deployment_id}",
        daemon=True,
    )
    thread.start()
    return runner


def deployment_snapshot(row: ComputerUseDeployment, runner: DeploymentRunner | None = None) -> dict:
    """数据库记录 + 内存里的实时进度（前端轮询/SSE 都用这个形状）。"""
    data = row.to_dict()
    data["stageLabel"] = STAGE_LABELS.get(row.stage or "", row.stage or "")
    data["stages"] = [{"key": key, "label": label, "progress": pct} for key, label, pct in STAGES]
    if runner is not None:
        data.update(
            {
                "status": runner.status,
                "stage": runner.stage,
                "stageLabel": STAGE_LABELS.get(runner.stage, runner.stage),
                "progress": runner.progress,
                "lastSeq": runner.last_seq,
                "live": runner.status == "running",
                "error": runner.error or data.get("error") or "",
            }
        )
    else:
        data["lastSeq"] = 0
        data["live"] = row.status == "running"
    return data
