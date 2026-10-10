"""「风险与压力测试」章节渲染与风控 CSV 冒烟测试（v1.3）。"""

import pandas as pd

from src.analysis.risk_metrics import build_risk_daily
from src.analysis.stress import run_stress
from src.engine.contracts import ContractSpec
from src.engine.portfolio_engine import run_portfolio
from src.report.risk_report import build_risk_section, save_risk_artifacts
from src.strategy.base import StrategyBase


class _Stub(StrategyBase):
    name = "stub_sig"

    def __init__(self, params=None):
        super().__init__(params)
        self._signals = list(self.params.get("signals", []))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = self._signals
        return out


ROW = (100.0, 100.5, 99.5, 100.0)


def _spec():
    return ContractSpec("A0", multiplier=10, tick_size=1.0, margin_rate=0.1, commission_per_lot=1.0)


def _frame(rows, symbol="A0"):
    n = len(rows)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "symbol": [symbol] * n,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100000] * n,
            "open_interest": [1] * n,
        }
    )


def _run_with_stop_event():
    rows = [
        ROW,
        ROW,
        ROW,
        ROW,
        (100.0, 100.5, 96.0, 98.0),
        (98.0, 98.5, 97.5, 98.0),
        (98.0, 98.5, 97.5, 98.0),
        (98.0, 98.5, 97.5, 98.0),
        (98.0, 99.5, 97.5, 99.0),
    ]
    signals = [0, 1, 1, 1, 1, 1, 1, 1, 1]
    contracts = {"A0": _spec()}
    return run_portfolio(
        {"A0": _frame(rows)},
        {"A0": (_Stub, {"signals": signals})},
        contracts,
        initial_capital=100000,
        sizing_cfg={"mode": "fixed", "lots": 1, "atr_window": 2},
        risk_cfg={"enabled": True},
    )


def _compare_stub():
    base = {
        "total_return": -0.01,
        "max_drawdown": -0.05,
        "sharpe": -0.1,
        "worst_day": -0.02,
        "volatility": 0.08,
        "trade_count": 3,
    }
    return {
        "bare": dict(base),
        "risk": {**base, "stop_events": 1, "fuse_events": 0},
    }


def test_risk_section_renders(tmp_path):
    result = _run_with_stop_event()
    stress = run_stress(
        result.equity_df,
        {"A0": _frame([ROW] * 9)},
        {"A0": _spec()},
        initial_capital=100000,
        top_n=1,
        mc_runs=10,
        seed=1,
    )
    risk_daily = build_risk_daily(result.equity_df, var_window=250)
    text = build_risk_section(
        result,
        risk_cfg={"enabled": True},
        stress=stress,
        risk_daily=risk_daily,
        compare=_compare_stub(),
        initial_capital=100000,
    )
    for token in (
        "风险与压力测试",
        "【风控配置摘要】",
        "【风控事件汇总】",
        "【敞口与杠杆】",
        "【压力测试】",
        "【风控开 / 关对比】",
        "【口径说明】",
    ):
        assert token in text
    assert "固定止损 1" in text  # 事件计数（1 次固定止损）

    paths = save_risk_artifacts(result, stress, risk_daily, _compare_stub(), out_dir=tmp_path)
    assert len(paths) == 4
    ev_csv = (tmp_path / "risk_events.csv").read_text(encoding="utf-8-sig")
    assert "stop_fixed" in ev_csv
    rd_csv = (tmp_path / "risk_daily.csv").read_text(encoding="utf-8-sig")
    assert "var95" in rd_csv and "fuse_state" in rd_csv
    st_csv = (tmp_path / "stress_results.csv").read_text(encoding="utf-8-sig")
    assert "scenario" in st_csv
    cmp_csv = (tmp_path / "risk_compare.csv").read_text(encoding="utf-8-sig")
    assert "bare_fixed_1lot" in cmp_csv


def test_risk_artifacts_empty_events_keep_header(tmp_path):
    # 无事件场景：risk_events.csv 仅表头
    rows = [ROW] * 4
    contracts = {"A0": _spec()}
    result = run_portfolio(
        {"A0": _frame(rows)},
        {"A0": (_Stub, {"signals": [0, 0, 0, 0]})},
        contracts,
        initial_capital=100000,
        sizing_cfg={"mode": "fixed", "lots": 1},
        risk_cfg={"enabled": True},
    )
    paths = save_risk_artifacts(result, None, None, None, out_dir=tmp_path)
    assert len(paths) == 1
    ev_csv = (tmp_path / "risk_events.csv").read_text(encoding="utf-8-sig")
    assert ev_csv.splitlines()[0] == "date,kind,symbol,detail"
