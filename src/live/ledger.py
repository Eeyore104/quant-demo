"""虚拟账本（v2.0 · docs/16 T2.0-3b）：10 万口径，与回测账户逐项对齐。

**核心复用**：直接使用组合引擎的 ``PortfolioAccount`` 做入账——成交 → 引擎
``Trade`` → ``account.apply_trades``，保证金 / 可用 / 权益 / 敞口全部透传；
口径与回测「同一份代码」，天然一致。

本模块额外负责：
- 合约映射（真实合约 m2701 ↔ 品种代码 M0）
- 今仓 / 昨仓分组（上期所平今平昨拆单依据；按成交回报的交易日归属）
- 日终统计（day_idx / day_ref_equity / 峰值 / 连亏笔数——熔断判定输入）
- 降级执行（资金不足 → 可负担手数）
- 状态持久化（JSON 序列化 / 恢复，重启不丢账）

成交回报的手续费按 config 口径计提（``commission_per_lot × volume``），
与回测的成本假设一致。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..engine.account import PortfolioAccount
from ..engine.contracts import ContractSpec
from ..engine.portfolio import Trade
from ..utils.logger import get_logger

log = get_logger("live.ledger")

# 业务语义开平（与 CTP 值互转由 adapter 负责）
OFFSET_OPEN = "OPEN"
OFFSET_CLOSE = "CLOSE"
OFFSET_CLOSE_TODAY = "CLOSE_TODAY"
OFFSET_CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class VirtualLedger:
    """虚拟账本：10 万口径的组合账户（包装 PortfolioAccount）。"""

    def __init__(
        self,
        initial_capital: float,
        contracts: dict[str, ContractSpec],
        instrument_map: dict[str, str],
    ):
        """``instrument_map``：真实合约 → 品种代码（如 ``{"m2701": "M0"}``）。"""
        self.initial_capital = float(initial_capital)
        self.contracts = contracts
        self.instrument_map = dict(instrument_map)
        self.symbol_to_instrument = {v: k for k, v in instrument_map.items()}

        self.account = PortfolioAccount(initial_capital, contracts)
        # 今昨仓：{(symbol, side): {trading_day: 未平手数}}
        self.open_lots: dict[tuple[str, str], dict[str, int]] = {}

        # 统计（熔断 / 信号基准）
        self.day_idx = 0  # 仿真运行第几个交易日（自 1 起）
        self.day_ref_equity = float(initial_capital)  # 当日开盘前权益（信号手数基准）
        self.prev_equity = float(initial_capital)  # 前一交易日收盘权益
        self.peak_equity = float(initial_capital)  # 历史峰值权益
        self.losing_streak = 0  # 连续亏损笔数（平仓口径）

    # -------------------------------------------------- 只读量

    @property
    def equity(self) -> float:
        return self.account.equity

    @property
    def available(self) -> float:
        return self.account.available

    @property
    def margin_used(self) -> float:
        return self.account.margin_used

    @property
    def total_commission(self) -> float:
        return self.account.total_commission

    def net_size(self, symbol: str) -> int:
        """净持仓（正=多 / 负=空）。"""
        return int(self.account.state(symbol).size)

    def state(self, symbol: str):
        return self.account.state(symbol)

    def exposures(self) -> dict[str, float]:
        """逐品种名义敞口（风控 / 日报用）。"""
        return {s: self.account.exposure_notional(s) for s in self.contracts}

    # -------------------------------------------------- 成交入账

    def apply_fill(
        self,
        *,
        instrument: str,
        direction: str,
        offset: str,
        price: float,
        volume: int,
        trading_day: str,
    ) -> Trade:
        """成交回报入账（业务语义 offset：OPEN / CLOSE / CLOSE_TODAY / CLOSE_YESTERDAY）。"""
        symbol = self.instrument_map.get(instrument)
        if symbol is None:
            raise ValueError(f"未登记的合约：{instrument}（检查 live.contracts_map）")
        spec = self.contracts[symbol]
        action = "OPEN" if offset == OFFSET_OPEN else "CLOSE"
        trade = Trade(
            date=trading_day,
            symbol=symbol,
            direction=direction,
            action=action,
            price=float(price),
            volume=int(volume),
            commission=spec.commission_per_lot * int(volume),
        )

        # 今昨仓分组（先于资金入账维护；平仓按「先昨后今」扣减）
        if action == "OPEN":
            book = self.open_lots.setdefault((symbol, direction), {})
            book[trading_day] = book.get(trading_day, 0) + int(volume)
        else:
            self._reduce_open_lots(symbol, direction, int(volume))

        self.account.apply_trades([trade])

        # 连亏统计（平仓口径，与回测 record_closes 一致）
        if action == "CLOSE":
            if trade.pnl < 0:
                self.losing_streak += 1
            elif trade.pnl > 0:
                self.losing_streak = 0
        log.info(
            "账本入账：%s %s %s %d手 @%.4f（手续费 %.2f，已实现 %s）",
            instrument,
            "买" if direction == "LONG" else "卖",
            offset,
            volume,
            price,
            trade.commission,
            f"{trade.pnl:,.0f}" if action == "CLOSE" else "—",
        )
        return trade

    def _reduce_open_lots(self, symbol: str, direction: str, volume: int) -> None:
        """平仓扣减（先昨后今）。"""
        book = self.open_lots.get((symbol, direction))
        if not book:
            return
        remaining = volume
        for day in sorted(book):  # 旧日期在前
            if remaining <= 0:
                break
            take = min(book[day], remaining)
            book[day] -= take
            remaining -= take
            if book[day] == 0:
                del book[day]

    # -------------------------------------------------- 今昨仓查询（拆单依据）

    def today_lots(self, symbol: str, direction: str, current_day: str) -> int:
        return int((self.open_lots.get((symbol, direction)) or {}).get(current_day, 0))

    def yesterday_lots(self, symbol: str, direction: str, current_day: str) -> int:
        book = self.open_lots.get((symbol, direction)) or {}
        return int(sum(n for d, n in book.items() if d != current_day))

    def close_breakdown(
        self, symbol: str, direction: str, volume: int, current_day: str
    ) -> list[tuple[str, int]]:
        """平仓拆单：先平昨、再平今；返回 [(offset, volume), ...]。"""
        n_y = min(volume, self.yesterday_lots(symbol, direction, current_day))
        n_t = volume - n_y
        parts: list[tuple[str, int]] = []
        if n_y > 0:
            parts.append((OFFSET_CLOSE_YESTERDAY, n_y))
        if n_t > 0:
            parts.append((OFFSET_CLOSE_TODAY, n_t))
        return parts

    # -------------------------------------------------- 降级执行

    def affordable_lots(self, symbol: str, price: float, want_lots: int) -> tuple[int, str]:
        """资金不足 → 按可负担手数降级（v1.3 margin_degrade 口径）。

        返回 (可开手数, 说明文字)；资金充足时说明为空。
        """
        needed = self.account.open_margin(symbol, want_lots, price)
        avail = self.account.available
        if needed <= avail + 1e-9:
            return want_lots, ""
        per_lot = self.account.open_margin(symbol, 1, price)
        n = int(avail // per_lot) if per_lot > 0 else 0
        n = max(0, n)
        detail = (
            f"资金不足：目标 {want_lots} 手 → 可负担 {n} 手"
            f"（需 {needed:,.0f} / 可用 {avail:,.0f} 元）"
        )
        return n, detail

    # -------------------------------------------------- 日推进（时序对齐回测主循环）

    def begin_day(self) -> None:
        """交易日开始（21:00 开盘前调用）：天序 +1，锁定当日信号手数基准。

        对应回测主循环「每天开始：day_idx += 1；day_ref_equity = equity」
        （此时价格仍为上一交易日收盘价，权益即"开盘前权益"）。
        """
        self.day_idx += 1
        self.day_ref_equity = self.equity

    def close_day(self) -> dict:
        """交易日收盘结算（熔断判定之后调用）：更新峰值 / 前一日权益。

        调用时序必须为：risk_guard 用「旧 prev_equity / 旧 peak_equity」完成
        ``fuse.day_end`` 判定后，再调用本方法推进。
        """
        equity = self.equity
        snap = {
            "day_idx": self.day_idx,
            "equity": equity,
            "prev_equity": self.prev_equity,
            "peak": self.peak_equity,
            "losing_streak": self.losing_streak,
        }
        self.peak_equity = max(self.peak_equity, equity)
        self.prev_equity = equity
        return snap

    # -------------------------------------------------- 持久化

    def to_dict(self) -> dict:
        return {
            "initial_capital": self.initial_capital,
            "cash": self.account.cash,
            "states": {
                s: {
                    "size": st.size,
                    "avg_price": st.avg_price,
                    "last_price": st.last_price,
                    "realized_pnl": st.realized_pnl,
                    "commission_paid": st.commission_paid,
                }
                for s, st in self.account.states.items()
            },
            "open_lots": {
                f"{sym}|{side}": {d: n for d, n in book.items()}
                for (sym, side), book in self.open_lots.items()
            },
            "stats": {
                "day_idx": self.day_idx,
                "day_ref_equity": self.day_ref_equity,
                "prev_equity": self.prev_equity,
                "peak_equity": self.peak_equity,
                "losing_streak": self.losing_streak,
            },
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("账本已保存：%s", p)

    def load(self, path: str | Path) -> bool:
        """从磁盘恢复；文件不存在返回 False。"""
        p = Path(path)
        if not p.exists():
            return False
        d = json.loads(p.read_text(encoding="utf-8"))
        self.account.cash = float(d["cash"])
        for s, st in (d.get("states") or {}).items():
            if s in self.account.states:
                target = self.account.states[s]
                target.size = int(st["size"])
                target.avg_price = float(st["avg_price"])
                target.last_price = float(st["last_price"])
                target.realized_pnl = float(st["realized_pnl"])
                target.commission_paid = float(st["commission_paid"])
        self.open_lots = {}
        for key, book in (d.get("open_lots") or {}).items():
            sym, side = key.split("|", 1)
            self.open_lots[(sym, side)] = {str(k): int(v) for k, v in book.items()}
        stats = d.get("stats") or {}
        self.day_idx = int(stats.get("day_idx", 0))
        self.day_ref_equity = float(stats.get("day_ref_equity", self.initial_capital))
        self.prev_equity = float(stats.get("prev_equity", self.initial_capital))
        self.peak_equity = float(stats.get("peak_equity", self.initial_capital))
        self.losing_streak = int(stats.get("losing_streak", 0))
        log.info("账本已恢复：%s（权益 %.2f）", p, self.equity)
        return True
