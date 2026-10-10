"""组合报告与产出冒烟测试。"""

from pathlib import Path

import pandas as pd
import pytest

from src.engine.contracts import ContractSpec
from src.engine.portfolio_engine import run_portfolio
from src.report.portfolio_report import (
    build_portfolio_report,
    correlation_matrix,
    save_portfolio_artifacts,
    symbol_summary,
)
from src.strategy.base import StrategyBase


class _Stub(StrategyBase):
    """测试用固定信号策略：直接回放 ``params["signals"]``。"""

    name = "stub_sig"

    def __init__(self, params=None):
        super().__init__(params)
        self._signals = list(self.params.get("signals", []))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = self._signals
        return out


def _make_df(dates, symbol):
    n = len(dates)
    closes = [1000.0] * n
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "symbol": [symbol] * n,
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [10] * n,
            "open_interest": [1] * n,
        }
    )


def _result():
    dates = pd.date_range("2024-01-01", periods=40, freq="D")
    data = {"A0": _make_df(dates, "A0"), "B0": _make_df(dates, "B0")}
    sig_a = [1 if (i // 5) % 2 == 0 else -1 for i in range(40)]
    sig_b = [-1 if (i // 5) % 2 == 0 else 1 for i in range(40)]
    specs = {"A0": (_Stub, {"signals": sig_a}), "B0": (_Stub, {"signals": sig_b})}
    contracts = {
        "A0": ContractSpec(
            "A0", multiplier=10, tick_size=1.0, margin_rate=0.10, commission_per_lot=2.0
        ),
        "B0": ContractSpec(
            "B0", multiplier=10, tick_size=1.0, margin_rate=0.10, commission_per_lot=2.0
        ),
    }
    return run_portfolio(
        data, specs, contracts, initial_capital=100000, sizing_cfg={"mode": "fixed", "lots": 1}
    )


def test_report_sections_and_tables():
    result = _result()
    labels = {"A0": "甲品种 A0（主力连续）", "B0": "乙品种 B0（主力连续）"}
    text = build_portfolio_report(
        result,
        labels=labels,
        initial_capital=100000,
        start_date="2024-01-01",
        end_date="2024-02-09",
        sizing_desc="fixed（每品种 1 手）",
    )
    for token in ("组合回测报告", "【组合汇总】", "【逐品种汇总】", "【相关性矩阵", "【口径说明】"):
        assert token in text
    assert "【约束事件（共 0 条，前 10 条）】" in text

    sym_df = symbol_summary(result, labels, 100000)
    assert len(sym_df) == 2
    assert {"symbol", "pnl", "contribution", "trades", "sharpe", "max_drawdown"}.issubset(
        sym_df.columns
    )

    corr = correlation_matrix(result)
    assert corr.shape == (2, 2)
    assert corr.iloc[0, 1] == pytest.approx(corr.iloc[1, 0])


def test_save_artifacts(tmp_path):
    result = _result()
    labels = {"A0": "A0", "B0": "B0"}
    paths = save_portfolio_artifacts(
        result, labels=labels, initial_capital=100000, out_dir=tmp_path
    )
    assert len(paths) == 3
    for p in paths.values():
        assert Path(p).exists()
    events_csv = (tmp_path / "portfolio_events.csv").read_text(encoding="utf-8-sig")
    assert "symbol" in events_csv  # 空事件时仅有表头
    daily_csv = (tmp_path / "portfolio_daily.csv").read_text(encoding="utf-8-sig")
    assert "margin_used" in daily_csv and "pnl_A0" in daily_csv
