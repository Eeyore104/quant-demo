"""订单状态机测试（v2.0 轮 2）：状态流转 / 成交去重 / 超时候选。"""

from src.live.ctp_api import OrderRejected, OrderRtn, TradeRtn
from src.live.orders import (
    ST_ACCEPTED,
    ST_CANCELED,
    ST_FILLED,
    ST_PARTIAL,
    ST_PENDING,
    ST_REJECTED,
    LiveOrder,
    OrderBook,
)


def _mk_order(**kw) -> LiveOrder:
    base = dict(
        order_ref="1",
        symbol="M0",
        instrument="m2701",
        direction="LONG",
        offset="OPEN",
        price=3000.0,
        volume=2,
    )
    base.update(kw)
    return LiveOrder(**base)


def test_submit_initial_state():
    book = OrderBook()
    o = book.submit(_mk_order(), now=100.0)
    assert o.status == ST_PENDING
    assert o.is_active and o.remaining == 2
    assert o.created_ts == 100.0 and o.updated_ts == 100.0
    assert book.get("1") is o


def test_order_rtn_progression_to_filled():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    book.apply_order_rtn(OrderRtn(order_ref="1", status="3", order_sys_id="S1", ts=1.0))
    o = book.get("1")
    assert o.status == ST_ACCEPTED and o.order_sys_id == "S1"

    book.apply_order_rtn(OrderRtn(order_ref="1", status="1", ts=2.0))
    assert book.get("1").status == ST_PARTIAL

    book.apply_order_rtn(OrderRtn(order_ref="1", status="0", ts=3.0))
    assert book.get("1").status == ST_FILLED


def test_unknown_status_frame_keeps_state():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    # 'a'（未知）是报单初始帧：不改变本地状态
    book.apply_order_rtn(OrderRtn(order_ref="1", status="a", ts=1.0))
    assert book.get("1").status == ST_PENDING


def test_canceled_status():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    book.apply_order_rtn(OrderRtn(order_ref="1", status="3", ts=1.0))
    book.apply_order_rtn(OrderRtn(order_ref="1", status="5", ts=2.0))
    assert book.get("1").status == ST_CANCELED


def test_trade_accumulation_and_dedup():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    book.apply_trade(TradeRtn(order_ref="1", volume=1, trade_id="T1", ts=1.0))
    assert book.get("1").filled == 1 and book.get("1").status == ST_PARTIAL
    # 重复 trade_id → 忽略
    book.apply_trade(TradeRtn(order_ref="1", volume=1, trade_id="T1", ts=1.1))
    assert book.get("1").filled == 1
    # 第二笔真实成交 → 全成
    book.apply_trade(TradeRtn(order_ref="1", volume=1, trade_id="T2", ts=2.0))
    assert book.get("1").filled == 2 and book.get("1").status == ST_FILLED
    # 超量保护（不会超过委托量）
    book.apply_trade(TradeRtn(order_ref="1", volume=5, trade_id="T3", ts=3.0))
    assert book.get("1").filled == 2


def test_rejected_records_message():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    book.apply_rejected(OrderRejected(order_ref="1", error_id=31, error_msg="价格非法", ts=1.0))
    o = book.get("1")
    assert o.status == ST_REJECTED and "31" in o.status_msg and "价格非法" in o.status_msg


def test_action_rsp_failure_records_msg():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    book.apply_action_rsp(ok=False, order_ref="1", message="无此报单", ts=1.0)
    assert "撤单被拒" in book.get("1").status_msg


def test_stale_orders_time_gate():
    book = OrderBook()
    book.submit(_mk_order(), now=0.0)
    # 未收到柜台回报（pending）→ 不在超时候选
    assert book.stale_orders(timeout_seconds=5, now=100.0) == []
    book.apply_order_rtn(OrderRtn(order_ref="1", status="3", ts=0.5))
    assert book.stale_orders(timeout_seconds=5, now=4.9) == []
    stale = book.stale_orders(timeout_seconds=5, now=5.1)
    assert len(stale) == 1 and stale[0].order_ref == "1"


def test_active_orders_filter_by_symbol():
    book = OrderBook()
    book.submit(_mk_order(order_ref="1", symbol="M0"), now=0.0)
    book.submit(_mk_order(order_ref="2", symbol="RB0"), now=0.0)
    book.apply_order_rtn(OrderRtn(order_ref="2", status="5", ts=1.0))  # 撤掉 RB0
    assert len(book.active_orders()) == 1
    assert book.active_orders("M0")[0].order_ref == "1"
    assert book.active_orders("RB0") == []
    assert book.has_active() is True
