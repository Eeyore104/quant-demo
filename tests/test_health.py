"""研究体检单元测试：参数邻域 / 蒙特卡洛 / 成本敏感性 / 判定逻辑 / 使用登记。"""

import numpy as np
import pandas as pd

from src.analysis import cost_sensitivity as cost_mod
from src.analysis import monte_carlo as mc_mod
from src.analysis import param_sensitivity as ps_mod
from src.analysis.health import (
    DEFAULT_THRESHOLDS,
    GRADE_FAIL,
    GRADE_PASS,
    GRADE_SKIP,
    GRADE_WARN,
    HealthItem,
    _grade_cost,
    _grade_oos,
    overall_grade,
)
from src.analysis.oos_usage import bump_usage, get_usage
from src.engine.broker import Broker
from src.engine.portfolio import Portfolio
from src.report.metrics import PerformanceMetrics
from src.strategy.dual_ma import DualMAStrategy


def _make_df(n: int = 160) -> pd.DataFrame:
    """构造可复现的带趋势日线（与 test_walkforward 同构）。"""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    close = 100 + np.cumsum(rng.normal(0.05, 1, n))
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


def test_neighborhood_values_int_and_float():
    assert ps_mod.neighborhood_values(5) == [3, 4, 5, 6, 8]
    assert ps_mod.neighborhood_values(20) == [12, 16, 20, 25, 30]
    assert ps_mod.neighborhood_values(2.0) == [1.2, 1.6, 2.0, 2.5, 3.0]


def test_detect_cliffs():
    rows = []
    table = {20: [0.03, 0.04, 0.05, -0.02, -0.03], 30: [0.02, 0.03, 0.03, 0.01, 0.0]}
    for slow, rets in table.items():
        for fast, ret in zip([3, 4, 5, 6, 8], rets, strict=True):
            rows.append({"fast": fast, "slow": slow, "total_return": ret})
    scan = pd.DataFrame(rows)

    events = ps_mod.detect_cliffs(scan, {"fast": 5, "slow": 20})
    assert len(events) == 1
    assert events[0].param == "fast"
    assert events[0].from_value == 5 and events[0].to_value == 6
    assert "fast: 5 → 6" in events[0].describe()  # 整数值不显示小数点


def test_run_param_neighborhood_end_to_end():
    df = _make_df(160)
    res = ps_mod.run_param_neighborhood(
        df,
        DualMAStrategy,
        {"fast": 5, "slow": 20},
        _broker_factory,
        _portfolio_factory,
        position_size=1,
    )
    assert res.total == 25  # 5×5 全组合均合法（fast ≤ 8 < slow ≥ 12）
    assert set(res.focus_params) == {"fast", "slow"}
    assert 0.0 <= res.stable_fraction <= 1.0
    center = res.scan[(res.scan["fast"] == 5) & (res.scan["slow"] == 20)]
    assert len(center) == 1


def test_signal_shuffle_deterministic_and_percentile():
    df = _make_df(120)
    df_sig = DualMAStrategy({"fast": 3, "slow": 10}).generate_signals(df)
    res1 = mc_mod.run_signal_shuffle(
        df_sig, _broker_factory, _portfolio_factory, 1, actual_return=0.01, runs=20, seed=1
    )
    res2 = mc_mod.run_signal_shuffle(
        df_sig, _broker_factory, _portfolio_factory, 1, actual_return=0.01, runs=20, seed=1
    )
    assert np.allclose(res1.returns, res2.returns)  # 同种子 → 结果可复现
    assert 0.0 <= res1.percentile <= 100.0
    # 打乱保持多空天数分布不变
    base = df_sig["signal"].to_numpy()
    shuffled = np.random.default_rng(0).permutation(base)
    assert (shuffled == 1).sum() == (base == 1).sum()
    assert (shuffled == -1).sum() == (base == -1).sum()


def test_bootstrap_quantiles_on_synthetic_equity():
    rng = np.random.default_rng(3)
    equity = 100000 * np.cumprod(1 + rng.normal(0.0005, 0.01, 300))
    equity_df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=300), "equity": equity})
    res = mc_mod.run_bootstrap(equity_df, 100000, runs=100, seed=5)
    assert res.return_q05 <= res.return_q50 <= res.return_q95
    assert res.mdd_q05 <= res.mdd_q50 <= 0.0
    assert 0.0 <= res.prob_negative <= 1.0


