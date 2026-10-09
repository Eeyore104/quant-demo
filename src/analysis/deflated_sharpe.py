"""Deflated Sharpe Ratio（Bailey & López de Prado, 2014）：多重试验校正后的夏普显著性。

动机：参数扫描试了 N 组参数，挑出来的「最优」夏普天然含选择偏差——试得越多、
最好那次越好看，哪怕全是噪声。DSR 用试验次数 N 与试验间夏普方差，推导
「纯运气可达到的期望最大夏普 SR0」，再看实际夏普扣除 SR0 后还有多显著。

公式（夏普均为「每期」口径，如日频）::

    DSR = Φ[ (SR − SR0) · √(T−1) / √(1 − γ3·SR + (γ4−1)/4·SR²) ]
    SR0 = √V · [(1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))]

其中 T 为收益样本数、γ3 偏度、γ4 峰度（非超额）、V 为各试验夏普的方差、
γ 为欧拉常数。DSR ≥ 0.95 通常视为「校正后仍显著」。
"""

import math
from dataclasses import dataclass

import numpy as np

from .stats_utils import EULER_GAMMA, kurtosis, normal_cdf, normal_ppf, skewness


@dataclass
class DSRResult:
    """Deflated Sharpe 结果（除备注外，夏普均为每期口径）。"""

    dsr: float  # 校正后概率：真实夏普 > 0 的置信度（越高越可信）
    sr_observed: float  # 实际每期夏普
    sr_expected_max: float  # 期望最大每期夏普 SR0（N 次试验纯运气期望）
    sr_variance: float  # 试验间每期夏普方差
    n_trials: int  # 参数试验次数
    n_obs: int  # 收益样本数
    skew: float  # 收益偏度
    kurtosis: float  # 收益峰度（非超额）

    @property
    def sr_observed_annual(self) -> float:
        """实际夏普（年化，×√252）。"""
        return self.sr_observed * math.sqrt(252.0)

    @property
    def sr_expected_max_annual(self) -> float:
        """期望最大夏普（年化，×√252）。"""
        return self.sr_expected_max * math.sqrt(252.0)


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """N 次独立试验下「最好那次」的期望夏普（每期口径）。

    ``n_trials < 2`` 或方差非正时返回 0。
    """
    if n_trials < 2 or sr_variance <= 0.0:
        return 0.0
    z1 = normal_ppf(1.0 - 1.0 / n_trials)
    z2 = normal_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe_ratio(returns, n_trials: int, sr_variance: float) -> DSRResult:
    """计算 DSR。

    - ``returns``：策略逐期收益序列（如日收益；无效值会被剔除）。
    - ``sr_variance``：各参数试验的每期夏普方差（调用方由网格扫描结果换算）。
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 30:
        raise ValueError(f"收益样本过短（{n} < 30），无法做 DSR 校正")

    std = r.std(ddof=0)
    sr = float(r.mean() / std) if std > 0.0 else 0.0
    g3 = skewness(r)
    g4 = kurtosis(r)
    sr0 = expected_max_sharpe(n_trials, sr_variance)

    den = math.sqrt(max(1e-12, 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr))
    dsr = float(normal_cdf((sr - sr0) * math.sqrt(n - 1) / den))
    return DSRResult(
        dsr=dsr,
        sr_observed=sr,
        sr_expected_max=sr0,
        sr_variance=float(sr_variance),
        n_trials=int(n_trials),
        n_obs=n,
        skew=g3,
        kurtosis=g4,
    )
