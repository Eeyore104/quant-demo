"""对账（v2.0 · docs/16 T2.0-5a）：本地虚拟账本 vs SimNow 柜台。

- ``query_counter(session)``：等交易就绪 → 查资金 + 持仓 → 柜台快照；
- ``reconcile_positions``：逐（合约 × 方向）核对（柜台按今昨分行返回 → 聚合求和）；
- ``reconcile_equity``：权益趋势核对（两账本金不同：2000 万 vs 10 万，核「相对初始
  的变动」差异在容差内——手续费率 / 保证金率口径差异允许小幅偏差）；
- 输出 ``ReconcileItem`` 列表（供日报 / journal）。

持仓为**严格一致**要求；权益为**趋势核对**（容忍口径差）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..utils.logger import get_logger
from .ctp_api import (
    AccountRsp,
    CtpSession,
    PositionRsp,
    QueryDone,
    SettlementConfirmed,
)
from .ledger import VirtualLedger

log = get_logger("live.reconcile")

# CTP PosiDirection：'2' = 多、'3' = 空
DIR_TO_SIDE = {"2": 1, "3": -1}

# SimNow 初始模拟资金（账户级固定值；用于「相对初始的变动」口径）
COUNTER_INITIAL = 20_000_000.0


@dataclass
class CounterSnapshot:
    """柜台快照（资金 + 持仓）。"""

    account: AccountRsp | None
    positions: list[PositionRsp]
    ok: bool = False
    detail: str = ""


@dataclass
class ReconcileItem:
    """一条对账结论。"""

    kind: str  # position / equity
    scope: str  # 合约 + 方向，或「当日盈亏」
    local: str
    counter: str
    match: bool
    detail: str = ""

    def describe(self) -> str:
        mark = "一致" if self.match else "差异"
        extra = f"（{self.detail}）" if self.detail else ""
        return f"[{mark}] {self.kind} {self.scope}：本地 {self.local} / 柜台 {self.counter}{extra}"


def query_counter(
    session: CtpSession, *, account_timeout: float = 10.0, position_timeout: float = 15.0
) -> CounterSnapshot:
    """查询柜台资金与持仓（内部等待交易就绪）。

    就绪判定：优先使用会话**持续状态** ``td_ready``（启动后保持为真、断线自动置假）；
    ``td_ready`` 不可用（如测试替身）或为假（断线重连中）时，回退等待
    ``SettlementConfirmed`` 事件。
    """
    td_ready = getattr(session, "td_ready", None)
    if td_ready is not True:
        settle = session.wait_for(SettlementConfirmed, timeout=30)
        if settle is None or not settle.ok:
            return CounterSnapshot(None, [], False, "交易未就绪（登录 / 结算确认失败）")

    session.query_account()
    acc = session.wait_for(AccountRsp, timeout=account_timeout)
    session.wait_for(QueryDone, timeout=3)

    session.query_position()
    positions: list[PositionRsp] = []
    deadline = time.time() + position_timeout
    while time.time() < deadline:
        ev = session.wait_for((PositionRsp, QueryDone), timeout=3)
        if isinstance(ev, PositionRsp):
            positions.append(ev)
        elif isinstance(ev, QueryDone) and ev.query == "position":
            break

    if acc is None:
        return CounterSnapshot(None, positions, False, "资金查询无响应")
    return CounterSnapshot(acc, positions, True, "")


def counter_position_map(snapshot: CounterSnapshot) -> dict[tuple[str, int], int]:
    """柜台持仓聚合：{(合约, +1/-1): 手数}（今昨 / 多空分行 → 求和）。"""
    agg: dict[tuple[str, int], int] = {}
    for p in snapshot.positions:
        side = DIR_TO_SIDE.get(str(p.posi_direction), 0)
        if side == 0:
            continue
        key = (str(p.instrument), side)
        agg[key] = agg.get(key, 0) + int(p.position)
    return {k: v for k, v in agg.items() if v != 0}


def local_position_map(ledger: VirtualLedger) -> dict[tuple[str, int], int]:
    """本地账本持仓：{(合约, +1/-1): 手数}。"""
    out: dict[tuple[str, int], int] = {}
    for sym in ledger.contracts:
        size = ledger.net_size(sym)
        if size == 0:
            continue
        inst = ledger.symbol_to_instrument.get(sym)
        if not inst:
            continue
        out[(inst, 1 if size > 0 else -1)] = abs(size)
    return out


def reconcile_positions(
    local: dict[tuple[str, int], int], counter: dict[tuple[str, int], int]
) -> list[ReconcileItem]:
    """持仓逐项对账（严格一致）。"""
    items: list[ReconcileItem] = []
    for key in sorted(set(local) | set(counter)):
        inst, side = key
        lv = int(local.get(key, 0))
        cv = int(counter.get(key, 0))
        side_txt = "多" if side > 0 else "空"
        items.append(
            ReconcileItem(
                kind="position",
                scope=f"{inst} {side_txt}",
                local=str(lv),
                counter=str(cv),
                match=(lv == cv),
            )
        )
    return items


def reconcile_equity(
    ledger: VirtualLedger,
    snapshot: CounterSnapshot,
    *,
    counter_initial: float = COUNTER_INITIAL,
) -> list[ReconcileItem]:
    """权益趋势核对：两账本「相对初始的变动」差异 ≤ 容差（名义权益 0.5% 或 100 元取大）。"""
    if snapshot.account is None:
        return [ReconcileItem("equity", "当日盈亏", "—", "—", False, "柜台资金不可用")]
    local_delta = ledger.equity - ledger.initial_capital
    counter_delta = float(snapshot.account.balance) - float(counter_initial)
    tol = max(abs(ledger.initial_capital) * 0.005, 100.0)
    diff = abs(local_delta - counter_delta)
    return [
        ReconcileItem(
            kind="equity",
            scope="当日盈亏",
            local=f"{local_delta:,.2f}",
            counter=f"{counter_delta:,.2f}",
            match=diff <= tol,
            detail=f"差异 {diff:,.2f} / 容差 {tol:,.2f}",
        )
    ]


def reconcile_all(ledger: VirtualLedger, snapshot: CounterSnapshot) -> list[ReconcileItem]:
    """完整对账（持仓 + 权益趋势），返回逐项结论。"""
    items = reconcile_positions(local_position_map(ledger), counter_position_map(snapshot))
    items += reconcile_equity(ledger, snapshot)
    bad = [i for i in items if not i.match]
    if bad:
        log.warning("对账完成：%d 项，%d 项差异！", len(items), len(bad))
        for i in bad:
            log.warning("  %s", i.describe())
    else:
        log.info("对账完成：%d 项全部一致", len(items))
    return items
