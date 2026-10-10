"""一键入口：组合回测（v1.3：风控全开 + 裸奔对比）。

用法（在项目根目录下执行）：
    uv run python run_portfolio.py

配置见 ``config/config.yaml`` 的 ``portfolio`` / ``risk`` 段。
产出：主报告（含「风险与压力测试」章节）+ 7 张图 + 7 个明细 CSV；
``risk.enabled=true`` 时自动加跑「裸奔基线」（fixed 1 手 · 风控全关）并输出对比。
"""

import pandas as pd

from run_backtest import load_data  # 复用单品种数据缓存链路（缓存命中即不联网）
from src.analysis.risk_metrics import build_risk_daily
from src.analysis.stress import run_stress
from src.data.symbols import instrument_label, normalize_symbol
from src.engine.contracts import build_contracts
from src.engine.portfolio_engine import run_portfolio
from src.report.metrics import analyze as analyze_metrics
from src.report.portfolio_plot import (
    plot_portfolio_contribution,
    plot_portfolio_correlation,
    plot_portfolio_equity,
    plot_portfolio_margin,
)
from src.report.portfolio_report import (
    build_portfolio_report,
    correlation_matrix,
    save_portfolio_artifacts,
    symbol_summary,
)
from src.report.report import save_report
from src.report.risk_plot import plot_risk_compare, plot_risk_metrics, plot_risk_stress
from src.report.risk_report import build_risk_section, save_risk_artifacts
from src.strategy.registry import get_strategy
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger


def _headline_stats(result, initial_capital: float) -> dict:
    """对比实验汇总读数。"""
    m = analyze_metrics(result.equity_df, result.trades, initial_capital)
    rets = result.equity_df["equity"].astype(float).pct_change().dropna()
    worst_day = float(rets.min()) if len(rets) else 0.0
    vol = float(rets.std(ddof=0) * (252**0.5)) if len(rets) else 0.0
    kinds = [e.kind for e in result.risk_events]
    return {
        "total_return": m.total_return,
        "max_drawdown": m.max_drawdown,
        "sharpe": m.sharpe,
        "worst_day": worst_day,
        "volatility": vol,
        "trade_count": m.trade_count,
        "stop_events": sum(1 for k in kinds if k in ("stop_fixed", "stop_trailing", "take_profit")),
        "fuse_events": sum(
            1 for k in kinds if k in ("fuse_pause", "fuse_degrade", "fuse_liquidate")
        ),
    }


