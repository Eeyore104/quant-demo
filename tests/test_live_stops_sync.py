"""v2.1 轮 1：live 侧止损状态同步 / 每日信号释放 / 日初推进 / 联调覆盖解析。

覆盖 docs/19 T2.1-1 ~ T2.1-5 的三分支语义与日界时序，守住「与回测
``portfolio_engine.register_entry_stops`` 同口径」这条线。
"""

import pytest

from run_live import LiveRuntime, advance_day, parse_force_targets
from src.engine.contracts import ContractSpec
from src.live.ctp_api import Tick
from src.live.ledger import VirtualLedger
from src.live.risk_guard import LiveRiskGuard
from src.live.signals import TargetPosition


def _risk_cfg() -> dict:
    return {
        "enabled": True,
        "reentry": {"mode": "signal_reset", "cooldown_days": 3},
        "execution": {"intrabar": True, "margin_degrade": True},
        "stops": {
            "fixed": {"enabled": True, "atr_mult": 2.0},
            "trailing": {"enabled": True, "atr_mult": 3.0},
            "take_profit": {"enabled": False, "pct": 0.04},
        },
        "fuse": {
            "daily_loss": {"enabled": True, "threshold": 0.03, "cooldown_days": 3},
            "drawdown": {
                "enabled": True,
                "threshold": 0.15,
                "recovery": 0.10,
                "degrade_factor": 0.5,
                "max_cooldown_days": 20,
            },
            "losing_streak": {"enabled": True, "count": 5, "cooldown_days": 10},
            "liquidate": {"enabled": False, "threshold": 0.25, "cooldown_days": 10},
        },
        "exposure": {"enabled": True, "max_symbol_pct": 1.50, "max_total_pct": 4.00},
        "price_limit": {"enabled": True, "detect_factor": 0.95, "rates": {"M0": 0.07}},
        "liquidity": {"enabled": True, "max_volume_share": 0.05},
    }


def _setup(capital: float = 100000.0):
    contracts = {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}
    ledger = VirtualLedger(capital, contracts, {"m2701": "M0"})
    ledger.begin_day()
    guard = LiveRiskGuard(_risk_cfg(), contracts, ledger, prev_close={"M0": 3000.0})
    return ledger, guard


def _open(ledger, price: float = 3000.0, volume: int = 2, direction: str = "LONG") -> None:
    ledger.apply_fill(
        instrument="m2701",
        direction=direction,
        offset="OPEN",
        price=price,
        volume=volume,
        trading_day="2026-10-22",
    )


def _close(ledger, price: float = 3000.0, volume: int = 2, direction: str = "LONG") -> None:
    ledger.apply_fill(
        instrument="m2701",
        direction=direction,
        offset="CLOSE",
        price=price,
        volume=volume,
        trading_day="2026-10-22",
    )


def _tick(last: float = 3000.0, volume: int = 100000, high=None, low=None) -> Tick:
    return Tick(
        instrument="m2701",
        last_price=last,
        open_price=last,
        highest_price=high if high is not None else last,
        lowest_price=low if low is not None else last,
        volume=volume,
    )


# ---------------------------------------------------------------- 三分支同步


def test_sync_register_on_fill():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)

    events = guard.sync_stops("M0", date="2026-10-22")

    st = guard.stop_state("M0")
    assert st is not None
    assert st.direction == 1
    assert st.entry_price == pytest.approx(3000.0)
    assert st.fixed_stop == pytest.approx(2960.0)  # 3000 - 2 × 20
    assert st.trail_stop == pytest.approx(2940.0)  # 3000 - 3 × 20
    assert st.entry_row == ledger.day_idx
    assert [e.kind for e in events] == ["stop_register"]


def test_sync_update_on_add_position():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)
    guard.sync_stops("M0")
    row0 = guard.stop_state("M0").entry_row

    _open(ledger, 2900.0, 2)  # 同向加仓 → 均价 2950
    events = guard.sync_stops("M0")

    st = guard.stop_state("M0")
    assert [e.kind for e in events] == ["stop_update"]
    assert st.entry_price == pytest.approx(2950.0)
    assert st.fixed_stop == pytest.approx(2910.0)  # 2950 - 2 × 20
    assert st.atr_entry == pytest.approx(20.0)  # ATR 保持入场读数
    assert st.entry_row == row0  # 基准日不因加仓重置


def test_sync_clear_when_flat():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)
    guard.sync_stops("M0")

    _close(ledger, 3050.0, 2)
    events = guard.sync_stops("M0")

    assert guard.stop_state("M0") is None
    assert [e.kind for e in events] == ["stop_clear"]


def test_sync_reverse_direction_rebuild():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)
    guard.sync_stops("M0")

    _close(ledger, 3000.0, 2)
    _open(ledger, 3000.0, 1, direction="SHORT")
    events = guard.sync_stops("M0")

    st = guard.stop_state("M0")
    assert [e.kind for e in events] == ["stop_register"]
    assert st.direction == -1
    assert st.fixed_stop == pytest.approx(3040.0)  # 3000 + 2 × 20


