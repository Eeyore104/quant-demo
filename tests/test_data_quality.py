"""数据质量校验测试：结构 / 跳变 / 缺口 / 新鲜度 / 汇总。"""

import pandas as pd

from src.data import quality


def _df(dates, closes, highs=None, lows=None, volume=100):
    n = len(dates)
    closes = list(closes)
    highs = list(highs) if highs else [c * 1.01 for c in closes]
    lows = list(lows) if lows else [c * 0.99 for c in closes]
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [volume] * n,
        }
    )


def _by_check(rows, check):
    return [r for r in rows if r["check"] == check]


def test_clean_data_all_pass_with_calendar():
    dates = pd.date_range("2026-09-01", periods=10, freq="B")
    td = {d.strftime("%Y-%m-%d") for d in dates}
    df = _df(dates, [100 + i for i in range(10)])
    rows = quality.check_symbol(df, "X0", trade_dates=td)
    assert all(r["status"] == "pass" for r in rows)
    assert _by_check(rows, "summary")[0]["detail"].startswith("10 行")


def test_gap_detected_with_calendar():
    dates = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-07"])
    td = {f"2026-09-0{i}" for i in range(1, 8)}  # 日历 01~07
    df = _df(dates, [100, 101, 102])
    rows = quality.check_symbol(df, "X0", trade_dates=td)
    gap = _by_check(rows, "gap")
    assert gap and gap[0]["status"] == "warn"
    assert "4 个交易日" in gap[0]["detail"]


def test_freshness_lag_detected():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    td = {"2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"}
    df = _df(dates, [100, 101, 102])
    rows = quality.check_symbol(df, "X0", trade_dates=td)
    fresh = _by_check(rows, "freshness")
    assert fresh and fresh[0]["status"] == "warn"
    assert "滞后 3 个交易日" in fresh[0]["detail"]
    assert "缺失 2026-01-08、2026-01-09、2026-01-12" in fresh[0]["detail"]  # 证据列明


def test_structure_error_ohlc():
    dates = pd.date_range("2026-09-01", periods=3, freq="B")
    df = _df(dates, [100, 101, 102])
    df.loc[0, "high"] = 90  # high < close → 结构异常
    rows = quality.check_symbol(df, "X0")
    st = _by_check(rows, "structure")
    assert st and st[0]["status"] == "error"


def test_jump_warn_mentions_rollover_note():
    dates = pd.date_range("2026-09-01", periods=4, freq="B")
    df = _df(dates, [100, 101, 102, 130])  # 最后一日 +27%
    rows = quality.check_symbol(df, "X0")
    jp = _by_check(rows, "jump")
    assert jp and jp[0]["status"] == "warn"
    assert "换月跳空" in jp[0]["detail"]


def test_no_calendar_gap_fallback():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-03-01"])  # 间隔 58 天
    df = _df(dates, [100, 101, 102])
    rows = quality.check_symbol(df, "X0", trade_dates=None)
    gp = _by_check(rows, "gap")
    assert gp and gp[0]["status"] == "warn"


def test_check_dataset_report_and_render():
    dates = pd.date_range("2026-09-01", periods=5, freq="B")
    items = [
        ("A0", _df(dates, [100 + i for i in range(5)])),
        ("B0", _df(dates, [200 + i for i in range(5)])),
    ]
    td = {d.strftime("%Y-%m-%d") for d in dates}
    report = quality.check_dataset(items, trade_dates=td)
    assert report.ok()
    assert report.symbols == ["A0", "B0"]
    assert set(report.counts()) >= {"pass", "warn", "error"}
    md = quality.render_markdown(report, labels={"A0": "品种 A0"})
    assert "# 数据质量报告" in md
    assert "品种 A0" in md
    assert "全部检查通过" in md
    assert "排查提示" not in md  # 无问题时不出排查提示


def test_render_includes_troubleshoot_hints_when_issues():
    dates = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-07"])
    td = {f"2026-09-0{i}" for i in range(1, 8)}
    df = _df(dates, [100, 101, 102])
    report = quality.check_dataset([("X0", df)], trade_dates=td)
    assert not report.ok() or report.counts()["warn"] > 0
    md = quality.render_markdown(report)
    assert "排查提示" in md
    assert "行情缓存未更新" in md
