"""过拟合体检（v1.1）编排：四件套（样本外 / 参数敏感性 / 蒙特卡洛 / 成本敏感性）
+ Deflated Sharpe 校正 → 逐项判定 + 总判定。

判定口径（阈值均可在 ``config.health_check.thresholds`` 覆盖）：
- 样本外：测试段收益 > 0 且夏普 > 0 → 通过；仅收益 > 0 → 存疑；否则 不通过；
  使用次数 > 3 → 降一级（样本外反复使用，可信度下降）。
- 参数敏感性：无悬崖且稳健占比达标 → 通过；有悬崖或稳健占比不足 → 存疑；
  ≥2 处悬崖或孤峰 → 不通过。
- 蒙特卡洛：分位 ≥ 90 → 通过；≥ 70 → 存疑；否则 不通过（择时未显著优于随机）。
- 成本敏感性：归零点 ≥ 3×（或 3× 成本仍为正）→ 通过；≥ 1.5× → 存疑；否则 不通过。
- Deflated Sharpe：DSR ≥ 0.95 → 通过；≥ 0.90 → 存疑；否则 不通过。
总判定：各项取「最差」（跳过项不参与）。
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..engine.walkforward import expand_grid
from ..utils.logger import get_logger
from . import cost_sensitivity as cost_mod
from . import deflated_sharpe as dsr_mod
from . import monte_carlo as mc_mod
from . import param_sensitivity as ps_mod

log = get_logger("analysis")

GRADE_PASS = "通过"
GRADE_WARN = "存疑"
GRADE_FAIL = "不通过"
GRADE_SKIP = "跳过"

_GRADE_ORDER = {GRADE_SKIP: 0, GRADE_PASS: 1, GRADE_WARN: 2, GRADE_FAIL: 3}

#: 默认判定阈值（config.health_check.thresholds 可覆盖）
DEFAULT_THRESHOLDS = {
    "mc_good_percentile": 90.0,
    "mc_warn_percentile": 70.0,
    "cost_good_multiplier": 3.0,
    "cost_warn_multiplier": 1.5,
    "dsr_good": 0.95,
    "dsr_warn": 0.90,
    "stable_fraction_warn": 0.6,
    "oos_usage_warn": 3,
}


@dataclass
class HealthItem:
    """体检单项：标题 / 判定 / 主要读数 / 备注。"""

    key: str
    title: str
    grade: str
    detail: str
    note: str = ""


@dataclass
class HealthReport:
    """体检总报告（items 为逐项判定；overall 为总判定）。"""

    items: list[HealthItem] = field(default_factory=list)
    overall: str = GRADE_SKIP
    summary: str = ""
    psr: ps_mod.ParamSensitivityResult | None = None  # 参数邻域细检
    mc: mc_mod.SignalShuffleResult | None = None  # 信号重排对照
    bootstrap: mc_mod.BootstrapResult | None = None  # 收益 Bootstrap
    cs: cost_mod.CostSensitivityResult | None = None  # 成本敏感性
    dsr: dsr_mod.DSRResult | None = None  # Deflated Sharpe


def overall_grade(items: list[HealthItem]) -> str:
    """总判定 = 各项最差（跳过项不参与；全跳过时返回「跳过」）。"""
    grades = [it.grade for it in items if it.grade != GRADE_SKIP]
    if not grades:
        return GRADE_SKIP
    return max(grades, key=lambda g: _GRADE_ORDER[g])


def _downgrade(grade: str) -> str:
    """降一级（通过 → 存疑 → 不通过；跳过不变）。"""
    if grade == GRADE_PASS:
        return GRADE_WARN
    if grade == GRADE_WARN:
        return GRADE_FAIL
    return grade


def _grade_oos(oos, usage: int | None, th: dict) -> HealthItem:
    title = "样本外验证"
    if oos is None or getattr(oos, "skipped", True):
        return HealthItem("oos", title, GRADE_SKIP, "未启用或训练段为空，未执行")
    if getattr(oos, "test_range", (None, None))[0] is None:
        return HealthItem("oos", title, GRADE_SKIP, "测试段为空（数据太短）")

    sm = oos.test_metrics
    if sm.total_return > 0 and sm.sharpe > 0:
        grade = GRADE_PASS
    elif sm.total_return > 0:
        grade = GRADE_WARN
    else:
        grade = GRADE_FAIL

    detail = f"测试段收益 {sm.total_return:.2%} / 夏普 {sm.sharpe:.2f} / 回撤 {sm.max_drawdown:.2%}"
    note = ""
    if usage is not None:
        detail += f" · 使用次数 {usage}"
        warn_n = int(th["oos_usage_warn"])
        if usage > warn_n:
            grade = _downgrade(grade)
            note = f"样本外已使用 {usage} 次（> {warn_n}）→ 结论降级：它已不再是「没见过」的数据"
    return HealthItem("oos", title, grade, detail, note)


def _grade_param(psr: ps_mod.ParamSensitivityResult | None, th: dict) -> HealthItem:
    title = "参数敏感性"
    if psr is None or psr.total == 0:
        return HealthItem("param", title, GRADE_SKIP, "未启用或邻域网格为空")

    if psr.cliff_count >= 2 or psr.isolated_peak:
        grade = GRADE_FAIL
    elif psr.cliff_count >= 1 or psr.stable_fraction < float(th["stable_fraction_warn"]):
        grade = GRADE_WARN
    else:
        grade = GRADE_PASS

    detail = f"邻域 {psr.total} 格 · 稳健占比 {psr.stable_fraction:.0%} · 悬崖 {psr.cliff_count} 处"
    notes = [f"悬崖：{ev.describe()}" for ev in psr.cliffs[:3]]
    if psr.isolated_peak:
        notes.append("孤峰：当前参数为正收益，但相邻档位多数为负——疑似拟合到局部峰点")
    return HealthItem("param", title, grade, detail, "；".join(notes))


def _grade_mc(mc: mc_mod.SignalShuffleResult | None, boot: mc_mod.BootstrapResult | None, th: dict):
    title = "蒙特卡洛对照"
    if mc is None:
        return HealthItem("mc", title, GRADE_SKIP, "未启用")

    good, warn = float(th["mc_good_percentile"]), float(th["mc_warn_percentile"])
    if mc.percentile >= good:
        grade = GRADE_PASS
    elif mc.percentile >= warn:
        grade = GRADE_WARN
    else:
        grade = GRADE_FAIL

    detail = (
        f"实际收益优于 {mc.percentile:.1f}% 的随机重排对照（{mc.runs} 次，p ≈ {mc.p_value:.2f}）"
    )
    notes = []
    if mc.actual_return <= 0:
        notes.append("注：策略本样本累计收益为负，该分位不代表可盈利")
    if boot is not None:
        notes.append(
            f"Bootstrap {boot.runs} 条路径：收益 5%/50%/95% 分位 "
            f"{boot.return_q05:.2%} / {boot.return_q50:.2%} / {boot.return_q95:.2%}；"
            f"回撤中位 {boot.mdd_q50:.2%}、5% 分位 {boot.mdd_q05:.2%}；路径为负比例 "
            f"{boot.prob_negative:.0%}（iid 日收益重采样口径）"
        )
    return HealthItem("mc", title, grade, detail, "；".join(notes))


def _grade_cost(cs: cost_mod.CostSensitivityResult | None, th: dict) -> HealthItem:
    title = "成本敏感性"
    if cs is None or not cs.rows:
        return HealthItem("cost", title, GRADE_SKIP, "未启用")

    good = float(th["cost_good_multiplier"])
    warn = float(th["cost_warn_multiplier"])
    max_mult = max(r["multiplier"] for r in cs.rows)

    if cs.reason == "negative_at_base":
        grade = GRADE_FAIL
        detail = "基准成本（×1）下累计收益已为负——策略本身不成立"
    elif cs.zero_multiplier is not None:
        z = cs.zero_multiplier
        detail = f"收益在成本 ×{z:.2f} 处归零（相邻档位线性插值）"
        if z >= good:
            grade = GRADE_PASS
        elif z >= warn:
            grade = GRADE_WARN
        else:
            grade = GRADE_FAIL
    else:  # not_breached：放大到最高档仍为正
        detail = f"成本放大至 ×{max_mult:g} 收益仍为正（未归零）"
        grade = GRADE_PASS if max_mult >= good else GRADE_WARN

    rows_txt = " · ".join(
        f"×{r['multiplier']:g}: {r['total_return']:.2%}" for r in cs.rows if r["multiplier"] > 1.0
    )
    return HealthItem("cost", title, grade, detail, f"各档收益：{rows_txt}")


def _grade_dsr(dsr: dsr_mod.DSRResult | None, th: dict) -> HealthItem:
    title = "Deflated Sharpe"
    if dsr is None:
        return HealthItem("dsr", title, GRADE_SKIP, "未启用或有效参数组合不足（< 2 组）")

    good, warn = float(th["dsr_good"]), float(th["dsr_warn"])
    if dsr.dsr >= good:
        grade = GRADE_PASS
    elif dsr.dsr >= warn:
        grade = GRADE_WARN
    else:
        grade = GRADE_FAIL

    detail = (
        f"DSR = {dsr.dsr:.3f}（{dsr.n_trials} 次参数试验校正）· "
        f"实际夏普(年化) {dsr.sr_observed_annual:.2f} vs 期望最大 {dsr.sr_expected_max_annual:.2f}"
    )
    return HealthItem("dsr", title, grade, detail)


def _run_dsr(
    df: pd.DataFrame,
    strategy_cls,
    grid: dict,
    broker_factory,
    portfolio_factory,
    position_size: int,
    equity_df: pd.DataFrame,
) -> dsr_mod.DSRResult | None:
    """在参数网格上重跑试验、统计试验间夏普方差，对主回测做 DSR 校正。"""
    sr_annuals: list[float] = []
    for combo in expand_grid(grid):
        if not ps_mod.combo_is_valid(combo):
            continue
        try:
            m = ps_mod.evaluate_combo(
                strategy_cls, combo, df, broker_factory, portfolio_factory, position_size
            )
        except ValueError:
            continue
        sr_annuals.append(float(m.sharpe))

    if len(sr_annuals) < 2:
        return None

    sr_per = np.asarray(sr_annuals, dtype=float) / math.sqrt(252.0)
    variance = float(sr_per.var(ddof=0))
    daily = equity_df["equity"].astype(float).pct_change().dropna()
    return dsr_mod.deflated_sharpe_ratio(
        daily.to_numpy(), n_trials=sr_per.size, sr_variance=variance
    )


def _summarize(report: HealthReport) -> str:
    fails = [it.title for it in report.items if it.grade == GRADE_FAIL]
    warns = [it.title for it in report.items if it.grade == GRADE_WARN]
    if report.overall == GRADE_PASS:
        return "各项均通过：本样本内未发现明显过拟合证据（不构成收益保证）"
    if report.overall == GRADE_WARN:
        return "存疑项：" + "、".join(warns) + " —— 结论可用但需谨慎"
    if report.overall == GRADE_FAIL:
        parts = []
        if fails:
            parts.append("不通过项：" + "、".join(fails))
        if warns:
            parts.append("存疑项：" + "、".join(warns))
        return "；".join(parts) + " —— 研究结论需谨慎，请勿据此直接上实盘"
    return "无有效体检项"


def run_health_check(
    *,
    df: pd.DataFrame,
    df_with_signal: pd.DataFrame,
    metrics,
    equity_df: pd.DataFrame,
    strategy_cls,
    strategy_params: dict,
    grid: dict | None,
    broker_factory: Callable,
    portfolio_factory: Callable,
    broker_kwargs: dict,
    initial_capital: float,
    position_size: int,
    contract_multiplier: int,
    oos=None,
    oos_usage: int | None = None,
    cfg: dict | None = None,
) -> HealthReport:
    """执行全部体检项并汇总。单项失败不中断整体（该记「跳过」+ 原因）。"""
    cfg = cfg or {}
    th = {**DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
    report = HealthReport()

    # ① 样本外（复用主流程已有结果）
    report.items.append(_grade_oos(oos, oos_usage, th))

    # ② 参数敏感性
    try:
        if (cfg.get("param_neighborhood") or {}).get("enabled", True):
            report.psr = ps_mod.run_param_neighborhood(
                df, strategy_cls, strategy_params, broker_factory, portfolio_factory, position_size
            )
            report.items.append(_grade_param(report.psr, th))
        else:
            report.items.append(HealthItem("param", "参数敏感性", GRADE_SKIP, "未启用"))
    except Exception as exc:  # noqa: BLE001 —— 单项失败不拖垮整个体检
        log.exception("参数邻域细检失败")
        report.items.append(HealthItem("param", "参数敏感性", GRADE_SKIP, f"计算失败：{exc}"))

    # ③ 蒙特卡洛（信号重排 + Bootstrap）
    try:
        mc_cfg = cfg.get("monte_carlo") or {}
        if mc_cfg.get("enabled", True):
            seed = int(mc_cfg.get("seed", 42))
            report.mc = mc_mod.run_signal_shuffle(
                df_with_signal,
                broker_factory,
                portfolio_factory,
                position_size,
                actual_return=metrics.total_return,
                runs=int(mc_cfg.get("runs", 150)),
                seed=seed,
            )
            report.bootstrap = mc_mod.run_bootstrap(
                equity_df,
                initial_capital,
                runs=int(mc_cfg.get("bootstrap_runs", 500)),
                seed=seed + 1,
            )
            report.items.append(_grade_mc(report.mc, report.bootstrap, th))
        else:
            report.items.append(HealthItem("mc", "蒙特卡洛对照", GRADE_SKIP, "未启用"))
    except Exception as exc:  # noqa: BLE001
        log.exception("蒙特卡洛对照失败")
        report.items.append(HealthItem("mc", "蒙特卡洛对照", GRADE_SKIP, f"计算失败：{exc}"))

    # ④ 成本敏感性
    try:
        cs_cfg = cfg.get("cost_sensitivity") or {}
        if cs_cfg.get("enabled", True):
            multipliers = tuple(cs_cfg.get("multipliers", [1.5, 2.0, 3.0]))
            report.cs = cost_mod.run_cost_sensitivity(
                df,
                strategy_cls,
                strategy_params,
                broker_kwargs,
                initial_capital,
                position_size,
                contract_multiplier,
                multipliers=(1.0, *multipliers),
            )
            report.items.append(_grade_cost(report.cs, th))
        else:
            report.items.append(HealthItem("cost", "成本敏感性", GRADE_SKIP, "未启用"))
    except Exception as exc:  # noqa: BLE001
        log.exception("成本敏感性失败")
        report.items.append(HealthItem("cost", "成本敏感性", GRADE_SKIP, f"计算失败：{exc}"))

    # ⑤ Deflated Sharpe
    try:
        if (cfg.get("deflated_sharpe") or {}).get("enabled", True) and grid:
            report.dsr = _run_dsr(
                df, strategy_cls, grid, broker_factory, portfolio_factory, position_size, equity_df
            )
            report.items.append(_grade_dsr(report.dsr, th))
        else:
            report.items.append(
                HealthItem("dsr", "Deflated Sharpe", GRADE_SKIP, "未启用或缺少参数网格")
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("Deflated Sharpe 计算失败")
        report.items.append(HealthItem("dsr", "Deflated Sharpe", GRADE_SKIP, f"计算失败：{exc}"))

    report.overall = overall_grade(report.items)
    report.summary = _summarize(report)
    log.info("过拟合体检完成：总判定 %s", report.overall)
    return report
