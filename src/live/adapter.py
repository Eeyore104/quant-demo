"""SimNow 执行适配器（v2.0 · docs/16 T2.0-3d）：目标仓位 → 差额订单 → 逐腿执行。

编排链路（时序与回测开仓腿对齐）：
    目标仓位 → 与账本净持仓比对 → 差额订单（**先平后开**拆分）
    → 平仓腿（涨跌停方向检查）→ 开仓腿（风控闸门）→ CTP 限价报单
    → 回报分发（订单簿 / 账本 / 风控）

要点：
- **限价追单**：以最新价 ± (1+追单次数) 跳挂限价单；超时未全成 → 撤单重挂
  （最多 ``chase_max`` 次），仍不成交 → 撤单放弃 + 记录事件（不自动转市价）；
- **交易所差异**：上期所品种平仓拆「平昨 / 平今」；其余交易所直接平仓；
- **止损单防重**：同品种止损单在途（``pending_stop``）时不再重复触发；
  止损平仓单完成（全成 / 撤单 / 拒单）后释放；被拒则恢复监控（下个 tick 重试）；
- **账本以回报为准**：成交回报（实际价格 / 数量 / 今昨标志）入账，不用订单意图。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.risk import StopTrigger
from ..utils.logger import get_logger
from .ctp_api import (
    DIR_BUY,
    DIR_SELL,
    OFFSET_CLOSE,
    OFFSET_CLOSE_TODAY,
    OFFSET_CLOSE_YESTERDAY,
    OFFSET_OPEN,
    ActionRejected,
    ActionRsp,
    OrderRejected,
    OrderRtn,
    TradeRtn,
)
from .ledger import VirtualLedger
from .orders import ST_CANCELED, ST_FILLED, ST_REJECTED, LiveOrder, OrderBook
from .risk_guard import LiveRiskGuard

log = get_logger("live.adapter")

# 业务语义 ↔ CTP 值
DIR_TO_CTP = {"LONG": DIR_BUY, "SHORT": DIR_SELL}
OFFSET_TO_CTP = {
    "OPEN": OFFSET_OPEN,
    "CLOSE": OFFSET_CLOSE,
    "CLOSE_TODAY": OFFSET_CLOSE_TODAY,
    "CLOSE_YESTERDAY": OFFSET_CLOSE_YESTERDAY,
}
CTP_TO_OFFSET = {
    OFFSET_OPEN: "OPEN",
    OFFSET_CLOSE: "CLOSE",
    OFFSET_CLOSE_TODAY: "CLOSE_TODAY",
    OFFSET_CLOSE_YESTERDAY: "CLOSE_YESTERDAY",
}


def order_ctp_direction(direction: str, offset: str) -> str:
    """业务方向（持仓方向）→ 报单买卖方向：开多=买、开空=卖；平多=卖、平空=买。"""
    if offset == "OPEN":
        return DIR_BUY if direction == "LONG" else DIR_SELL
    return DIR_SELL if direction == "LONG" else DIR_BUY


def fill_biz_direction(ctp_direction: str, offset: str) -> str:
    """成交回报买卖方向 → 业务方向（持仓方向；与引擎 Trade 口径一致）。"""
    is_buy = str(ctp_direction) == DIR_BUY
    if offset == "OPEN":
        return "LONG" if is_buy else "SHORT"
    return "LONG" if not is_buy else "SHORT"

# 上期所品种（平仓需区分平今 / 平昨）；组合池内：RB / AL
SHFE_PREFIXES = (
    "RB", "AL", "CU", "ZN", "PB", "NI", "SN", "AU", "AG", "HC", "SS", "FU", "BU", "RU", "SP",
)


def needs_close_split(symbol: str) -> bool:
    """是否上期所品种（需要平今 / 平昨拆分）。"""
    base = symbol.rstrip("0123456789")  # RB0 → RB
    return base.upper() in SHFE_PREFIXES


@dataclass
class ExecNote:
    """一条执行事件（供 journal / 日报）。"""

    kind: str  # open / close / stop / chase / abandon / reject ...
    symbol: str
    detail: str
    order_ref: str = ""


class Executor:
    """执行编排：目标执行 / 止损执行 / 追单 / 回报分发。"""

    def __init__(
        self,
        session,
        book: OrderBook,
        ledger: VirtualLedger,
        guard: LiveRiskGuard,
        live_cfg: dict,
    ):
        self.session = session
        self.book = book
        self.ledger = ledger
        self.guard = guard
        self.execute_cfg = dict(live_cfg.get("execute") or {})
        self.ticks: dict[str, object] = {}  # 最新 tick（symbol → Tick）
        self.stop_orders: set[str] = set()  # 止损单标记
        self.pending_stop: set[str] = set()  # 止损在途的品种
        self.chase_pending: set[str] = set()  # 已请求撤单、待重挂的订单
        self.notes: list[ExecNote] = []

    # -------------------------------------------------- 目标执行

    def execute_target(self, targets: dict, date: str = "") -> list[ExecNote]:
        """执行目标仓位表（先平后开；逐腿过风控闸门后报单）。"""
        out: list[ExecNote] = []
        for symbol, tp in targets.items():
            tick = self.ticks.get(symbol)
            if tick is None:
                out.append(ExecNote("skip", symbol, "无行情快照，跳过"))
                continue
            if getattr(tick, "last_price", 0) <= 0:
                out.append(ExecNote("skip", symbol, "行情无效，跳过"))
                continue
            instrument = self.ledger.symbol_to_instrument.get(symbol, "")
            if not instrument:
                out.append(ExecNote("skip", symbol, "无合约映射，跳过"))
                continue

            cur = self.ledger.net_size(symbol)
            target = tp.direction * tp.lots
            if target == cur:
                continue
            legs = self._plan_legs(symbol, cur, target, date)
            for offset, direction, lots in legs:
                price = self._quote_price(symbol, direction, chase=0)
                if offset == "OPEN":
                    lots2, _notes, events = self.guard.gate_open(
                        symbol, direction, lots, price, tick, sig=tp.direction, date=date
                    )
                else:
                    lots2, _notes, events = self.guard.gate_close(
                        symbol, direction, lots, tick, date=date
                    )
                for ev in events:
                    out.append(ExecNote("risk", symbol, ev.describe()))
                if lots2 <= 0:
                    out.append(ExecNote("refuse", symbol, f"{offset} 被风控拦截（{lots} 手）"))
                    continue
                order_ref = self._submit(symbol, instrument, direction, offset, price, lots2)
                out.append(
                    ExecNote(
                        "open" if offset == "OPEN" else "close",
                        symbol,
                        f"{offset} {direction} {lots2} 手 @{price:.4f}（目标 {target}／现 {cur}）",
                        order_ref,
                    )
                )
        self.notes.extend(out)
        return out

    # -------------------------------------------------- 止损执行

    def on_tick(self, symbol: str, tick, date: str = "") -> list[ExecNote]:
        """行情驱动：更新快照 + 检查止损触发（在途止损单时跳过）。"""
        self.ticks[symbol] = tick
        out: list[ExecNote] = []
        if symbol in self.pending_stop:
            return out
        trig = self.guard.on_tick(symbol, tick)
        if trig is not None:
            out = self.execute_stop(symbol, trig, tick, date=date)
            self.notes.extend(out)
        return out

    def execute_stop(
        self, symbol: str, trig: StopTrigger, tick, date: str = ""
    ) -> list[ExecNote]:
        """止损 / 止盈触发 → 平仓腿（上期所拆分）→ 报单 + 重入标记。"""
        out: list[ExecNote] = []
        cur = self.ledger.net_size(symbol)
        if cur == 0:
            return out
        instrument = self.ledger.symbol_to_instrument.get(symbol, "")
        direction = "LONG" if cur > 0 else "SHORT"
        for offset, lots in self._close_legs(symbol, direction, abs(cur), date, split=True):
            price = self._quote_price(symbol, direction, chase=0)
            lots2, _notes, events = self.guard.gate_close(symbol, direction, lots, tick, date=date)
            for ev in events:
                out.append(ExecNote("risk", symbol, ev.describe()))
            if lots2 <= 0:
                out.append(ExecNote("refuse", symbol, f"止损平仓被拦（{offset} {lots} 手）"))
                continue
            order_ref = self._submit(symbol, instrument, direction, offset, price, lots2)
            self.stop_orders.add(order_ref)
            self.pending_stop.add(symbol)
            out.append(
                ExecNote(
                    "stop",
                    symbol,
                    f"{trig.kind} 触发（价位 {trig.level:.4f}）→ {offset} {lots2} 手 @{price:.4f}",
                    order_ref,
                )
            )
        # 注：止损监控的清除与重入锁定在「平仓单全成」后执行（_after_terminal）；
        # 若平仓单被拒 / 放弃，监控保留，下个 tick 自动重试（防重由 pending_stop 保证）。
        return out

    # -------------------------------------------------- 追单

    def process_chase(self, now: float | None = None, date: str = "") -> list[ExecNote]:
        """超时未全成订单：撤单重挂（最多 chase_max 次）；超次撤单放弃。"""
        out: list[ExecNote] = []
        timeout = float(self.execute_cfg.get("quote_timeout_seconds", 5))
        chase_max = int(self.execute_cfg.get("chase_max", 3))
        for order in self.book.stale_orders(timeout, now):
            if order.order_ref in self.chase_pending:
                continue  # 撤单在途
            if order.chase_count >= chase_max:
                self.session.order_action(order.order_ref, order.instrument, order.order_sys_id)
                self.chase_pending.add(order.order_ref)
                out.append(
                    ExecNote(
                        "abandon",
                        order.symbol,
                        f"追单 {order.chase_count} 次仍未全成"
                        f"（{order.filled}/{order.volume}）→ 撤单放弃",
                        order.order_ref,
                    )
                )
                continue
            self.session.order_action(order.order_ref, order.instrument, order.order_sys_id)
            self.chase_pending.add(order.order_ref)
            out.append(
                ExecNote(
                    "chase",
                    order.symbol,
                    f"超时未全成 → 撤单追价（第 {order.chase_count + 1} 次）",
                    order.order_ref,
                )
            )
        self.notes.extend(out)
        return out

    # -------------------------------------------------- 回报分发

    def on_event(self, ev) -> list[ExecNote]:
        """消费行情 / 回报事件（成交入账、订单簿更新、追单重挂、止损释放）。"""
        out: list[ExecNote] = []
        if isinstance(ev, OrderRtn):
            order = self.book.apply_order_rtn(ev)
            if order is not None:
                out.extend(self._after_terminal(order, ev.ts))
        elif isinstance(ev, TradeRtn):
            # 防重：trade_id 已入账 → 只更新订单簿，不重复入账
            fresh = (not ev.trade_id) or (ev.trade_id not in self.book.seen_trades)
            order = self.book.apply_trade(ev)
            offset = CTP_TO_OFFSET.get(str(ev.offset), "CLOSE")
            if fresh:
                try:
                    self.ledger.apply_fill(
                        instrument=ev.instrument,
                        direction=fill_biz_direction(str(ev.direction), offset),
                        offset=offset,
                        price=float(ev.price),
                        volume=int(ev.volume),
                        trading_day=str(ev.trading_day or ""),
                    )
                except ValueError as e:
                    out.append(
                        ExecNote("error", ev.instrument, f"成交入账失败：{e}", ev.order_ref)
                    )
                else:
                    # v2.1：逐笔成交后同步止损状态（对齐回测 register_entry_stops 三分支）
                    symbol = self.ledger.instrument_map.get(ev.instrument)
                    if symbol:
                        for risk_ev in self.guard.sync_stops(
                            symbol, date=str(ev.trading_day or "")
                        ):
                            out.append(ExecNote("risk", symbol, risk_ev.detail))
            if order is not None:
                out.extend(self._after_terminal(order, ev.ts))
        elif isinstance(ev, OrderRejected):
            order = self.book.apply_rejected(ev)
            if order is not None:
                out.append(
                    ExecNote(
                        "reject",
                        order.symbol,
                        f"报单被拒：{ev.error_id} {ev.error_msg}",
                        ev.order_ref,
                    )
                )
                out.extend(self._after_terminal(order, ev.ts))
        elif isinstance(ev, ActionRsp):
            self.book.apply_action_rsp(ev.ok, ev.order_ref, ev.error_msg, ev.ts)
            if not ev.ok:
                self.chase_pending.discard(ev.order_ref)  # 撤单失败：允许下次再尝试
                out.append(
                    ExecNote(
                        "reject",
                        ev.instrument,
                        f"撤单被拒：{ev.error_id} {ev.error_msg}",
                        ev.order_ref,
                    )
                )
        elif isinstance(ev, ActionRejected):
            self.chase_pending.discard(ev.order_ref)
            out.append(
                ExecNote(
                    "reject",
                    ev.instrument,
                    f"撤单被拒：{ev.error_id} {ev.error_msg}",
                    ev.order_ref,
                )
            )
        self.notes.extend(out)
        return out

    def _after_terminal(self, order: LiveOrder, ts) -> list[ExecNote]:
        """订单到达终态后的收尾：追单重挂 / 止损释放。"""
        out: list[ExecNote] = []
        terminal = order.status in (ST_FILLED, ST_CANCELED, ST_REJECTED)
        if not terminal:
            return out
        # 追单重挂（撤单成功 → 以新价重挂剩余量）
        if order.order_ref in self.chase_pending:
            self.chase_pending.discard(order.order_ref)
            if order.status == ST_CANCELED and order.chase_count < int(
                self.execute_cfg.get("chase_max", 3)
            ) and order.remaining > 0:
                pending_stop = order.order_ref in self.stop_orders
                direction = order.direction
                price = self._quote_price(order.symbol, direction, chase=order.chase_count + 1)
                try:
                    new_ref = self._submit(
                        order.symbol, order.instrument, direction, order.offset,
                        price, order.remaining, chase_count=order.chase_count + 1,
                    )
                except Exception as e:  # 极端情形：会话不可用
                    out.append(
                        ExecNote("error", order.symbol, f"追单重挂失败：{e}", order.order_ref)
                    )
                    return out
                if pending_stop:
                    self.stop_orders.add(new_ref)
                out.append(
                    ExecNote(
                        "chase",
                        order.symbol,
                        f"追价重挂 {order.remaining} 手 @{price:.4f}"
                        f"（第 {order.chase_count + 1} 次）",
                        new_ref,
                    )
                )
        # 止损单收尾：全成 → 清止损监控 + 登记重入锁定（对齐回测 execute_stop）；
        # 未成（拒单 / 放弃撤单）→ 仅释放防重标记，监控保留（下个 tick 重试）
        if order.order_ref in self.stop_orders:
            self.stop_orders.discard(order.order_ref)
            self.pending_stop.discard(order.symbol)
            if order.status == ST_FILLED:
                direction = 1 if order.direction == "LONG" else -1
                self.guard.notify_stop(order.symbol, direction)
        return out

    # -------------------------------------------------- 拆分与报价

    def _plan_legs(
        self, symbol: str, cur: int, target: int, date: str
    ) -> list[tuple[str, str, int]]:
        """差额 → 腿列表（先平后开；口径与回测 broker.fill 一致）。"""
        legs: list[tuple[str, str, int]] = []
        size = cur
        # ① 平仓腿
        if size != 0 and abs(target) < abs(size) and target * size >= 0:
            close_vol = abs(size) - abs(target)
        elif size != 0 and (target == 0 or target * size < 0):
            close_vol = abs(size)
        else:
            close_vol = 0
        if close_vol > 0:
            direction = "LONG" if size > 0 else "SHORT"
            for offset, n in self._close_legs(symbol, direction, close_vol, date, split=True):
                legs.append((offset, direction, n))
            size = size - close_vol if size > 0 else size + close_vol
        # ② 开仓腿
        open_delta = target - size
        if open_delta != 0:
            legs.append(("OPEN", "LONG" if open_delta > 0 else "SHORT", abs(open_delta)))
        return legs

    def _close_legs(
        self, symbol: str, direction: str, lots: int, date: str, *, split: bool
    ) -> list[tuple[str, int]]:
        """平仓腿（上期所拆平昨 / 平今；其余直接平仓）。"""
        if split and needs_close_split(symbol):
            return self.ledger.close_breakdown(symbol, direction, lots, date)
        return [("CLOSE", lots)]

    def _quote_price(self, symbol: str, direction: str, chase: int) -> float:
        """限价：买入 = 最新价 + (1+追单数) 跳；卖出 = 最新价 − (1+追单数) 跳。"""
        tick = self.ticks.get(symbol)
        if tick is None:
            raise RuntimeError(f"{symbol} 无行情快照，无法报价")
        tick_size = float(self.ledger.contracts[symbol].tick_size)
        base = float(tick.last_price)
        k = (1 + int(chase)) * tick_size
        return base + k if direction == "LONG" else base - k

    def _submit(
        self,
        symbol: str,
        instrument: str,
        direction: str,
        offset: str,
        price: float,
        lots: int,
        chase_count: int = 0,
    ) -> str:
        """报单 + 登记本地订单簿。"""
        order_ref = self.session.order_insert(
            instrument,
            order_ctp_direction(direction, offset),
            OFFSET_TO_CTP[offset],
            price,
            lots,
        )
        self.book.submit(
            LiveOrder(
                order_ref=order_ref,
                symbol=symbol,
                instrument=instrument,
                direction=direction,
                offset=offset,
                price=float(price),
                volume=int(lots),
                chase_count=int(chase_count),
            )
        )
        return order_ref
