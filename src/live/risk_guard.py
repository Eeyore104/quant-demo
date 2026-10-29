"""实时风控闸门（v2.0 · docs/16 T2.0-4a）：全量对齐 v1.3 口径。

**核心复用**：直接使用组合引擎的风控组件（同一份判定逻辑）——
``RiskConfig`` / ``RiskManager``（内含 ``StopEngine`` / ``ReentryGuard`` /
``FuseMachine`` / 涨跌停判定）/ ``exposure_room``。

live 特有编排：
- **下单前闸门**（顺序对齐回测开仓腿）：熔断状态 → 重入锁定 → 涨跌停方向
  → 敞口 → 流动性（当日累计成交量参与率）→ 资金（降级 / 拒绝）；
  平仓腿仅受涨跌停方向约束；
- **盘中止损止盈**：由 tick 构造「合成 bar」（high/low 取快照当日极值），
  调用 ``StopEngine.intrabar_trigger`` 触价判定——与回测同逻辑；
  触发后按市价执行（真实成交价由 SimNow 撮合决定，偏差记入事件流）；
- **收盘处理**：移动止损更新（当日 ATR）→ 熔断 ``fuse.day_end`` → 账本推进；
- **状态持久化**：止损状态 / 重入锁定 / 熔断状态 / 前收盘价。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..engine.risk import RiskConfig, RiskEvent, RiskManager, StopTrigger, exposure_room
from ..utils.logger import get_logger

log = get_logger("live.risk_guard")


@dataclass
class TickBar:
    """由 tick 合成的「截至当前 bar」：让引擎侧判定逻辑原样复用。"""

    open: float
    high: float
    low: float
    close: float
    volume: float
    date: str = ""


def tick_bar(tick) -> TickBar:
    """行情快照 → 合成 bar（open/high/low 取当日快照字段，close 取最新价）。"""
    last = float(tick.last_price)
    return TickBar(
        open=float(tick.open_price) or last,
        high=float(tick.highest_price) or last,
        low=float(tick.lowest_price) or last,
        close=last,
        volume=float(tick.volume),
        date=str(tick.update_time or ""),
    )


class LiveRiskGuard:
    """实时风控：闸门 + 止损监控 + 熔断（复用引擎组件，live 仅编排）。"""

    def __init__(self, risk_cfg: dict, contracts, ledger, prev_close: dict | None = None):
        self.cfg = RiskConfig.from_dict(risk_cfg)
        self.contracts = contracts
        self.ledger = ledger
        self.manager = RiskManager(self.cfg, contracts)
        self.prev_close: dict[str, float] = {k: float(v) for k, v in (prev_close or {}).items()}
        # 最近一次信号重放得到的 ATR（截至信号日收盘；与回测 register_entry 同源）
        self.atr_hint: dict[str, float] = {}
        # v2.1-R5：止损注册后的极值（防止用「建仓前」的当日高低点误判）
        self.since: dict[str, list[float]] = {}  # symbol → [low, high]
        self.reg_day: dict[str, int] = {}  # symbol → 注册时的 day_idx

    # -------------------------------------------------- 下单前闸门

    def gate_open(
        self,
        symbol: str,
        direction: str,
        lots: int,
        price: float,
        tick,
        *,
        sig: int,
        date: str = "",
    ) -> tuple[int, list[str], list[RiskEvent]]:
        """开仓腿闸门：返回 (调整后手数, 说明列表, 事件列表)；0 = 拒绝。"""
        notes: list[str] = []
        events: list[RiskEvent] = []

        # ① 熔断状态（冷却中 → 拒开仓；平仓不受限）
        if self.manager.fuse.opening_blocked(self.ledger.day_idx):
            return 0, ["熔断冷却中，新开仓暂停"], [
                RiskEvent(date, "fuse_block", symbol, "熔断冷却：新开仓暂停")
            ]

        # ② 止损重入锁定（signal_reset / cooldown）
        if not self.manager.guard.allows_open(symbol, sig):
            return 0, ["止损重入锁定中（signal_reset）"], [
                RiskEvent(date, "reentry_block", symbol, f"重入锁定：信号 {sig} 被拦")
            ]

        # ③ 涨跌停（方向感知）
        limit = self._limit_state(symbol, tick)
        if self.manager.leg_blocked(limit, "OPEN", direction):
            tag = "涨停" if limit == "up" else "跌停"
            return 0, [f"{tag}方向拒单"], [
                RiskEvent(date, "limit_refuse", symbol, f"{tag}：拒绝开仓方向 {direction}")
            ]

        # ④ 敞口上限（开关与回测 apply_exposure_filter 一致）
        spec = self.contracts[symbol]
        if self.cfg.exposure_enabled:
            room = exposure_room(self.ledger.account, self.cfg, symbol, price, spec.multiplier)
            if room < lots:
                if room <= 0:
                    msg = f"敞口上限：拒绝开 {lots} 手"
                    return 0, ["敞口上限拒绝"], [
                        RiskEvent(date, "exposure_refuse", symbol, msg)
                    ]
                notes.append(f"敞口调整 {lots}→{room} 手")
                events.append(
                    RiskEvent(date, "exposure_trim", symbol, f"敞口上限：{lots}→{room} 手")
                )
                lots = room

        # ⑤ 流动性（当日累计成交量参与率）
        if self.cfg.liquidity_enabled:
            cap = int(float(tick.volume) * self.cfg.max_volume_share)
            if cap < lots:
                if cap <= 0:
                    msg2 = f"参与率上限：拒绝开 {lots} 手"
                    return 0, ["流动性不足拒绝"], [
                        RiskEvent(date, "participation_trim", symbol, msg2)
                    ]
                notes.append(f"参与率调整 {lots}→{cap} 手")
                events.append(
                    RiskEvent(
                        date,
                        "participation_trim",
                        symbol,
                        f"参与率上限：{lots}→{cap} 手"
                        f"（≤ 成交量 × {self.cfg.max_volume_share:.0%}）",
                    )
                )
                lots = cap

        # ⑥ 资金（降级 / 拒绝）
        n, msg = self.ledger.affordable_lots(symbol, price, lots)
        if msg:
            if n <= 0:
                notes.append(msg)
                return 0, notes, events + [
                    RiskEvent(date, "margin_refuse", symbol, msg)
                ]
            notes.append(msg)
            events.append(RiskEvent(date, "margin_degrade", symbol, msg))
            lots = n

        return lots, notes, events

    def gate_close(
        self, symbol: str, direction: str, lots: int, tick, *, date: str = ""
    ) -> tuple[int, list[str], list[RiskEvent]]:
        """平仓腿闸门：仅受涨跌停方向约束（保证金上永不拒绝，与回测一致）。"""
        limit = self._limit_state(symbol, tick)
        if self.manager.leg_blocked(limit, "CLOSE", direction):
            tag = "涨停" if limit == "up" else "跌停"
            return 0, [f"{tag}方向拒单"], [
                RiskEvent(date, "limit_refuse", symbol, f"{tag}：拒绝平仓方向 {direction}")
            ]
        return lots, [], []

    # -------------------------------------------------- 止损状态同步（v2.1）

    def sync_stops(
        self, symbol: str, row_idx: int | None = None, *, date: str = ""
    ) -> list[RiskEvent]:
        """成交后同步止损状态（对齐回测 ``portfolio_engine.register_entry_stops``）。

        三分支语义（必须逐笔成交后调用，不能只在开仓时调用）：
        - 持仓归零 → 清除状态（普通调仓平仓也要清，否则残留会成为下次开仓的「旧止损位」）；
        - 无状态 / 方向变更 → 按账本均价与 ``atr_hint`` 建立；
        - 同向仍持仓 → 按新均价重估固定止损（加仓）。
        """
        row = self.ledger.day_idx if row_idx is None else int(row_idx)
        pos = self.ledger.net_size(symbol)
        events: list[RiskEvent] = []

        if pos == 0:
            if self.manager.stops.state(symbol) is not None:
                self.manager.stops.clear(symbol)
                self.since.pop(symbol, None)
                self.reg_day.pop(symbol, None)
                events.append(RiskEvent(date, "stop_clear", symbol, "持仓归零 → 清除止损状态"))
            return events

        direction = 1 if pos > 0 else -1
        avg = float(self.ledger.state(symbol).avg_price)
        st = self.manager.stops.state(symbol)
        atr = float(self.atr_hint.get(symbol, 0.0) or 0.0)
        if st is not None and st.direction == direction and st.atr_entry > 0:
            if abs(avg - st.entry_price) < 1e-9:
                # v2.1-R9：无加仓 → 不做重估。否则「每日校准」会按当前配置把固定止损
                # 重算一遍（10-29 实测：演练设定的 6664 被抹成 2×ATR 的 6160.4）
                return events
            self.manager.stops.update_entry(symbol, avg)
            events.append(RiskEvent(date, "stop_update", symbol, f"加仓重估：均价 {avg:.4f}"))
            return events

        # 新建 / 反向重建 / ATR 补全（保留原基准日，避免 armed 被重置）
        is_new = st is None or st.direction != direction
        entry_row = row if is_new else st.entry_row
        self.manager.stops.register_entry(symbol, direction, avg, atr, entry_row)
        # v2.1-R5：记录「注册后极值」起点（当日判定只用注册后的价格）
        if is_new or symbol not in self.since:
            self.since[symbol] = [avg, avg]
        self.reg_day[symbol] = self.ledger.day_idx
        if atr > 0:
            events.append(
                RiskEvent(
                    date,
                    "stop_register",
                    symbol,
                    f"建立止损：{direction:+d} @ {avg:.4f}，ATR {atr:.4f}，基准日 {entry_row}",
                )
            )
        else:
            events.append(
                RiskEvent(
                    date,
                    "stop_register_no_atr",
                    symbol,
                    f"建立止损但 ATR 缺失（仅止盈可用）：{direction:+d} @ {avg:.4f}",
                )
            )
        return events

    # -------------------------------------------------- 盘中止损止盈

    def on_tick(self, symbol: str, tick) -> StopTrigger | None:
        """tick 触价判定（合成 bar → 复用回测 intrabar 逻辑）。

        v2.1-R5：**当日注册**的止损只用「注册后观察到的极值」——否则补建 / 新建仓
        会拿建仓之前出现的当日最低（最高）价把自己止损掉（2026-10-26 实测）。
        跨日持仓仍用 tick 的当日极值，保留隔夜跳空识别能力。
        """
        last = float(tick.last_price)
        ext = self.since.get(symbol)
        if ext is None:
            ext = [last, last]
            self.since[symbol] = ext
        else:
            ext[0] = min(ext[0], last)
            ext[1] = max(ext[1], last)
        bar = tick_bar(tick)
        if self.reg_day.get(symbol) == self.ledger.day_idx:
            bar = TickBar(
                open=bar.open,
                high=max(ext[1], last),
                low=min(ext[0], last),
                close=bar.close,
                volume=bar.volume,
                date=bar.date,
            )
        return self.manager.stops.intrabar_trigger(symbol, self.ledger.day_idx, bar)

    def notify_stop(self, symbol: str, direction: int, *, date: str = "") -> None:
        """止损/止盈平仓执行完成后调用：清状态 + 登记重入锁定（对齐回测）。"""
        self.manager.stops.clear(symbol)
        self.since.pop(symbol, None)
        self.reg_day.pop(symbol, None)
        self.manager.guard.on_stop(symbol, self.ledger.day_idx, direction)

    def update_signal(self, symbol: str, sig: int) -> None:
        self.manager.guard.update_signal(symbol, sig, self.ledger.day_idx)

    def new_day(self) -> None:
        """交易日开始调用（与 ledger.begin_day 同步）。"""
        self.manager.guard.new_day()

    def opening_blocked(self) -> bool:
        return self.manager.fuse.opening_blocked(self.ledger.day_idx)

    def liquidate_pending(self) -> bool:
        """全平待执行（最高级熔断，默认关）。"""
        return bool(self.manager.fuse.liquidate_next)

    def finish_liquidate(self) -> None:
        self.manager.fuse.finish_liquidate(self.ledger.day_idx)

    def fuse_state(self) -> str:
        return self.manager.fuse.state_name(self.ledger.day_idx)

    def stop_state(self, symbol: str):
        return self.manager.stops.state(symbol)

    # -------------------------------------------------- 收盘处理

    def on_day_close(
        self, date: str, atr_map: dict[str, float], *, liquidating_done: bool = False
    ) -> dict:
        """收盘流程：移动止损更新 → 熔断判定 → 账本推进（顺序勿换）。

        ``atr_map``：各品种「含当日」的 ATR（由信号数据侧计算传入）。
        """
        # ① 移动止损更新（当日收盘价 + 当日 ATR，次日生效）
        if self.cfg.stop_trailing_enabled:
            for symbol in self.contracts:
                if self.ledger.net_size(symbol) != 0:
                    last = float(self.ledger.state(symbol).last_price)
                    atr_v = float(atr_map.get(symbol) or 0.0)
                    self.manager.stops.update_after_close(symbol, last, atr_v)

        # ② 熔断判定（使用「旧 prev / 旧 peak」——与回测时序一致）
        events = self.manager.fuse.day_end(
            day_idx=self.ledger.day_idx,
            date=date,
            equity=self.ledger.equity,
            prev_equity=self.ledger.prev_equity,
            peak=self.ledger.peak_equity,
            losing_streak=self.ledger.losing_streak,
            has_positions=any(
                self.ledger.net_size(s) != 0 for s in self.contracts
            ),
        )
        if liquidating_done:
            self.manager.fuse.finish_liquidate(self.ledger.day_idx)

        # ③ 账本推进（峰值 / 前一日权益）
        snap = self.ledger.close_day()
        for ev in events:
            log.warning("熔断事件：%s", ev.describe())
        return {
            "events": events,
            "snap": snap,
            "fuse_state": self.manager.fuse.state_name(self.ledger.day_idx),
        }

    # -------------------------------------------------- 辅助

    def _limit_state(self, symbol: str, tick) -> str | None:
        if not self.cfg.price_limit_enabled:
            return None
        return self.manager.limit_state(symbol, self.prev_close.get(symbol), tick_bar(tick))

    # -------------------------------------------------- 持久化

    def to_dict(self) -> dict:
        stops = {}
        for s, st in self.manager.stops.states.items():
            stops[s] = {
                "direction": st.direction,
                "entry_price": st.entry_price,
                "atr_entry": st.atr_entry,
                "entry_row": st.entry_row,
                "fixed_stop": st.fixed_stop,
                "trail_stop": st.trail_stop,
                "take_profit": st.take_profit,
            }
        g = self.manager.guard
        f = self.manager.fuse
        return {
            "prev_close": self.prev_close,
            "stops": stops,
            "guard": {
                "stopped_today": sorted(g.stopped_today),
                "dir_lock": dict(g.dir_lock),
                "day_lock": dict(g.day_lock),
            },
            "fuse": {
                "until_daily": f.until_daily,
                "until_streak": f.until_streak,
                "until_liquidate": f.until_liquidate,
                "dd_active": f.dd_active,
                "dd_since": f.dd_since,
                "liquidate_next": f.liquidate_next,
            },
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("风控状态已保存：%s", p)

    def load(self, path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        d = json.loads(p.read_text(encoding="utf-8"))
        self.prev_close = {k: float(v) for k, v in (d.get("prev_close") or {}).items()}
        from ..engine.risk import StopState

        self.manager.stops.states = {}
        for s, st in (d.get("stops") or {}).items():
            self.manager.stops.states[s] = StopState(
                direction=int(st["direction"]),
                entry_price=float(st["entry_price"]),
                atr_entry=float(st["atr_entry"]),
                entry_row=int(st["entry_row"]),
                fixed_stop=float(st["fixed_stop"]),
                trail_stop=float(st["trail_stop"]),
                take_profit=float(st["take_profit"]),
            )
        g = self.manager.guard
        gd = d.get("guard") or {}
        g.stopped_today = set(gd.get("stopped_today") or [])
        g.dir_lock = {k: int(v) for k, v in (gd.get("dir_lock") or {}).items()}
        g.day_lock = {k: int(v) for k, v in (gd.get("day_lock") or {}).items()}
        f = self.manager.fuse
        fd = d.get("fuse") or {}
        f.until_daily = fd.get("until_daily")
        f.until_streak = fd.get("until_streak")
        f.until_liquidate = fd.get("until_liquidate")
        f.dd_active = bool(fd.get("dd_active", False))
        f.dd_since = fd.get("dd_since")
        f.liquidate_next = bool(fd.get("liquidate_next", False))
        log.info("风控状态已恢复：%s（止损状态 %d 个品种）", p, len(self.manager.stops.states))
        return True
