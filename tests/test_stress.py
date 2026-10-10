"""压力测试单元测试（v1.3）：情景识别 / 重估口径 / 假设情景 / 确定性。"""

import pandas as pd
import pytest

from src.analysis.stress import identify_historical_scenarios, run_stress
from src.engine.contracts import ContractSpec


def _equity_df(vals):
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(vals), freq="D"),
            "equity": [float(v) for v in vals],
            "margin_used": [0.0] * len(vals),
        }
    )


def _symbol_df(prices, name="A0"):
    n = len(prices)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "symbol": [name] * n,
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1000] * n,
            "open_interest": [1] * n,
        }
    )


def _spec():
    return ContractSpec("A0", multiplier=10, tick_size=1.0, margin_rate=0.1, commission_per_lot=1.0)


def test_identify_scenarios_picks_extremes():
    vals = []
    v = 100.0
    for i in range(40):
        if i == 10:
            v *= 0.94
        if i == 25:
            v *= 0.90
        vals.append(v)
    windows = identify_historical_scenarios(_equity_df(vals), top_n=2)
    spans = {(s, e) for _, s, e in windows}
    assert (25, 25) in spans and (10, 10) in spans  # 两个单日极值均被识别


def test_replay_loss_math():
    # 每日 +1%、恒定权益：单日窗口重估损失 = 基准价 × 乘数 × 1%
    prices = [100.0 * (1.01**i) for i in range(30)]
    contracts = {"A0": _spec()}
    res = run_stress(
        _equity_df([100000.0] * 30),
        {"A0": _symbol_df(prices)},
        contracts,
        initial_capital=100000,
        top_n=1,
        mc_runs=0,
    )
    hist = [s for s in res.scenarios if s.category == "historical"]
    assert len(hist) == 1
    assert hist[0].replay_loss == pytest.approx(100.0 * 10 * 0.01, rel=1e-6)
    assert hist[0].actual_loss == pytest.approx(0.0)


def test_hypothetical_scenarios_present_and_deterministic():
    n = 60
    prices_a = [100.0 * (1.001**i) for i in range(n)]
    prices_b = [200.0 * (1.0005**i) for i in range(n)]
    data = {"A0": _symbol_df(prices_a), "B0": _symbol_df(prices_b, "B0")}
    contracts = {
        "A0": _spec(),
        "B0": ContractSpec(
            "B0", multiplier=10, tick_size=1.0, margin_rate=0.1, commission_per_lot=1.0
        ),
    }
    vals = [100000.0 + 10.0 * i for i in range(n)]
    kwargs = {
        "initial_capital": 100000,
        "top_n": 1,
        "mc_runs": 50,
        "seed": 42,
        "limit_rates": {"A0": 0.08, "B0": 0.07},
    }
    s1 = run_stress(_equity_df(vals), data, contracts, **kwargs)
    s2 = run_stress(_equity_df(vals), data, contracts, **kwargs)
    names = [x.name for x in s1.scenarios]
    assert sum("跳空冲击" in nm for nm in names) == 2
    assert any("跌停" in nm for nm in names)
    assert any("相关性跳升" in nm for nm in names)
    assert [x.as_row() for x in s1.scenarios] == [x.as_row() for x in s2.scenarios]
    assert s1.mc["p50"] == s2.mc["p50"]
    assert 0.0 <= s1.mc["p50"] <= s1.mc["p99"] <= 1.0


def test_stress_rows_csv_shape():
    prices = [100.0] * 30
    res = run_stress(
        _equity_df([100000.0] * 30),
        {"A0": _symbol_df(prices)},
        {"A0": _spec()},
        initial_capital=100000,
        top_n=1,
        mc_runs=10,
        seed=1,
    )
    rows = res.as_rows()
    assert all(
        set(r)
        == {
            "category",
            "scenario",
            "window",
            "actual_loss",
            "actual_pct",
            "replay_loss_1lot",
            "replay_pct_1lot",
            "note",
        }
        for r in rows
    )
    assert any(r["category"] == "mc" for r in rows)
