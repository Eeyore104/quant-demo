"""启动清理 / 退出收尾 / 告警测试（v2.2 · docs/21 T2.2-5 ~ T2.2-8）。"""

import datetime as dt

import pytest

from src.live import alert as alert_mod
from src.live.ctp_api import OrderQryRsp, QueryDone

# ---------------------------------------------------------------- OrderQryRsp


def _rsp(status: str, ref: str = "1") -> OrderQryRsp:
    return OrderQryRsp(
        instrument="m2701", order_ref=ref, direction="0", offset="0",
        status=status, volume_total=1, volume_traded=0,
    )


@pytest.mark.parametrize("status", ["1", "2", "3", "a"])
def test_order_rsp_active_statuses(status):
    assert _rsp(status).is_active is True


@pytest.mark.parametrize("status", ["0", "4", "5", ""])
def test_order_rsp_terminal_statuses(status):
    assert _rsp(status).is_active is False


def test_order_rsp_describe():
    text = _rsp("3").describe()
    assert "m2701" in text and "未成交" in text


# ---------------------------------------------------------------- 启动清理


class _FakeQrySession:
    def __init__(self, events):
        self.events = list(events)
        self.queried = 0
        self.cancels: list[tuple[str, str, str]] = []

    def query_order(self, instrument=""):
        self.queried += 1

    def next_event(self, timeout=0.5):
        return self.events.pop(0) if self.events else None

    def order_action(self, order_ref, instrument, order_sys_id=""):
        self.cancels.append((order_ref, instrument, order_sys_id))


class _FakeJournal:
    def __init__(self):
        self.entries = []

    def log(self, kind, **kw):
        self.entries.append((kind, kw))

    def record_notes(self, notes):
        pass


def _runtime(session, *, dry_run=False):
    from run_live import LiveRuntime

    rt = object.__new__(LiveRuntime)
    rt.session = session
    rt.dry_run = dry_run
    rt.journal = _FakeJournal()
    return rt


def test_clear_orphans_cancels_only_active():
    session = _FakeQrySession([
        _rsp("3", "1"), _rsp("0", "2"), QueryDone(query="order", ok=True),
    ])
    rt = _runtime(session)
    lines = rt._clear_orphan_orders(timeout=1.0)
    assert session.queried == 1
    assert session.cancels == [("1", "m2701", "")]
    assert len(lines) == 1 and "撤销孤儿委托" in lines[0]


def test_clear_orphans_none_is_silent():
    session = _FakeQrySession([_rsp("0", "1"), QueryDone(query="order", ok=True)])
    rt = _runtime(session)
    assert rt._clear_orphan_orders(timeout=1.0) == []
    assert session.cancels == []


def test_clear_orphans_skipped_in_dry_run():
    session = _FakeQrySession([])
    rt = _runtime(session, dry_run=True)
    assert rt._clear_orphan_orders(timeout=0.2) == []
    assert session.queried == 0


def test_clear_orphans_query_failure_does_not_raise():
    class _Boom(_FakeQrySession):
        def query_order(self, instrument=""):
            raise RuntimeError("柜台未就绪")

    rt = _runtime(_Boom([]))
    assert rt._clear_orphan_orders(timeout=0.2) == []


# ---------------------------------------------------------------- 退出收尾


class _FakeOrder:
    symbol = "M0"

    def __init__(self, ref="1"):
        self.order_ref = ref
        self.instrument = "m2701"
        self.order_sys_id = "S1"

    def describe(self):
        return "m2701 买开 1手@3000（挂单中 0/1）"


class _FakeBook:
    def __init__(self, drain_on_pump: bool):
        self.orders = [_FakeOrder()]
        self.drain_on_pump = drain_on_pump

    def active_orders(self, symbol=None):
        return list(self.orders)


def test_drain_orders_cancels_and_waits():
    session = _FakeQrySession([])
    rt = _runtime(session)
    rt.book = _FakeBook(drain_on_pump=True)

    def _pump(seconds=0.5):
        rt.book.orders.clear()

    rt.pump = _pump
    notes = rt.drain_orders(timeout=1.0)
    assert session.cancels == [("1", "m2701", "S1")]
    assert notes == []


def test_drain_orders_timeout_reports_pending():
    session = _FakeQrySession([])
    rt = _runtime(session)
    rt.book = _FakeBook(drain_on_pump=False)
    rt.pump = lambda seconds=0.5: None
    notes = rt.drain_orders(timeout=0.1)
    assert notes and notes[0].kind == "exit_pending"


def test_drain_orders_noop_when_empty():
    session = _FakeQrySession([])
    rt = _runtime(session)
    rt.book = _FakeBook(drain_on_pump=True)
    rt.book.orders.clear()
    assert rt.drain_orders(timeout=0.1) == []


# ---------------------------------------------------------------- 告警


@pytest.fixture()
def sent(monkeypatch):
    box: list[str] = []
    monkeypatch.setattr(alert_mod, "send_markdown", lambda text, **kw: box.append(text) or True)
    alert_mod.reset()
    return box


def test_alert_dedup_within_ttl(sent):
    now = dt.datetime(2026, 11, 6, 10, 0, 0)
    assert alert_mod.alert("k", "第一次", now=now) is True
    assert alert_mod.alert("k", "冷却中", now=now + dt.timedelta(seconds=10)) is False
    assert alert_mod.alert("k", "过了冷却", now=now + dt.timedelta(seconds=400)) is True
    assert len(sent) == 2


def test_alert_dedup_key_includes_symbol(sent):
    now = dt.datetime(2026, 11, 6, 10, 0, 0)
    assert alert_mod.alert("k", "M0", symbol="M0", now=now) is True
    assert alert_mod.alert("k", "RB0", symbol="RB0", now=now) is True
    assert len(sent) == 2


def test_alert_daily_cap(sent):
    now = dt.datetime(2026, 11, 6, 10, 0, 0)
    assert alert_mod.alert("a", "1", now=now, daily_cap=2) is True
    assert alert_mod.alert("b", "2", now=now, daily_cap=2) is True
    assert alert_mod.alert("c", "3", now=now, daily_cap=2) is False
    assert len(sent) == 2


def test_alert_cap_resets_next_day(sent):
    day1 = dt.datetime(2026, 11, 6, 10, 0, 0)
    day2 = dt.datetime(2026, 11, 7, 10, 0, 0)
    assert alert_mod.alert("a", "1", now=day1, daily_cap=1) is True
    assert alert_mod.alert("b", "2", now=day1, daily_cap=1) is False
    assert alert_mod.alert("c", "3", now=day2, daily_cap=1) is True


def test_alert_without_webhook_returns_false(monkeypatch):
    monkeypatch.setattr(alert_mod, "send_markdown", lambda text, **kw: False)
    alert_mod.reset()
    assert alert_mod.alert("k", "无 webhook", now=dt.datetime(2026, 11, 6, 10, 0, 0)) is False


def test_alert_writes_journal(sent):
    journal = _FakeJournal()
    alert_mod.alert("k", "内容", symbol="M0", journal=journal,
                    now=dt.datetime(2026, 11, 6, 10, 0, 0))
    assert journal.entries[0][0] == "alert_k"
    assert journal.entries[0][1]["symbol"] == "M0"
