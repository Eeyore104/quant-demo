"""v2.1 轮 2（可靠性）：止损追单防重、报单无回执告警。

来自 2026-10-22/23 真实运行的两个暴露：
- 10-23 10:33 止损平仓撤单追价后 `pending_stop` 被清空 → 同批多个止损单同时在场，
  柜台连环返回「平仓量超过持仓量」（错误 30）；
- 10-23 09:00 首单报出后无回执也无成交 → `stale_orders` 只覆盖 accepted/partial，
  整笔静默丢失，无任何告警。
"""

import time

from src.engine.contracts import ContractSpec
from src.live.adapter import Executor
from src.live.ctp_api import Tick
from src.live.ledger import VirtualLedger
from src.live.orders import ST_CANCELED, ST_PENDING, LiveOrder, OrderBook
from src.live.risk_guard import LiveRiskGuard


class FakeSession:
    """假会话：记录报单 / 撤单请求。"""

    def __init__(self):
        self.inserted: list[dict] = []
        self.actions: list[str] = []
        self._seq = 0

    def order_insert(self, instrument, direction, offset, price, volume):
        self._seq += 1
        ref = str(self._seq)
        self.inserted.append({"ref": ref, "instrument": instrument, "volume": volume})
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


def _executor():
    contracts = {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}
    ledger = VirtualLedger(100000.0, contracts, {"m2701": "M0"})
    ledger.begin_day()
    guard = LiveRiskGuard(_risk_cfg(), contracts, ledger)
    session = FakeSession()
    ex = Executor(session, OrderBook(), ledger, guard, {"execute": {"chase_max": 3}})
    ex.ticks["M0"] = Tick(
        instrument="m2701",
        last_price=3000.0,
        open_price=3000.0,
        highest_price=3000.0,
        lowest_price=3000.0,
        volume=100000,
    )
    return ex, session


def test_chase_keeps_pending_stop():
    ex, session = _executor()
    order = LiveOrder(
        order_ref="old",
        symbol="M0",
        instrument="m2701",
        direction="LONG",
        offset="CLOSE",
        price=3000.0,
        volume=5,
        status=ST_CANCELED,
        filled=0,
    )
    ex.book.orders["old"] = order
    ex.stop_orders.add("old")
    ex.pending_stop.add("M0")
    ex.chase_pending.add("old")

    notes = ex._after_terminal(order, 0.0)

    assert any(n.kind == "chase" for n in notes)
    assert len(session.inserted) == 1
    new_ref = session.inserted[0]["ref"]
    assert new_ref in ex.stop_orders  # 新单仍是止损单
    assert "M0" in ex.pending_stop  # 追单在途 → 继续防重（旧代码此处被清空）


def test_chase_failure_does_not_raise_or_leave_stale_flags():
    ex, _ = _executor()
    ex.ticks.pop("M0")  # 无行情快照 → 报价抛错
    order = LiveOrder(
        order_ref="old",
        symbol="M0",
        instrument="m2701",
        direction="LONG",
        offset="CLOSE",
        price=3000.0,
        volume=5,
        status=ST_CANCELED,
        filled=0,
    )
    ex.book.orders["old"] = order
    ex.stop_orders.add("old")
    ex.pending_stop.add("M0")
    ex.chase_pending.add("old")

    notes = ex._after_terminal(order, 0.0)  # 不应抛异常（旧代码在 try 外报价）

    assert any(n.kind == "error" for n in notes)
    assert "M0" not in ex.pending_stop  # 未重挂 → 释放防重，下个 tick 重试
    assert "old" not in ex.stop_orders


def test_no_ack_reported_once_after_timeout():
    ex, _ = _executor()
    order = LiveOrder(
        order_ref="1",
        symbol="M0",
        instrument="m2701",
        direction="SHORT",
        offset="OPEN",
        price=5381.0,
        volume=1,
        status=ST_PENDING,
        created_ts=time.time() - 30,
    )
    ex.book.orders["1"] = order

    first = ex._check_unacked()
    second = ex._check_unacked()

    assert [n.kind for n in first] == ["no_ack"]
    assert second == []  # 每单只报一次
    assert "无回执" in first[0].detail


def test_no_ack_waits_for_timeout():
    ex, _ = _executor()
    order = LiveOrder(
        order_ref="1",
        symbol="M0",
        instrument="m2701",
        direction="SHORT",
        offset="OPEN",
        price=5381.0,
        volume=1,
        status=ST_PENDING,
        created_ts=time.time(),
    )
    ex.book.orders["1"] = order

    assert ex._check_unacked() == []


def test_quote_price_uses_order_side_not_position_side():
    """R10：平多=卖（挂低）、平空=买（挂高）；开多=买、开空=卖。"""
    ex, _ = _executor()
    last, tick = 3000.0, 1.0

    assert ex._quote_price("M0", "LONG", 0, offset="OPEN") == last + tick
    assert ex._quote_price("M0", "LONG", 0, offset="CLOSE") == last - tick
    assert ex._quote_price("M0", "SHORT", 0, offset="OPEN") == last - tick
    assert ex._quote_price("M0", "SHORT", 0, offset="CLOSE") == last + tick
    # 追单继续沿同一方向加价
    assert ex._quote_price("M0", "LONG", 2, offset="CLOSE") == last - 3 * tick


def test_stop_reject_backoff_after_three_rejects():
    """R12：止损连续被拒 3 次 → 退避 5 分钟，避免每秒重报刷屏。"""
    ex, session = _executor()
    ex.ledger.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN",
        price=3000.0, volume=1, trading_day="2026-10-29",
    )
    # 直接构造 3 次拒绝
    for _ in range(3):
        ex.stop_orders.add("x")
        order = LiveOrder(
            order_ref="x", symbol="M0", instrument="m2701", direction="LONG",
            offset="CLOSE", price=3000.0, volume=1, status=ST_PENDING,
        )
        ex.book.orders["x"] = order
        from src.live.ctp_api import OrderRejected

        ex.on_event(OrderRejected(instrument="m2701", order_ref="x",
                                  error_id=30, error_msg="平仓量超过持仓量"))
        ex.stop_orders.discard("x")

    assert "M0" in ex.reject_backoff
    notes = ex.execute_stop("M0", type("T", (), {"kind": "stop_fixed", "level": 2960.0})(),
                            ex.ticks["M0"])
    assert notes and notes[0].kind == "skip"
