"""参数扫描：双均线快/慢参数网格 → 参数-绩效对比表（CSV + 控制台）。

用法（在项目根目录下执行，需先跑过一次 run_backtest.py 生成数据缓存）：
    uv run python scripts/run_param_scan.py
"""

import itertools
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.data import store
from src.engine.broker import Broker
from src.engine.engine import BacktestEngine
from src.engine.portfolio import Portfolio
from src.report import metrics as metrics_mod
from src.strategy.dual_ma import DualMAStrategy
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger

# 参数网格（可按需调整）
FAST_LIST = [5, 10, 15]
SLOW_LIST = [20, 40, 60]


def main() -> None:
    log = get_logger("param_scan")
    cfg = load_config()
    d, b = cfg["data"], cfg["backtest"]

    clean_path = resolve_path(f"{d['clean_dir']}/{d['symbol']}_clean.csv")
    if not store.exists(clean_path):
        raise SystemExit("未找到数据缓存，请先运行：uv run python run_backtest.py")
    df = store.load_csv(clean_path)
    df["date"] = pd.to_datetime(df["date"])

    rows = []
    for fast, slow in itertools.product(FAST_LIST, SLOW_LIST):
        if fast >= slow:
            continue
        strategy = DualMAStrategy({"fast": fast, "slow": slow})
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
                "fast": fast,
                "slow": slow,
                "total_return": round(m.total_return, 4),
                "max_drawdown": round(m.max_drawdown, 4),
                "sharpe": round(m.sharpe, 3),
                "win_rate": round(m.win_rate, 4),
                "trade_count": m.trade_count,
                "commission": round(m.total_commission, 2),
            }
        )

    result = pd.DataFrame(rows).sort_values("sharpe", ascending=False).reset_index(drop=True)
    out_path = resolve_path("output/reports/param_scan_dual_ma.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    print()
    print(result.to_string(index=False))
    log.info("参数对比表已保存：%s", out_path)


if __name__ == "__main__":
    main()
