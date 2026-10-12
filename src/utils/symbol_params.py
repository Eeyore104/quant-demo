"""品种参数解析：手动登记 → 自动全量表 → 默认值（三层，供各入口脚本复用）。

- 手动登记：``config.yaml`` 的 ``backtest.params_by_symbol``（优先级最高）；
- 自动全量表：``config/symbol_params.yaml``（由 ``scripts/build_symbol_params.py`` 生成）；
- 默认值：``backtest`` 段的兜底字段（玉米口径），并提示告警（未收录品种）。
"""

from __future__ import annotations

DEFAULT_SRC = "默认值（未登记 ⚠️）"


def resolve_symbol_params(b: dict, symbol: str, auto_table: dict | None = None) -> tuple[dict, str]:
    """解析单品种参数；返回 ``(参数, 来源标签)``。

    登记条目可只写部分字段，缺的用默认值补齐；手动条目缺字段时由自动表补缺。
    """
    manual = (b.get("params_by_symbol") or {}).get(symbol) or {}
    auto = (auto_table or {}).get(symbol) or {}
    if manual and auto:
        entry, src = {**auto, **manual}, "手动登记（自动表补缺）"
    elif manual:
        entry, src = manual, "手动登记（backtest.params_by_symbol）"
    elif auto:
        entry, src = auto, "自动全量表（config/symbol_params.yaml）"
    else:
        entry, src = {}, DEFAULT_SRC
    return (
        {
            "tick_size": float(entry.get("tick_size", b["tick_size"])),
            "contract_multiplier": int(entry.get("contract_multiplier", b["contract_multiplier"])),
            "commission_per_lot": float(entry.get("commission_per_lot", b["commission_per_lot"])),
        },
        src,
    )


def apply_symbol_params(b: dict, symbol: str, auto_table: dict | None = None) -> tuple[dict, str]:
    """解析参数并写回 ``b``（就地更新）；返回 ``(参数, 来源标签)``。"""
    resolved, src = resolve_symbol_params(b, symbol, auto_table)
    b.update(resolved)
    return resolved, src
