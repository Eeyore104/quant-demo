"""对账模块测试（v2.0 轮 3）：柜台分行聚合 / 逐项核对 / 装配与容差。"""

from src.engine.contracts import ContractSpec
from src.live.ctp_api import AccountRsp, PositionRsp, QueryDone, SettlementConfirmed
from src.live.ledger import VirtualLedger
from src.live.reconcile import (
    CounterSnapshot,
    counter_position_map,
    local_position_map,
    query_counter,
    reconcile_all,
    reconcile_equity,
    reconcile_positions,
)


class FakeSession:
    """假会话：按顺序吐事件（wait_for 消费式）。"""

    def __init__(self, events):
        self.events = list(events)
        self.queried: list[str] = []

    def wait_for(self, kinds, timeout=10.0, pred=None):
        for i, ev in enumerate(self.events):
            if isinstance(ev, kinds):
                return self.events.pop(i)
        return None

    def query_account(self):
        self.queried.append("account")

    def query_position(self):
        self.queried.append("position")


def _ledger() -> VirtualLedger:
    contracts = {
        "M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2),
        "RB0": ContractSpec("RB0", 10, 1.0, 0.07, 3.1),
    }
    lg = VirtualLedger(100000, contracts, {"m2701": "M0", "rb2701": "RB0"})
    lg.begin_day()
    return lg


def test_counter_map_aggregates_split_rows():
    # 柜台按今昨 / 明细分行返回：1 + 1 = 2（聚合求和）
    snap = CounterSnapshot(
        account=None,
        positions=[
            PositionRsp(instrument="m2701", posi_direction="3", position=1),
            PositionRsp(instrument="m2701", posi_direction="3", position=1),
            PositionRsp(instrument="rb2701", posi_direction="2", position=2),
        ],
    )
    agg = counter_position_map(snap)
    assert agg == {("m2701", -1): 2, ("rb2701", 1): 2}
    # 零仓行被过滤
    snap2 = CounterSnapshot(None, [PositionRsp(instrument="x", posi_direction="2", position=0)])
    assert counter_position_map(snap2) == {}


def test_local_position_map():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="OPEN", price=3000.0,
        volume=1, trading_day="2026-10-13",
    )
    assert local_position_map(lg) == {("m2701", -1): 1}


def test_positions_match_and_mismatch():
    local = {("m2701", -1): 1}
    counter = {("m2701", -1): 1, ("rb2701", 1): 2}
    items = reconcile_positions(local, counter)
    by_scope = {i.scope: i for i in items}
    assert by_scope["m2701 空"].match is True
    assert by_scope["rb2701 多"].match is False  # 本地缺失
    assert by_scope["rb2701 多"].local == "0"


def test_query_counter_full_flow():
    events = [
        SettlementConfirmed(ok=True),
        AccountRsp(balance=20_000_000.0, available=19_990_000.0, curr_margin=10_000.0),
        QueryDone(query="account"),
        PositionRsp(instrument="m2701", posi_direction="3", position=1),
        QueryDone(query="position"),
    ]
    sess = FakeSession(events)
    snap = query_counter(sess)
    assert snap.ok is True
    assert snap.account is not None and snap.account.balance == 20_000_000.0
    assert len(snap.positions) == 1
    assert sess.queried == ["account", "position"]


def test_query_counter_not_ready():
    sess = FakeSession([])  # 无任何事件（登录未就绪）
    snap = query_counter(sess)
    assert snap.ok is False and "未就绪" in snap.detail


def test_reconcile_all_pass():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="OPEN", price=3000.0,
        volume=1, trading_day="2026-10-13",
    )
    lg.apply_fill(
        instrument="rb2701", direction="LONG", offset="OPEN", price=3000.0,
        volume=2, trading_day="2026-10-13",
    )
    lg.account.mark_price("M0", 3000.0)
    lg.account.mark_price("RB0", 3000.0)
    snap = CounterSnapshot(
        account=AccountRsp(balance=20_000_000.0 - 4.0),  # 变动 ≈ 本地手续费 3.6
        positions=[
            PositionRsp(instrument="m2701", posi_direction="3", position=1),
            PositionRsp(instrument="rb2701", posi_direction="2", position=2),
        ],
        ok=True,
    )
    items = reconcile_all(lg, snap)
    assert len(items) == 3  # 2 持仓 + 1 权益
    assert all(i.match for i in items)


def test_equity_mismatch_detected():
    lg = _ledger()
    snap = CounterSnapshot(account=AccountRsp(balance=20_000_000.0 + 5000.0), positions=[], ok=True)
    items = reconcile_equity(lg, snap)
    assert items[0].match is False  # 本地变动 0 vs 柜台 +5000
    assert "容差" in items[0].detail


def test_equity_missing_account():
    lg = _ledger()
    items = reconcile_equity(lg, CounterSnapshot(account=None, positions=[], ok=False))
    assert items[0].match is False and "不可用" in items[0].detail
