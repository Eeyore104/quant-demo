"""一键入口：数据 → 清洗 → 策略 → 回测 → 报告（样本外验证 + 过拟合体检）。

用法（在项目根目录下执行）：
    uv run python run_backtest.py
"""

import pandas as pd

from src.analysis.health import run_health_check
from src.analysis.oos_usage import bump_usage
from src.data import cleaner, loader, store
from src.data.symbols import instrument_label, normalize_symbol
from src.engine.broker import Broker
from src.engine.engine import BacktestEngine
from src.engine.portfolio import Portfolio
from src.engine.walkforward import run_oos
from src.report import metrics as metrics_mod
from src.report.plotter import (
    plot_drawdown,
    plot_equity,
    plot_health_cost,
    plot_health_monte_carlo,
    plot_health_neighborhood,
    plot_monthly_heatmap,
    plot_signals,
)
from src.report.report import build_health_table, build_oos_table, build_report_text, save_report
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
    raw_symbol = d["symbol"]
    d["symbol"] = normalize_symbol(raw_symbol)
    if d["symbol"] != raw_symbol:
        log.warning("config 品种代码 %r 结尾是字母 O，已自动按 %r 处理", raw_symbol, d["symbol"])
    label = instrument_label(d["symbol"])

    # ① 数据
    df = load_data(d, log)
    log.info("数据就绪：%s · %d 根 bar", label, len(df))

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
    eq_png = plot_equity(equity_df, resolve_path(f"{out['figure_dir']}/equity.png"), label=label)
    sig_png = plot_signals(
        engine.df, trades, resolve_path(f"{out['figure_dir']}/signals.png"), label=label
    )
    dd_png = plot_drawdown(
        equity_df, resolve_path(f"{out['figure_dir']}/drawdown.png"), label=label
    )
    mh_png = plot_monthly_heatmap(
        equity_df, resolve_path(f"{out['figure_dir']}/monthly_heatmap.png"), label=label
    )

    params_desc = ", ".join(f"{k}={v}" for k, v in params.items())
    header = [
        f"品种       : {label}",
        f"区间       : {d['start_date']} ~ {d['end_date']}",
        f"策略       : {name}({params_desc})",
        f"初始资金   : {b['initial_capital']:.0f} 元 / 固定 {b['position_size']} 手",
        f"成本设定   : 手续费 {b['commission_per_lot']} 元/手，滑点 {b['slippage_ticks']} 跳",
    ]

    # ④.1 样本外验证（旁路增强：engine.run() 零改动，仅在上层编排）
    extra_sections: list[str] = []
    oos_result = None
    oos_usage_count: int | None = None
    oos_cfg = b.get("oos", {})
    if oos_cfg.get("enabled", False):
        grid = s.get("grid", {}).get(name, {})
        if grid:
            oos_result = run_oos(
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
            if oos_result.skipped:
                log.warning("样本外验证已跳过（训练段为空，请检查 backtest.oos.split_date）")
            else:
                extra_sections.append(build_oos_table(oos_result, label=label))
                split_desc = oos_cfg.get("split_date") or f"ratio={oos_cfg.get('ratio', 0.7)}"
                oos_usage_count = bump_usage(
                    resolve_path("output/oos_usage.json"), f"{d['symbol']}|{name}|{split_desc}"
                )
                if oos_usage_count > 1:
                    log.info(
                        "样本外使用次数登记：该「品种|策略|分段」已使用 %d 次"
                        "（>3 次将影响体检判定）",
                        oos_usage_count,
                    )
        else:
            log.warning("样本外验证跳过：config.strategy.grid 缺少 %s 的网格", name)

    figures = {
        "权益曲线图": eq_png,
        "买卖点图": sig_png,
        "回撤区间图": dd_png,
        "月度收益热力图": mh_png,
    }

    # ④.2 过拟合体检（v1.1：四件套 + DSR；单项失败不拖垮主流程）
    hc_cfg = cfg.get("health_check") or {}
    if hc_cfg.get("enabled", False):
        health = run_health_check(
            df=df,
            df_with_signal=engine.df,
            metrics=m,
            equity_df=equity_df,
            strategy_cls=get_strategy(name),
            strategy_params=params,
            grid=s.get("grid", {}).get(name),
            broker_factory=make_broker,
            portfolio_factory=make_portfolio,
            broker_kwargs={
                "commission_per_lot": b["commission_per_lot"],
                "slippage_ticks": b["slippage_ticks"],
                "tick_size": b["tick_size"],
                "contract_multiplier": b["contract_multiplier"],
            },
            initial_capital=b["initial_capital"],
            position_size=b["position_size"],
            contract_multiplier=b["contract_multiplier"],
            oos=oos_result,
            oos_usage=oos_usage_count,
            cfg=hc_cfg,
        )
        extra_sections.append(build_health_table(health, label=label))

        if health.psr is not None and not health.psr.scan.empty:
            nb_png = plot_health_neighborhood(
                health.psr.scan,
                params,
                resolve_path(f"{out['figure_dir']}/health_param_neighborhood.png"),
                label=label,
            )
            if nb_png:
                figures["参数邻域热力图"] = nb_png
            nb_csv = resolve_path(f"{out['report_dir']}/health_param_neighborhood_{name}.csv")
            nb_csv.parent.mkdir(parents=True, exist_ok=True)
            health.psr.scan.to_csv(nb_csv, index=False, encoding="utf-8-sig")
            log.info("参数邻域明细已保存：%s", nb_csv)
        if health.mc is not None:
            figures["蒙特卡洛对照图"] = plot_health_monte_carlo(
                health.mc,
                resolve_path(f"{out['figure_dir']}/health_monte_carlo.png"),
                label=label,
            )
        if health.cs is not None and health.cs.rows:
            figures["成本敏感性图"] = plot_health_cost(
                health.cs,
                resolve_path(f"{out['figure_dir']}/health_cost_sensitivity.png"),
                label=label,
            )
            cs_csv = resolve_path(f"{out['report_dir']}/health_cost_sensitivity_{name}.csv")
            cs_csv.parent.mkdir(parents=True, exist_ok=True)
            health.cs.table.to_csv(cs_csv, index=False, encoding="utf-8-sig")
            log.info("成本敏感性明细已保存：%s", cs_csv)
    else:
        log.info("过拟合体检未启用（config.health_check.enabled = false）")

    text = build_report_text(m, header, figures, extra_sections=extra_sections)
    report_path = save_report(text, resolve_path(f"{out['report_dir']}/backtest_report.txt"))

    print()
    print(text)
    log.info("报告已生成：%s", report_path)


if __name__ == "__main__":
    main()
