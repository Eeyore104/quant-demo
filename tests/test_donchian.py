"""唐奇安通道策略单元测试。"""

import pandas as pd
import pytest

from src.strategy.donchian import DonchianStrategy
from src.strategy.registry import STRATEGIES, get_strategy


def _df(highs, lows, closes) -> pd.DataFrame:
    """按给定 high/low/close 序列构造最小可用 bar，其余列用默认值。"""
    n = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "symbol": ["C0"] * n,
            "open": list(closes),
            "high": list(highs),
            "low": list(lows),
            "close": list(closes),
            "volume": [10] * n,
            "open_interest": [1] * n,
        }
    )


def test_registered_by_registry():
    """注册表自动发现 donchian，且可通过名称取类。"""
    assert "donchian" in STRATEGIES
    assert get_strategy("donchian") is DonchianStrategy


def test_default_window_from_empty_params():
    """无参数时使用默认通道周期 20。"""
    assert DonchianStrategy().window == 20


def test_warmup_is_flat():
    """通道未形成（前 window 根）前应输出空仓 0，并新增上下轨列。"""
    df = _df([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6])
    out = DonchianStrategy({"window": 3}).generate_signals(df)

    assert list(out["signal"].iloc[:3]) == [0, 0, 0]
    assert "donchian_up" in out.columns and "donchian_low" in out.columns
    # 第 4 根（index=3）close=4 上破前 3 根最高 3 → 做多
    assert out["signal"].iloc[3] == 1


def test_up_breakout_long():
    """收盘上破上轨 → 持多（+1）。"""
    df = _df([1, 2, 3, 10], [1, 2, 3, 10], [1, 2, 3, 10])
    out = DonchianStrategy({"window": 3}).generate_signals(df)
    assert out["signal"].iloc[-1] == 1


def test_down_breakout_short_and_persist():
    """下破下轨 → 做空（-1）；未反向突破前一路保持仓位。"""
    highs = [10, 11, 12, 3, 3, 3]
    lows = [9, 9, 9, 2, 2, 2]
    closes = [10, 11, 12, 3, 2, 3]  # 第 4 根 close=3 跌破前 3 根最低 9 → 做空
    out = DonchianStrategy({"window": 3}).generate_signals(_df(highs, lows, closes))
    assert list(out["signal"]) == [0, 0, 0, -1, -1, -1]


def test_invalid_window_raises():
    """通道周期过小应报错。"""
    with pytest.raises(ValueError):
        DonchianStrategy({"window": 1})
