"""命令行查看历史回测记录（v1.4）。

用法（项目根目录）：
    uv run python scripts/list_runs.py               # 最近 10 条
    uv run python scripts/list_runs.py --limit 50    # 指定条数
    uv run python scripts/list_runs.py --kind portfolio
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.results import db as results_db
from src.utils.config_loader import load_config, resolve_path


def main() -> None:
    args = sys.argv[1:]
    limit = 10
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    kind = args[args.index("--kind") + 1] if "--kind" in args else None

    cfg = load_config()
    storage = cfg.get("storage") or {}
    db_path = resolve_path(storage.get("db_path", "output/db/quant.sqlite3"))
    if not db_path.exists():
        raise SystemExit("结果库不存在——先运行一次回测（run_backtest.py / run_portfolio.py）")

    conn = results_db.open_db(db_path)
    runs = results_db.list_runs(conn, limit=limit, kind=kind)
    conn.close()
    if not runs:
        print("暂无回测记录。")
        return

    df = pd.DataFrame(runs)[
        [
            "id",
            "created_at",
            "kind",
            "symbol",
            "total_return",
            "max_drawdown",
            "sharpe",
            "trade_count",
            "health_verdict",
            "report_html",
        ]
    ].rename(
        columns={
            "id": "ID",
            "created_at": "时间",
            "kind": "类型",
            "symbol": "品种",
            "total_return": "收益",
            "max_drawdown": "回撤",
            "sharpe": "夏普",
            "trade_count": "交易数",
            "health_verdict": "体检",
            "report_html": "报告",
        }
    )
    for col in ("收益", "回撤"):
        df[col] = df[col].map(lambda v: "—" if pd.isna(v) else f"{v:.2%}")
    df["夏普"] = df["夏普"].map(lambda v: "—" if pd.isna(v) else f"{v:.2f}")
    df["类型"] = df["类型"].map({"portfolio": "组合", "backtest": "单品种"})
    df["品种"] = df["品种"].fillna("（组合池）")
    df["报告"] = df["报告"].map(
        lambda v: Path(str(v)).name if isinstance(v, str) and v else "—"
    )
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
