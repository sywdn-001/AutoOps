"""扩展模块运行时入口。

``init_modules(app)`` 必须在应用工厂的 ``seed_data`` 之后调用，并且**永不抛异常**：
模块是实验性能力，一个坏包、一个坏钩子都不应该让整个堡垒机起不来。

系统**不内置任何模块**：全新实例装好就是一个模块都没有的干净状态，官方示例
（hello-bastion / js-demo / rdp-gateway）与第三方扩展一样，都是以
``*.mod.ops_ext`` 包的形式由管理员在「系统设置 → 扩展模块」里上传安装。
启动时只做一件事：扫描 ``MODULES_DIR`` 下已经装好的模块并加载。
"""

from __future__ import annotations

import logging

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
        registry.scan()
    except Exception:  # noqa: BLE001
        logger.exception("扫描已安装模块失败")

    logger.info(
        "模块运行时就绪：已安装 %s 个，已加载 %s 个",
        len(registry.all()),
        len(registry.loaded_all()),
    )
    return registry
