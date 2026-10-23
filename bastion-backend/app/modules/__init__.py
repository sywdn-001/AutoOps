"""扩展模块运行时入口。

``init_modules(app)`` 必须在应用工厂的 ``seed_data`` 之后调用，并且**永不抛异常**：
模块是实验性能力，一个坏包、一个坏钩子都不应该让整个堡垒机起不来。
"""

from __future__ import annotations

import logging

from .builtin import sync_builtin
from .context import ModuleApiError, ModuleContext, ModuleError
from .registry import ModuleRecord, ModuleRegistry, registry

logger = logging.getLogger("bastion.modules")

__all__ = [
    "ModuleApiError",
    "ModuleContext",
    "ModuleError",
    "ModuleRecord",
    "ModuleRegistry",
    "init_modules",
    "registry",
]


def init_modules(app) -> ModuleRegistry:
    """初始化模块运行时；任何异常都只记录日志并返回单例。"""
    try:
        registry.reset(app)
    except Exception:  # noqa: BLE001 - 启动阶段的最后一道兜底
        logger.exception("模块运行时初始化失败，后续模块能力不可用")
        return registry

    try:
        for message in sync_builtin(registry):
            logger.warning("内置模块同步告警：%s", message)
    except Exception:  # noqa: BLE001
        logger.exception("内置模块同步失败，继续加载已安装模块")

    try:
        registry.scan()
    except Exception:  # noqa: BLE001
        logger.exception("扫描已安装模块失败")

    logger.info(
        "模块运行时就绪：已安装 %s 个，已加载 %s 个",
        len(registry.all()),
        len(registry.loaded_all()),
    )
    return registry
