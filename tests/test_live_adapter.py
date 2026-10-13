"""执行适配器测试（v2.0 轮 2）：目标执行 / 拆分 / 账本入账 / 止损 / 追单。

用 FakeSession 模拟 CTP 会话（不连网）；事件用 ctp_api 的事件对象构造。
"""

import time

from src.engine.contracts import ContractSpec
from src.live.adapter import Executor
from src.live.ctp_api import OrderRejected, OrderRtn, Tick, TradeRtn
from src.live.ledger import VirtualLedger
from src.live.orders import LiveOrder, OrderBook
from src.live.risk_guard import LiveRiskGuard
from src.live.signals import TargetPosition


class FakeSession:
    """假会话：记录报单 / 撤单请求。"""

    def __init__(self):
        self.inserted: list[dict] = []
        self.actions: list[str] = []
        self._seq = 0

    def order_insert(self, instrument, direction, offset, price, volume):
        self._seq += 1
        ref = str(self._seq)
        self.inserted.append(
            {
                "ref": ref,
                "instrument": instrument,
                "direction": direction,
                "offset": offset,
                "price": price,
                "volume": volume,
            }
        )
        return ref

    def order_action(self, order_ref, instrument, order_sys_id=""):
        self.actions.append(order_ref)


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
        "price_limit": {"enabled": True, "detect_factor": 0.95, "rates": {}},
        "liquidity": {"enabled": True, "max_volume_share": 0.05},
    }


def _tick(price=3000.0, volume=100000) -> Tick:
    return Tick(
        instrument="m2701", last_price=price, open_price=price,
        highest_price=price, lowest_price=price, volume=volume,
    )


def _setup():
    contracts = {
        "M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2),
        "RB0": ContractSpec("RB0", 10, 1.0, 0.07, 3.1),
    }
    ledger = VirtualLedger(100000, contracts, {"m2701": "M0", "rb2701": "RB0"})
    ledger.begin_day()
    guard = LiveRiskGuard(_risk_cfg(), contracts, ledger, prev_close={})
    book = OrderBook()
    session = FakeSession()
    ex = Executor(
        session, book, ledger, guard,
        {"execute": {"quote_timeout_seconds": 5, "chase_max": 3}},
    )
    ex.ticks["M0"] = _tick()
    ex.ticks["RB0"] = _tick()
    return session, book, ledger, guard, ex


def _target(symbol, direction, lots):
    return TargetPosition(
        symbol=symbol, signal=direction, direction=direction, lots=lots, ref_price=3000.0
    )


def test_execute_target_open():
    session, book, ledger, _, ex = _setup()
    notes = ex.execute_target({"M0": _target("M0", 1, 1)}, date="2026-10-13")
    assert len(session.inserted) == 1
    o = session.inserted[0]
    assert o["offset"] == "0" and o["direction"] == "0" and o["volume"] == 1  # 开多=买
    assert any(n.kind == "open" for n in notes)
    assert book.get(o["ref"]) is not None


def test_execute_target_close_split_shfe():
    session, book, ledger, _, ex = _setup()
    ledger.apply_fill(
        instrument="rb2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=1, trading_day="2026-10-10",
    )
    ledger.apply_fill(
        instrument="rb2701", direction="LONG", offset="OPEN", price=3010.0,
        volume=1, trading_day="2026-10-13",
    )
    ex.execute_target({"RB0": _target("RB0", 0, 0)}, date="2026-10-13")
    # 上期所：先平昨（'4'）再平今（'3'），均为卖出（'1'）
    assert len(session.inserted) == 2
    assert session.inserted[0]["offset"] == "4" and session.inserted[0]["direction"] == "1"
    assert session.inserted[1]["offset"] == "3" and session.inserted[1]["direction"] == "1"


