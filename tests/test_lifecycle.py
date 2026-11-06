"""合约生命周期测试（v2.2 · docs/21 T2.2-1 / T2.2-2）。

覆盖两层：
1. 状态机（纯数据）：边界值、未登记、停用、批量查询；
2. 到期闸（接入 Executor）：开仓腿被拦、平仓腿放行、到期强平生成平仓腿。

用 FakeSession 模拟 CTP（不连网），与 test_live_adapter 同风格。
"""

import datetime as dt

import pytest

from src.engine.contracts import ContractSpec
from src.live.adapter import Executor
from src.live.ctp_api import OFFSET_CLOSE, OFFSET_OPEN, Tick
from src.live.ledger import VirtualLedger
from src.live.lifecycle import (
    STATE_FORCE,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_WARN,
    ContractLife,
    LifecycleBook,
)
from src.live.orders import OrderBook
from src.live.risk_guard import LiveRiskGuard
from src.live.signals import TargetPosition

TODAY = dt.date(2026, 11, 6)


# ---------------------------------------------------------------- 夹具


class FakeSession:
    """假会话：记录报单 / 撤单请求。"""

    def __init__(self):
        self.inserted: list[dict] = []
        self.actions: list[str] = []
        self._seq = 0

    def order_insert(self, instrument, direction, offset, price, volume):
        self._seq += 1
        ref = str(self._seq)
        self.inserted.append(
            {
                "ref": ref,
                "instrument": instrument,
                "direction": direction,
                "offset": offset,
                "price": price,
                "volume": volume,
            }
        )
        return ref

    def order_action(self, order_ref, instrument, order_sys_id=""):
        self.actions.append(order_ref)


def _life(instrument: str, symbol: str, days: int) -> ContractLife:
    """构造一条生命周期：自然人最后了结日 = TODAY + days。"""
    return ContractLife(
        instrument=instrument,
        symbol=symbol,
        exchange="DCE",
        last_trade=TODAY + dt.timedelta(days=days + 3),
        individual_last=TODAY + dt.timedelta(days=days),
    )


def _book(days: int = 30, *, enabled: bool = True) -> LifecycleBook:
    return LifecycleBook(
        {"m2701": _life("m2701", "M0", days)},
        contracts_map={"M0": "m2701"},
        warn_days=5,
        force_close_days=1,
        enabled=enabled,
        today=TODAY,
    )


def _tick(price: float = 3000.0, volume: int = 100000) -> Tick:
    return Tick(
        instrument="m2701",
        last_price=price,
        open_price=price,
        highest_price=price,
        lowest_price=price,
        volume=volume,
    )


def _risk_cfg() -> dict:
    return {
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
        "exposure": {"enabled": True, "max_symbol_pct": 1.50, "max_total_pct": 4.00},
        "price_limit": {"enabled": True, "detect_factor": 0.95, "rates": {"M0": 0.07}},
        "liquidity": {"enabled": True, "max_volume_share": 0.05},
    }


def _setup(lifecycle: LifecycleBook | None = None, *, long_lots: int = 0):
    contracts = {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}
    ledger = VirtualLedger(100000, contracts, {"m2701": "M0"})
    ledger.begin_day()
    if long_lots:
        ledger.apply_fill(
            instrument="m2701", direction="LONG", offset="OPEN",
            price=3000.0, volume=long_lots, trading_day="2026-11-05",
        )
    guard = LiveRiskGuard(_risk_cfg(), contracts, ledger, prev_close={})
    book = OrderBook()
    session = FakeSession()
    ex = Executor(
        session, book, ledger, guard,
        {"execute": {"quote_timeout_seconds": 5, "chase_max": 3}},
        lifecycle=lifecycle,
    )
    ex.ticks["M0"] = _tick()
    return ex, session, ledger


# ---------------------------------------------------------------- 状态机


def test_state_ok_far_from_expiry():
    book = _book(days=30)
    assert book.state("M0") == STATE_OK
    assert book.can_open("M0") == (True, "")


def test_warn_boundary_is_inclusive():
    assert _book(days=6).state("M0") == STATE_OK
    assert _book(days=5).state("M0") == STATE_WARN
    allowed, reason = _book(days=5).can_open("M0")
    assert allowed is False and "禁止新开仓" in reason


