"""参数敏感性细检：当前参数邻域网格 + 悬崖式衰减检测 + 稳健区域占比。

思路：不只看「扫描网格里这个点好不好」，还要看**它的邻居们好不好**——
邻域普遍能打 = 稳健；只有它一枝独秀、隔一格就崩 = 过拟合嫌疑（悬崖 / 孤峰）。
"""

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..engine.engine import BacktestEngine
from ..engine.walkforward import expand_grid
from ..report import metrics as metrics_mod
from ..utils.logger import get_logger

# 复用 engine 的 logger：子 logger 会向父 logger 冒泡导致重复打印（同 walkforward）
log = get_logger("engine")

#: 邻域伸缩系数：中心值的 0.6 / 0.8 / 1.0 / 1.25 / 1.5 倍
NEIGHBOR_FACTORS = (0.6, 0.8, 1.0, 1.25, 1.5)


def neighborhood_values(center, factors=NEIGHBOR_FACTORS) -> list:
    """围绕中心值生成邻域取值。

    - 整数参数：四舍五入回整数（如 5 → [3, 4, 5, 6, 8]）
    - 浮点参数：保留 2 位小数（如 2.0 → [1.2, 1.6, 2.0, 2.5, 3.0]）
    """
    is_int = isinstance(center, (int, np.integer)) and not isinstance(center, bool)
    vals: set = set()
    for f in factors:
        v = float(center) * f
        if is_int:
            v = int(math.floor(v + 0.5))  # 半值向上取整，避免 banker's rounding 歧义
        else:
            v = round(v, 2)
        if v > 0:
            vals.add(v)
    return sorted(vals)


def build_neighborhood_grid(center_params: dict, factors=NEIGHBOR_FACTORS) -> dict:
    """{参数: 中心值} → {参数: [邻域取值...]}（保持参数原顺序）。"""
    return {k: neighborhood_values(v, factors) for k, v in center_params.items()}


def combo_is_valid(combo: dict) -> bool:
    """跳过非法组合（与 walkforward 同一规则）：如双均线要求 fast < slow。"""
    if "fast" in combo and "slow" in combo and combo["fast"] >= combo["slow"]:
        return False
    return True


def _fmt_param_value(v: float) -> str:
    """参数展示：整数值省去小数点（4.0 → 4），其余保留原样。"""
    return str(int(v)) if float(v).is_integer() else f"{float(v):g}"


@dataclass
class CliffEvent:
    """一处「悬崖式衰减」：单维相邻档位间收益大幅跌落。"""

    param: str
    from_value: float
    to_value: float
    from_return: float
    to_return: float

    def describe(self) -> str:
        return (
            f"{self.param}: {_fmt_param_value(self.from_value)} → "
            f"{_fmt_param_value(self.to_value)} 时收益 "
            f"{self.from_return:.2%} 跌至 {self.to_return:.2%}"
        )


@dataclass
class ParamSensitivityResult:
    """参数邻域细检结果。"""

    scan: pd.DataFrame = field(default_factory=pd.DataFrame)  # 邻域全部组合明细
    focus_params: dict = field(default_factory=dict)  # 当前（中心）参数
    cliffs: list[CliffEvent] = field(default_factory=list)  # 悬崖事件
    stable_fraction: float = 0.0  # 稳健区域占比（收益 ≥ 70%·最佳）
    isolated_peak: bool = False  # 孤峰：中心为正而多数相邻点为负
    total: int = 0  # 邻域组合总数

    @property
    def cliff_count(self) -> int:
        return len(self.cliffs)


