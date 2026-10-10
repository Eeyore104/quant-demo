"""组合熔断状态机单元测试（v1.3 · FuseMachine 三级阶梯 + 全平）。"""

from src.engine.risk import FuseMachine, RiskConfig


def _day(f, idx, *, equity, prev, peak, streak=0, positions=True, date="2024-01-01"):
    return f.day_end(
        day_idx=idx,
        date=date,
        equity=equity,
        prev_equity=prev,
        peak=peak,
        losing_streak=streak,
        has_positions=positions,
    )


def test_daily_loss_pause_and_cooldown_resume():
    f = FuseMachine(RiskConfig())
    evs = _day(f, 10, equity=96000, prev=100000, peak=100000)  # -4% ≤ -3%
    assert any(e.kind == "fuse_pause" for e in evs)
    assert f.opening_blocked(11) and f.opening_blocked(13)  # 冷却 3 日（11~13）
    assert f.state_name(11) == "paused"
    evs2 = _day(f, 13, equity=96000, prev=96000, peak=100000)  # 冷却到期清理
    assert any(e.kind == "fuse_resume" for e in evs2)
    assert not f.opening_blocked(14)


def test_drawdown_degrade_and_recovery():
    f = FuseMachine(RiskConfig())
    evs = _day(f, 10, equity=84000, prev=85000, peak=100000)  # -16% ≤ -15%，日跌 -1.2% 不触发暂停
    assert any(e.kind == "fuse_degrade" for e in evs)
    assert f.degrade_factor(11) == 0.5 and not f.opening_blocked(11)
    assert f.state_name(11) == "degraded"
    evs2 = _day(f, 20, equity=92000, prev=92000, peak=100000)  # 回撤 -8% > -10% → 解除
    assert any(e.kind == "fuse_resume" for e in evs2)
    assert f.degrade_factor(21) == 1.0


def test_drawdown_forced_release_after_max_cooldown():
    f = FuseMachine(RiskConfig())
    _day(f, 10, equity=80000, prev=80000, peak=100000)  # -20% 触发
    assert f.dd_active
    evs = _day(f, 30, equity=80000, prev=80000, peak=100000)  # 20 个交易日冷却期满（强制解除）
    assert any(e.kind == "fuse_resume" for e in evs)
    assert not f.dd_active


def test_losing_streak_pause():
    f = FuseMachine(RiskConfig())
    evs = _day(f, 5, equity=100000, prev=100000, peak=100000, streak=5)
    assert any(e.kind == "fuse_pause" for e in evs)
    assert f.opening_blocked(6) and f.opening_blocked(15)
    assert not f.opening_blocked(16)  # 冷却 10 日（6~15）


def test_priority_strictest_wins():
    # 回撤档（降规模）+ 单日亏损档（暂停）同时命中 → 有效状态取最严「暂停」
    f = FuseMachine(RiskConfig())
    _day(f, 1, equity=84000, prev=100000, peak=100000)
    assert f.dd_active and f.opening_blocked(2)
    assert f.state_name(2) == "paused"


def test_liquidate_default_off_then_enabled():
    f = FuseMachine(RiskConfig())
    evs = _day(f, 1, equity=70000, prev=80000, peak=100000)
    assert not any(e.kind == "fuse_liquidate" for e in evs)  # 默认关

    f2 = FuseMachine(RiskConfig(fuse_liquidate_enabled=True))
    evs2 = _day(f2, 1, equity=70000, prev=80000, peak=100000)
    assert any(e.kind == "fuse_liquidate" for e in evs2)
    assert f2.liquidate_next and f2.opening_blocked(2)
    f2.finish_liquidate(2)
    assert not f2.liquidate_next
    assert f2.opening_blocked(3) and not f2.opening_blocked(13)  # 冷却 10 日


def test_liquidate_skipped_without_positions():
    f = FuseMachine(RiskConfig(fuse_liquidate_enabled=True))
    evs = _day(f, 1, equity=70000, prev=80000, peak=100000, positions=False)
    assert not any(e.kind == "fuse_liquidate" for e in evs)
