"""本地订单簿与订单状态机（v2.0 · docs/16 T2.0-3a）。

职责（纯逻辑，不依赖 openctp-ctp / 网络，可单测）：
- ``LiveOrder``：本地订单（业务语义字段 + CTP 回报字段）
- ``OrderBook``：提交订单、消费回报事件更新状态、超时候选查询

状态流转：
    pending（已报出，待柜台回报）→ accepted（挂单中）/ rejected（被拒）
    accepted → partial（部分成交）→ filled（全部成交）
    accepted / partial → canceled（撤单成功）

口径约定：
- 成交量 ``filled`` **仅由成交回报（TradeRtn）累加**（按 trade_id 去重）；
  OrderRtn 的 ``VolumeTraded`` 只用于状态判定，不参与计数（避免双计）；
- CTP 的「未知」（'a'）状态帧（报单初始帧）不改变本地状态，等后续帧；
- 时间注入：``submit`` / ``stale_orders`` 接受 ``now`` 参数，便于测试控制。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .ctp_api import (
    ORDER_STATUS_ALL_TRADED,
    ORDER_STATUS_CANCELED,
    ORDER_STATUS_NO_TRADE_NOT_QUEUEING,
    ORDER_STATUS_NO_TRADE_QUEUEING,
    ORDER_STATUS_PART_TRADED,
    ORDER_STATUS_PART_TRADED_NOT_QUEUEING,
)

# 本地状态
ST_PENDING = "pending"
ST_ACCEPTED = "accepted"
ST_PARTIAL = "partial"
ST_FILLED = "filled"
ST_CANCELED = "canceled"
ST_REJECTED = "rejected"

ACTIVE_STATES = (ST_PENDING, ST_ACCEPTED, ST_PARTIAL)

STATUS_TEXT = {
    ST_PENDING: "待回报",
    ST_ACCEPTED: "挂单中",
    ST_PARTIAL: "部分成交",
    ST_FILLED: "全部成交",
    ST_CANCELED: "已撤单",
    ST_REJECTED: "被拒绝",
}


@dataclass
class LiveOrder:
    """一笔本地订单（业务语义 + CTP 回报字段）。"""

    order_ref: str
    symbol: str  # 品种代码（如 M0）
    instrument: str  # 真实合约（如 m2701）
    direction: str  # LONG / SHORT
    offset: str  # OPEN / CLOSE / CLOSE_TODAY / CLOSE_YESTERDAY
    price: float
    volume: int
    status: str = ST_PENDING
    filled: int = 0
    order_sys_id: str = ""
    status_msg: str = ""
    chase_count: int = 0  # 已追单次数（adapter 维护）
    created_ts: float = 0.0
    updated_ts: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATES

    @property
    def remaining(self) -> int:
        return max(0, self.volume - self.filled)

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)

    def describe(self) -> str:
        side = "买" if self.direction == "LONG" else "卖"
        return (
            f"{self.instrument} {side}{self.offset} {self.volume}手@{self.price}"
            f"（{self.status_text} {self.filled}/{self.volume}）"
        )


class OrderBook:
    """本地订单簿（按 order_ref 索引）。"""

    def __init__(self) -> None:
        self.orders: dict[str, LiveOrder] = {}
        self.seen_trades: set[str] = set()  # 已入账的成交号（去重）

    # -------------------------------------------------- 提交

    def submit(self, order: LiveOrder, now: float | None = None) -> LiveOrder:
        now = time.time() if now is None else now
        order.status = ST_PENDING
        order.filled = 0
        order.created_ts = now
        order.updated_ts = now
        self.orders[order.order_ref] = order
        return order

    # -------------------------------------------------- 回报消费

    def apply_order_rtn(self, ev) -> LiveOrder | None:
        """消费订单状态回报（ctp_api.OrderRtn）。"""
        order = self.orders.get(ev.order_ref)
        if order is None:
            return None
        if ev.order_sys_id:
            order.order_sys_id = ev.order_sys_id
        status = str(ev.status)
        if status == ORDER_STATUS_ALL_TRADED:
            order.status = ST_FILLED
        elif status in (
            ORDER_STATUS_PART_TRADED,
            ORDER_STATUS_PART_TRADED_NOT_QUEUEING,
        ):
            order.status = ST_PARTIAL
        elif status in (
            ORDER_STATUS_NO_TRADE_QUEUEING,
            ORDER_STATUS_NO_TRADE_NOT_QUEUEING,
        ):
            order.status = ST_ACCEPTED
        elif status == ORDER_STATUS_CANCELED:
            order.status = ST_CANCELED
        # 'a'（未知）等其余状态帧：不改变本地状态，等后续帧
        if ev.status_msg:
            order.status_msg = str(ev.status_msg)
        order.updated_ts = float(ev.ts)
        return order

    def apply_trade(self, ev) -> LiveOrder | None:
        """消费成交回报（ctp_api.TradeRtn）：按 trade_id 去重后累加成交量。"""
        order = self.orders.get(ev.order_ref)
        if order is None:
            return None
        if ev.trade_id:
            if ev.trade_id in self.seen_trades:
                return order  # 重复回报，忽略
            self.seen_trades.add(ev.trade_id)
        order.filled = min(order.volume, order.filled + int(ev.volume))
        if order.filled >= order.volume:
            order.status = ST_FILLED
        elif order.filled > 0 and order.is_active:
            order.status = ST_PARTIAL
        order.updated_ts = float(ev.ts)
        return order

    def apply_rejected(self, ev) -> LiveOrder | None:
        """消费报单被拒（ctp_api.OrderRejected）。"""
        order = self.orders.get(ev.order_ref)
        if order is None:
            return None
        order.status = ST_REJECTED
        order.status_msg = f"{ev.error_id}: {ev.error_msg}" if ev.error_msg else str(ev.error_id)
        order.updated_ts = float(ev.ts)
        return order

    def apply_action_rsp(
        self, ok: bool, order_ref: str, message: str = "", ts=None
    ) -> LiveOrder | None:
        """撤单响应：ok 时订单继续等待 '5'（已撤）状态帧；失败时记录原因。"""
        order = self.orders.get(order_ref)
        if order is None:
            return None
        if not ok and message:
            order.status_msg = f"撤单被拒：{message}"
        if ts is not None:
            order.updated_ts = float(ts)
        return order

    # -------------------------------------------------- 查询

    def get(self, order_ref: str) -> LiveOrder | None:
        return self.orders.get(order_ref)

    def active_orders(self, symbol: str | None = None) -> list[LiveOrder]:
        return [
            o
            for o in self.orders.values()
            if o.is_active and (symbol is None or o.symbol == symbol)
        ]

    def stale_orders(self, timeout_seconds: float, now: float | None = None) -> list[LiveOrder]:
        """挂单超时候选（已挂出且超过 ``timeout_seconds`` 未全成）。

        仅包含 accepted / partial（已有柜台受理、可撤单）；pending 不在此列
        （回报尚未到达，撤单需等待 sys_info，交由回报超时告警处理）。
        """
        now = time.time() if now is None else now
        return [
            o
            for o in self.orders.values()
            if o.status in (ST_ACCEPTED, ST_PARTIAL)
            and (now - o.created_ts) >= timeout_seconds
        ]

    def has_active(self, symbol: str | None = None) -> bool:
        return any(o.is_active for o in self.active_orders(symbol))