def detect_cliffs(
    scan: pd.DataFrame, focus: dict, metric: str = "total_return", drop_ratio: float = 0.5
) -> list[CliffEvent]:
    """悬崖检测：固定其他参数为中心值，单维升序扫描；相邻档位跌幅超过
    ``drop_ratio``（此前为正值）即记一次悬崖事件。"""
    events: list[CliffEvent] = []
    if scan.empty:
        return events
    for key in focus:
        sub = scan
        for other, val in focus.items():
            if other != key:
                sub = sub[sub[other] == val]
        if len(sub) < 2:
            continue
        sub = sub.sort_values(key)
        vals = sub[key].astype(float).to_numpy()
        rets = sub[metric].astype(float).to_numpy()
        for i in range(len(vals) - 1):
            r0, r1 = float(rets[i]), float(rets[i + 1])
            if r0 > 0.0 and r1 < r0 * (1.0 - drop_ratio):
                events.append(CliffEvent(key, float(vals[i]), float(vals[i + 1]), r0, r1))
    return events


def evaluate_combo(
    strategy_cls,
    params: dict,
    df: pd.DataFrame,
    broker_factory,
    portfolio_factory,
    position_size: int,
):
    """在给定数据上跑一组参数，返回绩效指标。"""
    portfolio = portfolio_factory()
    engine = BacktestEngine(
        df,
        strategy_cls(params),
        broker=broker_factory(),
        portfolio=portfolio,
        position_size=position_size,
    )
    equity_df, trades = engine.run()
    return metrics_mod.analyze(equity_df, trades, portfolio.initial_capital)


def run_param_neighborhood(
    df: pd.DataFrame,
    strategy_cls,
    params: dict,
    broker_factory,
    portfolio_factory,
    position_size: int,
    metric: str = "total_return",
    factors=NEIGHBOR_FACTORS,
) -> ParamSensitivityResult:
    """在当前参数邻域上细检：全组合回测 → 悬崖检测 + 稳健占比 + 孤峰标记。"""
    grid = build_neighborhood_grid(params, factors)
    rows: list[dict] = []
    for combo in expand_grid(grid):
        if not combo_is_valid(combo):
            continue
        try:
            m = evaluate_combo(
                strategy_cls, combo, df, broker_factory, portfolio_factory, position_size
            )
        except ValueError as exc:  # 策略自身校验不通过 → 跳过非法组合
            log.warning("参数邻域：跳过非法组合 %s（%s）", combo, exc)
            continue
        rows.append(
            {
                **combo,
                "total_return": m.total_return,
                "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown,
                "trade_count": m.trade_count,
            }
        )

    scan = pd.DataFrame(rows)
    result = ParamSensitivityResult(scan=scan, focus_params=dict(params), total=len(scan))
    if scan.empty:
        return result

    best = float(scan[metric].max())
    if best > 0:
        stable = scan[scan[metric] >= 0.7 * best]
        result.stable_fraction = float(len(stable) / len(scan))

    result.cliffs = detect_cliffs(scan, params, metric)

    # 孤峰：中心为正、且单维紧邻点中「一半以上为负」
    center_mask = pd.Series(True, index=scan.index)
    for k, v in params.items():
        center_mask &= scan[k] == v
    if center_mask.any():
        center_ret = float(scan.loc[center_mask, metric].iloc[0])
        neigh_rets: list[float] = []
        for k, v in params.items():
            vals = sorted(scan[k].unique())
            if v not in vals:
                continue
            idx = vals.index(v)
            for nb in (idx - 1, idx + 1):
                if 0 <= nb < len(vals):
                    cond = pd.Series(True, index=scan.index)
                    for k2, v2 in params.items():
                        cond &= scan[k2] == (vals[nb] if k2 == k else v2)
                    if cond.any():
                        neigh_rets.append(float(scan.loc[cond, metric].iloc[0]))
        if (
            center_ret > 0
            and neigh_rets
            and sum(1 for x in neigh_rets if x < 0) >= math.ceil(len(neigh_rets) / 2)
        ):
            result.isolated_peak = True

    log.info(
        "参数邻域细检完成：%d 格 · 稳健占比 %.1f%% · 悬崖 %d 处",
        result.total,
        result.stable_fraction * 100,
        result.cliff_count,
    )
    return result
