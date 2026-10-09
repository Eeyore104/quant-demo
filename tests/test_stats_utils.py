"""轻量统计工具 + Deflated Sharpe 单元测试。"""

import numpy as np
import pytest

from src.analysis.deflated_sharpe import deflated_sharpe_ratio, expected_max_sharpe
from src.analysis.stats_utils import kurtosis, normal_cdf, normal_ppf, skewness


def test_normal_cdf_known_values():
    assert normal_cdf(0.0) == 0.5
    assert abs(normal_cdf(1.959963985) - 0.975) < 1e-9
    assert abs(normal_cdf(-1.281551566) - 0.10) < 1e-9


def test_normal_ppf_known_values_and_symmetry():
    assert abs(normal_ppf(0.975) - 1.959963985) < 1e-6
    assert abs(normal_ppf(0.5) - 0.0) < 1e-12
    assert abs(normal_ppf(0.001) - (-3.090232306)) < 1e-6
    for p in (0.01, 0.2, 0.35, 0.8, 0.999):
        assert abs(normal_ppf(p) + normal_ppf(1.0 - p)) < 1e-7


def test_normal_ppf_rejects_out_of_range():
    for bad in (0.0, 1.0, -0.1, 1.1):
        with pytest.raises(ValueError):
            normal_ppf(bad)


def test_skewness_and_kurtosis_moments():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert abs(skewness(x)) < 1e-12
    assert abs(kurtosis(x) - 1.7) < 1e-12
    # 空样本的兜底口径
    assert skewness([]) == 0.0
    assert kurtosis([]) == 3.0


def test_expected_max_sharpe_monotone_and_scaling():
    base = expected_max_sharpe(20, 0.0004)
    assert base > 0
    assert expected_max_sharpe(100, 0.0004) > base  # 试验越多、期望最大越高
    # 与 √V 成正比：方差 ×4 → 期望最大 ×2
    assert abs(expected_max_sharpe(20, 0.0016) - 2.0 * base) < 1e-12
    assert expected_max_sharpe(1, 0.0004) == 0.0
    assert expected_max_sharpe(20, 0.0) == 0.0


def test_deflated_sharpe_strong_vs_weak():
    rng = np.random.default_rng(7)
    strong = rng.normal(0.003, 0.01, 600)  # 每期夏普约 0.3
    weak = rng.normal(-0.0005, 0.01, 600)  # 每期夏普约 -0.05
    dsr_strong = deflated_sharpe_ratio(strong, n_trials=50, sr_variance=0.0016)
    dsr_weak = deflated_sharpe_ratio(weak, n_trials=50, sr_variance=0.0016)
    assert 0.0 <= dsr_weak.dsr <= 1.0 and 0.0 <= dsr_strong.dsr <= 1.0
    assert dsr_strong.dsr > 0.95
    assert dsr_weak.dsr < 0.5
    assert dsr_strong.dsr > dsr_weak.dsr
    assert dsr_strong.n_trials == 50 and dsr_strong.n_obs == 600


def test_deflated_sharpe_requires_enough_samples():
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(np.zeros(5), n_trials=5, sr_variance=0.001)
