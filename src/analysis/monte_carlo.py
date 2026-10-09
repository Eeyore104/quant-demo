"""蒙特卡洛对照：① 信号重排随机对照 ② 收益 Bootstrap（路径重采样）。

本模块回答两个问题：
- 「策略的择时」比「随机择时」强吗？→ 把信号序列随机打乱（保留多空天数分布），
  重跑 N 次，看实际收益在随机分布中的分位（信号重排对照）。
- 「这份收益有多依赖运气」？→ 对权益日收益做 iid Bootstrap 重采样，重建 N 条路径，
  得到收益 / 最大回撤的分布与分位数。
"""

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..engine.engine import BacktestEngine
from ..strategy.base import StrategyBase
from ..utils.logger import get_logger

log = get_logger("analysis")
_engine_log = get_logger("engine")


class _SignalOverride(StrategyBase):
    """内部工具：把预生成的 signal 序列直接注入引擎（用于信号重排对照）。"""

    name = "__signal_override"

    def __init__(self, signal):
        super().__init__()
        self._signal = np.asarray(signal, dtype=int)

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = self._signal
        return out


@dataclass
class SignalShuffleResult:
    """信号重排随机对照结果。"""

    runs: int
    seed: int
    actual_return: float  # 实际策略累计收益率
    percentile: float  # 实际收益在随机分布中的分位（0~100，越高越好）
    median_return: float
    q05_return: float
    q95_return: float
    returns: np.ndarray  # 每次对照的累计收益率（供画图 / 复现）

    @property
    def p_value(self) -> float:
        """粗略 p 值（1 − 分位 / 100）：分位越高，p 越小。"""
        return max(0.0, 1.0 - self.percentile / 100.0)


@dataclass
class BootstrapResult:
    """收益 Bootstrap（iid 日收益重采样）结果。"""

    runs: int
    seed: int
    actual_return: float
    actual_max_drawdown: float
    return_q05: float
    return_q50: float
    return_q95: float
    mdd_q05: float  # 回撤 5% 分位（更差的尾部）
    mdd_q50: float
    mdd_q95: float
    prob_negative: float  # 重采样路径中累计收益为负的比例
    returns: np.ndarray
    max_drawdowns: np.ndarray


def run_signal_shuffle(
    df_with_signal: pd.DataFrame,
    broker_factory,
    portfolio_factory,
    position_size: int,
    actual_return: float,
    runs: int = 150,
    seed: int = 42,
) -> SignalShuffleResult:
    """信号随机重排对照：打乱 signal 列的时序（保留多空天数分布），重跑 ``runs`` 次。"""
    rng = np.random.default_rng(seed)
    base_signal = df_with_signal["signal"].to_numpy()
    rets: list[float] = []

    prev_level = _engine_log.level
    _engine_log.setLevel(logging.WARNING)  # 批量运行期间静默引擎日志
    try:
        for i in range(runs):
            shuffled = rng.permutation(base_signal)
            portfolio = portfolio_factory()
            engine = BacktestEngine(
                df_with_signal,
                _SignalOverride(shuffled),
                broker=broker_factory(),
                portfolio=portfolio,
                position_size=position_size,
            )
            equity_df, _ = engine.run()
            rets.append(float(equity_df["equity"].iloc[-1] / portfolio.initial_capital - 1.0))
            if (i + 1) % 50 == 0:
                log.info("蒙特卡洛（信号重排）：已完成 %d/%d 次", i + 1, runs)
    finally:
        _engine_log.setLevel(prev_level)

    arr = np.asarray(rets, dtype=float)
    percentile = float((arr < actual_return).mean() * 100.0)
    result = SignalShuffleResult(
        runs=runs,
        seed=seed,
        actual_return=float(actual_return),
        percentile=percentile,
        median_return=float(np.median(arr)),
        q05_return=float(np.quantile(arr, 0.05)),
        q95_return=float(np.quantile(arr, 0.95)),
        returns=arr,
    )
    log.info(
        "蒙特卡洛（信号重排）完成：实际收益 %.2f%% 优于 %.1f%% 的随机对照（%d 次）",
        actual_return * 100,
        percentile,
        runs,
    )
    return result


def run_bootstrap(
    equity_df: pd.DataFrame,
    initial_capital: float,
    runs: int = 500,
    seed: int = 42,
) -> BootstrapResult:
    """收益 Bootstrap：对权益日收益做 iid 重采样，重建 ``runs`` 条路径。

    注意：iid 假设忽略收益自相关，作为「运气成分」的粗略参照（口径写入报告）。
    """
    equity = equity_df["equity"].astype(float)
    daily = equity.pct_change().dropna().to_numpy()
    n = daily.size

    rng = np.random.default_rng(seed)
    rets = np.empty(runs, dtype=float)
    mdds = np.empty(runs, dtype=float)
    for i in range(runs):
        sample = rng.choice(daily, size=n, replace=True)
        path = np.cumprod(1.0 + sample)
        rets[i] = float(path[-1] - 1.0)
        peak = np.maximum.accumulate(path)
        mdds[i] = float((path / peak - 1.0).min())

    actual_return = float(equity.iloc[-1] / initial_capital - 1.0)
    actual_mdd = float((equity / equity.cummax() - 1.0).min())
    return BootstrapResult(
        runs=runs,
        seed=seed,
        actual_return=actual_return,
        actual_max_drawdown=actual_mdd,
        return_q05=float(np.quantile(rets, 0.05)),
        return_q50=float(np.quantile(rets, 0.50)),
        return_q95=float(np.quantile(rets, 0.95)),
        mdd_q05=float(np.quantile(mdds, 0.05)),
        mdd_q50=float(np.quantile(mdds, 0.50)),
        mdd_q95=float(np.quantile(mdds, 0.95)),
        prob_negative=float((rets < 0).mean()),
        returns=rets,
        max_drawdowns=mdds,
    )
