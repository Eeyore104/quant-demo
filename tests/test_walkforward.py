"""样本外验证（walkforward）单元测试。"""

import numpy as np
import pandas as pd

from src.engine.broker import Broker
from src.engine.portfolio import Portfolio
from src.engine.walkforward import run_oos, split_segments
from src.strategy.dual_ma import DualMAStrategy

GRID = {"fast": [2, 3], "slow": [4, 6]}


def _make_df(n: int = 40) -> pd.DataFrame:
    """构造可复现的带趋势日线。"""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + 0.5
    low = close - 0.5
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": ["C0"] * n,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": [10] * n,
            "open_interest": [1] * n,
        }
    )


def _broker_factory():
    return Broker(commission_per_lot=1.2, slippage_ticks=1, tick_size=1.0, contract_multiplier=10)


def _portfolio_factory():
    return Portfolio(100000, contract_multiplier=10)


def test_split_by_date_inclusive_train():
    """split_date 分段：<= 归训练段，> 归测试段。"""
    df = _make_df(40)
    train, test = split_segments(df, split_date="2024-01-31", ratio=0.7)
    assert len(train) + len(test) == 40
    assert train["date"].max() <= pd.Timestamp("2024-01-31")
    assert test["date"].min() > pd.Timestamp("2024-01-31")


def test_split_by_ratio():
    """未给 split_date 时按比例切分。"""
    df = _make_df(40)
    train, test = split_segments(df, split_date=None, ratio=0.7)
    assert len(train) == 28 and len(test) == 12


def test_run_oos_end_to_end():
    """端到端：切分 → 训练段择优 → 测试段检验。"""
    df = _make_df(40)
    oos = run_oos(
        df,
        DualMAStrategy,
        GRID,
        _broker_factory,
        _portfolio_factory,
        position_size=1,
        split_date="2024-01-31",
        metric="sharpe",
    )

    assert isinstance(oos.best_params, dict)
    assert set(oos.best_params) == {"fast", "slow"}
    assert oos.best_params["fast"] < oos.best_params["slow"]
    assert not oos.scan_train.empty
    assert oos.train_range[0] <= oos.train_range[1]
    assert oos.test_range[0] <= oos.test_range[1]
    # 指标对象可用（收益/回撤/夏普均为浮点）
    assert isinstance(oos.train_metrics.total_return, float)
    assert isinstance(oos.test_metrics.sharpe, float)


def test_run_oos_empty_test_segment_is_safe():
    """测试段为空（split_date 晚于全部数据）时只 WARN、不抛异常。"""
    df = _make_df(10)
    oos = run_oos(
        df,
        DualMAStrategy,
        GRID,
        _broker_factory,
        _portfolio_factory,
        position_size=1,
        split_date="2099-01-01",
    )
    assert oos.test_range == (None, None)
    assert oos.test_metrics.total_return == 0.0


def test_empty_train_segment_is_skipped_not_crash():
    """split_date 早于数据起点 → 训练段为空 → 返回 skipped=True，不抛异常。"""
    df = _make_df(20)
    oos = run_oos(
        df,
        DualMAStrategy,
        GRID,
        _broker_factory,
        _portfolio_factory,
        position_size=1,
        split_date="2020-01-01",
    )
    assert oos.skipped is True
    assert oos.best_params == {}
    assert oos.scan_train.empty


def test_empty_string_split_date_falls_back_to_ratio():
    """空串 split_date 视为「未提供」→ 回退 ratio 正常切分并出结果。"""
    df = _make_df(40)
    train, test = split_segments(df, split_date="", ratio=0.7)
    assert len(train) == 28 and len(test) == 12

    oos = run_oos(
        df,
        DualMAStrategy,
        GRID,
        _broker_factory,
        _portfolio_factory,
        position_size=1,
        split_date="",
        ratio=0.7,
    )
    assert oos.skipped is False
    assert not oos.scan_train.empty
