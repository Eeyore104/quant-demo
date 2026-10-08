"""策略基类：回测与（未来）实盘共用的唯一接口。

- 回测：generate_signals() 预计算整段 signal 列
- 实盘（P1/P2 预留）：ExecutionAdapter 把实时行情组装成 ctx 调用 on_bar()，
  策略代码保持不变 —— 这是「回测 → 仿真 → 实盘」共用一套策略的地基。
"""
from abc import ABC, abstractmethod

import pandas as pd


class StrategyBase(ABC):
    """所有策略继承此类。signal 取值：1=做多 / -1=做空 / 0=空仓。"""

    name: str = "base"

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """输入清洗后的日线 DataFrame，返回新增 signal 列的 DataFrame。"""

    def on_bar(self, ctx: dict) -> int:
        """回测/实盘统一入口：给定当前上下文，返回目标仓位（手，正多负空）。"""
        raise NotImplementedError("实盘接入时实现（P1/P2）")
