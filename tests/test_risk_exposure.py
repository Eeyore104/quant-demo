"""敞口上限测试（v1.3）：辅助函数 + 引擎集成（截断 / 拒绝 / 超限记录）。"""

import pandas as pd

from src.engine.account import PortfolioAccount
from src.engine.contracts import ContractSpec
from src.engine.portfolio import Trade
from src.engine.portfolio_engine import run_portfolio
from src.engine.risk import RiskConfig, exposure_room, exposure_warnings
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


def _spec(margin_rate=0.1):
    return ContractSpec(
        "A0", multiplier=10, tick_size=1.0, margin_rate=margin_rate, commission_per_lot=1.0
    )


def _frame(rows, symbol="A0", volume=100000):
    n = len(rows)
    return pd.DataFrame(
        {
            "date": pd.to_datetime([f"2024-01-{i + 1:02d}" for i in range(n)]),
            "symbol": [symbol] * n,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [volume] * n,
            "open_interest": [1] * n,
        }
    )


PRICE_ROWS = [(1000.0, 1000.5, 999.5, 1000.0)] * 4


def test_exposure_room_unit():
    acc = PortfolioAccount(100000, {"A0": _spec()})
    cfg = RiskConfig(exposure_max_symbol_pct=1.5, exposure_max_total_pct=4.0)
    room = exposure_room(acc, cfg, "A0", 1000.0, 10)
    assert room == 15  # min(150000, 400000) // 10000 = 15


def test_exposure_warnings_unit():
    acc = PortfolioAccount(100000, {"A0": _spec()})
    cfg = RiskConfig(exposure_max_symbol_pct=0.5, exposure_max_total_pct=1.0)
    acc.mark_price("A0", 1000)
    acc.apply_trades(
        [
            Trade(
                date="d",
                symbol="A0",
                direction="LONG",
                action="OPEN",
                price=1000,
                volume=6,
                commission=6.0,
            )
        ]
    )
    # 敞口 60000 > 单品种上限 50000 → 记 1；总敞口 60000 ≤ 组合上限 100000 → 不再加
    assert exposure_warnings(acc, cfg) == 1


def _run(rows, signals, *, risk_cfg, sizing, capital=100000, margin_rate=0.1):
    return run_portfolio(
        {"A0": _frame(rows)},
        {"A0": (_Stub, {"signals": signals})},
        {"A0": _spec(margin_rate)},
        initial_capital=capital,
        sizing_cfg=sizing,
        risk_cfg=risk_cfg,
    )


def test_exposure_trim_in_engine():
    # 目标 2 手；单品种上限 0.2 → 可用空间 = 20000 // 10010 = 1 手 → 截断为 1 手
    result = _run(
        PRICE_ROWS,
        [0, 0, 1, 1],
        risk_cfg={"exposure": {"enabled": True, "max_symbol_pct": 0.2, "max_total_pct": 4.0}},
        sizing={"mode": "fixed", "lots": 2},
    )
    opens = [t for t in result.trades if t.action == "OPEN"]
    assert len(opens) == 1 and opens[0].volume == 1
    assert any(e.kind == "exposure_trim" for e in result.risk_events)


def test_exposure_refuse_in_engine():
    # 单品种上限 0.08 → 可用空间 = 8000 // 10010 = 0 → 拒绝开仓
    result = _run(
        PRICE_ROWS,
        [0, 0, 1, 1],
        risk_cfg={"exposure": {"enabled": True, "max_symbol_pct": 0.08, "max_total_pct": 4.0}},
        sizing={"mode": "fixed", "lots": 2},
    )
    assert len(result.trades) == 0
    assert any(e.kind == "exposure_refuse" for e in result.risk_events)


def test_fuse_state_recorded_in_daily():
    result = _run(
        PRICE_ROWS,
        [0, 0, 1, 1],
        risk_cfg={"exposure": {"enabled": True}},
        sizing={"mode": "fixed", "lots": 1},
    )
    assert "fuse_state" in result.equity_df.columns
    assert set(result.equity_df["fuse_state"]) == {"normal"}
    assert "exposure_A0" in result.equity_df.columns
