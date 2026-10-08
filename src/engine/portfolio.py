"""资金与持仓管理（净值模型：正=净多，负=净空；MVP 不计保证金占用）。"""
from dataclasses import dataclass


@dataclass
class Position:
    symbol: str = ""
    size: int = 0             # 手数：正=多，负=空
    avg_price: float = 0.0    # 持仓均价
    unrealized_pnl: float = 0.0


@dataclass
class Trade:
    date: object              # 成交日（datetime）
    symbol: str
    direction: str            # LONG / SHORT
    action: str               # OPEN / CLOSE
    price: float              # 成交价（已含滑点）
    volume: int               # 手数
    commission: float         # 本笔手续费（元）
    slippage_cost: float = 0.0  # 本笔滑点成本（元，信息披露用）
    pnl: float = 0.0          # 平仓已实现盈亏（元；开仓为 0）


class Portfolio:
    def __init__(self, initial_capital: float, contract_multiplier: int = 10):
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.multiplier = int(contract_multiplier)
        self.position = Position()
        self.equity_curve: list[dict] = []

    def apply_trades(self, trades: list[Trade]) -> None:
        """成交入账：计手续费、更新持仓/现金、算平仓已实现盈亏。"""
        for t in trades:
            self.cash -= t.commission
            if t.action == "OPEN":
                if self.position.size == 0:
                    self.position.avg_price = t.price
                else:  # 同向加仓：加权平均
                    old_abs = abs(self.position.size)
                    self.position.avg_price = (
                        self.position.avg_price * old_abs + t.price * t.volume
                    ) / (old_abs + t.volume)
                self.position.size += t.volume if t.direction == "LONG" else -t.volume
            else:  # CLOSE
                if t.direction == "LONG":
                    realized = (t.price - self.position.avg_price) * t.volume * self.multiplier
                    self.position.size -= t.volume
                else:
                    realized = (self.position.avg_price - t.price) * t.volume * self.multiplier
                    self.position.size += t.volume
                t.pnl = realized
                self.cash += realized
                if self.position.size == 0:
                    self.position.avg_price = 0.0

    def mark_to_market(self, bar) -> None:
        """用当根 bar 收盘价更新浮动盈亏。"""
        if self.position.size != 0:
            self.position.symbol = bar.symbol
            self.position.unrealized_pnl = (
                (bar.close - self.position.avg_price) * self.multiplier * self.position.size
            )
        else:
            self.position.unrealized_pnl = 0.0

    def equity(self) -> float:
        return self.cash + self.position.unrealized_pnl

    def record(self, date) -> None:
        self.equity_curve.append(
            {
                "date": date,
                "equity": self.equity(),
                "cash": self.cash,
                "unrealized_pnl": self.position.unrealized_pnl,
                "size": self.position.size,
            }
        )
