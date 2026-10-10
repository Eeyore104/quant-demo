"""风控体系（v1.3）：ATR 工具 / 止损止盈 / 组合熔断 / 敞口检查 / 事件模型。

口径（详见 ``docs/12``）：

- 所有判定只用「截至当日」数据（防未来函数）；
- 止损止盈为「盘中触价」近似：跳空穿过 → 开盘价成交；盘中触及 → 触发价成交；
  新开仓位自次一交易日起受盘中保护；备选口径（``execution.intrabar=false``）为
  「收盘判定 → 次日开盘成交」；
- 熔断为组合级：每日收盘判定 → 次日开盘生效；
- 平仓腿不受保证金限制，但受行情停板方向约束（跌停拒卖 / 涨停拒买）；
- 风控为 opt-in：未启用时组合引擎行为与 v1.2 完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .contracts import ContractSpec

# ---------------------------------------------------------------- ATR 工具


def atr_series(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """平均真实波幅（TR 的 ``window`` 日滚动均值）；与 ``df`` 行对齐，样本不足为 NaN。"""
    prev_close = df["close"].shift()
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window).mean()


# ---------------------------------------------------------------- 事件模型


@dataclass
class RiskEvent:
    """统一风控事件（止损 / 止盈 / 熔断 / 敞口 / 停板 / 降级 ……）。"""

    date: object
    kind: str
    symbol: str = ""
    detail: str = ""

    def describe(self) -> str:
        day = str(self.date)[:10]
        who = f" {self.symbol}" if self.symbol else ""
        return f"{day} [{self.kind}]{who}：{self.detail}"


# ---------------------------------------------------------------- 配置


@dataclass
class RiskConfig:
    """风控配置（从 ``config.yaml`` 的 ``risk`` 段解析；全部字段有默认值）。"""

    enabled: bool = True
    # 止损后重入规则：signal_reset（默认，等信号重置）/ cooldown / immediate
    reentry_mode: str = "signal_reset"
    reentry_cooldown_days: int = 3
    # 执行口径
    intrabar: bool = True  # 盘中触价（默认）；False = 收盘判定（备选口径）
    margin_degrade: bool = True  # 资金不足时按可负担手数降级；False = 整笔拒绝（v1.2 口径）
    # 止损止盈
    stop_fixed_enabled: bool = True
    stop_fixed_atr: float = 2.0
    stop_trailing_enabled: bool = True
    stop_trailing_atr: float = 3.0
    take_profit_enabled: bool = False
    take_profit_pct: float = 0.04
    # 熔断：三级阶梯 + 全平
    fuse_daily_loss_enabled: bool = True
    fuse_daily_loss_threshold: float = 0.03
    fuse_daily_loss_cooldown: int = 3
    fuse_drawdown_enabled: bool = True
    fuse_drawdown_threshold: float = 0.15
    fuse_drawdown_recovery: float = 0.10
    fuse_drawdown_degrade_factor: float = 0.5
    fuse_drawdown_max_cooldown: int = 20
    fuse_streak_enabled: bool = True
    fuse_streak_count: int = 5
    fuse_streak_cooldown: int = 10
    fuse_liquidate_enabled: bool = False
    fuse_liquidate_threshold: float = 0.25
    fuse_liquidate_cooldown: int = 10
    # 敞口上限（名义价值 / 权益）
    exposure_enabled: bool = True
    exposure_max_symbol_pct: float = 1.5
    exposure_max_total_pct: float = 4.0
    # 涨跌停（近似：|日涨跌幅| ≥ 停板幅度 × detect_factor）
    price_limit_enabled: bool = True
    price_limit_detect: float = 0.95
    price_limit_rates: dict = field(default_factory=dict)
    # 流动性
    liquidity_enabled: bool = True
    max_volume_share: float = 0.05

    @classmethod
    def from_dict(cls, cfg: dict | None) -> RiskConfig:
        """从配置字典解析（缺失字段用默认值；非法值校验后抛错）。"""
        cfg = cfg or {}
        c = cls()
        c.enabled = bool(cfg.get("enabled", c.enabled))

        r = cfg.get("reentry") or {}
        c.reentry_mode = str(r.get("mode", c.reentry_mode))
        if c.reentry_mode not in ("signal_reset", "cooldown", "immediate"):
            raise ValueError(f"未知 risk.reentry.mode：{c.reentry_mode}")
        c.reentry_cooldown_days = int(r.get("cooldown_days", c.reentry_cooldown_days))

        e = cfg.get("execution") or {}
        c.intrabar = bool(e.get("intrabar", c.intrabar))
        c.margin_degrade = bool(e.get("margin_degrade", c.margin_degrade))

        s = cfg.get("stops") or {}
        f = s.get("fixed") or {}
        c.stop_fixed_enabled = bool(f.get("enabled", c.stop_fixed_enabled))
        c.stop_fixed_atr = float(f.get("atr_mult", c.stop_fixed_atr))
        t = s.get("trailing") or {}
        c.stop_trailing_enabled = bool(t.get("enabled", c.stop_trailing_enabled))
        c.stop_trailing_atr = float(t.get("atr_mult", c.stop_trailing_atr))
        tp = s.get("take_profit") or {}
        c.take_profit_enabled = bool(tp.get("enabled", c.take_profit_enabled))
        c.take_profit_pct = float(tp.get("pct", c.take_profit_pct))

        fu = cfg.get("fuse") or {}
        dl = fu.get("daily_loss") or {}
        c.fuse_daily_loss_enabled = bool(dl.get("enabled", c.fuse_daily_loss_enabled))
        c.fuse_daily_loss_threshold = float(dl.get("threshold", c.fuse_daily_loss_threshold))
        c.fuse_daily_loss_cooldown = int(dl.get("cooldown_days", c.fuse_daily_loss_cooldown))
        dd = fu.get("drawdown") or {}
        c.fuse_drawdown_enabled = bool(dd.get("enabled", c.fuse_drawdown_enabled))
        c.fuse_drawdown_threshold = float(dd.get("threshold", c.fuse_drawdown_threshold))
        c.fuse_drawdown_recovery = float(dd.get("recovery", c.fuse_drawdown_recovery))
        c.fuse_drawdown_degrade_factor = float(
            dd.get("degrade_factor", c.fuse_drawdown_degrade_factor)
        )
        c.fuse_drawdown_max_cooldown = int(
            dd.get("max_cooldown_days", c.fuse_drawdown_max_cooldown)
        )
        ls = fu.get("losing_streak") or {}
        c.fuse_streak_enabled = bool(ls.get("enabled", c.fuse_streak_enabled))
        c.fuse_streak_count = int(ls.get("count", c.fuse_streak_count))
        c.fuse_streak_cooldown = int(ls.get("cooldown_days", c.fuse_streak_cooldown))
        lq = fu.get("liquidate") or {}
        c.fuse_liquidate_enabled = bool(lq.get("enabled", c.fuse_liquidate_enabled))
        c.fuse_liquidate_threshold = float(lq.get("threshold", c.fuse_liquidate_threshold))
        c.fuse_liquidate_cooldown = int(lq.get("cooldown_days", c.fuse_liquidate_cooldown))

        ex = cfg.get("exposure") or {}
        c.exposure_enabled = bool(ex.get("enabled", c.exposure_enabled))
        c.exposure_max_symbol_pct = float(ex.get("max_symbol_pct", c.exposure_max_symbol_pct))
        c.exposure_max_total_pct = float(ex.get("max_total_pct", c.exposure_max_total_pct))

        pl = cfg.get("price_limit") or {}
        c.price_limit_enabled = bool(pl.get("enabled", c.price_limit_enabled))
        c.price_limit_detect = float(pl.get("detect_factor", c.price_limit_detect))
        c.price_limit_rates = {str(k): float(v) for k, v in (pl.get("rates") or {}).items()}

        lq2 = cfg.get("liquidity") or {}
        c.liquidity_enabled = bool(lq2.get("enabled", c.liquidity_enabled))
        c.max_volume_share = float(lq2.get("max_volume_share", c.max_volume_share))
        return c


# ---------------------------------------------------------------- 止损止盈


@dataclass
class StopState:
    """单品种持仓的止损止盈状态（入场时建立）。"""

    direction: int = 0  # +1 多 / -1 空
    entry_price: float = 0.0
    atr_entry: float = 0.0
    entry_row: int = -1  # 入场行号（新仓次日才受盘中保护）
    fixed_stop: float = 0.0
    trail_stop: float = 0.0
    take_profit: float = 0.0  # 0 = 未启用


@dataclass
class StopTrigger:
    """一次止损/止盈触发（供引擎执行平仓）。"""

    kind: str  # stop_fixed / stop_trailing / take_profit
    direction: int  # 持仓方向
    base_price: float  # 成交基准价（± 滑点由撮合器处理）
    level: float  # 触发价位（日志用）


class StopEngine:
    """止损止盈判定（纯逻辑，不涉及账户/撮合）。"""

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.states: dict[str, StopState] = {}
        self.pending: dict[str, StopTrigger] = {}  # 收盘判定口径：次日开盘待执行

    def state(self, symbol: str) -> StopState | None:
        return self.states.get(symbol)

    def register_entry(
        self, symbol: str, direction: int, entry_price: float, atr_entry: float, row_idx: int
    ) -> None:
        cfg = self.cfg
        st = StopState(
            direction=direction,
            entry_price=float(entry_price),
            atr_entry=float(atr_entry or 0.0),
            entry_row=int(row_idx),
        )
        if st.atr_entry > 0:
            if cfg.stop_fixed_enabled:
                st.fixed_stop = self._fixed_level(entry_price, direction, st.atr_entry)
            if cfg.stop_trailing_enabled:
                st.trail_stop = self._fixed_level(
                    entry_price, direction, st.atr_entry, cfg.stop_trailing_atr
                )
        if cfg.take_profit_enabled and cfg.take_profit_pct > 0:
            delta = entry_price * cfg.take_profit_pct
            st.take_profit = entry_price + delta if direction > 0 else entry_price - delta
        self.states[symbol] = st

    def update_entry(self, symbol: str, new_avg_price: float) -> None:
        """加仓后以新均价重估固定止损（ATR 保持入场时读数）。"""
        st = self.states.get(symbol)
        if st is None:
            return
        st.entry_price = float(new_avg_price)
        if self.cfg.stop_fixed_enabled and st.atr_entry > 0:
            st.fixed_stop = self._fixed_level(st.entry_price, st.direction, st.atr_entry)

    def clear(self, symbol: str) -> None:
        self.states.pop(symbol, None)
        self.pending.pop(symbol, None)

    def _fixed_level(
        self, price: float, direction: int, atr: float, mult: float | None = None
    ) -> float:
        k = self.cfg.stop_fixed_atr if mult is None else mult
        return price - k * atr if direction > 0 else price + k * atr

    def _effective_stop(self, st: StopState) -> float:
        fixed = st.fixed_stop if self.cfg.stop_fixed_enabled else 0.0
        trail = st.trail_stop if self.cfg.stop_trailing_enabled else 0.0
        if st.direction > 0:
            return max(fixed, trail)
        vals = [v for v in (fixed, trail) if v > 0]
        return min(vals) if vals else 0.0

    def _stop_kind(self, st: StopState) -> str:
        fixed = st.fixed_stop if self.cfg.stop_fixed_enabled else 0.0
        trail = st.trail_stop if self.cfg.stop_trailing_enabled else 0.0
        if st.direction > 0:
            return "stop_trailing" if trail > 0 and trail >= fixed else "stop_fixed"
        if trail > 0 and (fixed <= 0 or trail <= fixed):
            return "stop_trailing"
        return "stop_fixed"

    def _armed(self, st: StopState | None, row_idx: int) -> bool:
        """新开仓位自次一交易日起受保护（同行不判定）。"""
        return st is not None and st.direction != 0 and row_idx > st.entry_row

    def gap_trigger(self, symbol: str, row_idx: int, bar) -> StopTrigger | None:
        """跳空判定（开盘价穿过止损/止盈价）→ 按开盘价成交。

        收盘判定口径（``intrabar=False``）下：先执行前一收盘登记的待平仓，否则不判定。
        """
        pend = self.pending.pop(symbol, None)
        if pend is not None:
            return StopTrigger(pend.kind, pend.direction, float(bar.open), pend.level)
        if not self.cfg.intrabar:
            return None
        st = self.states.get(symbol)
        if not self._armed(st, row_idx):
            return None
        assert st is not None
        eff = self._effective_stop(st)
        if st.direction > 0:
            if eff > 0 and float(bar.open) <= eff:
                return StopTrigger(self._stop_kind(st), 1, float(bar.open), eff)
            if st.take_profit > 0 and float(bar.open) >= st.take_profit:
                return StopTrigger("take_profit", 1, float(bar.open), st.take_profit)
        else:
            if eff > 0 and float(bar.open) >= eff:
                return StopTrigger(self._stop_kind(st), -1, float(bar.open), eff)
            if st.take_profit > 0 and float(bar.open) <= st.take_profit:
                return StopTrigger("take_profit", -1, float(bar.open), st.take_profit)
        return None

    def intrabar_trigger(self, symbol: str, row_idx: int, bar) -> StopTrigger | None:
        """盘中判定：用 low/high 触价（按触发价成交）；收盘判定口径下不判定。"""
        if not self.cfg.intrabar:
            return None
        st = self.states.get(symbol)
        if not self._armed(st, row_idx):
            return None
        assert st is not None
        eff = self._effective_stop(st)
        lo, hi = float(bar.low), float(bar.high)
        if st.direction > 0:
            if eff > 0 and lo <= eff:
                return StopTrigger(self._stop_kind(st), 1, eff, eff)
            if st.take_profit > 0 and hi >= st.take_profit:
                return StopTrigger("take_profit", 1, st.take_profit, st.take_profit)
        else:
            if eff > 0 and hi >= eff:
                return StopTrigger(self._stop_kind(st), -1, eff, eff)
            if st.take_profit > 0 and lo <= st.take_profit:
                return StopTrigger("take_profit", -1, st.take_profit, st.take_profit)
        return None

    def check_close(self, symbol: str, row_idx: int, bar) -> None:
        """收盘判定口径（``intrabar=False``）：收盘破位 → 登记次日开盘待执行。"""
        if self.cfg.intrabar or symbol in self.pending:
            return
        st = self.states.get(symbol)
        if not self._armed(st, row_idx):
            return
        assert st is not None
        eff = self._effective_stop(st)
        c = float(bar.close)
        if st.direction > 0:
            if eff > 0 and c <= eff:
                self.pending[symbol] = StopTrigger(self._stop_kind(st), 1, c, eff)
            elif st.take_profit > 0 and c >= st.take_profit:
                self.pending[symbol] = StopTrigger("take_profit", 1, c, st.take_profit)
        else:
            if eff > 0 and c >= eff:
                self.pending[symbol] = StopTrigger(self._stop_kind(st), -1, c, eff)
            elif st.take_profit > 0 and c <= st.take_profit:
                self.pending[symbol] = StopTrigger("take_profit", -1, c, st.take_profit)

    def update_after_close(self, symbol: str, close: float, atr_value: float) -> None:
        """收盘后更新移动止损（次日生效）。"""
        st = self.states.get(symbol)
        if st is None or st.direction == 0 or not self.cfg.stop_trailing_enabled:
            return
        if atr_value is None or not (atr_value > 0):
            return
        k = self.cfg.stop_trailing_atr
        if st.direction > 0:
            st.trail_stop = max(st.trail_stop, close - k * atr_value)
        else:
            cand = close + k * atr_value
            st.trail_stop = cand if st.trail_stop <= 0 else min(st.trail_stop, cand)


# ---------------------------------------------------------------- 重入守卫


class ReentryGuard:
    """止损/止盈后的重入规则：signal_reset（默认）/ cooldown / immediate。"""

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.stopped_today: set[str] = set()
        self.dir_lock: dict[str, int] = {}  # signal_reset：锁定的方向
        self.day_lock: dict[str, int] = {}  # cooldown：触发日索引

    def new_day(self) -> None:
        self.stopped_today.clear()

    def on_stop(self, symbol: str, day_idx: int, direction: int) -> None:
        """保护性出场（止损/移动止损/止盈）后登记锁定。"""
        self.stopped_today.add(symbol)
        if self.cfg.reentry_mode == "signal_reset":
            self.dir_lock[symbol] = direction
        elif self.cfg.reentry_mode == "cooldown":
            self.day_lock[symbol] = day_idx

    def update_signal(self, symbol: str, sig: int, day_idx: int) -> None:
        """按当日信号释放锁定（信号回到 0 / 反向 → 重置）。"""
        if symbol in self.dir_lock and (sig == 0 or sig == -self.dir_lock[symbol]):
            del self.dir_lock[symbol]
        if (
            symbol in self.day_lock
            and day_idx - self.day_lock[symbol] >= self.cfg.reentry_cooldown_days
        ):
            del self.day_lock[symbol]

    def allows_open(self, symbol: str, sig: int) -> bool:
        if sig == 0:
            return False
        if symbol in self.stopped_today:
            return False
        if self.cfg.reentry_mode == "signal_reset":
            return self.dir_lock.get(symbol) != sig
        if self.cfg.reentry_mode == "cooldown":
            return symbol not in self.day_lock
        return True


# ---------------------------------------------------------------- 熔断状态机


class FuseMachine:
    """组合熔断：三级阶梯（单日亏损 / 回撤 / 连续亏损）+ 全平（默认关）。

    每日收盘判定 → 次日开盘生效；恢复按冷却期或状态恢复（先满足者）。
    优先级：全平 > 暂停 > 减半。
    """

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.until_daily: int | None = None
        self.until_streak: int | None = None
        self.until_liquidate: int | None = None
        self.dd_active = False
        self.dd_since: int | None = None
        self.liquidate_next = False

    # ---------- 当日生效判断 ----------

    @staticmethod
    def _active(until: int | None, day_idx: int) -> bool:
        return until is not None and day_idx <= until

    def opening_blocked(self, day_idx: int) -> bool:
        if self.liquidate_next:
            return True
        return any(
            self._active(u, day_idx)
            for u in (self.until_daily, self.until_streak, self.until_liquidate)
        )

    def degrade_factor(self, day_idx: int) -> float:
        return float(self.cfg.fuse_drawdown_degrade_factor) if self.dd_active else 1.0

    def state_name(self, day_idx: int) -> str:
        if self.opening_blocked(day_idx):
            return "paused"
        if self.dd_active:
            return "degraded"
        return "normal"

    # ---------- 每日收盘判定 ----------

    def day_end(
        self,
        *,
        day_idx: int,
        date,
        equity: float,
        prev_equity: float,
        peak: float,
        losing_streak: int,
        has_positions: bool,
    ) -> list[RiskEvent]:
        cfg = self.cfg
        events: list[RiskEvent] = []

        # ① 冷却到期清理
        tiers = (
            ("until_daily", "单日亏损"),
            ("until_streak", "连续亏损"),
            ("until_liquidate", "全平"),
        )
        for attr, label in tiers:
            until = getattr(self, attr)
            if until is not None and day_idx >= until:
                setattr(self, attr, None)
                events.append(RiskEvent(date, "fuse_resume", "", f"{label}冷却结束，恢复新开仓"))

        # ② 单日亏损 → 暂停新开仓
        if cfg.fuse_daily_loss_enabled and prev_equity > 0:
            ret = equity / prev_equity - 1.0
            if ret <= -cfg.fuse_daily_loss_threshold:
                new_until = day_idx + cfg.fuse_daily_loss_cooldown
                if self.until_daily is None:
                    events.append(
                        RiskEvent(
                            date,
                            "fuse_pause",
                            "",
                            f"单日亏损 {ret:.2%} ≤ -{cfg.fuse_daily_loss_threshold:.2%}"
                            " → 暂停新开仓"
                            f"（冷却 {cfg.fuse_daily_loss_cooldown} 个交易日）",
                        )
                    )
                    self.until_daily = new_until
                else:
                    self.until_daily = max(self.until_daily, new_until)

        # ③ 回撤 → 新开仓减半
        if cfg.fuse_drawdown_enabled and peak > 0:
            dd = equity / peak - 1.0
            if not self.dd_active and dd <= -cfg.fuse_drawdown_threshold:
                self.dd_active = True
                self.dd_since = day_idx
                events.append(
                    RiskEvent(
                        date,
                        "fuse_degrade",
                        "",
                        f"回撤 {dd:.2%} ≤ -{cfg.fuse_drawdown_threshold:.2%} → 新开仓减半"
                        f"（回撤回落至 -{cfg.fuse_drawdown_recovery:.0%} 或冷却 "
                        f"{cfg.fuse_drawdown_max_cooldown} 日解除）",
                    )
                )
            elif self.dd_active:
                forced = (
                    self.dd_since is not None
                    and day_idx - self.dd_since >= cfg.fuse_drawdown_max_cooldown
                )
                recovered = dd > -cfg.fuse_drawdown_recovery
                if recovered or forced:
                    self.dd_active = False
                    self.dd_since = None
                    why = "回撤回落" if recovered else "冷却期满"
                    events.append(
                        RiskEvent(date, "fuse_resume", "", f"降规模解除（{why}），新开仓恢复正常")
                    )

        # ④ 连续亏损 → 暂停新开仓
        if cfg.fuse_streak_enabled and losing_streak >= cfg.fuse_streak_count:
            new_until = day_idx + cfg.fuse_streak_cooldown
            if self.until_streak is None:
                events.append(
                    RiskEvent(
                        date,
                        "fuse_pause",
                        "",
                        f"连续亏损 {losing_streak} 笔 ≥ {cfg.fuse_streak_count} → 暂停新开仓"
                        f"（冷却 {cfg.fuse_streak_cooldown} 个交易日）",
                    )
                )
                self.until_streak = new_until
            else:
                self.until_streak = max(self.until_streak, new_until)

        # ⑤ 全平（最高级；默认关）
        if (
            cfg.fuse_liquidate_enabled
            and peak > 0
            and has_positions
            and not self.liquidate_next
            and not self._active(self.until_liquidate, day_idx)
        ):
            dd2 = equity / peak - 1.0
            if dd2 <= -cfg.fuse_liquidate_threshold:
                events.append(
                    RiskEvent(
                        date,
                        "fuse_liquidate",
                        "",
                        f"回撤 {dd2:.2%} ≤ -{cfg.fuse_liquidate_threshold:.2%} → 次日开盘全平",
                    )
                )
                self.liquidate_next = True
        return events

    def finish_liquidate(self, day_idx: int) -> None:
        """全平执行完成 → 进入冷却。"""
        self.liquidate_next = False
        self.until_liquidate = day_idx + self.cfg.fuse_liquidate_cooldown


# ---------------------------------------------------------------- 风控聚合


class RiskManager:
    """风控聚合：配置 + 止损引擎 + 重入守卫 + 熔断 + 涨跌停/敞口辅助判定。"""

    def __init__(self, cfg: RiskConfig, contracts: dict[str, ContractSpec]):
        self.cfg = cfg
        self.contracts = contracts
        self.stops = StopEngine(cfg)
        self.guard = ReentryGuard(cfg)
        self.fuse = FuseMachine(cfg)

    # ---------- 涨跌停（近似口径）----------

    def limit_state(self, symbol: str, prev_close: float | None, bar) -> str | None:
        """返回 'up' / 'down' / None。判定：|当根 bar 涨跌幅| ≥ 停板幅度 × detect_factor。"""
        if not self.cfg.price_limit_enabled or prev_close is None or prev_close <= 0:
            return None
        rate = float(self.cfg.price_limit_rates.get(symbol, 0.0) or 0.0)
        if rate <= 0:
            return None
        ret = float(bar.close) / float(prev_close) - 1.0
        if ret >= rate * self.cfg.price_limit_detect:
            return "up"
        if ret <= -rate * self.cfg.price_limit_detect:
            return "down"
        return None

    @staticmethod
    def leg_blocked(limit: str | None, action: str, direction: str) -> bool:
        """停板日方向感知拒单：涨停拒买（开多/平空）、跌停拒卖（开空/平多）。"""
        if limit is None:
            return False
        is_buy = (action == "OPEN" and direction == "LONG") or (
            action == "CLOSE" and direction == "SHORT"
        )
        if limit == "up":
            return is_buy
        return not is_buy


# ---------------------------------------------------------------- 敞口 / 流动性辅助


def exposure_room(account, cfg: RiskConfig, symbol: str, price: float, multiplier: int) -> int:
    """在单品种 / 组合名义上限内，该品种最多可再开的手数。"""
    per_lot = float(price) * int(multiplier)
    if per_lot <= 0:
        return 0
    equity = account.equity
    room_s = equity * cfg.exposure_max_symbol_pct - account.exposure_notional(symbol)
    room_t = equity * cfg.exposure_max_total_pct - account.total_exposure
    return max(0, int(min(room_s, room_t) // per_lot))


def exposure_warnings(account, cfg: RiskConfig) -> int:
    """当日收盘后超限计数（单品种逐项 + 组合总计，仅记录不强制减仓）。"""
    if not cfg.exposure_enabled:
        return 0
    equity = account.equity
    cap_s = equity * cfg.exposure_max_symbol_pct
    n = sum(1 for s in account.states if account.exposure_notional(s) > cap_s)
    if account.total_exposure > equity * cfg.exposure_max_total_pct:
        n += 1
    return n
