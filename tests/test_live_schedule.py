"""交易时段测试（v2.0 轮 2）：日盘 / 夜盘 / 周末规则。"""

import datetime as dt

from src.live.schedule import (
    in_day_session,
    in_night_session,
    is_trading_time,
    night_end_of,
    parse_hms,
)


def test_parse_hms():
    assert parse_hms("21:00:05") == dt.time(21, 0, 5)
    assert parse_hms("09:30") == dt.time(9, 30, 0)


def test_day_session_bounds():
    assert in_day_session(dt.time(9, 0))
    assert in_day_session(dt.time(11, 30))
    assert not in_day_session(dt.time(12, 0))
    assert in_day_session(dt.time(13, 30))
    assert in_day_session(dt.time(15, 0))
    assert not in_day_session(dt.time(15, 30))
    assert not in_day_session(dt.time(8, 59))


def test_night_session_bounds():
    # 当晚段
    assert in_night_session(dt.time(21, 0), "M0")
    assert in_night_session(dt.time(22, 59), "M0")
    # M0 夜盘 23:00 结束
    assert not in_night_session(dt.time(23, 30), "M0")
    # AL 夜盘至次日 01:00
    assert in_night_session(dt.time(23, 30), "AL0")
    assert in_night_session(dt.time(0, 30), "AL0")
    # 凌晨尾段：非跨 0 品种不交易
    assert not in_night_session(dt.time(0, 30), "M0")
    assert not in_night_session(dt.time(20, 0), "M0")


def test_night_end_of():
    assert night_end_of("AL0") == dt.time(1, 0)
    assert night_end_of("M0") == dt.time(23, 0)
    assert night_end_of("SR0") == dt.time(23, 0)


def test_is_trading_time_regular_day():
    symbols = ["M0", "AL0"]
    assert is_trading_time(dt.datetime(2026, 10, 13, 10, 0), symbols)  # 周二日盘
    assert not is_trading_time(dt.datetime(2026, 10, 13, 12, 0), symbols)  # 午休
    assert is_trading_time(dt.datetime(2026, 10, 13, 21, 30), symbols)  # 周二晚夜盘
    assert not is_trading_time(dt.datetime(2026, 10, 13, 17, 0), symbols)  # 傍晚


def test_is_trading_time_weekend():
    symbols = ["M0", "AL0"]
    # 周六：仅凌晨尾段
    assert is_trading_time(dt.datetime(2026, 10, 17, 1, 0), symbols)  # 周六 01:00
    assert not is_trading_time(dt.datetime(2026, 10, 17, 9, 30), symbols)  # 周六上午
    assert not is_trading_time(dt.datetime(2026, 10, 17, 21, 30), symbols)  # 周六晚
    # 周日：全天无交易
    assert not is_trading_time(dt.datetime(2026, 10, 18, 1, 0), symbols)
    assert not is_trading_time(dt.datetime(2026, 10, 18, 21, 30), symbols)
