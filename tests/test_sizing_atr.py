"""atr_risk 头寸规模单元测试（v1.3 · ATR 风险预算）。"""

from src.engine.contracts import ContractSpec
from src.engine.sizing import compute_target_lots


def _specs():
    return {
        "X": ContractSpec(
            "X", multiplier=10, tick_size=1.0, margin_rate=0.1, commission_per_lot=3.0
        )
    }


def test_atr_risk_formula():
    # 权益 100000 × 1.5% = 1500 元预算；2×ATR(20)×10 = 单手风险 400 → 3 手
    lots = compute_target_lots(
        "atr_risk",
        _specs(),
        {"X": 1000.0},
        {},
        100000,
        {"risk_pct": 0.015, "stop_atr_mult": 2.0, "max_lots_per_symbol": 10},
        atr_by_symbol={"X": 20.0},
    )
    assert lots == {"X": 3}


def test_atr_risk_clamped_and_invalid_inputs():
    # 上限截断
    lots = compute_target_lots(
        "atr_risk",
        _specs(),
        {"X": 1000.0},
        {},
        10_000_000,
        {"risk_pct": 0.015, "stop_atr_mult": 2.0, "max_lots_per_symbol": 5},
        atr_by_symbol={"X": 20.0},
    )
    assert lots["X"] == 5
    # ATR 缺失 → 0 手（无法按风险预算定手数）
    lots = compute_target_lots(
        "atr_risk",
        _specs(),
        {"X": 1000.0},
        {},
        100000,
        {"risk_pct": 0.015, "stop_atr_mult": 2.0},
        atr_by_symbol={},
    )
    assert lots["X"] == 0
    # 价格无效 → 0 手
    lots = compute_target_lots(
        "atr_risk",
        _specs(),
        {"X": 0.0},
        {},
        100000,
        {"risk_pct": 0.015, "stop_atr_mult": 2.0},
        atr_by_symbol={"X": 20.0},
    )
    assert lots["X"] == 0


def test_atr_risk_scales_with_equity():
    # 权益放大 10 倍 → 手数放大 10 倍（单手风险 2×25×10 = 500）
    cfg = {"risk_pct": 0.01, "stop_atr_mult": 2.0, "max_lots_per_symbol": 100}
    small = compute_target_lots(
        "atr_risk", _specs(), {"X": 1000.0}, {}, 100000, cfg, atr_by_symbol={"X": 25.0}
    )
    big = compute_target_lots(
        "atr_risk", _specs(), {"X": 1000.0}, {}, 1000000, cfg, atr_by_symbol={"X": 25.0}
    )
    assert small["X"] == 2 and big["X"] == 20


def test_atr_risk_budget_insufficient_gives_zero():
    # 预算 500 元 < 单手风险 1000 元（2×50×10）→ 0 手
    lots = compute_target_lots(
        "atr_risk",
        _specs(),
        {"X": 1000.0},
        {},
        50000,
        {"risk_pct": 0.01, "stop_atr_mult": 2.0},
        atr_by_symbol={"X": 50.0},
    )
    assert lots["X"] == 0
