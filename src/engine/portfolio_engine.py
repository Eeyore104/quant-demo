"""组合引擎（v1.2）：多品种并行 · 共享资金池 · 保证金约束 · 逐日盯市。

旁路原则：单品种引擎（``engine.py``）与其调用链**零改动**；本模块在引擎上层
实现跨品种事件循环。成交约定与单品种一致：**T 日收盘信号 → T+1 开盘价 ± 滑点**。

处理顺序：同日事件按**品种池配置顺序**处理（同日多品种竞争资金时先到先得，
该口径写入组合报告）；平仓腿永不拒绝、开仓腿保证金不足整笔拒绝并记事件。
"""

import itertools
from dataclasses import dataclass

import pandas as pd

from ..utils.logger import get_logger
from .account import ConstraintEvent, PortfolioAccount
from .broker import Broker
from .contracts import ContractSpec
from .engine import _to_bar
from .sizing import compute_target_lots

log = get_logger("engine")


@dataclass
class PortfolioResult:
    """组合回测结果。"""

    equity_df: pd.DataFrame  # 逐日：权益/现金/保证金/可用资金 + 逐品种盈亏（pnl_<品种> 列）
    trades: list  # 全部成交（Trade，含 symbol）
    events: list[ConstraintEvent]  # 拒绝开仓事件
    account: PortfolioAccount  # 共享账户（含逐品种状态）
    symbols: list[str]  # 品种池（配置顺序）


def run_portfolio(
    data: dict[str, pd.DataFrame],
    strategy_specs: dict[str, tuple[type, dict]],
    contracts: dict[str, ContractSpec],
    *,
    initial_capital: float,
    sizing_cfg: dict | None = None,
) -> PortfolioResult:
    """组合回测主流程：逐品种信号 → 合并事件流 → 共享账户逐日推进。"""
    symbols = list(data.keys())
    sizing_cfg = dict(sizing_cfg or {})

    # ① 逐品种信号预计算（复用既有策略接口，零改动）
    dfs: dict[str, pd.DataFrame] = {}
    for s in symbols:
        cls, params = strategy_specs[s]
        out = cls(params).generate_signals(data[s])
        if "signal" not in out.columns:
            raise ValueError(f"策略未生成 signal 列：{s}")
        dfs[s] = out

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

    # ③ 手数：fixed 模式为常量；equal_weight / inv_vol 按日懒计算（截至信号日收盘数据）
    mode = str(sizing_cfg.get("mode", "fixed"))
    max_lots = int(sizing_cfg.get("max_lots_per_symbol", 10))
    fixed_lots = (
        {s: max(0, min(int(sizing_cfg.get("lots", 1)), max_lots)) for s in symbols}
        if mode == "fixed"
        else None
    )
    lots_cache: dict[object, dict[str, int]] = {}

    def lots_for_date(d) -> dict[str, int]:
        if fixed_lots is not None:
            return fixed_lots
        if d not in lots_cache:
            prices: dict[str, float] = {}
            closes_map: dict[str, pd.Series] = {}
            for s in symbols:
                prior = dfs[s][dfs[s]["date"] < d]
                if len(prior) > 0:
                    closes_map[s] = prior["close"]
                    prices[s] = float(prior["close"].iloc[-1])
            lots_cache[d] = compute_target_lots(
                mode, contracts, prices, closes_map, float(initial_capital), sizing_cfg
            )
        return lots_cache[d]

    # ④ 事件循环：先平（永不拒绝）→ 再开（保证金检查）→ 盯市 → 日终记录
    trades: list = []
    refused = 0
    for d, group in itertools.groupby(events, key=lambda e: e[0]):
        for _, _, s, i in group:
            df = dfs[s]
            bar = _to_bar(df.iloc[i])
            if i > 0:
                sig = int(df["signal"].iloc[i - 1])
                target = sig * lots_for_date(bar.date)[s] if sig != 0 else 0
                cur = account.state(s).size
                if target != cur:
                    fills = brokers[s].fill(cur, target, bar)
                    closes = [t for t in fills if t.action == "CLOSE"]
                    opens = [t for t in fills if t.action == "OPEN"]
                    if closes:  # 平仓腿：永不拒绝（释放保证金后可支持开仓腿）
                        account.apply_trades(closes)
                        trades.extend(closes)
                    if opens:
                        needed = sum(account.open_margin(s, t.volume, t.price) for t in opens)
                        if needed <= account.available + 1e-9:
                            account.apply_trades(opens)
                            trades.extend(opens)
                        else:
                            account.record_event(bar.date, s, needed, account.available)
                            refused += 1
            account.mark_price(s, bar.close)
        account.record_day(d)

    equity_df = pd.DataFrame(account.daily)
    log.info(
        "组合回测完成：%d 品种 · %d 个交易日 · %d 笔成交 · 约束事件 %d 次",
        len(symbols),
        len(equity_df),
        len(trades),
        refused,
    )
    return PortfolioResult(
        equity_df=equity_df,
        trades=trades,
        events=account.events,
        account=account,
        symbols=symbols,
    )
