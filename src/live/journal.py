"""事件流与仿真日报（v2.0 · docs/16 T2.0-5b）。

- ``Journal``：结构化事件流——``output/live/journal/live_<YYYYMMDD>.jsonl``
  按日滚动（完整事件源），同时登记结果库 ``live_*`` 表（结构化索引；
  ``conn`` 缺省时只写文件，入库失败不影响运行）；
- ``build_daily_report``：当日运行汇总 → markdown（信号 / 执行 / 成交 / 持仓 /
  双账本权益 / 风控 / 连接异常），供落地文件与企微推送；
- ``push_report``：企微推送（复用 ``notify``；未配置自动降级为日志）。

约定：订单终态（filled/canceled/rejected）登记 ``live_orders``；
每条成交登记 ``live_fills``；其余事件（信号/风控/对账/连接）进 ``live_events``。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..results import db as results_db
from ..utils import notify
from ..utils.logger import get_logger

log = get_logger("live.journal")


def _now_hms() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def positions_brief(ledger) -> str:
    """持仓简述（如 "RB0-2、M0-1"；空仓时"空仓"）。"""
    parts = []
    for sym in ledger.contracts:
        n = ledger.net_size(sym)
        if n:
            parts.append(f"{sym}{n:+d}")
    return "、".join(parts) or "空仓"


class Journal:
    """结构化事件流（jsonl + 结果库可选）。"""

    def __init__(self, journal_dir: str | Path, *, conn=None, session_id: int = 0):
        self.dir = Path(journal_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.conn = conn
        self.session_id = int(session_id or 0)
        self.entries: list[dict] = []

    # -------------------------------------------------- 写事件

    def log(
        self,
        kind: str,
        *,
        symbol: str = "",
        detail: str = "",
        order_ref: str = "",
        extra: dict | None = None,
        date: str = "",
    ) -> dict:
        entry: dict = {
            "ts": _now_hms(),
            "date": date or _today(),
            "kind": kind,
            "symbol": symbol,
            "detail": detail,
        }
        if order_ref:
            entry["order_ref"] = order_ref
        if extra:
            entry.update(extra)

        path = self.dir / f"live_{entry['date'].replace('-', '')}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        if self.conn is not None and self.session_id:
            try:
                payload = {
                    k: v
                    for k, v in entry.items()
                    if k not in ("ts", "date", "kind", "symbol")
                }
                results_db.record_live_event(
                    self.conn,
                    self.session_id,
                    ts=str(entry["ts"]),
                    kind=kind,
                    symbol=symbol,
                    detail=payload,
                )
            except Exception as e:  # noqa: BLE001 - 入库失败不影响运行
                log.warning("事件入库失败（不影响运行）：%s", e)

        self.entries.append(entry)
        return entry

    def record_notes(self, notes) -> None:
        """把执行层事件（``adapter.ExecNote``）落入事件流。"""
        for n in notes or []:
            self.log(n.kind, symbol=n.symbol, detail=n.detail, order_ref=n.order_ref)

    def record_order_terminal(self, order) -> None:
        """订单到达终态：进事件流 + ``live_orders`` 表。"""
        self.log(
            "order",
            symbol=order.symbol,
            detail=order.describe(),
            order_ref=order.order_ref,
            extra={
                "status": order.status,
                "filled": order.filled,
                "price": order.price,
                "volume": order.volume,
                "instrument": order.instrument,
                "direction": order.direction,
                "offset": order.offset,
            },
        )
        if self.conn is not None and self.session_id:
            try:
                results_db.record_live_order(
                    self.conn,
                    self.session_id,
                    order_ref=order.order_ref,
                    instrument=order.instrument,
                    direction=order.direction,
                    offset=order.offset,
                    price=float(order.price),
                    volume=int(order.volume),
                    status=order.status,
                    ts_submit=datetime.fromtimestamp(order.created_ts).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    ts_update=datetime.fromtimestamp(order.updated_ts).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    detail={"filled": order.filled, "msg": order.status_msg},
                )
            except Exception as e:  # noqa: BLE001
                log.warning("订单入库失败：%s", e)

    def record_trade(self, ev, *, commission: float = 0.0) -> None:
        """成交回报：进事件流 + ``live_fills`` 表。"""
        detail = (
            f"{ev.instrument} 成交 {ev.volume} 手 @{ev.price}"
            f"（{'买' if str(ev.direction) == '0' else '卖'}，{ev.offset}）"
        )
        self.log(
            "fill",
            symbol=ev.instrument,
            detail=detail,
            order_ref=ev.order_ref,
            extra={"volume": ev.volume, "price": ev.price},
        )
        if self.conn is not None and self.session_id:
            try:
                results_db.record_live_fill(
                    self.conn,
                    self.session_id,
                    order_ref=ev.order_ref,
                    trade_id=ev.trade_id,
                    instrument=ev.instrument,
                    direction=str(ev.direction),
                    offset=str(ev.offset),
                    price=float(ev.price),
                    volume=int(ev.volume),
                    ts=f"{ev.trade_date} {ev.trade_time}".strip(),
                    commission=float(commission),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("成交入库失败：%s", e)

    def entries_of(self, date: str) -> list[dict]:
        return [e for e in self.entries if e.get("date") == date]


# ---------------------------------------------------------------- 日报


def build_daily_report(
    *,
    date: str,
    ledger,
    entries: list[dict],
    fuse_state: str = "",
    counter_equity: float | None = None,
    reconciles=None,
) -> str:
    """当日运行汇总 → markdown。"""
    ev = [e for e in entries if e.get("date") == date]

    def _of(*kinds: str) -> list[dict]:
        return [e for e in ev if e["kind"] in kinds]

    sig = _of("signal")
    opens = _of("open")
    closes = _of("close")
    stops = _of("stop")
    chases = _of("chase", "abandon")
    risks = _of("risk", "refuse", "reject", "margin_degrade")
    fills = _of("fill")
    conns = _of("connect", "disconnect", "heartbeat")
    errors = _of("error")

    lines: list[str] = []
    lines.append(f"**quant-demo · SimNow 仿真日报**（{date}）")
    lines.append("")
    lines.append(
        f"**账本（10 万口径）**：权益 {ledger.equity:,.2f} ｜ "
        f"可用 {ledger.available:,.2f} ｜ 保证金 {ledger.margin_used:,.2f}"
    )
    if counter_equity is not None:
        lines.append(f"**柜台权益**：{counter_equity:,.2f}")
    lines.append(f"**持仓**：{positions_brief(ledger)} ｜ **熔断状态**：{fuse_state or '正常'}")
    lines.append("")
    lines.append(f"**当日信号**：{len(sig)} 条")
    for e in sig[:8]:
        lines.append(f"- {e['detail']}")
    lines.append(
        f"**执行**：开仓 {len(opens)} 笔 ｜ 平仓 {len(closes)} 笔 ｜ 成交 {len(fills)} 笔"
    )
    for e in fills[:8]:
        lines.append(f"- {e['detail']}")
    if stops:
        lines.append(f"**止损 / 止盈**：{len(stops)} 次")
        for e in stops[:5]:
            lines.append(f"- {e['detail']}")
    if chases:
        lines.append(f"**追单 / 放弃**：{len(chases)} 次")
        for e in chases[:5]:
            lines.append(f"- {e['detail']}")
    if risks:
        lines.append(f"**风控拦截 / 提示**：{len(risks)} 次")
        for e in risks[:5]:
            lines.append(f"- {e['detail']}")
    if conns:
        lines.append(f"**连接事件**：{len(conns)} 次")
        for e in conns[:5]:
            lines.append(f"- {e['detail']}")
    if errors:
        lines.append(f"**异常**：{len(errors)} 条")
        for e in errors[:5]:
            lines.append(f"- {e['detail']}")
    if reconciles:
        bad = [r for r in reconciles if not r.match]
        lines.append("")
        lines.append(f"**对账**：{len(reconciles) - len(bad)}/{len(reconciles)} 项一致")
        for r in bad:
            lines.append(f"- {r.describe()}")

    lines.append("")
    lines.append("_SimNow 仿真环境 · 模拟 ≠ 实盘 · 仅验证工程链路_")
    return "\n".join(lines)


def push_report(md_text: str) -> bool:
    """企微推送日报（未配置自动降级为日志）。"""
    ok = notify.send_markdown(md_text)
    if ok:
        log.info("日报已推送（企业微信）")
    else:
        log.info("日报推送未生效（webhook 未配置或发送失败），已生成文本")
    return ok


def save_report(md_text: str, reports_dir: str | Path, date: str) -> Path:
    """日报落地为 markdown 文件。"""
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"sim_{date.replace('-', '')}.md"
    path.write_text(md_text, encoding="utf-8")
    return path
