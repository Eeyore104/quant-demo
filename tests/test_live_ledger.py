"""虚拟账本测试（v2.0 轮 2）：入账口径 / 今昨仓 / 降级 / 持久化。"""

import pytest

from src.engine.contracts import ContractSpec
from src.live.ledger import VirtualLedger


def _contracts() -> dict[str, ContractSpec]:
    # 玉米口径：乘数 10，保证金率 7%，手续费 1.2 元/手
    return {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}


def _ledger() -> VirtualLedger:
    return VirtualLedger(100000, _contracts(), {"m2701": "M0"})


def test_open_fill_margin_and_available():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=1, trading_day="2026-10-13",
    )
    lg.account.mark_price("M0", 3000.0)
    assert lg.net_size("M0") == 1
    assert lg.margin_used == pytest.approx(3000.0 * 10 * 0.07)  # 2100
    # 手续费已扣
    assert lg.equity == pytest.approx(100000 - 1.2)
    assert lg.available == pytest.approx(100000 - 1.2 - 2100)


def test_close_realized_pnl():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=2, trading_day="2026-10-13",
    )
    lg.account.mark_price("M0", 3100.0)
    t = lg.apply_fill(
        instrument="m2701", direction="LONG", offset="CLOSE", price=3100.0,
        volume=2, trading_day="2026-10-14",
    )
    assert t.pnl == pytest.approx((3100 - 3000) * 10 * 2)  # +2000
    assert lg.net_size("M0") == 0
    assert lg.equity == pytest.approx(100000 + 2000 - 1.2 * 4)  # 开 2 手 + 平 2 手手续费


def test_close_breakdown_yesterday_first():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=2, trading_day="2026-10-10",
    )
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3010.0,
        volume=1, trading_day="2026-10-13",
    )
    assert lg.yesterday_lots("M0", "LONG", "2026-10-13") == 2
    assert lg.today_lots("M0", "LONG", "2026-10-13") == 1
    # 平 3 手：先昨 2 再今 1
    assert lg.close_breakdown("M0", "LONG", 3, "2026-10-13") == [
        ("CLOSE_YESTERDAY", 2),
        ("CLOSE_TODAY", 1),
    ]
    # 平 1 手：只动昨仓
    assert lg.close_breakdown("M0", "LONG", 1, "2026-10-13") == [("CLOSE_YESTERDAY", 1)]


def test_reduce_open_lots_after_close_fill():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=2, trading_day="2026-10-10",
    )
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="CLOSE", price=3050.0,
        volume=1, trading_day="2026-10-13",
    )
    assert lg.yesterday_lots("M0", "LONG", "2026-10-13") == 1
    assert lg.net_size("M0") == 1


def test_affordable_lots_degrade():
    lg = _ledger()
    # 可用 10 万，单手保证金 2100 → 想开 50 手（需 10.5 万）→ 可负担 47 手
    n, msg = lg.affordable_lots("M0", 3000.0, 50)
    assert n == 47 and "资金不足" in msg
    # 资金充足：原样返回
    n2, msg2 = lg.affordable_lots("M0", 3000.0, 10)
    assert n2 == 10 and msg2 == ""


def test_unknown_instrument_raises():
    lg = _ledger()
    with pytest.raises(ValueError, match="未登记的合约"):
        lg.apply_fill(
            instrument="XX999", direction="LONG", offset="OPEN", price=1.0,
            volume=1, trading_day="2026-10-13",
        )


def test_persistence_roundtrip(tmp_path):
    lg = _ledger()
    lg.begin_day()
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="OPEN", price=3050.0,
        volume=1, trading_day="2026-10-13",
    )
    lg.account.mark_price("M0", 3040.0)
    lg.close_day()
    path = tmp_path / "ledger.json"
    lg.save(path)

    lg2 = _ledger()
    assert lg2.load(path) is True
    assert lg2.net_size("M0") == -1
    assert lg2.state("M0").avg_price == pytest.approx(3050.0)
    assert lg2.day_idx == lg.day_idx
    assert lg2.peak_equity == pytest.approx(lg.peak_equity)
    assert lg2.yesterday_lots("M0", "SHORT", "2026-10-14") == 1
    assert lg2.equity == pytest.approx(lg.equity)


def test_load_missing_file_returns_false(tmp_path):
    lg = _ledger()
    assert lg.load(tmp_path / "nope.json") is False
