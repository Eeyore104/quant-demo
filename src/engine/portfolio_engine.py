"""组合引擎（v1.2 / v1.3）：多品种并行 · 共享资金池 · 保证金约束 · 逐日盯市 · 风控体系。

旁路原则：单品种引擎（``engine.py``）与其调用链**零改动**；本模块在引擎上层
实现跨品种事件循环。成交约定与单品种一致：**T 日收盘信号 → T+1 开盘价 ± 滑点**。

v1.3 风控（``risk_cfg`` 启用时）——单根 bar 处理时序（详见 ``docs/12``）：

① 跳空检查：持仓止损/止盈被开盘价穿过 → 按开盘价平仓；
② 信号成交：基础目标 → 停板方向 / 熔断档位 / 敞口上限 / 保证金降级 逐级调整；
③ 盘中检查：low/high 触发止损/止盈 → 按触发价平仓（新仓自次日起受保护）；
④ 盯市 + 移动止损更新（收盘后，次日生效）；
⑤ 日终：熔断判定（收盘判定 → 次日生效）+ 逐日风险记录。

``risk_cfg`` 为 None 或缺省时行为与 v1.2 **完全一致**（零回归）。处理顺序：同日事件
按品种池配置顺序处理；平仓腿永不因保证金拒绝，但受行情停板方向约束。
"""

import itertools
from dataclasses import dataclass, field

import pandas as pd

from ..utils.logger import get_logger
from .account import ConstraintEvent, PortfolioAccount
from .broker import Broker
from .contracts import ContractSpec
from .engine import _to_bar
from .risk import (
    RiskConfig,
    RiskEvent,
    RiskManager,
    atr_series,
    exposure_room,
    exposure_warnings,
)
from .sizing import compute_target_lots

log = get_logger("engine")


@dataclass
class PortfolioResult:
    """组合回测结果。"""

    equity_df: pd.DataFrame  # 逐日：权益/现金/保证金/可用资金 + 逐品种盈亏（pnl_<品种> 列）
    trades: list  # 全部成交（Trade，含 symbol）
    events: list[ConstraintEvent]  # 拒绝开仓事件（保证金约束，v1.2 口径）
    account: PortfolioAccount  # 共享账户（含逐品种状态）
    symbols: list[str]  # 品种池（配置顺序）
    risk_events: list = field(default_factory=list)  # v1.3 风控事件（止损/熔断/敞口/停板…）


# ---------------------------------------------------------------- 目标手数调整


def _trim_open_only(target: int, cur: int) -> int:
    """禁止开仓场景（熔断暂停 / 止损当日 / 全平）：只允许减仓与平仓。"""
    if cur == 0 or target == 0:
        return 0
    if (target > 0) != (cur > 0):
        return 0  # 反向：只平不开
    if abs(target) > abs(cur):
        return cur  # 加仓：维持原仓
    return target  # 减仓正常


def _halve(mag: int, factor: float) -> int:
    """熔断降规模：向下缩放但保底 1 手（``mag`` 已不足 1 手则为 0）。"""
    if mag < 1:
        return 0
    return max(1, int(mag * factor))


def _degrade_target(target: int, cur: int, factor: float) -> int:
    """熔断「新开仓减半」：只压缩开仓侧，绝不压缩到低于当前持仓。"""
    if target == 0:
        return 0
    sign = 1 if target > 0 else -1
    if cur == 0 or (target > 0) != (cur > 0):
        return sign * _halve(abs(target), factor)
    if abs(target) > abs(cur):
        return sign * max(abs(cur), _halve(abs(target), factor))
    return target


# ---------------------------------------------------------------- 主流程


