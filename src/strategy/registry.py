"""策略自动发现注册表：「新增策略 = 新增一个文件」。

启动时用 ``pkgutil`` 扫描 ``src/strategy/`` 目录下的所有模块，凡定义了
``StrategyBase`` 的子类（且类属性 ``name`` 唯一）即自动注册。

用法::

    from src.strategy.registry import STRATEGIES, get_strategy

    STRATEGIES             # {"dual_ma": DualMAStrategy, "bollinger": ..., "donchian": ...}
    get_strategy("donchian")  # -> DonchianStrategy

新增策略无需修改本文件或任何入口：
只需在 ``src/strategy/`` 下新建 ``<name>.py``，定义
``class XxxStrategy(StrategyBase): name = "<name>"``。
"""

import importlib
import inspect
import os
import pkgutil

from .base import StrategyBase

_PKG = __name__.rsplit(".", 1)[0]  # "src.strategy"
_DIR = os.path.dirname(__file__)


def discover() -> dict[str, type[StrategyBase]]:
    """扫描同目录下所有模块，收集本模块定义的 ``StrategyBase`` 子类。"""
    reg: dict[str, type[StrategyBase]] = {}
    for m in pkgutil.iter_modules([_DIR]):
        if m.name in {"base", "registry"}:
            continue
        module = importlib.import_module(f"{_PKG}.{m.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, StrategyBase)
                and obj is not StrategyBase
                and obj.__module__ == module.__name__  # 只收本模块定义的类，避免重复导入
            ):
                reg[obj.name] = obj
    return reg


# 模块导入时即完成发现（进程内一次）
STRATEGIES: dict[str, type[StrategyBase]] = discover()


def get_strategy(name: str) -> type[StrategyBase]:
    """按名称取策略类；未知名称抛出 ``KeyError``（附可用列表）。"""
    if name not in STRATEGIES:
        raise KeyError(f"未知策略: {name}（可用: {sorted(STRATEGIES)}）")
    return STRATEGIES[name]
