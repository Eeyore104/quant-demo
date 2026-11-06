"""换月测试（v2.2 · docs/21 T2.2-3 / T2.2-4）：代码解析 / 账本迁移 / 防护重挂 / 计划生成。"""

import datetime as dt
import json

import pytest

from src.engine.contracts import ContractSpec
from src.live import roll as roll_mod
from src.live.contract_code import candidates, canonical, format_like, parse, shift
from src.live.ledger import VirtualLedger
from src.live.lifecycle import LifecycleBook
from src.live.risk_guard import LiveRiskGuard

TODAY = dt.date(2026, 11, 6)


# ---------------------------------------------------------------- 合约代码


def test_parse_four_digit():
    assert parse("rb2701") == ("rb", 2027, 1, 4)


def test_parse_three_digit_czce():
    assert parse("SR701") == ("SR", 2027, 1, 3)


def test_shift_cross_year():
    assert shift("al2612", 1) == "al2701"
    assert shift("SR712", 1) == "SR801"


def test_canonical_unifies_width():
    assert canonical("SR701") == "SR2701"
    assert canonical("al2611") == "AL2611"


def test_format_like_keeps_style():
    assert format_like("SR701", "SR2702") == "SR702"
    assert format_like("al2611", "AL2612") == "al2612"


def test_candidates_are_future_months():
    assert candidates("m2701", offsets=(1, 2)) == ["m2702", "m2703"]


def test_parse_rejects_bad_code():
    with pytest.raises(ValueError):
        parse("M0")
    with pytest.raises(ValueError):
        parse("m2713")


# ---------------------------------------------------------------- 账本迁移


def _ledger() -> VirtualLedger:
    contracts = {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}
    lg = VirtualLedger(100000, contracts, {"m2701": "M0"})
    lg.begin_day()
    return lg


def _open(lg, price=3000.0, lots=2, day="2026-11-05"):
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="OPEN",
        price=price, volume=lots, trading_day=day,
    )


def _roll_long(lg, close_price=3100.0, open_price=3120.0, lots=2):
    old_avg = float(lg.state("M0").avg_price)
    lg.register_instrument("M0", "m2705")
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="CLOSE",
        price=close_price, volume=lots, trading_day="2026-11-06",
    )
    lg.apply_fill(
        instrument="m2705", direction="LONG", offset="OPEN",
        price=open_price, volume=lots, trading_day="2026-11-06",
    )
    return lg.commit_roll(
        "M0", old_instrument="m2701", new_instrument="m2705",
        old_avg_price=old_avg, volume=lots, direction="LONG",
        trading_day="2026-11-06",
    )


def test_register_instrument_keeps_old_bookable():
    lg = _ledger()
    _open(lg)
    lg.register_instrument("M0", "m2705")
    # 旧合约仍可入账（平仓回报需要一个落点）
    lg.apply_fill(
        instrument="m2701", direction="LONG", offset="CLOSE",
        price=3050.0, volume=2, trading_day="2026-11-06",
    )
    assert lg.net_size("M0") == 0


def test_commit_roll_switches_mapping_and_cost():
    lg = _ledger()
    _open(lg)
    rec = _roll_long(lg)
    assert rec["roll_cost"] == pytest.approx((3120 - 3000) * 10 * 2)
    assert lg.symbol_to_instrument["M0"] == "m2705"
    assert "m2701" not in lg.instrument_map
    assert lg.net_size("M0") == 2
    assert lg.state("M0").avg_price == pytest.approx(3120.0)


def test_commit_roll_old_instrument_no_longer_bookable():
    lg = _ledger()
    _open(lg)
    _roll_long(lg)
    with pytest.raises(ValueError):
        lg.apply_fill(
            instrument="m2701", direction="LONG", offset="OPEN",
            price=3000.0, volume=1, trading_day="2026-11-06",
        )


def test_roll_cost_short_direction_sign():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="OPEN",
        price=3000.0, volume=1, trading_day="2026-11-05",
    )
    old_avg = float(lg.state("M0").avg_price)
    lg.register_instrument("M0", "m2705")
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="CLOSE",
        price=2950.0, volume=1, trading_day="2026-11-06",
    )
    lg.apply_fill(
        instrument="m2705", direction="SHORT", offset="OPEN",
        price=2940.0, volume=1, trading_day="2026-11-06",
    )
    rec = lg.commit_roll(
        "M0", old_instrument="m2701", new_instrument="m2705",
        old_avg_price=old_avg, volume=1, direction="SHORT", trading_day="2026-11-06",
    )
    # 空头换到更低的合约：卖价基准下移 60 点 → 不利（正成本）
    assert rec["roll_cost"] == pytest.approx((2940 - 3000) * -1 * 10)


