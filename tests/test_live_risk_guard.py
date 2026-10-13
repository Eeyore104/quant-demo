"""风控闸门测试（v2.0 轮 2）：闸门顺序 / 降级 / 止损触价 / 熔断 / 重入 / 持久化。"""

import pytest

from src.engine.contracts import ContractSpec
from src.live.ctp_api import Tick
from src.live.ledger import VirtualLedger
from src.live.risk_guard import LiveRiskGuard


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
                "enabled": True, "threshold": 0.15, "recovery": 0.10,
                "degrade_factor": 0.5, "max_cooldown_days": 20,
            },
            "losing_streak": {"enabled": True, "count": 5, "cooldown_days": 10},
            "liquidate": {"enabled": False, "threshold": 0.25, "cooldown_days": 10},
        },
        "exposure": {"enabled": True, "max_symbol_pct": 1.50, "max_total_pct": 4.00},
        "price_limit": {"enabled": True, "detect_factor": 0.95, "rates": {"M0": 0.07}},
        "liquidity": {"enabled": True, "max_volume_share": 0.05},
    }


def _contracts() -> dict[str, ContractSpec]:
    return {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}


def _setup(capital: float = 100000.0):
    contracts = _contracts()
    ledger = VirtualLedger(capital, contracts, {"m2701": "M0"})
    ledger.begin_day()
    guard = LiveRiskGuard(_risk_cfg(), contracts, ledger, prev_close={"M0": 3000.0})
    return ledger, guard


def _tick(last=3000.0, volume=100000, high=None, low=None, open_=None) -> Tick:
    return Tick(
        instrument="m2701",
        last_price=last,
        open_price=open_ if open_ is not None else last,
        highest_price=high if high is not None else last,
        lowest_price=low if low is not None else last,
        volume=volume,
    )


def test_gate_open_pass():
    _, guard = _setup()
    lots, notes, events = guard.gate_open(
        "M0", "LONG", 1, 3000.0, _tick(), sig=1, date="2026-10-13"
    )
    assert lots == 1 and notes == [] and events == []


def test_gate_open_blocked_by_fuse():
    ledger, guard = _setup()
    guard.manager.fuse.until_daily = ledger.day_idx + 5  # 熔断冷却中
    lots, notes, events = guard.gate_open(
        "M0", "LONG", 1, 3000.0, _tick(), sig=1, date="2026-10-13"
    )
    assert lots == 0 and any("熔断" in n for n in notes)
    assert events and events[0].kind == "fuse_block"


def test_gate_open_degraded_by_margin():
    contracts = _contracts()
    cfg = _risk_cfg()
    cfg["exposure"] = {"enabled": False}  # 关敞口，聚焦资金降级（默认敞口更紧会先拦）
    ledger = VirtualLedger(5000.0, contracts, {"m2701": "M0"})
    ledger.begin_day()
    guard = LiveRiskGuard(cfg, contracts, ledger, prev_close={"M0": 3000.0})
    # 开 5 手需 5×2100=10500 > 可用 5000 → 可负担 2 手
    lots, notes, events = guard.gate_open(
        "M0", "LONG", 5, 3000.0, _tick(), sig=1, date="2026-10-13"
    )
    assert lots == 2
    assert any("资金不足" in n for n in notes)
    assert any(e.kind == "margin_degrade" for e in events)


def test_gate_open_rejected_by_limit_up():
    _, guard = _setup()
    # prev_close=3000，涨停幅度 7%，detect=0.95 → 涨幅 ≥ 6.65% 视为涨停
    lots, notes, events = guard.gate_open(
        "M0", "LONG", 1, 3200.0, _tick(last=3200.0), sig=1, date="2026-10-13"
    )
    assert lots == 0 and any("涨停" in n for n in notes)