def run_portfolio(
    data: dict[str, pd.DataFrame],
    strategy_specs: dict[str, tuple[type, dict]],
    contracts: dict[str, ContractSpec],
    *,
    initial_capital: float,
    sizing_cfg: dict | None = None,
    risk_cfg: dict | None = None,
) -> PortfolioResult:
    """组合回测主流程：逐品种信号 → 合并事件流 → 共享账户逐日推进（v1.3 含风控层）。"""
    symbols = list(data.keys())
    sizing_cfg = dict(sizing_cfg or {})

    risk: RiskManager | None = None
    if risk_cfg:
        rc = RiskConfig.from_dict(risk_cfg)
        if rc.enabled:
            risk = RiskManager(rc, contracts)

    mode = str(sizing_cfg.get("mode", "fixed"))
    latch_mode = mode == "atr_risk"  # 入场时锁定手数（期间不随波动漂移）
    atr_window = int(sizing_cfg.get("atr_window", 20))
    need_atr = risk is not None or latch_mode

    # ① 逐品种信号预计算（复用既有策略接口，零改动）
    dfs: dict[str, pd.DataFrame] = {}
    for s in symbols:
        cls, params = strategy_specs[s]
        out = cls(params).generate_signals(data[s])
        if "signal" not in out.columns:
            raise ValueError(f"策略未生成 signal 列：{s}")
        dfs[s] = out

    atr_full: dict[str, pd.Series] = (
        {s: atr_series(dfs[s], atr_window) for s in symbols} if need_atr else {}
    )

    account = PortfolioAccount(initial_capital, contracts)
    brokers: dict[str, Broker] = {
        s: Broker(
            commission_per_lot=contracts[s].commission_per_lot,
            slippage_ticks=1,
            tick_size=contracts[s].tick_size,
            contract_multiplier=contracts[s].multiplier,
        )
        for s in symbols
    }

    # ② 合并事件流：每品种全部 bar（i=0 仅初始化价格），按 (日期, 品种池顺序) 排序
    order = {s: i for i, s in enumerate(symbols)}
    events: list[tuple] = []
    for s in symbols:
        dates = dfs[s]["date"]
        for i in range(len(dfs[s])):
            events.append((dates.iloc[i], order[s], s, i))
    events.sort(key=lambda e: (e[0], e[1]))

    # ③ 手数：fixed 为常量；equal_weight / inv_vol 按日计算（v1.2 口径）；
    #    atr_risk 按日计算 + 入场锁定（latch）
    max_lots = int(sizing_cfg.get("max_lots_per_symbol", 10))
    fixed_lots = (
        {s: max(0, min(int(sizing_cfg.get("lots", 1)), max_lots)) for s in symbols}
        if mode == "fixed"
        else None
    )
    lots_cache: dict[object, dict[str, int]] = {}
    latch: dict[str, int] = {}
    day_ref_equity = float(initial_capital)

    def lots_for_date(d) -> dict[str, int]:
        if fixed_lots is not None:
            return fixed_lots
        if d not in lots_cache:
            prices: dict[str, float] = {}
            closes_map: dict[str, pd.Series] = {}
            atr_map: dict[str, float] = {}
            for s in symbols:
                idx = int((dfs[s]["date"] < d).sum())  # 截至信号日收盘的行数
                if idx > 0:
                    closes_map[s] = dfs[s]["close"].iloc[:idx]
                    prices[s] = float(dfs[s]["close"].iloc[idx - 1])
                    if need_atr:
                        av = float(atr_full[s].iloc[idx - 1])
                        if av == av and av > 0:  # 非 NaN 且为正
                            atr_map[s] = av
            cap = day_ref_equity if latch_mode else float(initial_capital)
            lots_cache[d] = compute_target_lots(
                mode, contracts, prices, closes_map, cap, sizing_cfg, atr_by_symbol=atr_map
            )
        return lots_cache[d]

    trades: list = []
    refused = 0
    risk_events: list[RiskEvent] = []
    day_idx = -1
    losing_streak = 0
    peak_equity = float(initial_capital)
    prev_equity = float(initial_capital)
    liquidating_today = False

    def record_closes(close_trades: list) -> None:
        nonlocal losing_streak
        for t in close_trades:
            if t.pnl < 0:
                losing_streak += 1
            elif t.pnl > 0:
                losing_streak = 0

    def execute_stop(s: str, bar, prev_close: float | None, trig, venue: str) -> None:
        """执行止损/止盈平仓；停板方向不可成交则记录事件、持仓跨日。"""
        pos = account.state(s).size
        if pos == 0:
            return
        assert risk is not None
        limit = risk.limit_state(s, prev_close, bar)
        direction = "LONG" if pos > 0 else "SHORT"
        if risk.leg_blocked(limit, "CLOSE", direction):
            tag = "涨停" if limit == "up" else "跌停"
            risk_events.append(
                RiskEvent(
                    bar.date,
                    "stop_refused",
                    s,
                    f"{venue}触发（{trig.kind} 价位 {trig.level:,.2f}），但{tag}方向不可成交"
                    " → 持仓跨日，继续判定",
                )
            )
            return
        t = brokers[s].close_at(bar, pos, trig.base_price)
        account.apply_trades([t])
        trades.append(t)
        record_closes([t])
        risk.stops.clear(s)
        risk.guard.on_stop(s, day_idx, trig.direction)
        risk_events.append(
            RiskEvent(
                bar.date,
                trig.kind,
                s,
                f"{venue}触发（价位 {trig.level:,.2f}）→ 平 {abs(pos)} 手 @ {t.price:.2f}"
                f"（盈亏 {t.pnl:,.0f} 元）",
            )
        )

    def register_entry_stops(s: str, row_idx: int) -> None:
        """开仓成交后建立/更新止损状态（ATR 取截至信号日收盘，防未来）。"""
        assert risk is not None
        pos = account.state(s).size
        if pos == 0:
            risk.stops.clear(s)
            return
        direction = 1 if pos > 0 else -1
        avg = account.state(s).avg_price
        atr_entry = 0.0
        if need_atr and row_idx > 0:
            av = float(atr_full[s].iloc[row_idx - 1])
            if av == av and av > 0:
                atr_entry = av
        st = risk.stops.state(s)
        if st is None or st.direction != direction:
            risk.stops.register_entry(s, direction, avg, atr_entry, row_idx)
        else:
            risk.stops.update_entry(s, avg)

    def apply_limit_filter(s: str, bar, prev_close: float | None, closes: list, opens: list):
        """停板方向感知拒单（对信号单成交腿过滤）。"""
        if (not closes and not opens) or risk is None:
            return closes, opens
        limit = risk.limit_state(s, prev_close, bar)
        if limit is None:
            return closes, opens
        kept_c = [t for t in closes if not risk.leg_blocked(limit, t.action, t.direction)]
        kept_o = [t for t in opens if not risk.leg_blocked(limit, t.action, t.direction)]
        if len(kept_c) + len(kept_o) < len(closes) + len(opens):
            tag = "涨停" if limit == "up" else "跌停"
            risk_events.append(
                RiskEvent(
                    bar.date,
                    "limit_refuse",
                    s,
                    f"{tag}方向不可成交：计划 {len(closes)} 平 + {len(opens)} 开"
                    f" → 实际 {len(kept_c)} 平 + {len(kept_o)} 开",
                )
            )
        return kept_c, kept_o

    def apply_exposure_filter(s: str, bar, opens: list) -> list:
        """敞口上限：单品种 / 组合名义超限时截断开仓手数（截到 0 → 拒绝）。"""
        assert risk is not None
        if not risk.cfg.exposure_enabled or not opens:
            return opens
        t0 = opens[0]
        room = exposure_room(account, risk.cfg, s, t0.price, contracts[s].multiplier)
        if room >= t0.volume:
            return opens
        if room <= 0:
            risk_events.append(
                RiskEvent(
                    bar.date,
                    "exposure_refuse",
                    s,
                    f"敞口上限：单品种 {account.exposure_notional(s):,.0f} / 组合 "
                    f"{account.total_exposure:,.0f} 元 → 拒绝开 {t0.volume} 手",
                )
            )
            return []
        nt = brokers[s].resize(bar, t0, room)
        risk_events.append(
            RiskEvent(bar.date, "exposure_trim", s, f"敞口上限：开仓 {t0.volume} 手 → {room} 手")
        )
        return [nt]

    def apply_participation_filter(s: str, bar, opens: list) -> list:
        """成交量参与率上限：单笔下单 ≤ 当日成交量 × 参与率。"""
        assert risk is not None
        if not risk.cfg.liquidity_enabled or not opens:
            return opens
        t0 = opens[0]
        cap = int(float(bar.volume) * risk.cfg.max_volume_share)
        if cap >= t0.volume:
            return opens
        if cap <= 0:
            risk_events.append(
                RiskEvent(
                    bar.date,
                    "participation_trim",
                    s,
                    f"参与率上限：当日成交量 {bar.volume} 手 → 拒绝开 {t0.volume} 手",
                )
            )
            return []
        nt = brokers[s].resize(bar, t0, cap)
        risk_events.append(
            RiskEvent(
                bar.date,
                "participation_trim",
                s,
                f"参与率上限：开仓 {t0.volume} 手 → {cap} 手"
                f"（≤ 成交量 × {risk.cfg.max_volume_share:.0%}）",
            )
        )
        return [nt]

    # ④ 事件循环：先平（保证金上永不拒绝）→ 再开（资金检查）→ 盯市 → 日终记录
    for d, group in itertools.groupby(events, key=lambda e: e[0]):
        day_idx += 1
        if risk is not None:
            risk.guard.new_day()
            liquidating_today = risk.fuse.liquidate_next
        if latch_mode:
            day_ref_equity = account.equity

        for _, _, s, i in group:
            df = dfs[s]
            bar = _to_bar(df.iloc[i])
            prev_close = float(df["close"].iloc[i - 1]) if i > 0 else None

            # ---- ① 跳空检查（先于信号）----
            gap_triggered = False
            if risk is not None and account.state(s).size != 0:
                trig = risk.stops.gap_trigger(s, i, bar)
                if trig is not None:
                    gap_triggered = True
                    execute_stop(s, bar, prev_close, trig, "跳空")

            # ---- ② 信号成交 ----
            if i > 0:
                sig = int(df["signal"].iloc[i - 1])
                if risk is not None:
                    risk.guard.update_signal(s, sig, day_idx)

                if latch_mode:
                    cur0 = account.state(s).size
                    is_fresh = sig != 0 and (cur0 == 0 or (cur0 > 0) != (sig > 0))
                    if is_fresh:
                        latch[s] = lots_for_date(bar.date).get(s, 0)
                    target = sig * latch.get(s, 0) if sig != 0 else 0
                else:
                    target = sig * lots_for_date(bar.date)[s] if sig != 0 else 0

                cur = account.state(s).size
                if risk is not None:
                    if liquidating_today:
                        target = 0  # 全平：次日开盘清仓
                    elif not risk.guard.allows_open(s, sig):
                        target = _trim_open_only(target, cur)  # 止损当日/重入锁定：只平不开
                    elif risk.fuse.opening_blocked(day_idx):
                        target = _trim_open_only(target, cur)  # 熔断暂停：只平不开
                    else:
                        factor = risk.fuse.degrade_factor(day_idx)
                        if factor < 1.0:
                            target = _degrade_target(target, cur, factor)

                if target != cur:
                    fills = brokers[s].fill(cur, target, bar)
                    closes = [t for t in fills if t.action == "CLOSE"]
                    opens = [t for t in fills if t.action == "OPEN"]

                    if risk is not None:
                        closes, opens = apply_limit_filter(s, bar, prev_close, closes, opens)

                    if closes:  # 平仓腿：保证金上永不拒绝（释放保证金后可支持开仓腿）
                        account.apply_trades(closes)
                        trades.extend(closes)
                        record_closes(closes)
                        if account.state(s).size == 0 and risk is not None:
                            risk.stops.clear(s)

                    if opens:
                        if risk is not None:
                            opens = apply_exposure_filter(s, bar, opens)
                        if risk is not None and opens:
                            opens = apply_participation_filter(s, bar, opens)
                        if opens:
                            needed = sum(account.open_margin(s, t.volume, t.price) for t in opens)
                            if needed <= account.available + 1e-9:
                                account.apply_trades(opens)
                                trades.extend(opens)
                                if risk is not None:
                                    register_entry_stops(s, i)
                            elif risk is not None and risk.cfg.margin_degrade:
                                # 降级执行（v1.3，⑧）：资金不足 → 按可负担手数开仓
                                avail0 = account.available
                                t0 = opens[0]
                                per_lot = account.open_margin(s, 1, t0.price)
                                affordable = int(avail0 // per_lot) if per_lot > 0 else 0
                                if affordable >= 1:
                                    nt = brokers[s].resize(bar, t0, affordable)
                                    account.apply_trades([nt])
                                    trades.append(nt)
                                    register_entry_stops(s, i)
                                    detail = (
                                        f"资金不足：目标 {t0.volume} 手 → 实开 {affordable} 手"
                                        f"（需 {needed:,.0f} / 可用 {avail0:,.0f} 元）"
                                    )
                                    risk_events.append(
                                        RiskEvent(bar.date, "margin_degrade", s, detail)
                                    )
                                else:
                                    account.record_event(bar.date, s, needed, avail0)
                                    refused += 1
                            else:
                                account.record_event(bar.date, s, needed, account.available)
                                refused += 1

                if latch_mode:
                    pos_after = account.state(s).size
                    if pos_after != 0:
                        latch[s] = abs(pos_after)

            # ---- ③ 盘中检查（信号之后；新仓次日才受保护；跳空已判定则不重复）----
            if risk is not None and account.state(s).size != 0 and not gap_triggered:
                trig = risk.stops.intrabar_trigger(s, i, bar)
                if trig is not None:
                    execute_stop(s, bar, prev_close, trig, "盘中")

            # ---- ④ 盯市 + 移动止损更新（收盘后，次日生效；收盘判定口径在此时登记）----
            account.mark_price(s, bar.close)
            if risk is not None and need_atr and account.state(s).size != 0:
                av = float(atr_full[s].iloc[i])
                risk.stops.update_after_close(s, bar.close, av)
            if risk is not None and account.state(s).size != 0:
                risk.stops.check_close(s, i, bar)

        # ---- ⑤ 日终：熔断判定 + 逐日风险记录 ----
        equity_now = account.equity
        extras: dict = {}
        if risk is not None:
            fuse_events = risk.fuse.day_end(
                day_idx=day_idx,
                date=d,
                equity=equity_now,
                prev_equity=prev_equity,
                peak=peak_equity,
                losing_streak=losing_streak,
                has_positions=any(account.state(x).size != 0 for x in symbols),
            )
            risk_events.extend(fuse_events)
            if liquidating_today:
                risk.fuse.finish_liquidate(day_idx)
            extras = {
                "fuse_state": risk.fuse.state_name(day_idx),
                "exposure_total": round(account.total_exposure, 2),
                **{f"exposure_{x}": round(account.exposure_notional(x), 2) for x in symbols},
                "exposure_warn": exposure_warnings(account, risk.cfg),
            }
        account.record_day(d, extra=extras if extras else None)
        peak_equity = max(peak_equity, equity_now)
        prev_equity = equity_now

    equity_df = pd.DataFrame(account.daily)
    risk_note = f" · 风控事件 {len(risk_events)} 次" if risk is not None else ""
    log.info(
        "组合回测完成：%d 品种 · %d 个交易日 · %d 笔成交 · 约束事件 %d 次%s",
        len(symbols),
        len(equity_df),
        len(trades),
        refused,
        risk_note,
    )
    return PortfolioResult(
        equity_df=equity_df,
        trades=trades,
        events=account.events,
        account=account,
        symbols=symbols,
        risk_events=risk_events,
    )
