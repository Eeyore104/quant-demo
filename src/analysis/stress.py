"""压力测试（v1.3）：历史情景（数据驱动）+ 假设情景 + 蒙特卡洛回撤分布。

口径（详见 ``docs/12``）：

- 历史情景：自动识别样本期内组合「单日跌幅 Top3、5 日跌幅 Top3、波动率最高 3 段」，
  输出「当时实际损失」（直接读权益曲线）与「标准化重估」（按 1 手/品种名义敞口重演）；
- 假设情景：跳空冲击 −2σ/−3σ（全品种同向不利）、连续 N 日跌停、相关性跳升
  （分散化失效的额外损失：ρ→1 与按历史平均相关 ρ̄ 的差额）；
- 蒙特卡洛：iid bootstrap 组合日收益 → 最大回撤分布（P50/P90/P95/P99）。
- 说明：情景窗口识别与重估均为近似口径（σ 取近 60 日、重估按 1 手/品种、等名义近似），
  结果随数据与参数变化；确定性由随机种子保证。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..engine.contracts import ContractSpec


@dataclass
class ScenarioResult:
    """一条压力测试情景。"""

    category: str  # historical / hypothetical
    name: str
    window: str
    actual_loss: float | None = None  # 当时实际损失（元；仅历史情景）
    actual_pct: float | None = None
    replay_loss: float | None = None  # 标准化重估损失（1 手/品种，元）
    replay_pct: float | None = None
    note: str = ""

    def as_row(self) -> dict:
        def _r(v, nd=2):
            return None if v is None else round(float(v), nd)

        return {
            "category": self.category,
            "scenario": self.name,
            "window": self.window,
            "actual_loss": _r(self.actual_loss),
            "actual_pct": _r(self.actual_pct, 4),
            "replay_loss_1lot": _r(self.replay_loss),
            "replay_pct_1lot": _r(self.replay_pct, 4),
            "note": self.note,
        }


@dataclass
class StressResult:
    """压力测试汇总。"""

    scenarios: list[ScenarioResult]
    mc: dict  # 蒙特卡洛回撤分布（正数 = 回撤幅度）

    def as_rows(self) -> list[dict]:
        rows = [s.as_row() for s in self.scenarios]
        if self.mc:
            rows.append(
                {
                    "category": "mc",
                    "scenario": f"蒙特卡洛回撤分布（bootstrap × {self.mc.get('runs', 0)}）",
                    "window": "",
                    "actual_loss": None,
                    "actual_pct": None,
                    "replay_loss_1lot": None,
                    "replay_pct_1lot": None,
                    "note": (
                        f"P50 {self.mc['p50']:.2%} / P90 {self.mc['p90']:.2%} / "
                        f"P95 {self.mc['p95']:.2%} / P99 {self.mc['p99']:.2%}"
                    ),
                }
            )
        return rows


# ---------------------------------------------------------------- 情景识别


def identify_historical_scenarios(equity_df: pd.DataFrame, top_n: int = 3) -> list[tuple]:
    """识别历史极端情景：单日跌幅 Top3 / 5 日跌幅 Top3 / 波动率最高 3 段。

    返回 ``[(标签, 起始行, 结束行)]``（去重：低优先级情景与已接受窗口相交则跳过）。
    """
    eq = equity_df["equity"].astype(float).reset_index(drop=True)
    n = len(eq)
    rets = eq.pct_change()
    log1p = np.log1p(rets.fillna(0.0))

    candidates: list[tuple] = []  # (优先级, 起, 止, 标签)
    for idx, val in rets.dropna().nsmallest(top_n).items():
        candidates.append((0, int(idx), int(idx), f"单日跌幅 {val:.2%}"))
    if n >= 6:
        r5 = np.expm1(log1p.rolling(5).sum())
        for idx, val in r5.dropna().nsmallest(top_n).items():
            candidates.append((1, int(idx) - 4, int(idx), f"5 日跌幅 {val:.2%}"))
    if n >= 31:
        vol = rets.rolling(30).std()
        for idx, val in vol.dropna().nlargest(top_n).items():
            candidates.append((2, int(idx) - 29, int(idx), f"波动率最高 30 日段（日 σ {val:.2%}）"))

    accepted: list[tuple] = []
    for _, s, e, label in sorted(candidates, key=lambda c: (c[0], c[1])):
        if s < 1:  # 需要前一日基准
            continue
        if any(not (e < a_s or a_e < s) for _, a_s, a_e in accepted):
            continue
        accepted.append((label, s, e))
    accepted.sort(key=lambda t: t[1])
    return accepted


# ---------------------------------------------------------------- 辅助


def _window_return(df: pd.DataFrame, d0, d1) -> tuple[float, float] | None:
    """品种在 [d0, d1] 窗口的收益与期初基准价（用各自日历上的最近可用日）。"""
    dates = df["date"]
    before = df.loc[dates < d0, "close"]
    within = df.loc[(dates >= d0) & (dates <= d1), "close"]
    if len(before) == 0 or len(within) == 0:
        return None
    base = float(before.iloc[-1])
    if base <= 0:
        return None
    return float(within.iloc[-1]) / base - 1.0, base


def _mean_pair_corr(data: dict[str, pd.DataFrame], window: int = 60) -> float:
    """近 ``window`` 日逐品种收益的平均两两相关（分散化水平）。"""
    rets = {
        s: df["close"].astype(float).pct_change().dropna().tail(window).reset_index(drop=True)
        for s, df in data.items()
    }
    syms = list(rets)
    vals: list[float] = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = rets[syms[i]], rets[syms[j]]
            m = min(len(a), len(b))
            if m >= 5:
                c = float(np.corrcoef(a.iloc[-m:], b.iloc[-m:])[0, 1])
                if c == c:  # 非 NaN
                    vals.append(c)
    return float(np.mean(vals)) if vals else 0.0


# ---------------------------------------------------------------- 主流程


def run_stress(
    equity_df: pd.DataFrame,
    data: dict[str, pd.DataFrame],
    contracts: dict[str, ContractSpec],
    *,
    initial_capital: float,
    top_n: int = 3,
    gap_sigmas: tuple = (2.0, 3.0),
    limit_streak_days: int = 3,
    mc_runs: int = 500,
    seed: int = 42,
    limit_rates: dict | None = None,
    sigma_window: int = 60,
) -> StressResult:
    """执行压力测试：历史情景 + 假设情景 + 蒙特卡洛。"""
    scenarios: list[ScenarioResult] = []

    # ① 历史情景（当时实际损失 + 标准化重估）
    eq = equity_df["equity"].astype(float)
    for label, s, e in identify_historical_scenarios(equity_df, top_n):
        d0, d1 = equity_df["date"].iloc[s], equity_df["date"].iloc[e]
        base_eq = float(eq.iloc[s - 1])
        actual_amount = float(eq.iloc[e] - base_eq)
        actual_pct = actual_amount / base_eq if base_eq else None
        loss = 0.0
        for sym, df in data.items():
            res = _window_return(df, d0, d1)
            if res is None:
                continue
            r, base = res
            loss += base * contracts[sym].multiplier * r  # 1 手/品种、多头口径
        scenarios.append(
            ScenarioResult(
                "historical",
                label,
                f"{str(d0)[:10]} ~ {str(d1)[:10]}",
                actual_loss=actual_amount,
                actual_pct=actual_pct,
                replay_loss=loss,
                replay_pct=loss / initial_capital if initial_capital else None,
                note="重估口径：1 手/品种 · 多头方向",
            )
        )

    # ② 假设情景（以样本末端读数估计）
    stats: dict[str, tuple[float, float]] = {}
    for sym, df in data.items():
        closes = df["close"].astype(float)
        rets = closes.pct_change().dropna().tail(sigma_window)
        sigma = float(rets.std(ddof=0)) if len(rets) >= 2 else 0.0
        stats[sym] = (sigma, float(closes.iloc[-1]) * contracts[sym].multiplier)

    for k in gap_sigmas:
        loss = sum(sigma * k * notional for sigma, notional in stats.values())
        scenarios.append(
            ScenarioResult(
                "hypothetical",
                f"跳空冲击 −{k:g}σ（全品种同向不利）",
                "单日",
                replay_loss=loss,
                replay_pct=loss / initial_capital if initial_capital else None,
                note=f"σ 取各品种近 {sigma_window} 日日收益波动；多头口径，空头对称",
            )
        )

    rates = limit_rates or {}
    loss = 0.0
    for sym, (_, notional) in stats.items():
        rate = float(rates.get(sym, 0.07) or 0.07)
        loss += notional * (1.0 - (1.0 - rate) ** int(limit_streak_days))
    scenarios.append(
        ScenarioResult(
            "hypothetical",
            f"连续 {limit_streak_days} 日跌停",
            f"{limit_streak_days} 日",
            replay_loss=loss,
            replay_pct=loss / initial_capital if initial_capital else None,
            note="按品种停板幅度步进；含「无法平仓」含义的损失下界",
        )
    )

    corr = _mean_pair_corr(data)
    n = max(len(stats), 1)
    full_loss = sum(sigma * 2.0 * notional for sigma, notional in stats.values())
    partial = full_loss * float(np.sqrt((1.0 + (n - 1) * corr) / n))
    extra = full_loss - partial
    mult = (full_loss / partial) if partial > 0 else float("nan")
    scenarios.append(
        ScenarioResult(
            "hypothetical",
            "相关性跳升：分散化失效额外损失",
            "滚动 60 日",
            replay_loss=extra,
            replay_pct=extra / initial_capital if initial_capital else None,
            note=(
                f"全跌 −2σ 损失 {full_loss:,.0f} 元 vs 按平均相关 ρ̄={corr:.2f} 估算 "
                f"{partial:,.0f} 元（ρ→1 放大 ×{mult:.1f}）；等名义近似"
            ),
        )
    )

    # ③ 蒙特卡洛回撤分布（iid bootstrap）
    mc: dict = {}
    rets_p = eq.pct_change().dropna().to_numpy()
    if len(rets_p) >= 20 and mc_runs > 0:
        rng = np.random.default_rng(seed)
        dds = np.empty(int(mc_runs))
        for i in range(int(mc_runs)):
            sample = rng.choice(rets_p, size=len(rets_p), replace=True)
            path = np.cumprod(1.0 + sample)
            peak = np.maximum.accumulate(path)
            dds[i] = float(-(path / peak - 1.0).min())  # 正数 = 回撤幅度
        mc = {
            "runs": int(mc_runs),
            "p50": float(np.percentile(dds, 50)),
            "p90": float(np.percentile(dds, 90)),
            "p95": float(np.percentile(dds, 95)),
            "p99": float(np.percentile(dds, 99)),
            "worst": float(dds.max()),
            "samples": dds,  # 完整样本（供图表直方；不写入 CSV）
        }
    return StressResult(scenarios=scenarios, mc=mc)
