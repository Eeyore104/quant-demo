"""成本敏感性：手续费 / 滑点等比放大（×1.5 / ×2 / ×3）→「收益归零成本倍数」。

口径说明：手续费直接乘倍数；滑点通过 ``tick_size × 倍数`` 实现
（``slippage_ticks`` 保持整数约定不变，tick_size 为内部缩放载体）。
"""

from dataclasses import dataclass, field

import pandas as pd

from ..engine.broker import Broker
from ..engine.engine import BacktestEngine
from ..engine.portfolio import Portfolio
from ..report import metrics as metrics_mod
from ..utils.logger import get_logger

log = get_logger("engine")


@dataclass
class CostSensitivityResult:
    """成本敏感性结果。"""

    rows: list[dict] = field(default_factory=list)
    zero_multiplier: float | None = None  # 收益归零的（插值）成本倍数
    reason: str = ""  # interpolated / not_breached / negative_at_base

    @property
    def table(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def _interpolate_zero(rows: list[dict]) -> tuple[float | None, str]:
    """在相邻档位间线性插值找「收益 = 0」的成本倍数。"""
    points = [(float(r["multiplier"]), float(r["total_return"])) for r in rows]
    points.sort()
    if not points:
        return None, "empty"
    if points[0][1] <= 0.0:
        return None, "negative_at_base"
    for (m0, r0), (m1, r1) in zip(points[:-1], points[1:], strict=True):
        if r0 > 0.0 >= r1:
            m_star = m0 + (0.0 - r0) / (r1 - r0) * (m1 - m0)
            return m_star, "interpolated"
    return None, "not_breached"


def run_cost_sensitivity(
    df: pd.DataFrame,
    strategy_cls,
    params: dict,
    broker_kwargs: dict,
    initial_capital: float,
    position_size: int,
    contract_multiplier: int,
    multipliers: tuple = (1.0, 1.5, 2.0, 3.0),
) -> CostSensitivityResult:
    """按 ``multipliers`` 逐档放大成本复跑全样本，输出收益 / 成本对照表。"""
    rows: list[dict] = []
    for mult in multipliers:
        broker = Broker(
            commission_per_lot=broker_kwargs["commission_per_lot"] * mult,
            slippage_ticks=broker_kwargs["slippage_ticks"],
            tick_size=broker_kwargs["tick_size"] * mult,
            contract_multiplier=broker_kwargs["contract_multiplier"],
        )
        portfolio = Portfolio(initial_capital, contract_multiplier=contract_multiplier)
        engine = BacktestEngine(
            df,
            strategy_cls(params),
            broker=broker,
            portfolio=portfolio,
            position_size=position_size,
        )
        equity_df, trades = engine.run()
        m = metrics_mod.analyze(equity_df, trades, initial_capital)
        rows.append(
            {
                "multiplier": float(mult),
                "total_return": m.total_return,
                "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown,
                "total_cost": round(m.total_commission + m.total_slippage, 2),
            }
        )

    zero, reason = _interpolate_zero(rows)
    log.info(
        "成本敏感性完成：%s",
        f"收益归零 ≈ ×{zero:.2f}" if zero is not None else f"未归零（{reason}）",
    )
    return CostSensitivityResult(rows=rows, zero_multiplier=zero, reason=reason)
