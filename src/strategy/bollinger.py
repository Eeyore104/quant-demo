"""布林带策略：收盘突破上轨做多，跌破下轨做空，回到带内空仓。"""
import pandas as pd

from .base import StrategyBase


class BollingerStrategy(StrategyBase):
    name = "bollinger"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.window = int(self.params.get("window", 20))
        self.num_std = float(self.params.get("num_std", 2.0))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        mid = out["close"].rolling(self.window).mean()
        std = out["close"].rolling(self.window).std(ddof=0)
        out["boll_mid"] = mid
        out["boll_up"] = mid + self.num_std * std
        out["boll_low"] = mid - self.num_std * std

        signal = pd.Series(0, index=out.index, dtype=int)
        signal[out["close"] > out["boll_up"]] = 1
        signal[out["close"] < out["boll_low"]] = -1
        signal[out["boll_mid"].isna()] = 0  # 中轨未形成前空仓

        out["signal"] = signal
        return out
