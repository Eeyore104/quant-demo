"""组合引擎端到端风控测试（v1.3）：止损/重入、熔断暂停/恢复、降级执行、闭合与确定性。"""

import pandas as pd
import pytest

from src.engine.contracts import ContractSpec
from src.engine.portfolio_engine import run_portfolio
from src.strategy.base import StrategyBase

ROW = (100.0, 100.5, 99.5, 100.0)


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
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "symbol": [symbol] * n,
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [volume] * n,
            "open_interest": [1] * n,
        }
    )


def _run(rows, signals, *, risk_cfg, sizing, capital=100000, margin_rate=0.1, volume=100000):
    return run_portfolio(
        {"A0": _frame(rows, volume=volume)},
        {"A0": (_Stub, {"signals": signals})},
        {"A0": _spec(margin_rate)},
        initial_capital=capital,
        sizing_cfg=sizing,
        risk_cfg=risk_cfg,
    )


# ---------------------------------------------------------------- 场景 1：止损 → 锁定 → 重置 → 重入

STOP_ROWS = [
    ROW,  # 0
    ROW,  # 1
    ROW,  # 2 入场（signal[1]=1 → 本行开盘成交 @101）
    ROW,  # 3
    (100.0, 100.5, 96.0, 98.0),  # 4 盘中触及止损 @99 → 成交 @98
    (98.0, 98.5, 97.5, 98.0),  # 5 同向信号未重置 → 锁定
    (98.0, 98.5, 97.5, 98.0),  # 6 锁定
    (98.0, 98.5, 97.5, 98.0),  # 7 信号归零 → 释放
    (98.0, 99.5, 97.5, 99.0),  # 8 重入 @99
]
STOP_SIGNALS = [0, 1, 1, 1, 1, 1, 0, 1, 1]
DATES = pd.date_range("2024-01-01", periods=9, freq="D")


def test_stop_then_reentry_lock_and_reset():
    result = _run(
        STOP_ROWS,
        STOP_SIGNALS,
        risk_cfg={"enabled": True},
        sizing={"mode": "fixed", "lots": 1, "atr_window": 2},
    )
    trades = result.trades
    assert [(t.action, t.date, t.price) for t in trades] == [
        ("OPEN", DATES[2], 101.0),  # 入场：100 + 1 跳滑点
        ("CLOSE", DATES[4], 98.0),  # 止损：99 - 1 跳滑点
        ("OPEN", DATES[8], 99.0),  # 信号重置后重入
    ]
    assert trades[1].pnl == pytest.approx(-30.0)  # (98 - 101) × 10
    kinds = [e.kind for e in result.risk_events]
    assert kinds.count("stop_fixed") == 1
    # 锁定期间（d5/d6/d7）不得重入
    locked_days = {DATES[5], DATES[6], DATES[7]}
    assert not any(t.action == "OPEN" and t.date in locked_days for t in trades)
    # 财务闭合
    assert result.equity_df["equity"].iloc[-1] - 100000 == pytest.approx(
        sum(result.account.symbol_pnl(s) for s in result.symbols) - result.account.total_commission
    )


def test_risk_disabled_baseline_no_stops():
    # 同样行情、不启用风控：无止损；信号归零才平仓（v1.2 行为）
    result = _run(STOP_ROWS, STOP_SIGNALS, risk_cfg=None, sizing={"mode": "fixed", "lots": 1})
    assert [t.action for t in result.trades] == ["OPEN", "CLOSE", "OPEN"]
    assert result.trades[0].date == DATES[2] and result.trades[1].date == DATES[7]
    assert result.account.state("A0").size == 1
    assert len(result.risk_events) == 0


# ---------------------------------------------------------------- 场景 2：熔断暂停 → 冷却 → 恢复

FUSE_ROWS = [
    (1000.0, 1000.5, 999.5, 1000.0),  # 0
    (1000.0, 1000.5, 999.5, 1000.0),  # 1
    (1000.0, 1000.5, 999.5, 1000.0),  # 2 入场 @1001
    (1000.0, 1000.5, 969.5, 970.0),  # 3 单日 -3.1% → 熔断暂停
    (970.0, 970.5, 969.5, 970.0),  # 4 信号归零 → 平仓 @969
    (970.0, 970.5, 969.5, 970.0),  # 5 暂停中 → 禁开
    (970.0, 970.5, 969.5, 970.0),  # 6 暂停中 → 禁开
    (970.0, 970.5, 969.5, 970.0),  # 7 冷却结束 → 恢复开仓 @971
    (970.0, 970.5, 969.5, 970.0),  # 8
]
FUSE_SIGNALS = [0, 1, 1, 1, 0, 1, 1, 1, 1]


def test_fuse_pause_blocks_then_resumes():
    result = _run(
        FUSE_ROWS,
        FUSE_SIGNALS,
        risk_cfg={
            "enabled": True,
            "stops": {"fixed": {"enabled": False}, "trailing": {"enabled": False}},
        },
        sizing={"mode": "fixed", "lots": 1},
        capital=10000,
    )
    trades = result.trades
    assert [(t.action, t.date, t.price) for t in trades] == [
        ("OPEN", DATES[2], 1001.0),
        ("CLOSE", DATES[5], 969.0),
        ("OPEN", DATES[7], 971.0),
    ]
    kinds = [e.kind for e in result.risk_events]
    assert "fuse_pause" in kinds and "fuse_resume" in kinds
    # 暂停期（d6）无开仓
    assert not any(t.action == "OPEN" and t.date == DATES[6] for t in trades)
    # 财务闭合（含熔断路径）
    assert result.equity_df["equity"].iloc[-1] - 10000 == pytest.approx(
        sum(result.account.symbol_pnl(s) for s in result.symbols) - result.account.total_commission
    )