def test_force_close_boundary_is_inclusive():
    assert _book(days=2).state("M0") == STATE_WARN
    assert _book(days=1).state("M0") == STATE_FORCE
    allowed, reason = _book(days=1).can_open("M0")
    assert allowed is False and "清仓" in reason


def test_past_deadline_is_force_close():
    book = _book(days=-3)
    assert book.state("M0") == STATE_FORCE
    assert book.days_left("M0") == -3


def test_unknown_symbol_blocked():
    book = _book()
    book.contracts_map["AL0"] = "al2611"  # 表里没有 al2611
    assert book.state("AL0") == STATE_UNKNOWN
    allowed, reason = book.can_open("AL0")
    assert allowed is False and "未登记" in reason
    assert book.days_left("AL0") > 1000


def test_missing_lists_unregistered_only():
    book = _book()
    book.contracts_map.update({"AL0": "al2611", "RB0": "rb2701"})
    assert book.missing() == ["AL0", "RB0"]


def test_disabled_book_allows_everything():
    book = _book(days=0, enabled=False)
    assert book.state("M0") == STATE_OK
    assert book.can_open("M0") == (True, "")


def test_expiring_and_force_close_lists():
    book = _book(days=1)
    book.contracts_map["RB0"] = "rb2701"
    book.items["rb2701"] = _life("rb2701", "RB0", 30)
    assert book.expiring() == ["M0"]
    assert book.force_close_symbols() == ["M0"]


def test_describe_has_days_and_state():
    text = _book(days=4).describe("M0")
    assert "m2701" in text and "4 天" in text and STATE_WARN in text


def test_contract_of_falls_back_to_symbol_field():
    book = LifecycleBook({"al2611": _life("al2611", "AL0", 10)}, contracts_map={}, today=TODAY)
    assert book.contract_of("AL0") is not None


def test_from_config_reads_yaml(tmp_path):
    path = tmp_path / "life.yaml"
    path.write_text(
        "contracts:\n"
        "  m2701:\n"
        "    symbol: M0\n"
        "    exchange: DCE\n"
        "    last_trade: 2027-01-15\n"
        "    individual_last: 2026-12-31\n",
        encoding="utf-8",
    )
    book = LifecycleBook.from_config(
        {"lifecycle": {"warn_days": 5, "force_close_days": 1}},
        {"M0": "m2701"},
        today=TODAY,
        path=path,
    )
    assert book.state("M0") == STATE_OK
    assert book.items["m2701"].individual_last == dt.date(2026, 12, 31)


def test_from_config_skips_bad_entry(tmp_path):
    path = tmp_path / "life.yaml"
    path.write_text(
        "contracts:\n"
        "  broken: {symbol: XX0, exchange: DCE}\n"
        "  m2701: {symbol: M0, exchange: DCE, last_trade: 2027-01-15,"
        " individual_last: 2026-12-31}\n",
        encoding="utf-8",
    )
    book = LifecycleBook.from_config({}, {"M0": "m2701"}, today=TODAY, path=path)
    assert "broken" not in book.items
    assert book.state("M0") == STATE_OK


def test_from_config_missing_file_all_unknown(tmp_path):
    book = LifecycleBook.from_config({}, {"M0": "m2701"}, today=TODAY, path=tmp_path / "nope.yaml")
    assert book.state("M0") == STATE_UNKNOWN


# ---------------------------------------------------------------- 到期闸


def test_open_leg_blocked_when_warn():
    ex, session, _ = _setup(_book(days=3))
    notes = ex.execute_target({"M0": TargetPosition("M0", 1, 1, 1, 3000.0)}, date="2026-11-06")
    assert session.inserted == []
    assert any(n.kind == "refuse" and "到期闸" in n.detail for n in notes)


def test_close_leg_passes_when_expiring():
    ex, session, _ = _setup(_book(days=1), long_lots=1)
    notes = ex.execute_target({"M0": TargetPosition("M0", 0, 0, 0, 3000.0)}, date="2026-11-06")
    assert len(session.inserted) == 1
    assert session.inserted[0]["offset"] == OFFSET_CLOSE
    assert any(n.kind == "close" for n in notes)


def test_open_leg_passes_when_ok():
    ex, session, _ = _setup(_book(days=30))
    ex.execute_target({"M0": TargetPosition("M0", 1, 1, 1, 3000.0)}, date="2026-11-06")
    assert len(session.inserted) == 1
    assert session.inserted[0]["offset"] == OFFSET_OPEN


