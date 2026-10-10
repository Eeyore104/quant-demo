"""头寸规模单元测试：fixed / equal_weight / inv_vol。"""

import pandas as pd
import pytest

from src.engine.contracts import ContractSpec
from src.engine.sizing import compute_target_lots


def _spec():
    # 价格 1000 时单手保证金 = 1000 × 10 × 1.0 = 10,000（便于整数断言）
    return ContractSpec("X", multiplier=10, tick_size=1.0, margin_rate=1.0, commission_per_lot=3.0)


def _specs():
    return {"A": _spec(), "B": _spec()}


def test_fixed_mode():
    lots = compute_target_lots(
        "fixed",
        _specs(),
        {"A": 1000, "B": 1000},
        {},
        100000,
        {"lots": 2, "max_lots_per_symbol": 10},
    )
    assert lots == {"A": 2, "B": 2}
    lots = compute_target_lots("fixed", _specs(), {"A": 0, "B": 1000}, {}, 100000, {"lots": 1})
    assert lots == {"A": 0, "B": 1}


def test_equal_weight_budget():
    # 预算 = 100000 × (1/2) × 0.6 = 30000 → 30000 / 10000 = 3 手
    lots = compute_target_lots(
        "equal_weight",
        _specs(),
        {"A": 1000, "B": 1000},
        {},
        100000,
        {"target_margin_usage": 0.6, "max_lots_per_symbol": 10},
    )
    assert lots == {"A": 3, "B": 3}


def test_equal_weight_cap_and_zero_price():
    lots = compute_target_lots(
        "equal_weight",
        _specs(),
        {"A": 1000, "B": 0},
        {},
        1000000,
        {"target_margin_usage": 0.6, "max_lots_per_symbol": 5},
    )
    assert lots["A"] == 5  # 手数上限截断
    assert lots["B"] == 0  # 无有效价格


def test_inv_vol_prefers_low_volatility():
    # A：极小波动；B：大波动 → A 权重远大于 B
    closes_a = pd.Series([1000.0 * (1 + 0.00002 * (-1) ** i) for i in range(80)])
    closes_b = pd.Series([1000.0 * (1 + 0.03 * (-1) ** i) for i in range(80)])
    lots = compute_target_lots(
        "inv_vol",
        _specs(),
        {"A": 1000.0, "B": 1000.0},
        {"A": closes_a, "B": closes_b},
        100000,
        {"target_margin_usage": 0.6, "max_lots_per_symbol": 10, "vol_window": 60},
    )
    assert lots["A"] == 5  # ≈ 100000 × 0.9993 × 0.6 / 10000 = 5.99 → 5
    assert lots["B"] == 0  # 权重极小，预算不足一手


def test_inv_vol_falls_back_to_equal_when_no_vol_data():
    lots = compute_target_lots(
        "inv_vol",
        _specs(),
        {"A": 1000, "B": 1000},
        {},
        100000,
        {"target_margin_usage": 0.6, "max_lots_per_symbol": 10},
    )
    assert lots == {"A": 3, "B": 3}


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        compute_target_lots("bogus", _specs(), {"A": 1000}, {}, 100000, {})


def test_recent_vol_short_series_is_none():
    from src.engine.sizing import _recent_vol

    assert _recent_vol(None, 60) is None
    assert _recent_vol(pd.Series([1000.0]), 60) is None
    assert _recent_vol(pd.Series([1000.0, 1001.0]), 60) is None  # 仅 1 个收益样本


def test_inv_vol_weights_prefer_low_vol_both_trade():
    # A 收益波动 ±0.4%、B ±4%（放大 10 倍）：两者都应分到资金，且 A 手数 > B
    closes_a = pd.Series([1000.0 * (1 + 0.002 * (-1) ** i) for i in range(80)])
    closes_b = pd.Series([1000.0 * (1 + 0.02 * (-1) ** i) for i in range(80)])
    lots = compute_target_lots(
        "inv_vol",
        _specs(),
        {"A": 1000.0, "B": 1000.0},
        {"A": closes_a, "B": closes_b},
        1000000,
        {"target_margin_usage": 0.6, "max_lots_per_symbol": 100, "vol_window": 60},
    )
    assert lots["A"] > lots["B"] > 0
