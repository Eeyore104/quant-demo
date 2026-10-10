"""合约参数（乘数 / 最小变动 / 保证金率 / 手续费）：组合模式的逐品种参数表。

口径：保证金率与手续费为**近似口径**（2026-10-10 由 akshare ``futures_comm_info``
核对，交易所口径；实盘期货公司通常上浮 2–5 个百分点，可在 config 覆盖）。
"""

from dataclasses import dataclass

_REQUIRED = ("multiplier", "tick_size", "margin_rate", "commission_per_lot")


@dataclass(frozen=True)
class ContractSpec:
    """单个品种的合约与成本参数。"""

    symbol: str
    multiplier: int  # 合约乘数（吨/手等）
    tick_size: float  # 最小变动价位（元）
    margin_rate: float  # 保证金率（交易所口径近似，如 0.07）
    commission_per_lot: float  # 手续费（元/手，近似）


def build_contracts(cfg: dict) -> dict[str, ContractSpec]:
    """从 ``config.portfolio.contracts`` 构建参数表；缺字段或非法值抛 ``ValueError``。"""
    out: dict[str, ContractSpec] = {}
    if not cfg:
        raise ValueError("缺少 portfolio.contracts 配置（逐品种合约参数表）")
    for symbol, spec in cfg.items():
        missing = [k for k in _REQUIRED if k not in spec]
        if missing:
            raise ValueError(f"合约 {symbol} 缺少参数：{missing}")
        multiplier = int(spec["multiplier"])
        tick_size = float(spec["tick_size"])
        margin_rate = float(spec["margin_rate"])
        commission = float(spec["commission_per_lot"])
        if multiplier <= 0 or tick_size <= 0 or margin_rate <= 0 or commission < 0:
            raise ValueError(f"合约 {symbol} 参数非法：{spec}")
        out[symbol] = ContractSpec(
            symbol=symbol,
            multiplier=multiplier,
            tick_size=tick_size,
            margin_rate=margin_rate,
            commission_per_lot=commission,
        )
    return out