def test_gate_disabled_without_lifecycle():
    ex, session, _ = _setup(None)
    ex.execute_target({"M0": TargetPosition("M0", 1, 1, 1, 3000.0)}, date="2026-11-06")
    assert len(session.inserted) == 1


def test_force_flat_generates_close_leg():
    ex, session, _ = _setup(_book(days=0), long_lots=2)
    notes = ex.force_flat("M0", date="2026-11-06")
    assert len(session.inserted) == 1
    assert session.inserted[0]["offset"] == OFFSET_CLOSE
    assert session.inserted[0]["volume"] == 2
    assert notes[0].kind == "force_flat"


def test_force_flat_noop_when_flat():
    ex, session, _ = _setup(_book(days=0))
    assert ex.force_flat("M0", date="2026-11-06") == []
    assert session.inserted == []


def test_force_flat_skips_without_tick():
    ex, session, _ = _setup(_book(days=0), long_lots=1)
    ex.ticks.pop("M0")
    notes = ex.force_flat("M0", date="2026-11-06")
    assert session.inserted == []
    assert notes[0].kind == "skip" and "行情" in notes[0].detail


def test_force_flat_respects_limit_direction():
    """跌停时不能卖 → 平多被拦（涨跌停方向约束仍然生效）。"""
    ex, session, _ = _setup(_book(days=0), long_lots=1)
    ex.ticks["M0"] = _tick(price=2800.0)  # −6.67% ≥ 7%×0.95 → 跌停
    ex.guard.prev_close = {"M0": 3000.0}
    notes = ex.force_flat("M0", date="2026-11-06")
    assert session.inserted == []
    assert any(n.kind in ("refuse", "risk") for n in notes)


@pytest.mark.parametrize("days,expected", [(30, STATE_OK), (5, STATE_WARN), (1, STATE_FORCE)])
def test_state_matrix(days, expected):
    assert _book(days=days).state("M0") == expected


# ---------------------------------------------------------------- 运行器接线


class _FakeJournal:
    def __init__(self):
        self.entries: list[tuple[str, dict]] = []
        self.notes = []

    def log(self, kind, **kwargs):
        self.entries.append((kind, kwargs))

    def record_notes(self, notes):
        self.notes.extend(notes or [])


class _FakeExecutor:
    def __init__(self):
        self.calls: list[str] = []

    def force_flat(self, symbol, date=""):
        from src.live.adapter import ExecNote

        self.calls.append(symbol)
        return [ExecNote("force_flat", symbol, f"测试强平 {symbol}", "1")]


def _runtime(book: LifecycleBook, *, net: int = 0, dry_run: bool = False):
    from run_live import LiveRuntime

    rt = object.__new__(LiveRuntime)
    rt.lifecycle = book
    rt.symbols = ["M0"]
    rt.journal = _FakeJournal()
    rt._life_alerted = set()
    rt.dry_run = dry_run
    rt.executor = _FakeExecutor()
    rt.today = lambda: "2026-11-06"  # type: ignore[method-assign]
    rt.log_notes = lambda notes: rt.journal.record_notes(notes)  # type: ignore[method-assign]

    class _Ledger:
        def net_size(self, symbol):
            return net

    rt.ledger = _Ledger()
    return rt


def test_check_lifecycle_journals_once_per_day():
    from src.live import alert as alert_mod

    alert_mod.reset()
    rt = _runtime(_book(days=3))
    rt._check_lifecycle()
    rt._check_lifecycle()  # 同日重复 → 不重复记
    kinds = [k for k, _ in rt.journal.entries if not k.startswith("alert_")]
    assert kinds == ["expiry_warn"]


def test_check_lifecycle_ok_is_silent():
    rt = _runtime(_book(days=30))
    rt._check_lifecycle()
    assert rt.journal.entries == []


def test_force_close_calls_executor_when_holding():
    rt = _runtime(_book(days=0), net=1)
    rt._force_close_expiring()
    assert rt.executor.calls == ["M0"]
    assert rt.journal.notes[0].kind == "force_flat"


def test_force_close_skips_when_flat():
    rt = _runtime(_book(days=0), net=0)
    rt._force_close_expiring()
    assert rt.executor.calls == []


def test_force_close_skips_in_dry_run():
    rt = _runtime(_book(days=0), net=1, dry_run=True)
    rt._force_close_expiring()
    assert rt.executor.calls == []
