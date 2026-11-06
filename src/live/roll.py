"""换月计划（v2.2 · docs/21 T2.2-3）：数据结构 + 落盘 + 人工确认。

**半自动的边界**：

- 系统负责「算」——主力候选、到期倒计时、移仓手数，落成 ``output/live/plans/roll_<id>.json``；
- 人负责「批」——``run_live.py --confirm-roll <plan_id>`` 才允许执行；
- 超时未批——进入 ``force_close`` 窗口后由 ``lifecycle`` 只平不开兜底，绝不自动移仓。

**为什么计划要落盘而不是内存**：换月是跨日动作（盘后出计划、次日开盘执行），
进程随时可能被看门狗重启；计划必须能在重启后继续被读到，且带「谁在什么时候批的」。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..utils.logger import get_logger

log = get_logger("live.roll")

PLAN_DIR = "output/live/plans"


@dataclass
class RollItem:
    """一条移仓腿：把 symbol 的持仓从旧合约搬到新合约。"""

    symbol: str  # AL0
    from_instrument: str  # al2611
    to_instrument: str  # al2612
    direction: str  # LONG / SHORT（跟随账本，不由计划决定）
    lots: int
    reason: str = "expiry"  # expiry | oi_switch | manual
    days_left: int = 0
    oi_from: float = 0.0  # 持仓量（拿不到则 0）
    oi_to: float = 0.0


@dataclass
class RollPlan:
    """一份换月计划（一个交易日一份）。"""

    plan_id: str  # 20261120-01
    date: str  # 2026-11-20
    items: list[RollItem] = field(default_factory=list)
    confirmed: bool = False
    confirmed_at: str = ""
    confirmed_by: str = ""
    executed_at: str = ""  # 非空 = 已执行（防重复移仓）

    @property
    def empty(self) -> bool:
        return not self.items

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> RollPlan:
        items = [RollItem(**x) for x in (raw.get("items") or [])]
        return cls(
            plan_id=str(raw.get("plan_id") or ""),
            date=str(raw.get("date") or ""),
            items=items,
            confirmed=bool(raw.get("confirmed", False)),
            confirmed_at=str(raw.get("confirmed_at") or ""),
            confirmed_by=str(raw.get("confirmed_by") or ""),
            executed_at=str(raw.get("executed_at") or ""),
        )


def plan_dir(base: str | Path | None = None) -> Path:
    return Path(base) if base is not None else Path(PLAN_DIR)


def plan_file(plan_id: str, base: str | Path | None = None) -> Path:
    return plan_dir(base) / f"roll_{plan_id}.json"


def save_plan(plan: RollPlan, base: str | Path | None = None) -> Path:
    path = plan_file(plan.plan_id, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log.info("换月计划已写入：%s（%d 项）", path, len(plan.items))
    return path


def load_plan(plan_id: str, base: str | Path | None = None) -> RollPlan | None:
    path = plan_file(plan_id, base)
    if not path.exists():
        return None
    return RollPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))


def latest_plan(base: str | Path | None = None) -> RollPlan | None:
    """最近一份计划（按文件名倒序；文件名含日期，天然有序）。"""
    files = sorted(plan_dir(base).glob("roll_*.json"), reverse=True)
    if not files:
        return None
    return RollPlan.from_dict(json.loads(files[0].read_text(encoding="utf-8")))


def confirm_plan(
    plan_id: str, *, by: str = "operator", base: str | Path | None = None
) -> RollPlan | None:
    """人工确认：只置位 + 记时间，不改计划内容（可审计）。"""
    plan = load_plan(plan_id, base)
    if plan is None:
        return None
    plan.confirmed = True
    plan.confirmed_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan.confirmed_by = by
    save_plan(plan, base)
    return plan


def next_plan_id(today: dt.date | None = None, base: str | Path | None = None) -> str:
    """生成当日计划号：YYYYMMDD-01，同日递增。"""
    day = (today or dt.date.today()).strftime("%Y%m%d")
    seq = 1
    while plan_file(f"{day}-{seq:02d}", base).exists():
        seq += 1
    return f"{day}-{seq:02d}"


def summary_lines(plan: RollPlan) -> list[str]:
    """日报 / 日志用的一行摘要。"""
    if plan.empty:
        return []
    state = "已执行" if plan.executed_at else ("已确认" if plan.confirmed else "待确认")
    head = f"移仓计划 {plan.plan_id}（{state}）"
    lines = [head]
    for it in plan.items:
        side = it.direction or "空仓"
        lines.append(
            f"- {it.symbol}：{it.from_instrument} → {it.to_instrument}"
            f"（{side} {it.lots} 手，{it.reason}，剩 {it.days_left} 天）"
        )
    return lines
