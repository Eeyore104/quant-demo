"""参数扫描：按 ``config.strategy.grid`` 遍历任意策略的参数网格 → 对比表（CSV + 控制台）。

用法（在项目根目录下执行，需先跑过一次 run_backtest.py 生成数据缓存）：

    uv run python scripts/run_param_scan.py          # 扫描 config.strategy.name 指定的策略
    uv run python scripts/run_param_scan.py donchian # 也可显式指定策略名

因为网格统一放在配置里、策略实例由注册表创建，所以**新增策略无需改本脚本**。
"""

import itertools
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.data import store
from src.data.symbols import instrument_label, normalize_symbol
from src.engine.broker import Broker
from src.engine.engine import BacktestEngine
from src.engine.portfolio import Portfolio
from src.report import metrics as metrics_mod
from src.report.overview import build_overview
from src.report.plotter import plot_param_heatmap
from src.strategy.registry import get_strategy
from src.utils.config_loader import load_config, load_symbol_params, resolve_path
from src.utils.logger import get_logger
from src.utils.symbol_params import DEFAULT_SRC, apply_symbol_params


def _build_grid(grid: dict) -> list[dict]:
    """把 ``{参数: [取值...]}`` 展开为参数组合列表（等价 itertools.product）。"""
    keys = list(grid)
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[k] for k in keys))
    ]


def _is_valid(combo: dict) -> bool:
    """跳过非法组合：如双均线要求 fast < slow。"""
    if "fast" in combo and "slow" in combo and combo["fast"] >= combo["slow"]:
        return False
    return True


def main() -> None:
    log = get_logger("param_scan")
    cfg = load_config()
    d, b, s = cfg["data"], cfg["backtest"], cfg["strategy"]
    raw_symbol = d["symbol"]
    d["symbol"] = normalize_symbol(raw_symbol)
    if d["symbol"] != raw_symbol:
        log.warning("config 品种代码 %r 结尾是字母 O，已自动按 %r 处理", raw_symbol, d["symbol"])

    # 策略名：命令行优先，否则取 config.strategy.name
    name = sys.argv[1] if len(sys.argv) > 1 else s["name"]
    strategy_cls = get_strategy(name)
    grid = s.get("grid", {}).get(name)
    if not grid:
        raise SystemExit(f"config.strategy.grid 缺少策略 {name} 的网格，请在 config.yaml 补充")

    clean_path = resolve_path(f"{d['clean_dir']}/{d['symbol']}_clean.csv")
    if not store.exists(clean_path):
        raise SystemExit("未找到数据缓存，请先运行：uv run python run_backtest.py")
    df = store.load_csv(clean_path)
    df["date"] = pd.to_datetime(df["date"])
    label = instrument_label(d["symbol"])

    # 品种参数：与 run_backtest 同一三层解析（手动 → 自动全量表 → 默认 + 告警）
    resolved, src = apply_symbol_params(b, d["symbol"], load_symbol_params())
    if src == DEFAULT_SRC:
        log.warning(
            "⚠️ 品种 %s 未收录（手动登记表与自动全量表均无），使用默认参数"
            "（tick %s / 乘数 %s / 手续费 %s）——扫描结果口径可能失真",
            d["symbol"],
            resolved["tick_size"],
            resolved["contract_multiplier"],
            resolved["commission_per_lot"],
        )
    else:
        log.info("品种参数来源：%s → %s", src, resolved)

    rows = []
    for combo in _build_grid(grid):
        if not _is_valid(combo):
            continue
        strategy = strategy_cls(combo)
        broker = Broker(
            commission_per_lot=b["commission_per_lot"],
            slippage_ticks=b["slippage_ticks"],
            tick_size=b["tick_size"],
            contract_multiplier=b["contract_multiplier"],
        )
        portfolio = Portfolio(b["initial_capital"], contract_multiplier=b["contract_multiplier"])
        engine = BacktestEngine(
            df, strategy, broker=broker, portfolio=portfolio, position_size=b["position_size"]
        )
        equity_df, trades = engine.run()
        m = metrics_mod.analyze(equity_df, trades, b["initial_capital"])
        rows.append(
            {
                **combo,
                "total_return": round(m.total_return, 4),
                "max_drawdown": round(m.max_drawdown, 4),
                "sharpe": round(m.sharpe, 3),
                "win_rate": round(m.win_rate, 4),
                "trade_count": m.trade_count,
                "commission": round(m.total_commission, 2),
                "symbol": d["symbol"],
            }
        )

    result = pd.DataFrame(rows).sort_values("sharpe", ascending=False).reset_index(drop=True)
    out_path = resolve_path(f"output/reports/param_scan_{name}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    print()
    print(f"品种：{label} | 策略：{name}")
    print(result.to_string(index=False))
    log.info("参数对比表已保存：%s", out_path)

    heatmap_path = plot_param_heatmap(
        result, resolve_path(f"output/figures/param_heatmap_{name}.png"), label=label
    )
    if heatmap_path:
        log.info("参数扫描热力图已保存：%s", heatmap_path)

    # 图表总览刷新（失败不影响扫描结果）
    try:
        overview_paths = build_overview(
            resolve_path(cfg["output"]["figure_dir"]),
            resolve_path(cfg["output"]["figure_dir"]).parent,
        )
    except Exception:  # noqa: BLE001
        overview_paths = {}
        log.warning("图表总览生成失败（不影响扫描结果）", exc_info=True)
    for ov_name, ov_path in overview_paths.items():
        log.info("总览已刷新：%s -> %s", ov_name, ov_path)


if __name__ == "__main__":
    main()
