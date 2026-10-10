"""组合账户单元测试：保证金 / 可用资金 / 拒绝开仓 / 权益闭合。"""

import pytest

from src.engine.account import PortfolioAccount
from src.engine.contracts import ContractSpec
from src.engine.portfolio import Trade


def _contracts():
    return {
        "RB0": ContractSpec(
            "RB0", multiplier=10, tick_size=1.0, margin_rate=0.10, commission_per_lot=3.0
        ),
        "M0": ContractSpec(
            "M0", multiplier=10, tick_size=1.0, margin_rate=0.08, commission_per_lot=1.5
        ),
    }


def _trade(symbol, direction, action, price, volume, commission=0.0):
    return Trade(
        date="2024-01-02",
        symbol=symbol,
        direction=direction,
        action=action,
        price=price,
        volume=volume,
        commission=commission,
    )


def test_margin_and_available():
    acc = PortfolioAccount(100000, _contracts())
    acc.mark_price("RB0", 3000)
    acc.apply_trades([_trade("RB0", "LONG", "OPEN", 3000, 2, commission=6.0)])
    # 保证金 = 2 × 3000 × 10 × 0.10 = 6000
    assert acc.margin_used == pytest.approx(6000)
    assert acc.cash == pytest.approx(100000 - 6.0)
    assert acc.equity == pytest.approx(100000 - 6.0)  # 无浮盈（价格=开仓价）
    assert acc.available == pytest.approx(100000 - 6.0 - 6000)
    # 价格变动 → 浮盈 + 保证金重估
    acc.mark_price("RB0", 3100)
    assert acc.unrealized_pnl == pytest.approx(100 * 10 * 2)
    assert acc.margin_used == pytest.approx(6200)


def test_close_realized_pnl_and_closure():
    acc = PortfolioAccount(100000, _contracts())
    acc.mark_price("M0", 3000)
    acc.apply_trades([_trade("M0", "LONG", "OPEN", 3000, 2, commission=3.0)])
    acc.apply_trades([_trade("M0", "LONG", "CLOSE", 3050, 2, commission=3.0)])
    assert acc.state("M0").size == 0
    assert acc.state("M0").realized_pnl == pytest.approx(50 * 10 * 2)
    assert acc.cash == pytest.approx(100000 + 1000 - 6.0)
    # 财务闭合：权益 − 初始 = Σ品种盈亏 − 手续费
    assert acc.equity - 100000 == pytest.approx(
        sum(acc.symbol_pnl(s) for s in acc.states) - acc.total_commission
    )


def test_open_margin_and_constraint_event():
    acc = PortfolioAccount(5000, _contracts())
    acc.mark_price("RB0", 3000)
    needed = acc.open_margin("RB0", 2, 3000)
    assert needed == pytest.approx(6000)
    assert needed > acc.available  # 5,000 < 6,000 → 应拒绝
    acc.record_event("2024-01-02", "RB0", needed, acc.available)
    assert len(acc.events) == 1
    assert "RB0" in acc.events[0].describe()


def test_record_day_columns():
    acc = PortfolioAccount(100000, _contracts())
    acc.mark_price("RB0", 3000)
    acc.mark_price("M0", 3000)
    acc.apply_trades([_trade("RB0", "LONG", "OPEN", 3000, 1, commission=3)])
    acc.record_day("2024-01-02")
    row = acc.daily[0]
    assert set(row) >= {
        "date",
        "equity",
        "cash",
        "margin_used",
        "available",
        "unrealized_pnl",
        "pnl_RB0",
        "pnl_M0",
    }
    assert row["pnl_RB0"] == pytest.approx(0.0)
    assert row["margin_used"] == pytest.approx(3000)
