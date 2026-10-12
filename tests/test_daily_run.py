"""每日流水线测试：交易日判断三态（周末 / 有日历 / 无日历兜底）。"""

import datetime as dt

from scripts import daily_run


def _cfg(tmp_path):
    return {"data": {"raw_dir": str(tmp_path)}}


def test_weekend_is_not_trading_day(tmp_path):
    cfg = _cfg(tmp_path)
    saturday = dt.date(2026, 10, 10)
    assert daily_run.is_trading_day(cfg, saturday) is False


def test_calendar_hit_and_miss(tmp_path):
    (tmp_path / "trade_dates.csv").write_text(
        "trade_date\n2026-10-12\n2026-10-13\n", encoding="utf-8"
    )
    cfg = _cfg(tmp_path)
    assert daily_run.is_trading_day(cfg, dt.date(2026, 10, 12)) is True
    assert daily_run.is_trading_day(cfg, dt.date(2026, 10, 14)) is False


def test_no_calendar_weekday_conservative_true(tmp_path):
    cfg = _cfg(tmp_path)  # 目录为空：日历不可得 → 工作日保守放行
    assert daily_run.is_trading_day(cfg, dt.date(2026, 10, 12)) is True
