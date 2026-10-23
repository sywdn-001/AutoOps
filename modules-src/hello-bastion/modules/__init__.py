"""hello-bastion 示例模块的 Python 扩展入口。

宿主在模块加载时 import 本文件并调用 :func:`setup(ctx)`。
这里只依赖 ``autoops`` 门面，不 import 宿主的内部模块，方便第三方照抄。

重要：``autoops.bastion`` 里所有触库/触网的方法都是 ``async def``。
Flask 的普通视图是同步函数，所以下面用 :func:`_run_async` 把协程桥接回同步线程。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from autoops import ModuleApiError, bastion

MODULE_ID = "hello-bastion"

#: 与 description.json 里的 settings[].key 保持一致（不带 mod.<id>. 前缀）。
SETTING_SHOW_GREETING = "show_greeting"
SETTING_REFRESH_SECONDS = "refresh_seconds"
SETTING_GREETING_TEXT = "greeting_text"

DEFAULT_REFRESH_SECONDS = 30
MIN_REFRESH_SECONDS = 5
MAX_REFRESH_SECONDS = 600


# ---------------------------------------------------------------------------
# 同步视图 -> async SDK 的桥
# ---------------------------------------------------------------------------
def _run_async(factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """在一个同步 Flask 视图里安全地执行协程。

    * 普通 Flask（本项目 ``async_mode="threading"``）的视图跑在 worker 线程里，
      线程中没有正在运行的事件循环，直接 ``asyncio.run()`` 即可。
    * 如果宿主将来把视图放进已有事件循环（ASGI / 事件循环线程），
      ``asyncio.run()`` 会抛
      ``RuntimeError: asyncio.run() cannot be called from a running event loop``。
      此时把协程工厂丢到独立线程，让新线程自己建一个循环。

    注意传进来的是 **协程工厂** 而不是协程对象：协程必须在真正运行的循环所在线程里创建。
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环：最常见、也最快的路径。
        return asyncio.run(factory())

    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程，保留 4xx 语义
            box["error"] = exc

    thread = threading.Thread(target=target, name=f"{MODULE_ID}-api", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# 设置值解析：SystemSetting 里一律是字符串，模块自己转类型
# ---------------------------------------------------------------------------
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in _TRUTHY


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _read_settings(ctx) -> dict[str, Any]:
    """读取模块设置。这是同步的数据库读取，放在同步视图里做，不要放进 async 协程。"""
    return {
        "showGreeting": _as_bool(ctx.get_setting(SETTING_SHOW_GREETING, "1"), True),
        "refreshSeconds": _as_int(
            ctx.get_setting(SETTING_REFRESH_SECONDS, str(DEFAULT_REFRESH_SECONDS)),
            DEFAULT_REFRESH_SECONDS,
            MIN_REFRESH_SECONDS,
            MAX_REFRESH_SECONDS,
        ),
        "greetingText": str(ctx.get_setting(SETTING_GREETING_TEXT, "Hello, Bastion!") or ""),
    }


# ---------------------------------------------------------------------------
# 异步数据采集：只依赖契约 §5 的 SDK 签名，不猜 hosts/sessions 的字段结构
# ---------------------------------------------------------------------------
async def _collect_counts() -> dict[str, int]:
    hosts, sessions = await asyncio.gather(
        bastion.hosts.list(),
        bastion.sessions.list_active(),
    )
    return {
        "hostTotal": len(hosts or []),
        "activeSessions": len(sessions or []),
    }


async def _credential_summary(ctx, host_id: int, account_id: int | None) -> dict[str, Any]:
    """读取一次凭证，但只把用户名和口令长度交给调用方。

    明文口令只存在于本协程的局部变量里：不写日志、不进返回值、不写磁盘。
    """

    # 先确认资产存在，才能给「找不到资产」一个可读的 404，而不是让底层异常变成 500。
    try:
        host = await bastion.hosts.get(host_id)
    except Exception as exc:  # noqa: BLE001 - 不同实现对「不存在」可能抛异常或返回空
        ctx.logger.warning("hosts.get(%s) 失败：%s", host_id, type(exc).__name__)
        host = None
    if not host:
        raise ModuleApiError(f"资产不存在或当前账号不可见：host_id={host_id}", 404)

    try:
        credentials = await bastion.credentials.get(host_id, account_id=account_id)
    except Exception as exc:  # noqa: BLE001
        # 只记录异常类型，不记录异常文本：避免底层把凭据/连接串带进日志。
        ctx.logger.warning(
            "credentials.get(host_id=%s, account_id=%s) 失败：%s",
            host_id,
            account_id,
            type(exc).__name__,
        )
        raise ModuleApiError(
            "无法读取该资产凭证：资产或账号不存在，或凭据解密失败",
            404,
        ) from exc

    # 只返回可公开的元数据；password / private_key / passphrase 绝不回传。
    return {
        "hostId": host_id,
        "accountId": getattr(credentials, "account_id", account_id),
        "username": getattr(credentials, "username", "") or "",
        "passwordLength": len(getattr(credentials, "password", "") or ""),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _parse_int_arg(name: str, *, required: bool) -> int | None:
    from flask import request

    raw = (request.args.get(name) or "").strip()
    if not raw:
        if required:
            raise ModuleApiError(f"缺少查询参数 {name}", 400)
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ModuleApiError(f"参数 {name} 必须是正整数", 400) from None
    if value <= 0:
        raise ModuleApiError(f"参数 {name} 必须是正整数", 400)
    return value


def _register_routes(ctx) -> None:
    @ctx.route("/stats", methods=("GET",))
    def stats():
        """返回主机总数、在线会话数与三个模块设置项。"""
        counts = _run_async(_collect_counts)
        return {
            "module": {"id": ctx.id, "version": ctx.version},
            "generatedAt": bastion.util.now().isoformat(),
            "hostTotal": counts["hostTotal"],
            "activeSessions": counts["activeSessions"],
            "settings": _read_settings(ctx),
        }

    @ctx.route("/credentials-sample", methods=("GET",))
    def credentials_sample():
        """演示异步取凭证：只返回 username 与 password 长度。

        查询参数：``host_id``（必填）、``account_id``（可选）。
        """
        host_id = _parse_int_arg("host_id", required=True)
        account_id = _parse_int_arg("account_id", required=False)
        assert host_id is not None  # required=True 时不可能为 None
        return _run_async(lambda: _credential_summary(ctx, host_id, account_id))


# ---------------------------------------------------------------------------
# 生命周期钩子（函数名由 loader 识别；setup 必须存在）
# ---------------------------------------------------------------------------
def setup(ctx):
    """模块加载时调用一次；这里注册路由并初始化私有数据目录。"""
    _register_routes(ctx)

    # data_dir 是模块唯一的私有可写目录；不要往 package_dir 里写（升级时会被替换）。
    marker = ctx.data_dir / "hello-bastion.json"
    if not marker.exists():
        try:
            ctx.data_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text('{"installed": true}\n', encoding="utf-8")
        except OSError:
            ctx.logger.warning("无法写入模块数据目录：%s", ctx.data_dir)

    ctx.logger.info(
        "hello-bastion %s 已加载（data_dir=%s）",
        ctx.version,
        ctx.data_dir,
    )


def teardown(ctx):
    """模块禁用或卸载时调用。示例没有后台线程/连接需要释放。"""
    ctx.logger.info("hello-bastion 已停止")


def on_enable(ctx):
    ctx.logger.info("hello-bastion 已启用")


def on_disable(ctx):
    ctx.logger.info("hello-bastion 已停用")
