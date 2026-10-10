"""一键入口：组合回测（多品种 · 共享资金池 · 保证金约束 · 逐日盯市）。

用法（在项目根目录下执行）：
    uv run python run_portfolio.py

配置见 ``config/config.yaml`` 的 ``portfolio`` 段（品种池 / 头寸规模 / 合约参数表）。
"""

import pandas as pd

from run_backtest import load_data  # 复用单品种数据缓存链路（缓存命中即不联网）
from src.data.symbols import instrument_label, normalize_symbol
from src.engine.contracts import build_contracts
from src.engine.portfolio_engine import run_portfolio
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
from src.strategy.registry import get_strategy
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger


def main() -> None:
    log = get_logger("main")
    cfg = load_config()
    p = cfg.get("portfolio") or {}
    if not p or not p.get("symbols"):
        raise SystemExit("config.portfolio 未配置或缺少品种池（symbols）")
    d, out = cfg["data"], cfg["output"]
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

    # ② 组合回测（共享资金池）
    result = run_portfolio(
        data,
        strategy_specs,
        contracts,
        initial_capital=initial_capital,
        sizing_cfg=p.get("sizing") or {},
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
    }.get(mode, mode)

    # ③ 报告 + 图表 + 明细 CSV
    text = build_portfolio_report(
        result,
        labels=labels,
        initial_capital=initial_capital,
        start_date=d["start_date"],
        end_date=d["end_date"],
        sizing_desc=sizing_desc,
    )
    report_path = save_report(text, resolve_path(f"{out['report_dir']}/portfolio_report.txt"))

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
    csv_paths = save_portfolio_artifacts(
        result,
        labels=labels,
        initial_capital=initial_capital,
        out_dir=resolve_path(out["report_dir"]),
    )

    print()
    print(text)
    for name, path in figures.items():
        log.info("图表已保存：%s -> %s", name, path)
    for name, path in csv_paths.items():
        log.info("明细已保存：%s -> %s", name, path)
    log.info("组合报告已生成：%s", report_path)


if __name__ == "__main__":
    main()
