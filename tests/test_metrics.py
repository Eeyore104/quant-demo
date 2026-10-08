"""绩效指标单元测试。"""
import pandas as pd
import pytest

from src.engine.portfolio import Trade
from src.report.metrics import analyze, max_drawdown, sharpe_ratio


def _trade(action: str, pnl: float = 0.0) -> Trade:
    return Trade(
        date=None, symbol="C0", direction="LONG", action=action,
        price=100.0, volume=1, commission=1.2, slippage_cost=10.0, pnl=pnl,
    )


def test_max_drawdown():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0])
    # 峰值 120 → 谷底 90 → -25%
    assert max_drawdown(equity) == pytest.approx(-0.25)


def test_sharpe_zero_volatility():
    returns = pd.Series([0.01])  # 单点，波动为 0
    assert sharpe_ratio(returns) == 0.0


def test_analyze_basic():
    equity_df = pd.DataFrame({"equity": [100000.0, 101000.0, 99000.0]})
    trades = [_trade("OPEN"), _trade("CLOSE", pnl=8.0)]

    m = analyze(equity_df, trades, 100000.0)

    assert m.trade_count == 1
    assert m.win_rate == 1.0
    assert m.profit_factor == float("inf")  # 只有盈利、没有亏损
    assert m.total_commission == pytest.approx(2.4)
    assert m.total_slippage == pytest.approx(20.0)
    assert m.total_return == pytest.approx(-0.01)                      # 99000/100000 - 1
    assert m.max_drawdown == pytest.approx(99000.0 / 101000.0 - 1.0)   # ≈ -0.0198
