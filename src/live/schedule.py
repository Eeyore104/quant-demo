"""交易时段（v2.0 轮 2）：日盘 / 夜盘时段判断与时间工具。

时段口径（近似；以实际为准）：
- 日盘：09:00-10:15、10:30-11:30、13:30-15:00（含商品期货盘间小节休息）
- 夜盘：21:00 开始；上期所有色（AL 等）至次日 01:00，其余品种至 23:00

夜盘的跨 0 点尾段（00:00-01:00）归属「前一日开始的夜盘」。
"""

from __future__ import annotations

import datetime as dt

# 夜盘结束时间（品种前缀 → "HH:MM"）；跨 0 点品种以 < 12:00 的时点表示
NIGHT_END = {
    "AL": "01:00",
    "CU": "01:00",
    "ZN": "01:00",
    "PB": "01:00",
    "SN": "01:00",
    "NI": "01:00",
    "SS": "01:00",
    "AU": "02:30",
    "AG": "02:30",
}
DEFAULT_NIGHT_END = "23:00"


def parse_hms(text: str) -> dt.time:
    """解析 "HH:MM" / "HH:MM:SS" 为 ``datetime.time``。"""
    parts = [int(x) for x in str(text).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dt.time(parts[0], parts[1], parts[2])


def night_end_of(symbol: str) -> dt.time:
    """品种的夜盘结束时间（如 AL0 → 01:00；M0 → 23:00）。"""
    base = symbol.rstrip("0123456789").upper()
    return parse_hms(NIGHT_END.get(base, DEFAULT_NIGHT_END))


def in_day_session(t: dt.time) -> bool:
    """日盘是否处于交易时段（9:00-10:15 / 10:30-11:30 / 13:30-15:00）。

    含商品期货的盘间小节休息（10:15-10:30 无行情）。
    """
    return (
        dt.time(9, 0) <= t <= dt.time(10, 15)
        or dt.time(10, 30) <= t <= dt.time(11, 30)
        or dt.time(13, 30) <= t <= dt.time(15, 0)
    )


def in_night_session(t: dt.time, symbol: str = "") -> bool:
    """夜盘是否处于交易时段（含跨 0 点尾段）。"""
    end = night_end_of(symbol)
    if end.hour < 12:
        # 跨 0 点品种（如 AL 至次日 01:00）：当晚 21:00 起，或凌晨尾段
        return t >= dt.time(21, 0) or t <= end
    # 非跨 0 点品种（如 M0 至 23:00）
    return dt.time(21, 0) <= t <= end


def is_trading_time(now: dt.datetime, symbols) -> bool:
    """当前时刻是否任一品种在交易（用于心跳抑制 / 执行时刻判定）。

    周末规则：周日无交易；周六仅凌晨尾段（周五夜盘延续，至 02:30 上限）。
    节假日由数据侧的交易日历二次判定（骨架版不内置节假日表）。
    """
    wd = now.weekday()
    t = now.time()
    if wd == 6:  # 周日
        return False
    if wd == 5:  # 周六：仅凌晨尾段
        return t <= dt.time(2, 30)
    if in_day_session(t):
        return True
    return any(in_night_session(t, s) for s in symbols)
