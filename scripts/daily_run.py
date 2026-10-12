"""每日流水线（v1.4）：数据更新 → 跑批 → 报告 → 登记 → 通知。

用法（项目根目录）：
    uv run python scripts/daily_run.py              # 组合跑批（默认，config.portfolio.symbols）
    uv run python scripts/daily_run.py --backtest   # 单品种跑批（config.data.symbol）
    uv run python scripts/daily_run.py --force      # 非交易日也强制运行
    uv run python scripts/daily_run.py --no-notify  # 本次不发通知

退出码：0 成功（含「非交易日跳过」）；1 失败（失败时发企业微信告警）。
"""

import datetime as dt
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.data import cleaner, loader, quality, store
from src.data.symbols import instrument_label
from src.results import db as results_db
from src.utils import logger as logger_mod
from src.utils import notify
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger


def is_trading_day(cfg: dict, today: dt.date | None = None) -> bool:
    """交易日判断：周末直接排除；交易日历可得时严格判断，否则保守放行。"""
    today = today or dt.date.today()
    if today.weekday() >= 5:
        return False
    trade_dates = quality.load_trade_dates(resolve_path(cfg["data"]["raw_dir"]))
    if trade_dates:
        return today.strftime("%Y-%m-%d") in trade_dates
    return True


def refresh_data(cfg: dict, symbols: list[str], log) -> None:
    """重拉全区间覆盖缓存（简单可靠；顺带吸收主连历史修正）。"""
    d = cfg["data"]
    end = dt.date.today().strftime("%Y-%m-%d")
    for sym in symbols:
        raw = loader.fetch_daily(sym, d["start_date"], end)
        store.save_csv(raw, resolve_path(f"{d['raw_dir']}/{sym}.csv"))
        df = cleaner.clean(raw)
        store.save_csv(df, resolve_path(f"{d['clean_dir']}/{sym}_clean.csv"))
        log.info("数据已更新：%s（%d 行，最新 %s）", sym, len(df), str(df["date"].iloc[-1])[:10])


def run_quality(cfg: dict, symbols: list[str], log) -> quality.QualityReport:
    """对指定品种做数据质量检查：保存报告 md + 入库。"""
    d = cfg["data"]
    q_cfg = cfg.get("quality") or {}
    items: list[tuple[str, pd.DataFrame]] = []
    labels: dict[str, str] = {}
    for sym in symbols:
        path = resolve_path(f"{d['clean_dir']}/{sym}_clean.csv")
        if not store.exists(path):
            log.warning("质量检查跳过 %s：清洗数据不存在", sym)
            continue
        df = store.load_csv(path)
        df["date"] = pd.to_datetime(df["date"])
        items.append((sym, df))
        labels[sym] = instrument_label(sym)
    trade_dates = quality.load_trade_dates(resolve_path(d["raw_dir"]))
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
        conn = results_db.open_db(
            resolve_path(storage_cfg.get("db_path", "output/db/quant.sqlite3"))
        )
        results_db.record_quality_rows(conn, report.batch_id, report.rows)
        conn.close()
        log.info("质量结果已登记：batch %s（%d 行）", report.batch_id, len(report.rows))
    return report


def main() -> None:
    args = sys.argv[1:]
    log_path = logger_mod.enable_file_logging(PROJECT_ROOT / "logs")
    log = get_logger("daily_run")
    log.info("每日流水线启动（日志文件：%s）", log_path)

    cfg = load_config()
    today = dt.date.today().strftime("%Y-%m-%d")
    notify_cfg = cfg.get("notify") or {}
    notify_on = notify_cfg.get("enabled", True) and "--no-notify" not in args
    step = "交易日判断"

    def fail(error: str) -> None:
        log.error("流水线失败于「%s」：%s", step, error)
        if notify_on and notify_cfg.get("on_failure", True):
            notify.send_markdown(
                notify.build_failure_alert(date_str=today, step=step, error=error)
            )
        sys.exit(1)

    try:
        # ① 交易日判断（周末 / 法定节假日跳过）
        if "--force" not in args and not is_trading_day(cfg):
            log.info("今日（%s）非交易日，流水线跳过（--force 可强制运行）", today)
            return
        log.info("交易日确认：%s", today)

        # ② 数据更新（重拉全区间覆盖缓存）
        step = "数据更新"
        is_single = "--backtest" in args
        if is_single:
            symbols = [cfg["data"]["symbol"]]
        else:
            symbols = [item["code"] for item in ((cfg.get("portfolio") or {}).get("symbols") or [])]
        if not symbols:
            raise ValueError("未配置品种（portfolio.symbols 为空且非 --backtest 模式）")
        log.info("更新范围：%s（%d 个品种）", "单品种" if is_single else "组合池", len(symbols))
        refresh_data(cfg, symbols, log)

        # ③ 跑批（复用既有入口：报告 / 图表 / 结果登记一条龙）
        step = "回测跑批"
        if is_single:
            import run_backtest

            run_backtest.main()
        else:
            import run_portfolio

            run_portfolio.main()

        # ④ 数据质量检查（报告 + 入库）
        step = "数据质量检查"
        report = run_quality(cfg, symbols, log)

        # ⑤ 汇总通知
        step = "汇总通知"
        counts = report.counts()
        summary_lines = [
            f"跑批：{'单品种' if is_single else '组合'}（{len(symbols)} 品种）",
            f"数据质量：警告 {counts['warn']} · 错误 {counts['error']}",
        ]
        storage_cfg = cfg.get("storage") or {}
        if storage_cfg.get("enabled", True):
            conn = results_db.open_db(
                resolve_path(storage_cfg.get("db_path", "output/db/quant.sqlite3"))
            )
            latest = results_db.latest_run(conn)
            conn.close()
            if latest is not None:
                summary_lines.insert(
                    1,
                    f"最新回测 #{latest['id']}：收益 {latest['total_return']:.2%}"
                    f" · 回撤 {latest['max_drawdown']:.2%}",
                )
        log.info("流水线完成：%s", "；".join(summary_lines))
        if notify_on and notify_cfg.get("on_success", True):
            notify.send_markdown(notify.build_daily_summary(date_str=today, lines=summary_lines))
    except SystemExit as exc:
        fail(f"子流程异常退出（code={getattr(exc, 'code', 1)}）")
    except Exception as exc:  # noqa: BLE001 — 统一走失败告警出口
        fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
