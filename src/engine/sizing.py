"""头寸规模：fixed / equal_weight / inv_vol / atr_risk 四模式（v1.2 / v1.3）。

- ``fixed``：每品种固定 ``lots`` 手；
- ``equal_weight``：保证金预算 = capital × (1/N) × 使用率上限；手数 = floor(预算 ÷ 单手保证金)；
- ``inv_vol``：权重 w ∝ 1/σ（σ = 近 ``vol_window`` 日收盘价收益波动），其余同 equal_weight；
- ``atr_risk``（v1.3）：ATR 风险预算 —— 手数 = floor(权益 × risk_pct ÷ (k_stop × ATR × 乘数))，
  与止损（k_stop × ATR）配套，形成「单笔风险固定」框架；ATR 由调用方按「截至信号日收盘」
  计算后传入（防未来函数）。
- 手数截断到 [0, ``max_lots_per_symbol``]；价格无效或预算不足一手 → 0。

口径说明：单手保证金按「价格 × 乘数 × 保证金率」估算；波动率不足（样本过短/缺失）
时该品种退化为等权权重兜底；atr_risk 下 ATR 缺失/无效 → 0 手（无法按风险预算定手数）。
"""

import pandas as pd

from .contracts import ContractSpec


def compute_target_lots(
    mode: str,
    specs: dict[str, ContractSpec],
    prices: dict[str, float],
    closes_by_symbol: dict[str, pd.Series],
    capital: float,
    cfg: dict,
    *,
    atr_by_symbol: dict[str, float] | None = None,
) -> dict[str, int]:
    """为一篮子品种计算当前目标手数（0 = 不满足开仓条件 / 不配资）。"""
    max_lots = int(cfg.get("max_lots_per_symbol", 10))
    n = len(specs)
    if n == 0:
        return {}

    if mode == "fixed":
        base = max(0, min(int(cfg.get("lots", 1)), max_lots))
        return {s: (base if prices.get(s, 0.0) > 0 else 0) for s in specs}

    if mode == "atr_risk":
        return _atr_risk_lots(specs, prices, capital, cfg, atr_by_symbol or {}, max_lots)

    if mode not in ("equal_weight", "inv_vol"):
        raise ValueError(
            f"未知 sizing.mode：{mode}（可用：fixed / equal_weight / inv_vol / atr_risk）"
        )

    usage = float(cfg.get("target_margin_usage", 0.6))
    weights = _weights(mode, specs, closes_by_symbol, cfg)

    lots: dict[str, int] = {}
    for s, w in weights.items():
        price = float(prices.get(s, 0.0))
        if price <= 0 or w <= 0:
            lots[s] = 0
            continue
        spec = specs[s]
        margin_per_lot = price * spec.multiplier * spec.margin_rate
        budget = capital * w * usage
        lots[s] = max(0, min(int(budget // margin_per_lot), max_lots))
    return lots


def _atr_risk_lots(
    specs: dict[str, ContractSpec],
    prices: dict[str, float],
    capital: float,
    cfg: dict,
    atr_by_symbol: dict[str, float],
    max_lots: int,
) -> dict[str, int]:
    """ATR 风险预算：手数 = floor(权益 × risk_pct ÷ (k_stop × ATR × 乘数))。"""
    risk_pct = float(cfg.get("risk_pct", 0.015))
    k_stop = float(cfg.get("stop_atr_mult", 2.0))
    lots: dict[str, int] = {}
    for s, spec in specs.items():
        price = float(prices.get(s, 0.0))
        atr = float(atr_by_symbol.get(s, 0.0) or 0.0)
        if price <= 0 or atr <= 0 or k_stop <= 0:
            lots[s] = 0  # 无法按风险预算定手数
            continue
        risk_per_lot = k_stop * atr * spec.multiplier
        if risk_per_lot <= 0:
            lots[s] = 0
            continue
        lots[s] = max(0, min(int(capital * risk_pct / risk_per_lot), max_lots))
    return lots


def _weights(
    mode: str, specs: dict[str, ContractSpec], closes_by_symbol: dict[str, pd.Series], cfg: dict
) -> dict[str, float]:
    """计算权重（归一化到 1）。"""
    n = len(specs)
    if mode == "equal_weight":
        return {s: 1.0 / n for s in specs}

    # inv_vol：1/σ 归一化；缺失波动的品种用有效权重均值兜底
    window = int(cfg.get("vol_window", 60))
    inv_vol: dict[str, float | None] = {}
    for s in specs:
        sigma = _recent_vol(closes_by_symbol.get(s), window)
        inv_vol[s] = (1.0 / sigma) if sigma and sigma > 0 else None

    valid = {s: v for s, v in inv_vol.items() if v is not None}
    if not valid:
        return {s: 1.0 / n for s in specs}

    avg = sum(valid.values()) / len(valid)
    filled = {s: (valid.get(s) if valid.get(s) is not None else avg) for s in specs}
    total = sum(filled.values())
    return {s: v / total for s, v in filled.items()}


def _recent_vol(closes: pd.Series | None, window: int) -> float | None:
    """近 ``window`` 日收盘价收益波动（标准差）；样本不足返回 None。"""
    if closes is None or len(closes) < 2:
        return None
    rets = closes.astype(float).pct_change().dropna().tail(window)
    if len(rets) < 2:
        return None
    sigma = float(rets.std(ddof=0))
    return sigma if sigma > 0 else None
