"""数据新鲜度工具测试（v2.0 验收期增强：live 自更新数据链）。"""

import datetime as dt

from run_live import prev_workday


def test_prev_workday_wednesday():
    # 周三（10-14）→ 周二（10-13）
    assert prev_workday(dt.date(2026, 10, 14)) == dt.date(2026, 10, 13)


def test_prev_workday_monday():
    # 周一（10-19）→ 上周五（10-16）
    assert prev_workday(dt.date(2026, 10, 19)) == dt.date(2026, 10, 16)


def test_prev_workday_sunday():
    # 周日（10-18）→ 周五（10-16）
    assert prev_workday(dt.date(2026, 10, 18)) == dt.date(2026, 10, 16)