def test_roll_cost_persists(tmp_path):
    lg = _ledger()
    _open(lg)
    _roll_long(lg)
    path = tmp_path / "ledger.json"
    lg.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["current_instruments"]["M0"] == "m2705"

    lg2 = VirtualLedger(100000, {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}, {"m2701": "M0"})
    assert lg2.load(path) is True
    assert lg2.symbol_to_instrument["M0"] == "m2705"
    assert lg2.roll_cost["M0"] == pytest.approx(lg.roll_cost["M0"])
    assert lg2.instrument_map == {"m2705": "M0"}


# ---------------------------------------------------------------- 防护重挂


def _guard(lg):
    cfg = {
        "enabled": True,
        "reentry": {"mode": "signal_reset", "cooldown_days": 3},
        "execution": {"intrabar": True, "margin_degrade": True},
        "stops": {
            "fixed": {"enabled": True, "atr_mult": 2.0},
            "trailing": {"enabled": True, "atr_mult": 3.0},
            "take_profit": {"enabled": False, "pct": 0.04},
        },
        "fuse": {
            "daily_loss": {"enabled": True, "threshold": 0.03, "cooldown_days": 3},
            "drawdown": {
                "enabled": True, "threshold": 0.15, "recovery": 0.10,
                "degrade_factor": 0.5, "max_cooldown_days": 20,
            },
            "losing_streak": {"enabled": True, "count": 5, "cooldown_days": 10},
            "liquidate": {"enabled": False, "threshold": 0.25, "cooldown_days": 10},
        },
        "exposure": {"enabled": False, "max_symbol_pct": 1.5, "max_total_pct": 4.0},
        "price_limit": {"enabled": False, "detect_factor": 0.95, "rates": {}},
        "liquidity": {"enabled": False, "max_volume_share": 0.05},
    }
    return LiveRiskGuard(cfg, lg.contracts, lg)


def test_rearm_stop_makes_position_immediately_armed():
    lg = _ledger()
    _open(lg)
    guard = _guard(lg)
    guard.atr_hint = {"M0": 20.0}
    guard.sync_stops("M0")  # 今日入场 → entry_row = day_idx
    assert guard.manager.stops.state("M0").entry_row == lg.day_idx

    ev = guard.rearm_stop("M0")
    assert ev is not None and ev.kind == "roll_rearm"
    assert guard.manager.stops.state("M0").entry_row == lg.day_idx - 1
    assert guard.since["M0"] == [3000.0, 3000.0]
    assert guard.reg_day["M0"] == lg.day_idx


def test_rearm_stop_without_state_returns_none():
    lg = _ledger()
    guard = _guard(lg)
    assert guard.rearm_stop("M0") is None


def test_guard_state_roundtrip_includes_reg_day_and_since(tmp_path):
    lg = _ledger()
    _open(lg)
    guard = _guard(lg)
    guard.atr_hint = {"M0": 20.0}
    guard.sync_stops("M0", row_idx=lg.day_idx - 1)
    guard.since["M0"] = [2990.0, 3010.0]

    path = tmp_path / "risk.json"
    guard.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["reg_day"]["M0"] == lg.day_idx
    assert raw["since"]["M0"] == [2990.0, 3010.0]

    guard2 = _guard(lg)
    assert guard2.load(path) is True
    assert guard2.reg_day["M0"] == lg.day_idx
    assert guard2.since["M0"] == [2990.0, 3010.0]


# ---------------------------------------------------------------- 计划文件


def _plan(tmp_path, items=1):
    plan = roll_mod.RollPlan(
        plan_id="20261106-01",
        date="2026-11-06",
        items=[
            roll_mod.RollItem(
                symbol="AL0", from_instrument="al2611", to_instrument="al2612",
                direction="SHORT", lots=1, reason="expiry", days_left=5,
            )
        ][:items],
    )
    roll_mod.save_plan(plan, tmp_path)
    return plan


def test_plan_save_and_load(tmp_path):
    _plan(tmp_path)
    loaded = roll_mod.load_plan("20261106-01", tmp_path)
    assert loaded is not None
    assert loaded.items[0].to_instrument == "al2612"
    assert loaded.confirmed is False


def test_plan_confirm_sets_flag(tmp_path):
    _plan(tmp_path)
    plan = roll_mod.confirm_plan("20261106-01", base=tmp_path)
    assert plan is not None and plan.confirmed is True and plan.confirmed_at
    assert roll_mod.load_plan("20261106-01", tmp_path).confirmed is True


