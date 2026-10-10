"""组合引擎单元测试：T+1 成交 / 保证金约束 / 翻仓 / 日历缺失 / 财务闭合。"""

import pandas as pd
import pytest

from src.engine.contracts import ContractSpec
from src.engine.portfolio_engine import run_portfolio
from src.strategy.base import StrategyBase


class _Stub(StrategyBase):
    """测试用固定信号策略：直接回放 ``params["signals"]``（不做任何计算）。"""

    name = "stub_sig"

    def __init__(self, params=None):
        super().__init__(params)
        self._signals = list(self.params.get("signals", []))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = self._signals
        return out


def _make_df(dates, closes, symbol="A0"):
    n = len(dates)
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


def _contracts(margin_rate=0.10):
    return {
        "A0": ContractSpec(
            "A0", multiplier=10, tick_size=1.0, margin_rate=margin_rate, commission_per_lot=2.0
        ),
        "B0": ContractSpec(
            "B0", multiplier=10, tick_size=1.0, margin_rate=margin_rate, commission_per_lot=2.0
        ),
    }


DATES = pd.date_range("2024-01-01", periods=6, freq="D")


def test_t_plus_1_execution_and_closure():
    closes = [1000.0] * 6
    data = {"A0": _make_df(DATES, closes, "A0"), "B0": _make_df(DATES, closes, "B0")}
    specs = {"A0": (_Stub, {"signals": [0, 1, 1, 0, 0, 0]}), "B0": (_Stub, {"signals": [0] * 6})}

    result = run_portfolio(
        data, specs, _contracts(), initial_capital=100000, sizing_cfg={"mode": "fixed", "lots": 1}
    )

    trades = [t for t in result.trades if t.symbol == "A0"]
    assert len(trades) == 2
    # T+1：signal[1]=1 → 在 index 2（第 3 根 bar）开盘执行
    assert trades[0].action == "OPEN" and trades[0].date == DATES[2]
    assert trades[0].price == pytest.approx(1001.0)  # 开盘 1000 + 1 跳滑点
    # signal[3]=0 → 在 index 4 平仓
    assert trades[1].action == "CLOSE" and trades[1].date == DATES[4]
    assert trades[1].price == pytest.approx(999.0)

    assert len(result.equity_df) == 6
    assert result.account.state("A0").size == 0
    assert len(result.events) == 0  # 100k 资金充裕

    # 财务闭合：组合权益 − 初始 = Σ品种盈亏 − 手续费
    assert result.equity_df["equity"].iloc[-1] - 100000 == pytest.approx(
        sum(result.account.symbol_pnl(s) for s in result.symbols) - result.account.total_commission
    )
    # 逐日 pnl 列与账户口径一致
    pnl_cols = [c for c in result.equity_df.columns if c.startswith("pnl_")]
    assert result.equity_df[pnl_cols].iloc[-1].sum() == pytest.approx(
        sum(result.account.symbol_pnl(s) for s in result.symbols)
    )


def test_margin_constraint_refuses_open():
    closes = [1000.0] * 6
    data = {"A0": _make_df(DATES, closes, "A0"), "B0": _make_df(DATES, closes, "B0")}
    specs = {"A0": (_Stub, {"signals": [0, 1, 0, 0, 0, 0]}), "B0": (_Stub, {"signals": [0] * 6})}

    # margin_rate=1.0 → 单手保证金 ≈ 10000 > 可用资金 3000 → 拒绝开仓
    result = run_portfolio(
        data,
        specs,
        _contracts(margin_rate=1.0),
        initial_capital=3000,
        sizing_cfg={"mode": "fixed", "lots": 1},
    )

    assert len(result.events) == 1
    assert result.account.state("A0").size == 0
    assert len(result.trades) == 0
    assert result.equity_df["equity"].iloc[-1] == pytest.approx(3000.0)  # 无成交、无成本


def test_flip_produces_close_and_open():
    closes = [1000.0] * 6
    data = {"A0": _make_df(DATES, closes, "A0"), "B0": _make_df(DATES, closes, "B0")}
    specs = {
        "A0": (_Stub, {"signals": [0, 1, -1, -1, -1, -1]}),
        "B0": (_Stub, {"signals": [0] * 6}),
    }

    result = run_portfolio(
        data, specs, _contracts(), initial_capital=100000, sizing_cfg={"mode": "fixed", "lots": 1}
    )

    flip_day = [t for t in result.trades if t.symbol == "A0" and t.date == DATES[3]]
    assert [t.action for t in flip_day] == ["CLOSE", "OPEN"]  # 先平后开
    assert result.account.state("A0").size == -1


def test_missing_dates_hold_position():
    # B0 缺少中间一天（01-04）：持仓跨缺失日保持，组合仍按并集日历逐日记录
    dates_b = DATES.delete(3)
    data = {
        "A0": _make_df(DATES, [1000.0] * 6, "A0"),
        "B0": _make_df(dates_b, [1000.0] * 5, "B0"),
    }
    specs = {"A0": (_Stub, {"signals": [0] * 6}), "B0": (_Stub, {"signals": [0, 1, 1, 1, 1]})}

    result = run_portfolio(
        data, specs, _contracts(), initial_capital=100000, sizing_cfg={"mode": "fixed", "lots": 1}
    )

    assert len(result.equity_df) == 6  # 并集日历
    b_trades = [t for t in result.trades if t.symbol == "B0"]
    assert len(b_trades) == 1 and b_trades[0].date == pd.Timestamp("2024-01-03")
    assert result.account.state("B0").size == 1
    # 缺失日（01-04）仍记录，保证金按最近收盘价保持
    gap_row = result.equity_df[result.equity_df["date"] == pd.Timestamp("2024-01-04")].iloc[0]
    assert gap_row["margin_used"] > 0
