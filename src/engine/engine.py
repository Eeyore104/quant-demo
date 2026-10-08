"""事件驱动回测引擎：按 bar 推进 → 信号 → 撮合 → 记录。

成交约定（全项目统一）：信号在 T 日收盘产生，成交在 T+1 日开盘价 ± 滑点执行。
"""

from dataclasses import dataclass

import pandas as pd

from ..utils.logger import get_logger
from .broker import Broker
from .portfolio import Portfolio

log = get_logger("engine")


@dataclass
class Bar:
    date: object
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int


def _to_bar(row) -> Bar:
    return Bar(
        date=row["date"],
        symbol=str(row["symbol"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]),
        open_interest=int(row.get("open_interest", 0)),
    )


class BacktestEngine:
    """回测引擎。run() 返回 (权益曲线 DataFrame, 成交列表)。"""

    def __init__(
        self,
        df: pd.DataFrame,
        strategy,
        broker: Broker | None = None,
        portfolio: Portfolio | None = None,
        position_size: int = 1,
    ):
        self.df = df.reset_index(drop=True)
        self.strategy = strategy
        self.broker = broker or Broker()
        self.portfolio = portfolio or Portfolio(100000)
        self.position_size = int(position_size)

    def run(self) -> tuple[pd.DataFrame, list]:
        self.df = self.strategy.generate_signals(self.df)
        if "signal" not in self.df.columns:
            raise ValueError("策略未生成 signal 列")
        trades: list = []

        # 首日：只记录初始权益
        first = _to_bar(self.df.iloc[0])
        self.portfolio.mark_to_market(first)
        self.portfolio.record(first.date)

        for i in range(1, len(self.df)):
            bar = _to_bar(self.df.iloc[i])
            sig = int(self.df["signal"].iloc[i - 1])  # T 日收盘信号
            target = sig * self.position_size  # T+1 开盘执行
            fills = self.broker.fill(self.portfolio.position.size, target, bar)
            if fills:
                self.portfolio.apply_trades(fills)
                trades.extend(fills)
            self.portfolio.mark_to_market(bar)
            self.portfolio.record(bar.date)

        equity_df = pd.DataFrame(self.portfolio.equity_curve)
        log.info("回测完成：%d 根 bar，%d 笔成交", len(equity_df), len(trades))
        return equity_df, trades
