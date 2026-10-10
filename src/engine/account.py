"""组合账户：共享资金池 + 保证金占用 + 可用资金约束（v1.2）。

口径（近似，详见 ``docs/10``）：

- 保证金占用 = Σ(|手数| × 最新价 × 乘数 × 保证金率)，每交易日按收盘价重估；
- 可用资金 = 权益 − 保证金占用；
- 开仓 / 加仓前检查「新增保证金 ≤ 可用资金」，不足则**整笔拒绝**并记事件；
- **平仓永不拒绝**；浮亏导致可用资金为负时仅记录（不强平，留给 v1.3 风控）；
- 权益 = 现金 + Σ 浮动盈亏；逐品种盈亏 = 已实现 + 浮动（不含手续费）。
"""

from dataclasses import dataclass

from .contracts import ContractSpec
from .portfolio import Trade


@dataclass
class ConstraintEvent:
    """一次「拒绝开仓」事件（保证金不足）。"""

    date: object
    symbol: str
    needed_margin: float
    available: float

    def describe(self) -> str:
        day = str(self.date)[:10]  # 仅保留日期部分（如 2025-05-22）
        return (
            f"{day} {self.symbol}：需要保证金 {self.needed_margin:,.0f} 元 ＞ "
            f"可用资金 {self.available:,.0f} 元"
        )


@dataclass
class SymbolState:
    """单品种持仓状态。"""

    symbol: str = ""
    size: int = 0  # 手数：正=多、负=空
    avg_price: float = 0.0  # 持仓均价
    last_price: float = 0.0  # 最近收盘价（盯市基准）
    realized_pnl: float = 0.0  # 累计已实现盈亏（不含手续费）
    commission_paid: float = 0.0  # 累计手续费


class PortfolioAccount:
    """共享资金账户：现金 / 保证金 / 可用资金 / 权益 / 约束事件 / 逐日记录。"""

    def __init__(self, initial_capital: float, contracts: dict[str, ContractSpec]):
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.contracts = contracts
        self.states: dict[str, SymbolState] = {s: SymbolState(symbol=s) for s in contracts}
        self.events: list[ConstraintEvent] = []
        self.daily: list[dict] = []

    # ---------- 只读量 ----------

    def state(self, symbol: str) -> SymbolState:
        return self.states[symbol]

    @property
    def margin_used(self) -> float:
        """保证金占用（按各品种最新价重估，近似口径）。"""
        total = 0.0
        for s, st in self.states.items():
            if st.size != 0 and st.last_price > 0:
                spec = self.contracts[s]
                total += abs(st.size) * st.last_price * spec.multiplier * spec.margin_rate
        return total

    @property
    def unrealized_pnl(self) -> float:
        total = 0.0
        for s, st in self.states.items():
            if st.size != 0:
                spec = self.contracts[s]
                total += (st.last_price - st.avg_price) * spec.multiplier * st.size
        return total

    @property
    def equity(self) -> float:
        return self.cash + self.unrealized_pnl

    @property
    def available(self) -> float:
        return self.equity - self.margin_used

    @property
    def total_commission(self) -> float:
        return sum(st.commission_paid for st in self.states.values())

    def symbol_pnl(self, symbol: str) -> float:
        """某品种累计盈亏 = 已实现 + 浮动（不含手续费）。"""
        st = self.states[symbol]
        unreal = 0.0
        if st.size != 0:
            spec = self.contracts[symbol]
            unreal = (st.last_price - st.avg_price) * spec.multiplier * st.size
        return st.realized_pnl + unreal

    def open_margin(self, symbol: str, lots: int, price: float) -> float:
        """新增 ``lots`` 手所需保证金（按给定价格）。"""
        spec = self.contracts[symbol]
        return abs(lots) * float(price) * spec.multiplier * spec.margin_rate

    def exposure_notional(self, symbol: str) -> float:
        """某品种名义敞口 = |手数| × 最新价 × 乘数（风控敞口口径）。"""
        st = self.states[symbol]
        if st.size == 0 or st.last_price <= 0:
            return 0.0
        spec = self.contracts[symbol]
        return abs(st.size) * st.last_price * spec.multiplier

    @property
    def total_exposure(self) -> float:
        """组合总名义敞口（各品种之和）。"""
        return sum(self.exposure_notional(s) for s in self.states)

    # ---------- 变更 ----------

    def mark_price(self, symbol: str, price: float) -> None:
        """更新某品种最新价（盯市基准）。"""
        self.states[symbol].last_price = float(price)

    def apply_trades(self, trades: list[Trade]) -> None:
        """成交入账：手续费、持仓、平仓已实现盈亏（仅处理传入的成交）。"""
        for t in trades:
            st = self.states[t.symbol]
            spec = self.contracts[t.symbol]
            self.cash -= t.commission
            st.commission_paid += t.commission
            if t.action == "OPEN":
                if st.size == 0:
                    st.avg_price = t.price
                else:  # 同向加仓：加权平均
                    old_abs = abs(st.size)
                    st.avg_price = (st.avg_price * old_abs + t.price * t.volume) / (
                        old_abs + t.volume
                    )
                st.size += t.volume if t.direction == "LONG" else -t.volume
            else:  # CLOSE
                if t.direction == "LONG":
                    realized = (t.price - st.avg_price) * t.volume * spec.multiplier
                    st.size -= t.volume
                else:
                    realized = (st.avg_price - t.price) * t.volume * spec.multiplier
                    st.size += t.volume
                t.pnl = realized
                self.cash += realized
                st.realized_pnl += realized
                if st.size == 0:
                    st.avg_price = 0.0

    def record_event(self, date, symbol: str, needed_margin: float, available: float) -> None:
        """登记一次拒绝开仓事件。"""
        self.events.append(
            ConstraintEvent(
                date=date, symbol=symbol, needed_margin=needed_margin, available=available
            )
        )

    def record_day(self, date, extra: dict | None = None) -> None:
        """逐日记录：组合权益 / 保证金 / 可用资金 + 逐品种累计盈亏（供报告与 CSV）。

        ``extra`` 为 v1.3 风控附加列（熔断状态 / 敞口 / 超限计数）；缺省时与 v1.2 一致。
        """
        row: dict = {
            "date": date,
            "equity": self.equity,
            "cash": self.cash,
            "margin_used": self.margin_used,
            "available": self.available,
            "unrealized_pnl": self.unrealized_pnl,
        }
        for s in self.states:
            row[f"pnl_{s}"] = self.symbol_pnl(s)
        if extra:
            row.update(extra)
        self.daily.append(row)
