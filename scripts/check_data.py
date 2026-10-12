"""数据质量校验入口（v1.4）。

用法（项目根目录）：
    uv run python scripts/check_data.py            # 检查组合品种池（config.portfolio.symbols）
    uv run python scripts/check_data.py --all      # 检查 data/clean 下全部品种
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.data import quality, store
from src.data.symbols import instrument_label
from src.results import db as results_db
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger


def main() -> None:
    log = get_logger("check_data")
    cfg = load_config()
    d = cfg["data"]
    q_cfg = cfg.get("quality") or {}
    clean_dir = resolve_path(d["clean_dir"])
    raw_dir = resolve_path(d["raw_dir"])

    if "--all" in sys.argv:
        pairs = [
            (f.stem.replace("_clean", ""), f) for f in sorted(clean_dir.glob("*_clean.csv"))
        ]
    else:
        symbols = [item["code"] for item in ((cfg.get("portfolio") or {}).get("symbols") or [])]
        if not symbols:
            raise SystemExit("config.portfolio.symbols 为空；可用 --all 检查全部缓存品种")
        pairs = [(s, clean_dir / f"{s}_clean.csv") for s in symbols]

    items: list[tuple[str, pd.DataFrame]] = []
    labels: dict[str, str] = {}
    for symbol, path in pairs:
        if not store.exists(path):
            log.warning(
                "跳过 %s：清洗数据不存在（先运行 run_backtest.py / run_portfolio.py）", symbol
            )
            continue
        df = store.load_csv(path)
        df["date"] = pd.to_datetime(df["date"])
        items.append((symbol, df))
        labels[symbol] = instrument_label(symbol)
    if not items:
        raise SystemExit("没有可检查的数据缓存（先运行一次回测生成缓存）")

    trade_dates = quality.load_trade_dates(raw_dir)
    report = quality.check_dataset(
        items,
        trade_dates=trade_dates,
        gap_max_days=int(q_cfg.get("gap_max_days", 10)),
        jump_pct_warn=float(q_cfg.get("jump_pct_warn", 0.15)),
        recent_days=int(q_cfg.get("recent_days", 10)),
    )
    md = quality.render_markdown(report, labels=labels)

    out_path = resolve_path(f"{cfg['output']['report_dir']}/data_quality.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    log.info("数据质量报告已保存：%s", out_path)

    storage_cfg = cfg.get("storage") or {}
    if storage_cfg.get("enabled", True):
        try:
            conn = results_db.open_db(
                resolve_path(storage_cfg.get("db_path", "output/db/quant.sqlite3"))
            )
            n = results_db.record_quality_rows(conn, report.batch_id, report.rows)
            conn.close()
            log.info("质量结果已登记：%d 行 -> batch %s", n, report.batch_id)
        except Exception:
            log.warning("质量结果登记失败（不影响检查结果）", exc_info=True)

    print(md)
    counts = report.counts()
    log.info(
        "检查完成：%d 品种 | 警告 %d · 错误 %d | 总判定：%s",
        len(report.symbols),
        counts["warn"],
        counts["error"],
        "通过" if report.ok() else "存在问题",
    )
    if not report.ok():
        sys.exit(1)


if __name__ == "__main__":
    main()
