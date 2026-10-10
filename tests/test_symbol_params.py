"""单品种参数解析测试：三类来源（手动 → 自动全量表 → 默认）与字段补齐。"""

from run_backtest import _DEFAULT_SRC, resolve_symbol_params
from src.utils.config_loader import load_config, load_symbol_params


def test_manual_overrides_on_top_of_auto():
    b = load_config()["backtest"]
    b["params_by_symbol"] = {"AU0": {"tick_size": 0.05}}
    auto = {"AU0": {"tick_size": 0.02, "contract_multiplier": 1000, "commission_per_lot": 20.0}}
    params, src = resolve_symbol_params(b, "AU0", auto)
    assert src.startswith("手动登记")
    # 手动字段覆盖自动表；未手动写的字段由自动表补缺
    assert params == {"tick_size": 0.05, "contract_multiplier": 1000, "commission_per_lot": 20.0}


def test_auto_table_used_when_no_manual_entry():
    b = load_config()["backtest"]
    auto = {"CU0": {"tick_size": 10.0, "contract_multiplier": 5, "commission_per_lot": 82.2}}
    params, src = resolve_symbol_params(b, "CU0", auto)
    assert src.startswith("自动全量表")
    assert params == {"tick_size": 10.0, "contract_multiplier": 5, "commission_per_lot": 82.2}


def test_default_fallback_with_source_tag():
    b = load_config()["backtest"]
    params, src = resolve_symbol_params(b, "ZZ0", {})
    assert src == _DEFAULT_SRC
    assert params["tick_size"] == b["tick_size"]
    assert params["contract_multiplier"] == b["contract_multiplier"]


def test_partial_manual_entry_fills_defaults():
    b = {
        "tick_size": 9.9,
        "contract_multiplier": 9,
        "commission_per_lot": 9.9,
        "params_by_symbol": {"Z0": {"tick_size": 0.5}},
    }
    params, src = resolve_symbol_params(b, "Z0", None)
    assert params == {"tick_size": 0.5, "contract_multiplier": 9, "commission_per_lot": 9.9}
    assert src.startswith("手动登记")


def test_shipped_auto_table_covers_core_symbols():
    """随仓库分发的自动全量表：覆盖数量与关键读数抽查。"""
    table = load_symbol_params()
    assert len(table) >= 75
    assert table["AU0"]["tick_size"] == 0.02
    assert table["AU0"]["contract_multiplier"] == 1000
    assert table["RB0"]["contract_multiplier"] == 10
    assert table["CU0"]["contract_multiplier"] == 5
    assert table["SC0"]["contract_multiplier"] == 1000
