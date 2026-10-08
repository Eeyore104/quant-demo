"""绩效指标计算（公式透明，全自研）。"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class PerformanceMetrics:
    total_return: float = 0.0      # 累计收益率
    annual_return: float = 0.0     # 年化收益率
    max_drawdown: float = 0.0      # 最大回撤
    sharpe: float = 0.0            # 夏普比率（无风险利率按 0）
    win_rate: float = 0.0          # 胜率（按平仓次数）
    profit_factor: float = 0.0     # 盈亏比（总盈利 / 总亏损）
    trade_count: int = 0           # 平仓次数
    total_commission: float = 0.0  # 累计手续费（元）
    total_slippage: float = 0.0    # 累计滑点成本（元）


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤：权益曲线从峰值到谷底的最大跌幅（负数或 0）。"""
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def sharpe_ratio(returns: pd.Series, periods: int = 252) -> float:
    """夏普 = 日均收益 / 日收益波动 × sqrt(252)。"""
    if len(returns) == 0 or returns.std(ddof=0) == 0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=0) * np.sqrt(periods))


def analyze(equity_df: pd.DataFrame, trades: list, initial_capital: float) -> PerformanceMetrics:
    """汇总绩效指标。"""
    m = PerformanceMetrics()
    equity = equity_df["equity"].astype(float)
    if len(equity) < 2:
        return m

    m.total_return = float(equity.iloc[-1] / initial_capital - 1.0)
    n_days = len(equity)
    base = 1.0 + m.total_return
    m.annual_return = float(base ** (252.0 / n_days) - 1.0) if base > 0 else -1.0
    m.max_drawdown = max_drawdown(equity)
    m.sharpe = sharpe_ratio(equity.pct_change().dropna())

    closes = [t for t in trades if t.action == "CLOSE"]
    m.trade_count = len(closes)
    if closes:
        wins = [t.pnl for t in closes if t.pnl > 0]
        losses = [t.pnl for t in closes if t.pnl < 0]
        m.win_rate = len(wins) / len(closes)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            m.profit_factor = float(sum(wins) / gross_loss)
        else:
            m.profit_factor = float("inf") if wins else 0.0

    m.total_commission = float(sum(t.commission for t in trades))
    m.total_slippage = float(sum(t.slippage_cost for t in trades))
    return m