def test_latest_plan_picks_newest(tmp_path):
    _plan(tmp_path)
    newer = roll_mod.RollPlan(plan_id="20261107-01", date="2026-11-07", items=[])
    roll_mod.save_plan(newer, tmp_path)
    assert roll_mod.latest_plan(tmp_path).plan_id == "20261107-01"


def test_next_plan_id_increments(tmp_path):
    _plan(tmp_path)
    assert roll_mod.next_plan_id(TODAY, tmp_path) == "20261106-02"


def test_summary_lines_marks_confirmation(tmp_path):
    plan = _plan(tmp_path)
    text = "\n".join(roll_mod.summary_lines(plan))
    assert "待确认" in text and "al2611 → al2612" in text


# ---------------------------------------------------------------- 计划生成


def _lifecycle_yaml(tmp_path) -> str:
    path = tmp_path / "life.yaml"
    path.write_text(
        "contracts:\n"
        "  al2611: {symbol: AL0, exchange: SHFE, last_trade: 2026-11-16,"
        " individual_last: 2026-11-11}\n"
        "  al2612: {symbol: AL0, exchange: SHFE, last_trade: 2026-12-16,"
        " individual_last: 2026-12-11}\n"
        "  rb2701: {symbol: RB0, exchange: SHFE, last_trade: 2027-01-15,"
        " individual_last: 2027-01-12}\n"
        "  rb2705: {symbol: RB0, exchange: SHFE, last_trade: 2027-05-17,"
        " individual_last: 2027-05-12}\n",
        encoding="utf-8",
    )
    return str(path)


def _cfg(tmp_path) -> dict:
    return {
        "live": {
            "contracts_map": {"AL0": "al2611", "RB0": "rb2701"},
            "lifecycle": {
                "path": _lifecycle_yaml(tmp_path),
                "warn_days": 5,
                "force_close_days": 1,
                "roll_trigger_days": 10,
                "main_switch_ratio": 1.2,
                "main_switch_days": 2,
            },
        }
    }


def _build(tmp_path, **kw):
    from scripts.roll_plan import build_plan

    kw.setdefault("sizes", {"AL0": 1, "RB0": 0})
    kw.setdefault("plan_base", tmp_path)
    return build_plan(_cfg(tmp_path), today=TODAY, **kw)


def test_build_plan_expiry_triggers(tmp_path):
    oi = {"AL2611": 20000, "AL2612": 227071, "RB2701": 1681929, "RB2705": 390054}
    plan, notes = _build(tmp_path, oi=oi)
    item = next(i for i in plan.items if i.symbol == "AL0")
    assert item.reason == "expiry"
    assert item.to_instrument == "al2612"  # 沿用配置小写风格
    assert item.lots == 1 and item.direction == "LONG"
    assert notes == []


def test_build_plan_no_trigger_for_far_month(tmp_path):
    oi = {"AL2611": 20000, "AL2612": 227071, "RB2701": 1681929, "RB2705": 100}
    plan, _ = _build(tmp_path, oi=oi)
    assert all(i.symbol != "RB0" for i in plan.items)


def test_build_plan_oi_switch_needs_two_days(tmp_path):
    oi = {"AL2611": 20000, "AL2612": 227071, "RB2701": 100, "RB2705": 1000}
    prev = {"RB2701": 100, "RB2705": 1000}
    plan, _ = _build(tmp_path, oi=oi, history={"2026-11-05": prev})
    assert any(i.symbol == "RB0" and i.reason == "oi_switch" for i in plan.items)

    # 只有当日成立、前一日不成立 → 不切换
    prev2 = {"RB2701": 1000, "RB2705": 100}
    plan2, _ = _build(tmp_path, oi=oi, history={"2026-11-05": prev2})
    assert all(i.symbol != "RB0" for i in plan2.items)


def test_build_plan_manual_override(tmp_path):
    plan, _ = _build(tmp_path, oi={"AL2611": 1, "AL2612": 1}, overrides={"AL0": "al2701"})
    item = next(i for i in plan.items if i.symbol == "AL0")
    assert item.to_instrument == "al2701"


def test_build_plan_missing_oi_notes_manual(tmp_path):
    plan, notes = _build(tmp_path, oi={})
    assert plan.items == []
    assert any("--to" in n for n in notes)


def test_lifecycle_book_override_changes_gate(tmp_path):
    cmap = {"AL0": "al2611"}
    book = LifecycleBook.from_config(_cfg(tmp_path)["live"], cmap, today=TODAY)
    assert book.can_open("AL0")[0] is False  # al2611 只剩 5 天
    book.override("AL0", "al2612")
    assert book.can_open("AL0")[0] is True


