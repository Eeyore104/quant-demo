"""一键入口：数据 → 清洗 → 策略 → 回测 → 报告。

用法（在项目根目录下执行）：
    uv run python run_backtest.py
"""

import pandas as pd

from src.data import cleaner, loader, store
from src.engine.broker import Broker
from src.engine.engine import BacktestEngine
from src.engine.portfolio import Portfolio
from src.engine.walkforward import run_oos
from src.report import metrics as metrics_mod
from src.report.plotter import plot_drawdown, plot_equity, plot_monthly_heatmap, plot_signals
from src.report.report import build_oos_table, build_report_text, save_report
from src.strategy.registry import get_strategy
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger


def load_data(d: dict, log):
    """数据获取（缓存命中即不重复联网）→ 清洗。"""
    raw_path = resolve_path(f"{d['raw_dir']}/{d['symbol']}.csv")
    clean_path = resolve_path(f"{d['clean_dir']}/{d['symbol']}_clean.csv")

    if store.exists(clean_path):
        log.info("清洗数据缓存命中：%s", clean_path)
        df = store.load_csv(clean_path)
        df["date"] = pd.to_datetime(df["date"])
        return df

    if store.exists(raw_path):
        log.info("原始数据缓存命中：%s", raw_path)
        raw = store.load_csv(raw_path)
    else:
        raw = loader.fetch_daily(d["symbol"], d["start_date"], d["end_date"])
        store.save_csv(raw, raw_path)

    df = cleaner.clean(raw)
    store.save_csv(df, clean_path)
    return df


def main() -> None:
    log = get_logger("main")
    cfg = load_config()
    d, s, b, out = cfg["data"], cfg["strategy"], cfg["backtest"], cfg["output"]

    # ① 数据
    df = load_data(d, log)
    log.info("数据就绪：%d 根 bar", len(df))

    # ② 策略（自动注册表：新增策略 = 新增一个文件，此处无需改动）
    name = s["name"]
    params = s["params"].get(name, {})
    strategy = get_strategy(name)(params)
    log.info("策略: %s %s | 初始资金: %.0f 元", name, params, b["initial_capital"])

    # ③ 回测（工厂函数创建全新实例，供主回测与样本外各自复用）
    def make_broker() -> Broker:
        return Broker(
            commission_per_lot=b["commission_per_lot"],
            slippage_ticks=b["slippage_ticks"],
            tick_size=b["tick_size"],
            contract_multiplier=b["contract_multiplier"],
        )

    def make_portfolio() -> Portfolio:
        return Portfolio(b["initial_capital"], contract_multiplier=b["contract_multiplier"])

    engine = BacktestEngine(
        df,
        strategy,
        broker=make_broker(),
        portfolio=make_portfolio(),
        position_size=b["position_size"],
    )
    equity_df, trades = engine.run()

    # ④ 绩效 + 图表 + 报告
    m = metrics_mod.analyze(equity_df, trades, b["initial_capital"])
    eq_png = plot_equity(equity_df, resolve_path(f"{out['figure_dir']}/equity.png"))
    sig_png = plot_signals(engine.df, trades, resolve_path(f"{out['figure_dir']}/signals.png"))
    dd_png = plot_drawdown(equity_df, resolve_path(f"{out['figure_dir']}/drawdown.png"))
    mh_png = plot_monthly_heatmap(
        equity_df, resolve_path(f"{out['figure_dir']}/monthly_heatmap.png")
    )

    params_desc = ", ".join(f"{k}={v}" for k, v in params.items())
    header = [
        f"品种       : {d['symbol']}（{d['start_date']} ~ {d['end_date']}）",
        f"策略       : {name}({params_desc})",
        f"初始资金   : {b['initial_capital']:.0f} 元 / 固定 {b['position_size']} 手",
        f"成本设定   : 手续费 {b['commission_per_lot']} 元/手，滑点 {b['slippage_ticks']} 跳",
    ]
    # ④.1 样本外验证（旁路增强：engine.run() 零改动，仅在上层编排）
    extra_sections: list[str] = []
    oos_cfg = b.get("oos", {})
    if oos_cfg.get("enabled", False):
        grid = s.get("grid", {}).get(name, {})
        if grid:
            oos = run_oos(
                df,
                get_strategy(name),
                grid,
                broker_factory=make_broker,
                portfolio_factory=make_portfolio,
                position_size=b["position_size"],
                split_date=oos_cfg.get("split_date"),
                ratio=oos_cfg.get("ratio", 0.7),
                metric=oos_cfg.get("metric", "sharpe"),
            )
            if oos.skipped:
                log.warning("样本外验证已跳过（训练段为空，请检查 backtest.oos.split_date）")
            else:
                extra_sections.append(build_oos_table(oos))
        else:
            log.warning("样本外验证跳过：config.strategy.grid 缺少 %s 的网格", name)

    figures = {
        "权益曲线图": eq_png,
        "买卖点图": sig_png,
        "回撤区间图": dd_png,
        "月度收益热力图": mh_png,
    }
    text = build_report_text(m, header, figures, extra_sections=extra_sections)
    report_path = save_report(text, resolve_path(f"{out['report_dir']}/backtest_report.txt"))

    print()
    print(text)
    log.info("报告已生成：%s", report_path)


if __name__ == "__main__":
    main()