def test_execute_target_flip_close_then_open():
    session, _, ledger, _, ex = _setup()
    ledger.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=1, trading_day="2026-10-10",
    )
    ex.execute_target({"M0": _target("M0", -1, 1)}, date="2026-10-13")
    # 翻空：先平多 1 手（卖平），后开空 1 手（卖开）
    assert len(session.inserted) == 2
    assert session.inserted[0]["offset"] == "1" and session.inserted[0]["direction"] == "1"
    assert session.inserted[1]["offset"] == "0" and session.inserted[1]["direction"] == "1"


def test_fill_accounting_and_dedup():
    session, book, ledger, _, ex = _setup()
    ex.execute_target({"M0": _target("M0", 1, 1)}, date="2026-10-13")
    ref = session.inserted[0]["ref"]

    ev = TradeRtn(
        order_ref=ref, instrument="m2701", direction="0", offset="0",
        price=3000.0, volume=1, trade_id="T1", trading_day="2026-10-13",
    )
    ex.on_event(ev)
    assert ledger.net_size("M0") == 1
    assert book.get(ref).filled == 1 and book.get(ref).status == "filled"

    eq_after_first = ledger.equity
    ex.on_event(ev)  # 重复回报
    assert ledger.net_size("M0") == 1  # 不重复加仓
    assert ledger.equity == eq_after_first  # 不重复扣手续费


def test_stop_flow_single_dispatch_and_release():
    session, book, ledger, guard, ex = _setup()
    ledger.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=1, trading_day="2026-10-13",
    )
    guard.manager.stops.register_entry("M0", 1, 3000.0, 10.0, ledger.day_idx - 1)

    notes = ex.on_tick("M0", _tick(price=2970.0), date="2026-10-13")
    assert any(n.kind == "stop" for n in notes)
    assert "M0" in ex.pending_stop
    assert len(session.inserted) == 1 and session.inserted[0]["offset"] == "1"
    ref = session.inserted[0]["ref"]
    assert ref in ex.stop_orders

    # 在途时再次触发 → 不重复发单
    ex.on_tick("M0", _tick(price=2960.0), date="2026-10-13")
    assert len(session.inserted) == 1

    # 止损单被拒 → 释放（允许下个 tick 重试）
    ex.on_event(
        OrderRejected(order_ref=ref, instrument="m2701", error_id=31, error_msg="拒绝", ts=1.0)
    )
    assert "M0" not in ex.pending_stop
    ex.on_tick("M0", _tick(price=2950.0), date="2026-10-13")
    assert len(session.inserted) == 2


def test_chase_cancel_and_requote():
    session, book, ledger, _, ex = _setup()
    ex.execute_target({"M0": _target("M0", 1, 1)}, date="2026-10-13")
    ref = session.inserted[0]["ref"]
    # 柜台接受（挂单中）
    ex.on_event(OrderRtn(order_ref=ref, status="3", order_sys_id="S1", ts=time.time()))

    # 超时 → 撤单请求
    future = time.time() + 10
    notes = ex.process_chase(now=future, date="2026-10-13")
    assert session.actions == [ref]
    assert any(n.kind == "chase" for n in notes)
    assert ref in ex.chase_pending

    # 撤单回报 → 以追价重挂（新单 chase_count=1）
    ex.on_event(OrderRtn(order_ref=ref, status="5", ts=time.time()))
    assert len(session.inserted) == 2
    new_order = book.get(session.inserted[1]["ref"])
    assert new_order.chase_count == 1


def test_chase_abandon_when_exhausted():
    session, book, ledger, _, ex = _setup()
    order = LiveOrder(
        order_ref="9", symbol="M0", instrument="m2701",
        direction="LONG", offset="OPEN", price=3000.0, volume=1, chase_count=3,
    )
    book.submit(order, now=0.0)
    book.apply_order_rtn(OrderRtn(order_ref="9", status="3", ts=0.0))

    notes = ex.process_chase(now=999.0, date="2026-10-13")
    assert session.actions == ["9"]  # 撤单放弃
    assert any(n.kind == "abandon" for n in notes)