def test_cost_sensitivity_scales_costs():
    df = _make_df(200)
    res = cost_mod.run_cost_sensitivity(
        df,
        DualMAStrategy,
        {"fast": 3, "slow": 10},
        broker_kwargs={
            "commission_per_lot": 1.2,
            "slippage_ticks": 1,
            "tick_size": 1.0,
            "contract_multiplier": 10,
        },
        initial_capital=100000,
        position_size=1,
        contract_multiplier=10,
        multipliers=(1.0, 2.0),
    )
    base, doubled = res.rows[0], res.rows[1]
    assert base["total_cost"] > 0  # 保证样本内确实有交易
    assert abs(doubled["total_cost"] - 2.0 * base["total_cost"]) < 1e-6


def test_interpolate_zero_multiplier():
    zero, reason = cost_mod._interpolate_zero(
        [
            {"multiplier": 1.0, "total_return": 0.05},
            {"multiplier": 1.5, "total_return": 0.04},
            {"multiplier": 2.0, "total_return": 0.01},
            {"multiplier": 3.0, "total_return": -0.02},
        ]
    )
    assert reason == "interpolated"
    assert abs(zero - (2.0 + 0.01 / 0.03)) < 1e-9

    zero2, reason2 = cost_mod._interpolate_zero(
        [{"multiplier": 1.0, "total_return": 0.05}, {"multiplier": 2.0, "total_return": 0.02}]
    )
    assert zero2 is None and reason2 == "not_breached"

    zero3, reason3 = cost_mod._interpolate_zero([{"multiplier": 1.0, "total_return": -0.01}])
    assert zero3 is None and reason3 == "negative_at_base"


def test_grade_cost_rules():
    th = dict(DEFAULT_THRESHOLDS)
    neg = cost_mod.CostSensitivityResult(
        rows=[{"multiplier": 1.0, "total_return": -0.01}], reason="negative_at_base"
    )
    assert _grade_cost(neg, th).grade == GRADE_FAIL

    warn = cost_mod.CostSensitivityResult(
        rows=[
            {"multiplier": 1.0, "total_return": 0.03},
            {"multiplier": 2.0, "total_return": -0.01},
        ],
        zero_multiplier=2.0,
        reason="interpolated",
    )
    assert _grade_cost(warn, th).grade == GRADE_WARN

    ok = cost_mod.CostSensitivityResult(
        rows=[
            {"multiplier": 1.0, "total_return": 0.03},
            {"multiplier": 3.0, "total_return": 0.01},
        ],
        zero_multiplier=4.0,
        reason="interpolated",
    )
    assert _grade_cost(ok, th).grade == GRADE_PASS


def test_overall_grade_worst():
    def item(grade):
        return HealthItem("x", "x", grade, "")

    assert overall_grade([item(GRADE_PASS), item(GRADE_WARN)]) == GRADE_WARN
    assert overall_grade([item(GRADE_PASS), item(GRADE_FAIL), item(GRADE_SKIP)]) == GRADE_FAIL
    assert overall_grade([item(GRADE_PASS)]) == GRADE_PASS
    assert overall_grade([item(GRADE_SKIP)]) == GRADE_SKIP
    assert overall_grade([]) == GRADE_SKIP


def test_oos_usage_bump(tmp_path):
    path = tmp_path / "usage.json"
    key = "AU0|dual_ma|2024-10-01"
    assert get_usage(path, key) == 0
    assert bump_usage(path, key) == 1
    assert bump_usage(path, key) == 2
    assert get_usage(path, key) == 2


def test_grade_oos_downgrade_on_repeated_usage():
    """样本外使用次数 > 3 → 判定降一级并附提示。"""

    class _FakeOOS:
        skipped = False
        test_range = ("2024-10-08", "2026-09-30")
        test_metrics = PerformanceMetrics(total_return=0.05, sharpe=0.6, max_drawdown=-0.03)

    th = dict(DEFAULT_THRESHOLDS)
    assert _grade_oos(_FakeOOS, usage=1, th=th).grade == GRADE_PASS
    item = _grade_oos(_FakeOOS, usage=4, th=th)
    assert item.grade == GRADE_WARN
    assert "降级" in item.note