def test_gate_close_blocked_by_limit_down():
    _, guard = _setup()
    # 跌停拒卖：持多平仓 = 卖出 → 被拒
    lots, notes, _ = guard.gate_close("M0", "LONG", 1, _tick(last=2800.0), date="2026-10-13")
    assert lots == 0 and any("跌停" in n for n in notes)
    # 持空平仓 = 买入 → 放行
    lots2, _, _ = guard.gate_close("M0", "SHORT", 1, _tick(last=2800.0), date="2026-10-13")
    assert lots2 == 1


def test_exposure_room_trims_open():
    ledger, guard = _setup(capital=30000.0)
    ledger.account.mark_price("M0", 3000.0)
    # 单品种名义上限 = 权益 ×150% = 45000 元；1 手名义 = 3000×10 = 30000 元
    # → 最多 1 手；申请 20 手被裁剪到 1（再经资金/流动性检查通过）
    lots, notes, events = guard.gate_open(
        "M0", "LONG", 20, 3000.0, _tick(), sig=1, date="2026-10-13"
    )
    assert lots == 1 and any("敞口调整" in n for n in notes)
    assert any(e.kind == "exposure_trim" for e in events)


def test_participation_cap_trims_open():
    _, guard = _setup()
    # 当日成交量 20 手 × 5% = 1 手 → 申请 3 手调整到 1 手
    lots, notes, _ = guard.gate_open(
        "M0", "LONG", 3, 3000.0, _tick(volume=20), sig=1, date="2026-10-13"
    )
    assert lots == 1 and any("参与率调整" in n for n in notes)


def test_on_tick_stop_trigger_and_reentry_lock():
    ledger, guard = _setup()
    # 入场：多 1 手 @3000，ATR=10 → 固定止损 2980
    guard.manager.stops.register_entry("M0", 1, 3000.0, 10.0, ledger.day_idx - 1)
    trig = guard.on_tick("M0", _tick(last=2975.0, low=2975.0))
    assert trig is not None and trig.kind == "stop_fixed" and trig.level == pytest.approx(2980.0)

    # 止损执行完成 → 重入语义：当日禁开（stopped_today）；
    # 信号归零释放方向锁；次一交易日恢复正常
    guard.notify_stop("M0", 1, date="2026-10-13")
    assert guard.manager.guard.allows_open("M0", 1) is False
    guard.update_signal("M0", 0)  # 信号归零 → 释放 dir_lock
    assert guard.manager.guard.allows_open("M0", 1) is False  # 当日仍禁
    guard.new_day()
    assert guard.manager.guard.allows_open("M0", 1) is True


def test_on_day_close_fuse_daily_loss_and_advance():
    ledger, guard = _setup()
    # 开 10 手多 @3000，价格跌到 2970 → 浮亏 3000（-3%）→ 触发单日亏损熔断
    ledger.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=10, trading_day="2026-10-13",
    )
    ledger.account.mark_price("M0", 2970.0)
    result = guard.on_day_close("2026-10-13", atr_map={"M0": 10.0})
    kinds = [e.kind for e in result["events"]]
    assert "fuse_pause" in kinds
    assert result["fuse_state"] == "paused"
    # 账本推进：prev_equity = 当前权益；峰值保持不变
    assert ledger.prev_equity == pytest.approx(97000.0 - 12.0)
    assert ledger.peak_equity == pytest.approx(100000.0)


def test_persistence_roundtrip(tmp_path):
    ledger, guard = _setup()
    guard.manager.stops.register_entry("M0", 1, 3000.0, 10.0, ledger.day_idx - 1)
    guard.manager.fuse.until_daily = ledger.day_idx + 2
    guard.notify_stop("M0", 1)
    path = tmp_path / "risk.json"
    guard.save(path)

    ledger2, guard2 = _setup()
    assert guard2.load(path) is True
    assert guard2.manager.fuse.until_daily == guard.manager.fuse.until_daily
    assert guard2.prev_close == guard.prev_close
    # 注意：notify_stop 会 clear 入场状态 → stops 里已无 M0
    assert guard2.manager.stops.state("M0") is None
    assert guard2.manager.guard.dir_lock == guard.manager.guard.dir_lock
