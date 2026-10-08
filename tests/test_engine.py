"""回测引擎单元测试（手工可核对的小样本）。"""

import pandas as pd

from src.engine.broker import Broker
from src.engine.engine import BacktestEngine
from src.engine.portfolio import Portfolio
from src.strategy.base import StrategyBase


class StubStrategy(StrategyBase):
    """测试桩：直接输出预设 signal 序列。"""

    def __init__(self, signals):
        super().__init__({})
        self._signals = signals

    def generate_signals(self, df):
        out = df.copy()
        out["signal"] = self._signals
        return out


def _bar_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]),
            "symbol": ["C0"] * 4,
            "open": [100.0, 100.0, 110.0, 105.0],
            "high": [100.0, 100.0, 110.0, 105.0],
            "low": [100.0, 100.0, 110.0, 105.0],
            "close": [100.0, 100.0, 110.0, 105.0],
            "volume": [10, 10, 10, 10],
            "open_interest": [1, 1, 1, 1],
        }
    )


def _make_engine(signals):
    broker = Broker(commission_per_lot=1.2, slippage_ticks=1, tick_size=1.0, contract_multiplier=10)
    portfolio = Portfolio(100000, contract_multiplier=10)
    engine = BacktestEngine(
        _bar_df(), StubStrategy(signals), broker=broker, portfolio=portfolio, position_size=1
    )
    return engine, portfolio


def test_single_long_trade():
    """D1 收盘做多 → D2 开盘成交；D2 收盘平仓 → D3 开盘成交。"""
    engine, portfolio = _make_engine([1, 0, 0, 0])
    equity_df, trades = engine.run()

    assert len(trades) == 2
    open_t, close_t = trades
    # 开仓：D2 开盘 100 + 1 跳滑点 = 101
    assert open_t.action == "OPEN" and open_t.price == 101.0
    # 平仓：D3 开盘 110 - 1 跳滑点 = 109
    assert close_t.action == "CLOSE" and close_t.price == 109.0
    # 已实现盈亏：(109 - 101) × 1 手 × 10 = 80 元
    assert close_t.pnl == 80.0
    # 现金：100000 - 1.2 + 80 - 1.2 = 100077.6
    assert abs(portfolio.cash - 100077.6) < 1e-6
    assert abs(equity_df["equity"].iloc[-1] - 100077.6) < 1e-6
    # 滑点成本：1 跳 × 1 手 × 10 = 10 元/笔
    assert open_t.slippage_cost == 10.0 and close_t.slippage_cost == 10.0


def test_flip_short_to_long():
    """D1 信号 -1 做空 → D2 开盘开空；D2 信号 1 翻多 → D3 开盘平空 + 开多。"""
    engine, _ = _make_engine([-1, 1, 1, 1])
    _, trades = engine.run()

    assert len(trades) == 3
    # D2 开空 @ 100 - 1 = 99
    assert trades[0].action == "OPEN" and trades[0].direction == "SHORT" and trades[0].price == 99.0
    # D3 平空 @ 110 + 1 = 111，随后开多 @ 111
    assert (
        trades[1].action == "CLOSE" and trades[1].direction == "SHORT" and trades[1].price == 111.0
    )
    assert trades[2].action == "OPEN" and trades[2].direction == "LONG" and trades[2].price == 111.0
    # 平空盈亏：(99 - 111) × 10 = -120
    assert trades[1].pnl == -120.0