# ---------------------------------------------------------------- 执行编排


class _FakeExec:
    """假执行器：平旧/开新直接用账本成交模拟（不连柜台）。"""

    def __init__(self, ledger):
        self.ledger = ledger
        self.calls: list[tuple[str, str]] = []

    def force_flat(self, symbol, date=""):
        self.calls.append(("flat", symbol))
        st = self.ledger.state(symbol)
        if st.size:
            inst = self.ledger.symbol_to_instrument[symbol]
            self.ledger.apply_fill(
                instrument=inst,
                direction=("LONG" if st.size > 0 else "SHORT"),
                offset="CLOSE",
                price=st.last_price or st.avg_price,
                volume=abs(st.size),
                trading_day="2026-11-06",
            )
        return []

    def open_for_roll(self, symbol, direction, lots, date=""):
        self.calls.append(("open", symbol))
        inst = self.ledger.symbol_to_instrument[symbol]
        self.ledger.apply_fill(
            instrument=inst, direction=direction, offset="OPEN",
            price=3120.0, volume=lots, trading_day="2026-11-06",
        )
        return []


class _FakeBook:
    def active_orders(self, symbol=None):
        return []


class _FakeGuard:
    def rearm_stop(self, symbol, date=""):
        class _Ev:
            detail = "移仓后立即恢复防护"

        return _Ev()


class _FakeLife:
    def contract_of(self, instrument):
        return object() if str(instrument).lower().startswith("m27") else None

    def override(self, symbol, instrument):
        self.last = (symbol, instrument)


def _runtime(tmp_path, ledger):
    from run_live import LiveRuntime

    rt = object.__new__(LiveRuntime)
    rt.symbols = ["M0"]
    rt.ledger = ledger
    rt.journal = None
    rt.today = lambda: "2026-11-06"
    rt.plan_dir = tmp_path
    rt.executor = _FakeExec(ledger)
    rt.book = _FakeBook()
    rt.guard = _FakeGuard()
    rt.lifecycle = _FakeLife()
    rt.pump = lambda seconds=0.5: None
    rt.log_notes = lambda notes: None
    rt.save_state = lambda: None
    return rt


def _plan_file(tmp_path, lots, direction="LONG"):
    plan = roll_mod.RollPlan(
        plan_id="20261106-01",
        date="2026-11-06",
        items=[
            roll_mod.RollItem(
                symbol="M0", from_instrument="m2701", to_instrument="m2705",
                direction=direction, lots=lots, reason="expiry", days_left=3,
            )
        ],
        confirmed=True,
    )
    roll_mod.save_plan(plan, tmp_path)
    return plan


def test_execute_roll_flat_symbol_only_remaps(tmp_path):
    lg = _ledger()
    rt = _runtime(tmp_path, lg)
    plan = _plan_file(tmp_path, lots=0, direction="")

    rt._execute_roll(plan)

    assert lg.symbol_to_instrument["M0"] == "m2705"
    assert rt.executor.calls == []
    assert "m2705" in lg.instrument_map


def test_execute_roll_moves_position_and_records_cost(tmp_path):
    lg = _ledger()
    _open(lg, price=3000.0, lots=2)
    lg.account.mark_price("M0", 3100.0)
    rt = _runtime(tmp_path, lg)
    plan = _plan_file(tmp_path, lots=2)

    rt._execute_roll(plan)

    assert rt.executor.calls == [("flat", "M0"), ("open", "M0")]
    assert lg.net_size("M0") == 2
    assert lg.symbol_to_instrument["M0"] == "m2705"
    assert lg.state("M0").avg_price == pytest.approx(3120.0)
    # 平旧 @3100、开新 @3120 → 价差成本 (3120-3000)×10×2
    assert lg.roll_cost["M0"] == pytest.approx((3120 - 3000) * 10 * 2)
    loaded = roll_mod.load_plan("20261106-01", tmp_path)
    assert loaded.executed_at


def test_execute_roll_skips_unregistered_target(tmp_path):
    lg = _ledger()
    _open(lg, lots=1)
    rt = _runtime(tmp_path, lg)
    rt.lifecycle = type("L", (), {"contract_of": lambda self, i: None,
                                  "override": lambda self, s, i: None})()
    plan = _plan_file(tmp_path, lots=1)

    rt._execute_roll(plan)

    assert rt.executor.calls == []
    assert lg.symbol_to_instrument["M0"] == "m2701"  # 未切换