def test_sync_without_atr_keeps_state_but_no_level():
    ledger, guard = _setup()
    guard.atr_hint = {}
    _open(ledger, 3000.0, 2)

    events = guard.sync_stops("M0", row_idx=0)

    st = guard.stop_state("M0")
    assert [e.kind for e in events] == ["stop_register_no_atr"]
    assert st.atr_entry == 0.0
    assert st.fixed_stop == 0.0 and st.trail_stop == 0.0
    assert guard.on_tick("M0", _tick(last=1000.0, low=1000.0)) is None  # 无价位 → 不触发


def test_atr_upgrade_keeps_entry_row():
    ledger, guard = _setup()
    guard.atr_hint = {}
    _open(ledger, 3000.0, 2)
    guard.sync_stops("M0", row_idx=0)  # 先以 ATR=0 建状态

    guard.atr_hint = {"M0": 20.0}
    events = guard.sync_stops("M0")  # 每日校准补 ATR

    st = guard.stop_state("M0")
    assert [e.kind for e in events] == ["stop_register"]
    assert st.atr_entry == pytest.approx(20.0)
    assert st.entry_row == 0  # 保留原基准日，armed 不被重置
    assert st.fixed_stop == pytest.approx(2960.0)


# ---------------------------------------------------------------- 触发与重入


def test_backfill_armed_immediately():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)

    guard.sync_stops("M0", row_idx=ledger.day_idx - 1)  # 补建语义：视为昨日入场
    trig = guard.on_tick("M0", _tick(last=2950.0, low=2950.0))

    assert trig is not None
    assert trig.kind == "stop_fixed"
    assert trig.direction == 1


def test_new_position_not_armed_same_day():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)
    guard.sync_stops("M0")  # entry_row = 当日

    assert guard.on_tick("M0", _tick(last=2000.0, low=2000.0)) is None

    ledger.day_idx += 1  # 次一交易日
    assert guard.on_tick("M0", _tick(last=2950.0, low=2950.0)) is not None


def test_signal_reset_releases_lock_after_stop():
    ledger, guard = _setup()
    guard.atr_hint = {"M0": 20.0}
    _open(ledger, 3000.0, 2)
    guard.sync_stops("M0", row_idx=0)
    guard.on_tick("M0", _tick(last=2950.0, low=2950.0))
    guard.notify_stop("M0", 1, date="2026-10-22")

    assert guard.manager.guard.allows_open("M0", 1) is False  # stopped_today 拦截
    guard.new_day()
    assert guard.manager.guard.allows_open("M0", 1) is False  # dir_lock 仍锁
    guard.update_signal("M0", 0)
    assert guard.manager.guard.allows_open("M0", 1) is True


# ---------------------------------------------------------------- 日界推进


def test_advance_day_resets_state_and_baseline():
    ledger, guard = _setup()
    _open(ledger, 3000.0, 2)
    ledger.account.mark_price("M0", 3010.0)
    guard.manager.guard.stopped_today.add("M0")
    day0 = ledger.day_idx

    advance_day(ledger, guard)

    assert ledger.day_idx == day0 + 1
    assert ledger.day_ref_equity == pytest.approx(ledger.equity)
    assert "M0" not in guard.manager.guard.stopped_today
    assert ledger.equity - ledger.day_ref_equity == pytest.approx(0.0)


def test_day_pnl_is_single_day_after_advance():
    ledger, guard = _setup()
    _open(ledger, 3000.0, 2)
    ledger.account.mark_price("M0", 3010.0)
    advance_day(ledger, guard)

    ledger.account.mark_price("M0", 3020.0)
    day_pnl = ledger.equity - ledger.day_ref_equity

    assert day_pnl == pytest.approx(2 * 10 * 10)  # 2 手 × 乘数 10 × 涨 10 元


# ---------------------------------------------------------------- 联调参数


def test_parse_force_targets():
    assert parse_force_targets("M0=+1, RB0=-2", ["M0", "RB0"]) == {"M0": 1, "RB0": -2}
    assert parse_force_targets("", ["M0"]) == {}


def test_parse_force_targets_rejects_unknown_symbol():
    with pytest.raises(ValueError, match="不在 live.contracts_map"):
        parse_force_targets("XX=1", ["M0"])


def test_parse_force_targets_rejects_bad_lots():
    with pytest.raises(ValueError, match="手数须为整数"):
        parse_force_targets("M0=abc", ["M0"])
    with pytest.raises(ValueError, match="格式应为"):
        parse_force_targets("M0", ["M0"])


def test_apply_force_targets_keeps_signal():
    rt = object.__new__(LiveRuntime)
    rt.force_targets = {"M0": -3}
    targets = {"M0": TargetPosition(symbol="M0", signal=1, direction=1, lots=2, ref_price=3000.0)}

    rt._apply_force_targets(targets)

    assert targets["M0"].direction == -1
    assert targets["M0"].lots == 3
    assert targets["M0"].signal == 1  # 原信号保留，供重入锁定释放