# ---------------------------------------------------------------- 场景 3：降级执行 / 整笔拒绝

FLAT_ROWS = [(1000.0, 1000.5, 999.5, 1000.0)] * 4
FLAT_SIGNALS = [0, 0, 1, 1]
NO_EXPOSURE = {"exposure": {"enabled": False}}


def test_margin_degrade_to_affordable_lots():
    # 目标 3 手；单手保证金 2002 元；可用 5000 → 降级开 2 手
    result = _run(
        FLAT_ROWS,
        FLAT_SIGNALS,
        risk_cfg={**NO_EXPOSURE, "execution": {"margin_degrade": True}},
        sizing={"mode": "fixed", "lots": 3},
        capital=5000,
        margin_rate=0.2,
    )
    opens = [t for t in result.trades if t.action == "OPEN"]
    assert len(opens) == 1 and opens[0].volume == 2
    evs = [e for e in result.risk_events if e.kind == "margin_degrade"]
    assert len(evs) == 1 and "实开 2 手" in evs[0].detail


def test_margin_degrade_to_zero_records_constraint_event():
    # 单手保证金 6006 > 可用 5000 → 连 1 手都不够 → 走约束事件（拒绝）
    result = _run(
        FLAT_ROWS,
        FLAT_SIGNALS,
        risk_cfg={**NO_EXPOSURE, "execution": {"margin_degrade": True}},
        sizing={"mode": "fixed", "lots": 3},
        capital=5000,
        margin_rate=0.6,
    )
    assert len(result.trades) == 0
    assert len(result.events) == 1
    assert not any(e.kind == "margin_degrade" for e in result.risk_events)


def test_margin_degrade_disabled_refuses_whole():
    # 关闭降级执行 → 保持 v1.2 整笔拒绝口径（即便买得起 2 手也不开）
    result = _run(
        FLAT_ROWS,
        FLAT_SIGNALS,
        risk_cfg={**NO_EXPOSURE, "execution": {"margin_degrade": False}},
        sizing={"mode": "fixed", "lots": 3},
        capital=5000,
        margin_rate=0.2,
    )
    assert len(result.trades) == 0
    assert len(result.events) == 1


def test_fuse_liquidate_closes_all_positions():
    # 全平档（默认关，本测试显式开启）：极端回撤 → 次日开盘全平并进入暂停
    rows = [
        (1000.0, 1000.5, 999.5, 1000.0),  # 0
        (1000.0, 1000.5, 999.5, 1000.0),  # 1
        (1000.0, 1000.5, 999.5, 1000.0),  # 2 入场 @1001
        (900.0, 900.5, 899.5, 900.0),  # 3 暴跌 -10% → 触发全平
        (900.0, 900.5, 899.5, 900.0),  # 4 开盘全平 @899
        (900.0, 900.5, 899.5, 900.0),  # 5
    ]
    risk_cfg = {
        "enabled": True,
        "stops": {"fixed": {"enabled": False}, "trailing": {"enabled": False}},
        "exposure": {"enabled": False},
        "fuse": {
            "daily_loss": {"enabled": False},
            "drawdown": {"enabled": False},
            "losing_streak": {"enabled": False},
            "liquidate": {"enabled": True, "threshold": 0.08, "cooldown_days": 3},
        },
    }
    result = _run(
        rows,
        [0, 1, 1, 1, 1, 1],
        risk_cfg=risk_cfg,
        sizing={"mode": "fixed", "lots": 1},
        capital=10000,
    )
    liq = [e for e in result.risk_events if e.kind == "fuse_liquidate"]
    assert len(liq) == 1 and "全平" in liq[0].detail
    closes = [t for t in result.trades if t.action == "CLOSE"]
    assert len(closes) == 1 and closes[0].date == DATES[4] and closes[0].price == 899.0
    assert result.account.state("A0").size == 0
    assert result.equity_df["fuse_state"].iloc[4] == "paused"  # 全平后进入冷却


# ---------------------------------------------------------------- 场景 4：确定性


def test_risk_run_is_deterministic():
    cfg = {"enabled": True}
    sizing = {"mode": "fixed", "lots": 1, "atr_window": 2}
    r1 = _run(STOP_ROWS, STOP_SIGNALS, risk_cfg=cfg, sizing=sizing, capital=100000)
    r2 = _run(STOP_ROWS, STOP_SIGNALS, risk_cfg=cfg, sizing=sizing, capital=100000)
    assert r1.equity_df["equity"].tolist() == r2.equity_df["equity"].tolist()
    assert [(t.action, str(t.date), t.price, t.volume) for t in r1.trades] == [
        (t.action, str(t.date), t.price, t.volume) for t in r2.trades
    ]
    assert [e.kind for e in r1.risk_events] == [e.kind for e in r2.risk_events]
