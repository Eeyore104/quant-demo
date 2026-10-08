"""唐奇安通道策略（海龟突破风格）：双向突破。

规则（与「信号在 T 日收盘产生、T+1 开盘成交」的全项目约定一致）：
- 上轨 = 过去 ``window`` 根 bar 的最高价（**不含当根**，``shift(1)`` 防未来函数）
- 下轨 = 过去 ``window`` 根 bar 的最低价（**不含当根**）
- 收盘价上破上轨 → 持多（1）；下破下轨 → 持空（-1）；未突破 → 维持原仓位
- 通道未形成（不足 ``window`` 根）前空仓（0）

与双均线/布林带「逐根即时翻转」不同，唐奇安为**状态保持**型：突破后一路持有，
直到反向突破才反向 —— 这是海龟系统的经典行为，可显著减少震荡市中的来回打脸。
"""

import numpy as np
import pandas as pd

from .base import StrategyBase


class DonchianStrategy(StrategyBase):
    """唐奇安通道突破策略。参数：``window``（通道周期，默认 20）。"""

    name = "donchian"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.window = int(self.params.get("window", 20))
        if self.window < 2:
            raise ValueError(f"window({self.window}) 必须 >= 2")

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        # 上下轨取「历史 window 根」，shift(1) 确保不含当根 → 无未来函数
        upper = out["high"].rolling(self.window).max().shift(1)
        lower = out["low"].rolling(self.window).min().shift(1)
        out["donchian_up"] = upper
        out["donchian_low"] = lower

        # 突破点：上破记 1、下破记 -1、其余留空（后续用 ffill 保持仓位）
        raw = pd.Series(np.nan, index=out.index, dtype=float)
        raw[out["close"] > upper] = 1.0
        raw[out["close"] < lower] = -1.0

        signal = raw.ffill().fillna(0).astype(int)
        signal[upper.isna()] = 0  # 通道未形成前空仓

        out["signal"] = signal
        return out
