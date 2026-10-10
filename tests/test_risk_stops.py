"""止损止盈与重入规则单元测试（v1.3 · StopEngine / ReentryGuard）。"""

from types import SimpleNamespace

from src.engine.risk import ReentryGuard, RiskConfig, StopEngine


def _bar(o, h, low, c):
    return SimpleNamespace(open=o, high=h, low=low, close=c, volume=100000)


def test_fixed_stop_intrabar_long():
    eng = StopEngine(RiskConfig())
    eng.register_entry("A0", 1, 100.0, 5.0, 0)  # 固定止损 90；移动 85
    assert eng.intrabar_trigger("A0", 1, _bar(100, 101, 89, 95)).base_price == 90
    assert eng.intrabar_trigger("A0", 1, _bar(100, 101, 91, 95)) is None  # 未触及


def test_new_entry_not_checked_same_day():
    eng = StopEngine(RiskConfig())
    eng.register_entry("A0", 1, 100.0, 5.0, 3)
    assert eng.intrabar_trigger("A0", 3, _bar(100, 101, 80, 90)) is None  # 入场当日不受保护
    assert eng.intrabar_trigger("A0", 4, _bar(100, 101, 80, 90)) is not None  # 次日生效


def test_gap_through_stop_fills_at_open():
    eng = StopEngine(RiskConfig())
    eng.register_entry("A0", 1, 100.0, 5.0, 0)
    trig = eng.gap_trigger("A0", 1, _bar(88, 92, 87, 90))
    assert trig is not None
    assert trig.kind == "stop_fixed" and trig.base_price == 88 and trig.level == 90


def test_trailing_stop_ratchets_up_only():
    eng = StopEngine(RiskConfig(stop_fixed_enabled=False))
    eng.register_entry("A0", 1, 100.0, 5.0, 0)  # 移动止损初始 85
    eng.update_after_close("A0", 110.0, 5.0)  # 110 - 15 = 95
    assert eng.state("A0").trail_stop == 95
    eng.update_after_close("A0", 105.0, 5.0)  # 候选 90 < 95 → 不上移
    assert eng.state("A0").trail_stop == 95
    trig = eng.intrabar_trigger("A0", 1, _bar(106, 107, 94, 100))
    assert trig.kind == "stop_trailing" and trig.base_price == 95


def test_take_profit_triggers_when_enabled():
    eng = StopEngine(RiskConfig(take_profit_enabled=True, take_profit_pct=0.04))
    eng.register_entry("A0", 1, 100.0, 5.0, 0)  # 止盈 104
    assert eng.intrabar_trigger("A0", 1, _bar(101, 103.5, 100, 103)) is None
    trig = eng.intrabar_trigger("A0", 1, _bar(101, 105, 100, 104.5))
    assert trig.kind == "take_profit" and trig.base_price == 104


def test_short_side_stop():
    eng = StopEngine(RiskConfig())
    eng.register_entry("A0", -1, 100.0, 5.0, 0)  # 空头止损 110；移动 115
    trig = eng.gap_trigger("A0", 1, _bar(112, 113, 111, 112))
    assert trig.direction == -1 and trig.base_price == 112
    trig2 = eng.intrabar_trigger("A0", 1, _bar(108, 111, 107, 110))
    assert trig2 is not None and trig2.base_price == 110


def test_close_based_pending_variant():
    # intrabar=False（备选口径）：收盘价触发 → 次日开盘执行
    eng = StopEngine(RiskConfig(intrabar=False))
    eng.register_entry("A0", 1, 100.0, 5.0, 0)
    # 盘中被触及但收盘未破 → 不触发
    eng.check_close("A0", 1, _bar(100, 101, 89, 95))
    assert "A0" not in eng.pending
    assert eng.intrabar_trigger("A0", 1, _bar(100, 101, 89, 95)) is None
    # 收盘破位 → 登记待执行
    eng.check_close("A0", 2, _bar(100, 101, 89, 88))
    assert "A0" in eng.pending
    # 次日开盘执行（按开盘价成交）
    trig = eng.gap_trigger("A0", 3, _bar(87, 88, 86, 87))
    assert trig is not None and trig.base_price == 87 and trig.kind == "stop_fixed"
    assert "A0" not in eng.pending


def test_reentry_signal_reset_flow():
    g = ReentryGuard(RiskConfig())
    g.on_stop("A0", day_idx=5, direction=1)
    assert not g.allows_open("A0", -1)  # 止损当日：含反手在内一律禁开
    g.new_day()
    g.update_signal("A0", 1, 6)
    assert not g.allows_open("A0", 1)  # 同向信号未重置 → 锁定
    g.update_signal("A0", 0, 7)  # 信号回 0 → 释放
    assert g.allows_open("A0", 1)
    # 反向：也不再受锁（且方向不同本就可开）
    g2 = ReentryGuard(RiskConfig())
    g2.on_stop("A0", day_idx=5, direction=1)
    g2.new_day()
    g2.update_signal("A0", -1, 6)
    assert g2.allows_open("A0", -1)


def test_reentry_cooldown_mode():
    g = ReentryGuard(RiskConfig(reentry_mode="cooldown", reentry_cooldown_days=3))
    g.on_stop("A0", day_idx=5, direction=1)
    g.new_day()
    g.update_signal("A0", 1, 7)
    assert not g.allows_open("A0", 1)  # 7 - 5 = 2 < 3
    g.update_signal("A0", 1, 8)
    assert g.allows_open("A0", 1)  # 8 - 5 = 3 ≥ 3 → 释放


def test_reentry_immediate_mode():
    g = ReentryGuard(RiskConfig(reentry_mode="immediate"))
    g.on_stop("A0", day_idx=5, direction=1)
    assert not g.allows_open("A0", 1)  # 当日仍禁
    g.new_day()
    assert g.allows_open("A0", 1)  # 次日起即可（无锁定）
