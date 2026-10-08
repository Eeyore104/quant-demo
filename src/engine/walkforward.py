"""样本外验证编排：训练段择优 → 测试段检验（旁路增强，零侵入回测引擎）。

设计原则：``src/engine/engine.py`` 的 ``BacktestEngine.__init__`` / ``run()`` 的
签名、返回值与成交约定**完全不动**；本模块只在引擎上层做「切分 + 择优 + 检验」
的编排，全部复用现有引擎实例。

流程::

    clean df ─► split_segments() ─► train_df / test_df
                    │
        ① 训练段：用 grid 扫描 → 择优参数 best_params（按 metric，默认 sharpe）
        ② 测试段：用 best_params 在 test_df 上跑一次（样本外检验）
        ③ 汇总：train_metrics vs test_metrics → OOSResult（供报告渲染对比表）
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from ..report import metrics as metrics_mod
from ..report.metrics import PerformanceMetrics
from ..utils.logger import get_logger
from .engine import BacktestEngine

# 复用 engine 的 logger：子 logger（如 "engine.walkforward"）会向父 logger 冒泡，
# 而父子各自都被 get_logger 挂了 handler，会导致每条日志打印两遍。
log = get_logger("engine")

#: 训练段择优可用指标（均为「越大越好」口径）
VALID_METRICS = ("sharpe", "total_return", "max_drawdown")


@dataclass
class OOSResult:
    """样本外验证结果（训练段择优 → 测试段检验）。"""

    best_params: dict = field(default_factory=dict)  # 训练段择优参数
    train_metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)  # 样本内指标
    test_metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)  # 样本外指标
    scan_train: pd.DataFrame = field(default_factory=pd.DataFrame)  # 训练段参数扫描明细
    train_range: tuple = (None, None)  # (起, 止)
    test_range: tuple = (None, None)  # (起, 止)；测试段为空时为 (None, None)
    skipped: bool = False  # 训练段为空 → 整体跳过样本外验证


def expand_grid(grid: dict) -> list[dict]:
    """把 ``{参数: [取值...]}`` 展开为参数组合列表（等价 itertools.product）。"""
    keys = list(grid)
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[k] for k in keys))
    ]


def _is_valid(combo: dict) -> bool:
    """跳过非法组合：如双均线要求 fast < slow。"""
    if "fast" in combo and "slow" in combo and combo["fast"] >= combo["slow"]:
        return False
    return True


def _metric_value(m: PerformanceMetrics, metric: str) -> float:
    """取择优指标值（统一「越大越好」；最大回撤越接近 0 越好）。"""
    if metric not in VALID_METRICS:
        raise ValueError(f"未知择优指标: {metric}（可用: {VALID_METRICS}）")
    return float(getattr(m, metric))


def split_segments(
    df: pd.DataFrame, split_date: str | None = None, ratio: float = 0.7
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按日期切分训练段 / 测试段。

    - ``split_date`` 优先：``date <= split_date`` 归训练段、``date > split_date`` 归测试段。
    - ``split_date`` 为空串 / 无法解析（NaT）时视为「未提供」，回退 ``ratio`` 路径。
    - 未给 ``split_date``：按 ``ratio`` 比例切分（前 ``ratio`` 归训练段）。
    """
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"])

    cut = None
    if split_date is not None and str(split_date).strip() != "":
        cut = pd.to_datetime(split_date, errors="coerce")
        if pd.isna(cut):  # 空串已排除；此处兜底非法日期字符串
            log.warning("split_date=%r 不是有效日期，回退按 ratio=%.2f 切分", split_date, ratio)
            cut = None

    if cut is not None:
        train = data[data["date"] <= cut]
        test = data[data["date"] > cut]
    else:
        if not 0 < ratio < 1:
            raise ValueError(f"ratio 必须在 (0, 1) 之间，当前 {ratio}")
        k = int(len(data) * ratio)
        train = data.iloc[:k]
        test = data.iloc[k:]
    return train.reset_index(drop=True), test.reset_index(drop=True)


def run_segment(df: pd.DataFrame, strategy, broker, portfolio, position_size: int) -> tuple:
    """薄封装：直接复用 ``BacktestEngine``（引擎零改动）。返回 (权益曲线, 成交列表)。"""
    engine = BacktestEngine(
        df, strategy, broker=broker, portfolio=portfolio, position_size=position_size
    )
    return engine.run()


