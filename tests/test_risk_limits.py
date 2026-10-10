"""涨跌停与流动性约束测试（v1.3）：判定函数 + 引擎集成（拒单 / 止不掉 / 参与率）。"""

from types import SimpleNamespace

import pandas as pd

from src.engine.contracts import ContractSpec
from src.engine.portfolio_engine import run_portfolio
from src.engine.risk import RiskConfig, RiskManager
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


def test_limit_state_detection():
    rm = RiskManager(RiskConfig(price_limit_rates={"A0": 0.08}), {})
    assert rm.limit_state("A0", 100.0, SimpleNamespace(close=108.0)) == "up"
    assert rm.limit_state("A0", 100.0, SimpleNamespace(close=92.0)) == "down"
    assert rm.limit_state("A0", 100.0, SimpleNamespace(close=103.0)) is None
    assert rm.limit_state("B0", 100.0, SimpleNamespace(close=108.0)) is None  # 无幅度配置 → 不判定


def test_leg_blocked_matrix():
    assert RiskManager.leg_blocked("up", "OPEN", "LONG") is True  # 涨停不能买开
    assert RiskManager.leg_blocked("up", "CLOSE", "SHORT") is True  # 涨停不能买平
    assert RiskManager.leg_blocked("up", "OPEN", "SHORT") is False  # 涨停可以卖开
    assert RiskManager.leg_blocked("up", "CLOSE", "LONG") is False  # 涨停可以卖平
    assert RiskManager.leg_blocked("down", "OPEN", "SHORT") is True  # 跌停不能卖开
    assert RiskManager.leg_blocked("down", "CLOSE", "LONG") is True  # 跌停不能卖平（止不掉）
    assert RiskManager.leg_blocked("down", "OPEN", "LONG") is False
    assert RiskManager.leg_blocked(None, "OPEN", "LONG") is False


LIMIT_RISK = {"price_limit": {"enabled": True, "detect_factor": 0.95, "rates": {"A0": 0.08}}}


def test_limit_up_blocks_entry_then_allows_next_day():
    rows = [
        (92.0, 92.5, 91.5, 92.0),
        (92.0, 92.5, 91.5, 92.0),
        (100.0, 100.5, 92.5, 100.0),  # +8.7% 涨停日 → 信号单（买入）被拒
        (100.0, 100.5, 99.5, 100.0),  # 次日恢复 → 正常开仓
    ]
    result = run_portfolio(
        {"A0": _frame(rows)},
        {"A0": (_Stub, {"signals": [0, 1, 1, 1]})},
        {"A0": _spec()},
        initial_capital=100000,
        sizing_cfg={"mode": "fixed", "lots": 1},
        risk_cfg=LIMIT_RISK,
    )
    opens = [t for t in result.trades if t.action == "OPEN"]
    assert len(opens) == 1 and opens[0].date == pd.Timestamp("2024-01-04")
    assert any(e.kind == "limit_refuse" for e in result.risk_events)


def test_limit_down_stop_cannot_fill_then_fills_next_day():
    # 多头持仓 → 跌停日止损（卖出方向）被拒 → 次日开盘缺口再次触发并成交
    rows = [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.5, 99.5, 100.0),  # 入场（signal[1]=1 → 本行开盘成交）
        (100.0, 100.5, 99.5, 100.0),
        (92.0, 92.5, 91.5, 92.0),  # -8% 跌停：跳空触发止损 → 卖出被拒（stop_refused）
        (92.0, 92.5, 91.5, 92.0),  # 无停板：再次触发 → 正常平仓 @ 92-1
        (92.0, 92.5, 91.5, 92.0),
        (92.0, 92.5, 91.5, 92.0),
        (92.0, 92.5, 91.5, 92.0),
    ]
    result = run_portfolio(
        {"A0": _frame(rows)},
        {"A0": (_Stub, {"signals": [0, 1, 1, 1, 1, 1, 1, 1, 1]})},
        {"A0": _spec()},
        initial_capital=100000,
        sizing_cfg={"mode": "fixed", "lots": 1, "atr_window": 2},
        risk_cfg=LIMIT_RISK,
    )
    refused = [e for e in result.risk_events if e.kind == "stop_refused"]
    assert len(refused) == 1 and "跌停" in refused[0].detail
    closes = [t for t in result.trades if t.action == "CLOSE"]
    assert len(closes) == 1 and closes[0].date == pd.Timestamp("2024-01-06")
    assert closes[0].price == 91.0  # 92 - 1 跳滑点
    assert result.account.state("A0").size == 0
    # 止损后同向信号（未重置）不得重入
    assert len([t for t in result.trades if t.action == "OPEN"]) == 1


def test_participation_cap_trims_open():
    rows = [(1000.0, 1000.5, 999.5, 1000.0)] * 4
    result = run_portfolio(
        {"A0": _frame(rows, volume=30)},  # 参与率 5% → 最多 1 手
        {"A0": (_Stub, {"signals": [0, 0, 1, 1]})},
        {"A0": _spec()},
        initial_capital=1000000,
        sizing_cfg={"mode": "fixed", "lots": 3},
        risk_cfg={"liquidity": {"enabled": True, "max_volume_share": 0.05}},
    )
    opens = [t for t in result.trades if t.action == "OPEN"]
    assert len(opens) == 1 and opens[0].volume == 1  # 30 × 5% = 1.5 → 1 手
    assert any(e.kind == "participation_trim" for e in result.risk_events)


def test_participation_zero_refuses():
    rows = [(1000.0, 1000.5, 999.5, 1000.0)] * 4
    result = run_portfolio(
        {"A0": _frame(rows, volume=10)},  # 10 × 5% = 0 手 → 拒绝
        {"A0": (_Stub, {"signals": [0, 0, 1, 1]})},
        {"A0": _spec()},
        initial_capital=1000000,
        sizing_cfg={"mode": "fixed", "lots": 1},
        risk_cfg={"liquidity": {"enabled": True, "max_volume_share": 0.05}},
    )
    assert len(result.trades) == 0
    assert any(e.kind == "participation_trim" for e in result.risk_events)
