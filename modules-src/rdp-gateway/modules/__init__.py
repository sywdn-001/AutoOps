"""RDP 网关官方扩展模块的 Python 入口。

宿主在模块加载时 import 本包并调用 :func:`setup(ctx)`；其余生命周期钩子按
约定从模块顶层收集（``on_enable`` / ``on_disable`` / ``teardown``）。

本文件只做**装配**：路由、进程管理、资产/会话逻辑分别在
:mod:`.gateway`、:mod:`.targets`、:mod:`.bridge` 里，方便单独阅读与测试。
"""

from __future__ import annotations

from . import bridge, gateway, targets

MODULE_ID = "rdp-gateway"


def setup(ctx) -> None:
    """注册全部 HTTP 路由与生命周期兜底。"""
    try:
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # data_dir 不可写时不要让整个宿主启动失败；具体接口会返回结构化错误。
        ctx.logger.warning("RDP 网关模块数据目录不可写：%s", ctx.data_dir)

    gateway.register(ctx)
    targets.register(ctx)
    bridge.register(ctx)

    # 双保险：registry 会自动调用模块顶层的 on_disable/teardown，这里再显式
    # 注册一次 ctx.on("disable")，保证第三方宿主按事件方式卸载时也会停子进程。
    ctx.on("disable", lambda _ctx: _shutdown_all(_ctx, reason="module-disabled"))

    ctx.logger.info(
        "RDP 网关模块 %s 已加载（package=%s data=%s）",
        ctx.version,
        ctx.package_dir,
        ctx.data_dir,
    )


def on_enable(ctx) -> None:
    """模块启用时：按 autoStart 设置决定是否拉起网关进程。

    启动放到后台线程，避免网关启动慢/端口探测阻塞宿主的 enable 请求；
    任何异常只记日志，不影响模块启用本身。
    """
    try:
        config = gateway.settings_snapshot(ctx)
    except Exception:  # noqa: BLE001 - 设置读取失败不能阻止模块启用
        ctx.logger.warning("读取 RDP 网关设置失败，跳过自动启动", exc_info=True)
        return
    if not config["enabled"]:
        ctx.logger.info("RDP 网关在模块设置中处于停用状态，跳过自动启动")
        return
    if not config["autoStart"]:
        return
    ctx.logger.info("autoStart=1，后台启动 RDP 网关（端口 %s）", config["port"])
    gateway.start_in_background(ctx, trigger="auto-start")


def _shutdown_all(ctx, *, reason: str) -> None:
    """停进程、关桥；任何一步失败都不影响另一步。"""
    try:
        bridge.shutdown()
    except Exception:  # noqa: BLE001
        ctx.logger.warning("关闭 RDP 页面桥失败（%s）", reason, exc_info=True)
    gateway.shutdown(ctx, reason=reason)


def on_disable(ctx) -> None:
    """模块停用：必须收掉子进程，不能留僵尸。"""
    _shutdown_all(ctx, reason="module-disabled")


def teardown(ctx) -> None:
    """模块卸载/宿主退出：再兜一次底（stop 本身幂等）。"""
    _shutdown_all(ctx, reason="teardown")
