"""双均线策略：快线上穿慢线做多，下穿做空。"""
import pandas as pd

from .base import StrategyBase


class DualMAStrategy(StrategyBase):
    name = "dual_ma"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.fast = int(self.params.get("fast", 5))
        self.slow = int(self.params.get("slow", 20))
        if self.fast >= self.slow:
            raise ValueError(f"fast({self.fast}) 必须小于 slow({self.slow})")

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ma_fast"] = out["close"].rolling(self.fast).mean()
        out["ma_slow"] = out["close"].rolling(self.slow).mean()

        signal = pd.Series(0, index=out.index, dtype=int)
        signal[out["ma_fast"] > out["ma_slow"]] = 1
        signal[out["ma_fast"] < out["ma_slow"]] = -1
        signal[out["ma_slow"].isna()] = 0  # 慢均线未形成前空仓

        out["signal"] = signal
        return out
