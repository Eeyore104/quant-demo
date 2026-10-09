"""轻量统计工具：标准正态分布函数与矩统计。

不引入 scipy 依赖：Φ 用 ``math.erf`` 精确计算，Φ⁻¹ 用 Acklam 有理逼近
（相对误差量级 1e-9，足够本项目使用），保持依赖表精简。
"""

import math

import numpy as np

#: 欧拉–马歇罗尼常数 γ（Deflated Sharpe 期望最大夏普公式使用）
EULER_GAMMA = 0.5772156649015329


def normal_cdf(x: float) -> float:
    """标准正态分布累积分布函数 Φ(x)。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    """标准正态分布分位函数 Φ⁻¹(p)（Acklam 有理逼近）。

    参数 ``p`` 必须在开区间 (0, 1) 内，否则抛 ``ValueError``。
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p 必须在 (0, 1) 区间内，当前 {p}")

    # Acklam（2003）系数
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:  # 左尾
        q = math.sqrt(-2.0 * math.log(p))
        num = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        den = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        return num / den

    if p <= p_high:  # 中心区
        q = p - 0.5
        r = q * q
        num = ((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]
        den = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        return num * q / den

    q = math.sqrt(-2.0 * math.log(1.0 - p))  # 右尾
    num = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
    den = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    return -(num / den)


def skewness(values) -> float:
    """样本偏度 γ3（矩口径；样本为空或零波动时返回 0）。"""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return 0.0
    s = x.std(ddof=0)
    if s == 0.0:
        return 0.0
    return float(np.mean((x - x.mean()) ** 3) / s**3)


def kurtosis(values) -> float:
    """样本峰度 γ4（**非超额**口径，正态分布 = 3；样本为空或零波动时返回 3）。"""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return 3.0
    s = x.std(ddof=0)
    if s == 0.0:
        return 3.0
    return float(np.mean((x - x.mean()) ** 4) / s**4)