def _evaluate(
    strategy_cls,
    params: dict,
    df: pd.DataFrame,
    broker_factory: Callable,
    portfolio_factory: Callable,
    position_size: int,
) -> PerformanceMetrics:
    """在给定数据段上，用一组参数跑一次回测并返回绩效指标。"""
    strategy = strategy_cls(params)
    broker = broker_factory()
    portfolio = portfolio_factory()
    equity_df, trades = run_segment(df, strategy, broker, portfolio, position_size)
    return metrics_mod.analyze(equity_df, trades, portfolio.initial_capital)


def select_best_params(
    train_df: pd.DataFrame,
    strategy_cls,
    grid: dict,
    broker_factory: Callable,
    portfolio_factory: Callable,
    position_size: int,
    metric: str = "sharpe",
) -> tuple[dict, pd.DataFrame]:
    """训练段参数网格扫描，按 ``metric`` 择优，返回 (best_params, 明细表)。

    每格参数使用**全新的** Broker / Portfolio（工厂函数），避免状态串味。
    """
    if metric not in VALID_METRICS:
        raise ValueError(f"未知择优指标: {metric}（可用: {VALID_METRICS}）")

    rows: list[dict] = []
    best_params: dict | None = None
    best_value: float | None = None

    for combo in expand_grid(grid):
        if not _is_valid(combo):
            continue
        try:
            m = _evaluate(
                strategy_cls, combo, train_df, broker_factory, portfolio_factory, position_size
            )
        except ValueError as exc:  # 策略自身校验不通过 → 跳过非法组合
            log.warning("跳过非法参数 %s：%s", combo, exc)
            continue
        value = _metric_value(m, metric)
        rows.append(
            {
                **combo,
                "total_return": round(m.total_return, 4),
                "max_drawdown": round(m.max_drawdown, 4),
                "sharpe": round(m.sharpe, 3),
                "win_rate": round(m.win_rate, 4),
                "trade_count": m.trade_count,
            }
        )
        if best_value is None or value > best_value:
            best_value, best_params = value, combo

    if best_params is None:
        raise ValueError("训练段没有可用参数组合（grid 为空或全部非法）")

    scan = pd.DataFrame(rows).sort_values(metric, ascending=False).reset_index(drop=True)
    return best_params, scan


def run_oos(
    df: pd.DataFrame,
    strategy_cls,
    grid: dict,
    broker_factory: Callable,
    portfolio_factory: Callable,
    position_size: int,
    split_date: str | None = None,
    ratio: float = 0.7,
    metric: str = "sharpe",
) -> OOSResult:
    """完整样本外流程：切分 → 训练段择优 → 测试段检验 → 汇总。

    - 训练段为空（如 ``split_date`` 早于数据起点）：打 WARN 并返回 ``skipped=True`` 的
      空结果，**不抛异常**，让一键运行正常结束。
    - 测试段为空（数据太短）：打 WARN，``test_metrics`` 记为空，其余照常返回。
    """
    train_df, test_df = split_segments(df, split_date, ratio)
    if len(train_df) == 0:
        log.warning("训练段为空（split_date=%r 早于数据起点？），跳过样本外验证", split_date)
        return OOSResult(skipped=True)

    best_params, scan_train = select_best_params(
        train_df, strategy_cls, grid, broker_factory, portfolio_factory, position_size, metric
    )
    train_metrics = _evaluate(
        strategy_cls, best_params, train_df, broker_factory, portfolio_factory, position_size
    )

    if len(test_df) == 0:
        log.warning("测试段为空（数据太短），样本外检验跳过，test_metrics 记为空")
        test_metrics = PerformanceMetrics()
        test_range = (None, None)
    else:
        test_metrics = _evaluate(
            strategy_cls, best_params, test_df, broker_factory, portfolio_factory, position_size
        )
        test_range = (test_df["date"].iloc[0], test_df["date"].iloc[-1])

    log.info(
        "样本外完成：训练段 %d 根 / 测试段 %d 根，择优 %s=%s",
        len(train_df),
        len(test_df),
        metric,
        best_params,
    )
    return OOSResult(
        best_params=best_params,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        scan_train=scan_train,
        train_range=(train_df["date"].iloc[0], train_df["date"].iloc[-1]),
        test_range=test_range,
    )