def main() -> None:
    log = get_logger("main")
    cfg = load_config()
    p = cfg.get("portfolio") or {}
    if not p or not p.get("symbols"):
        raise SystemExit("config.portfolio 未配置或缺少品种池（symbols）")
    d, out = cfg["data"], cfg["output"]
    risk_cfg = cfg.get("risk") or {}
    risk_enabled = bool(risk_cfg.get("enabled", False))
    initial_capital = float(p["initial_capital"])
    contracts = build_contracts(p.get("contracts") or {})

    # ① 数据：逐品种复用单品种缓存链路（品种防呆 + 缓存 + 清洗）
    data: dict[str, pd.DataFrame] = {}
    labels: dict[str, str] = {}
    strategy_specs: dict[str, tuple[type, dict]] = {}
    for item in p["symbols"]:
        raw_code = item["code"]
        symbol = normalize_symbol(raw_code)
        if symbol != raw_code:
            log.warning("config 品种代码 %r 结尾是字母 O，已自动按 %r 处理", raw_code, symbol)
        if symbol not in contracts:
            raise SystemExit(f"config.portfolio.contracts 缺少 {symbol} 的合约参数")
        data[symbol] = load_data(
            {
                "symbol": symbol,
                "start_date": d["start_date"],
                "end_date": d["end_date"],
                "raw_dir": d["raw_dir"],
                "clean_dir": d["clean_dir"],
            },
            log,
        )
        labels[symbol] = instrument_label(symbol)
        strategy_specs[symbol] = (get_strategy(item["strategy"]), item.get("params", {}))
    log.info(
        "品种池就绪：%d 个品种（%s）",
        len(data),
        " / ".join(labels[s].split("（")[0] for s in data),
    )

    # ② 头寸配置：atr_risk 的止损倍数与 risk.stops 对齐（两处口径一致）
    sizing_cfg = dict(p.get("sizing") or {})
    if sizing_cfg.get("mode") == "atr_risk" and "stop_atr_mult" not in sizing_cfg:
        fixed_cfg = (risk_cfg.get("stops") or {}).get("fixed") or {}
        sizing_cfg["stop_atr_mult"] = float(fixed_cfg.get("atr_mult", 2.0))

    # ③ 主口径：风控全开（risk 未启用时退化为 v1.2 行为）
    result = run_portfolio(
        data,
        strategy_specs,
        contracts,
        initial_capital=initial_capital,
        sizing_cfg=sizing_cfg,
        risk_cfg=risk_cfg if risk_enabled else None,
    )

    sizing = p.get("sizing") or {}
    mode = str(sizing.get("mode", "fixed"))
    sizing_desc = {
        "fixed": f"fixed（每品种 {sizing.get('lots', 1)} 手）",
        "equal_weight": (
            f"equal_weight（等权资金 · 保证金使用率上限 "
            f"{float(sizing.get('target_margin_usage', 0.6)):.0%}）"
        ),
        "inv_vol": f"inv_vol（波动率倒数 · {sizing.get('vol_window', 60)} 日窗口）",
        "atr_risk": (
            f"atr_risk（ATR 风险预算 · 单笔风险 {float(sizing.get('risk_pct', 0.015)):.1%}"
            f" · {sizing.get('atr_window', 20)} 日 ATR · 入场锁定）"
        ),
    }.get(mode, mode)

    # ④ 风控分析件：压力测试 / 逐日风险指标 / 裸奔对比
    stress = None
    risk_daily_df = None
    compare = None
    bare = None
    if risk_enabled:
        s_cfg = risk_cfg.get("stress") or {}
        stress = run_stress(
            result.equity_df,
            data,
            contracts,
            initial_capital=initial_capital,
            top_n=int(s_cfg.get("historical_top_n", 3)),
            gap_sigmas=tuple(s_cfg.get("gap_sigmas") or [2.0, 3.0]),
            limit_streak_days=int(s_cfg.get("limit_streak_days", 3)),
            mc_runs=int(s_cfg.get("mc_runs", 500)),
            seed=int(s_cfg.get("seed", 42)),
            limit_rates=(risk_cfg.get("price_limit") or {}).get("rates") or {},
        )
        m_cfg = risk_cfg.get("metrics") or {}
        risk_daily_df = build_risk_daily(
            result.equity_df,
            var_window=int(m_cfg.get("var_window", 250)),
            confidences=tuple(m_cfg.get("confidences") or [0.95, 0.99]),
            corr_window=int(m_cfg.get("corr_window", 60)),
            corr_warn=float(m_cfg.get("corr_warn", 0.5)),
            sectors=risk_cfg.get("sectors") or None,
        )
        # 对比实验：A 裸奔（fixed 1 手 · 风控全关） vs B 全开（上一步结果）
        bare = run_portfolio(
            data,
            strategy_specs,
            contracts,
            initial_capital=initial_capital,
            sizing_cfg={"mode": "fixed", "lots": 1},
            risk_cfg=None,
        )
        compare = {
            "bare": _headline_stats(bare, initial_capital),
            "risk": _headline_stats(result, initial_capital),
        }

    # ⑤ 报告（主报告 + 「风险与压力测试」章节）
    text = build_portfolio_report(
        result,
        labels=labels,
        initial_capital=initial_capital,
        start_date=d["start_date"],
        end_date=d["end_date"],
        sizing_desc=sizing_desc,
        risk_enabled=risk_enabled,
    )
    if risk_enabled:
        text = (
            text
            + "\n\n"
            + build_risk_section(
                result,
                risk_cfg=risk_cfg,
                stress=stress,
                risk_daily=risk_daily_df,
                compare=compare,
                initial_capital=initial_capital,
            )
        )
    report_path = save_report(text, resolve_path(f"{out['report_dir']}/portfolio_report.txt"))

    # ⑥ 图表
    fig_dir = out["figure_dir"]
    figures = {
        "组合权益与回撤": plot_portfolio_equity(
            result.equity_df, resolve_path(f"{fig_dir}/portfolio_equity.png")
        ),
        "相关性矩阵": plot_portfolio_correlation(
            correlation_matrix(result), resolve_path(f"{fig_dir}/portfolio_correlation.png")
        ),
        "品种盈亏贡献": plot_portfolio_contribution(
            symbol_summary(result, labels, initial_capital),
            resolve_path(f"{fig_dir}/portfolio_contribution.png"),
        ),
        "保证金占用": plot_portfolio_margin(
            result.equity_df, initial_capital, resolve_path(f"{fig_dir}/portfolio_margin.png")
        ),
    }
    if risk_enabled and bare is not None:
        figures["风控开/关对比"] = plot_risk_compare(
            bare.equity_df, result.equity_df, resolve_path(f"{fig_dir}/risk_compare.png")
        )
        figures["压力测试"] = plot_risk_stress(stress, resolve_path(f"{fig_dir}/risk_stress.png"))
        figures["风险指标"] = plot_risk_metrics(
            risk_daily_df, resolve_path(f"{fig_dir}/risk_metrics.png")
        )

    # ⑦ 明细 CSV
    csv_paths = save_portfolio_artifacts(
        result,
        labels=labels,
        initial_capital=initial_capital,
        out_dir=resolve_path(out["report_dir"]),
    )
    if risk_enabled:
        csv_paths.update(
            save_risk_artifacts(
                result, stress, risk_daily_df, compare, out_dir=resolve_path(out["report_dir"])
            )
        )

    print()
    print(text)
    for name, path in figures.items():
        log.info("图表已保存：%s -> %s", name, path)
    for name, path in csv_paths.items():
        log.info("明细已保存：%s -> %s", name, path)
    log.info("组合报告已生成：%s", report_path)
    if risk_enabled and compare:
        b, r = compare["bare"], compare["risk"]
        log.info(
            "风控开/关对比：累计收益 %.2f%% → %.2f%%；最大回撤 %.2f%% → %.2f%%；"
            "最差单日 %.2f%% → %.2f%%",
            b["total_return"] * 100,
            r["total_return"] * 100,
            b["max_drawdown"] * 100,
            r["max_drawdown"] * 100,
            b["worst_day"] * 100,
            r["worst_day"] * 100,
        )


if __name__ == "__main__":
    main()
