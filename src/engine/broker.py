"""撮合器：按 T+1 开盘价 ± 滑点成交，计提手续费与滑点成本。

规则：先平后开；仓位翻转拆成「平仓 + 开仓」两笔成交。
"""

from ..utils.logger import get_logger
from .portfolio import Trade

log = get_logger("engine.broker")


class Broker:
    def __init__(
        self,
        commission_per_lot: float = 1.2,
        slippage_ticks: int = 1,
        tick_size: float = 1.0,
        contract_multiplier: int = 10,
    ):
        self.commission_per_lot = float(commission_per_lot)
        self.slippage_ticks = int(slippage_ticks)
        self.tick_size = float(tick_size)
        self.multiplier = int(contract_multiplier)

    @property
    def slip(self) -> float:
        """1 跳滑点的价格量（如玉米 1 元/吨）。"""
        return self.slippage_ticks * self.tick_size

    def fill(self, current_size: int, target_size: int, bar) -> list[Trade]:
        """把仓位从 current_size 调到 target_size，返回成交列表。
        成交价 = 当根 bar 开盘价 ± 滑点（买入向上、卖出向下）。
        """
        if target_size == current_size:
            return []

        trades: list[Trade] = []
        size = current_size

        # ① 平仓腿：目标为 0 / 方向翻转 / 同向减仓
        if size != 0 and abs(target_size) < abs(size) and target_size * size >= 0:
            close_vol = abs(size) - abs(target_size)
        elif size != 0 and (target_size == 0 or target_size * size < 0):
            close_vol = abs(size)
        else:
            close_vol = 0

        if close_vol > 0:
            direction = "LONG" if size > 0 else "SHORT"
            # 平多 = 卖出（向下滑）；平空 = 买入（向上滑）
            price = bar.open - self.slip if size > 0 else bar.open + self.slip
            trades.append(self._make_trade(bar, direction, "CLOSE", price, close_vol))
            size = size - close_vol if size > 0 else size + close_vol

        # ② 开仓腿：剩余差额
        open_delta = target_size - size
        if open_delta != 0:
            direction = "LONG" if open_delta > 0 else "SHORT"
            price = bar.open + self.slip if open_delta > 0 else bar.open - self.slip
            trades.append(self._make_trade(bar, direction, "OPEN", price, abs(open_delta)))

        return trades

    def close_at(self, bar, size: int, base_price: float) -> Trade:
        """按给定基准价平掉 ``size`` 手（止损/止盈场景）：含滑点与手续费。

        与 ``fill`` 一致的方向口径：平多 = 卖出（向下滑）、平空 = 买入（向上滑）。
        """
        direction = "LONG" if size > 0 else "SHORT"
        price = base_price - self.slip if size > 0 else base_price + self.slip
        return self._make_trade(bar, direction, "CLOSE", price, abs(size))

    def resize(self, bar, trade: Trade, volume: int) -> Trade:
        """按新手数生成同方向/同价的替代成交（敞口截断、降级执行用），成本按手数重算。"""
        return self._make_trade(bar, trade.direction, trade.action, trade.price, volume)

    def _make_trade(self, bar, direction: str, action: str, price: float, volume: int) -> Trade:
        return Trade(
            date=bar.date,
            symbol=bar.symbol,
            direction=direction,
            action=action,
            price=round(price, 4),
            volume=volume,
            commission=round(self.commission_per_lot * volume, 4),
            slippage_cost=round(self.slip * volume * self.multiplier, 4),
        )
