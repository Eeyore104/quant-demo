"""风险指标（v1.3）：日频 VaR/CVaR（历史模拟）+ 集中度 + 相关性预警。

口径（详见 ``docs/12``）：

- VaR/CVaR：历史模拟法，滚动窗口（默认 250 日）95% / 99%；只用截至当日历史（防未来），
  读数表示为「损失幅度（正数）」；样本不足 30 个收益日时留空；
- 集中度：逐日品种 / 板块名义敞口占比（取当日最大占比作为集中度读数）；
- 相关性预警：逐品种策略日收益的滚动平均两两相关（默认 60 日窗口）> 阈值 → 预警。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def build_risk_daily(
    equity_df: pd.DataFrame,
    *,
    var_window: int = 250,
    confidences: tuple = (0.95, 0.99),
    corr_window: int = 60,
    corr_warn: float = 0.5,
    sectors: dict | None = None,
) -> pd.DataFrame:
    """构建逐日风险指标表（供报告、图表与 ``risk_daily.csv``）。"""
    df = pd.DataFrame(index=equity_df.index)
    df["date"] = equity_df["date"]
    equity = equity_df["equity"].astype(float)
    df["equity"] = equity
    df["drawdown"] = equity / equity.cummax() - 1.0
    if "exposure_total" in equity_df.columns:
        exposure = equity_df["exposure_total"].astype(float)
        df["leverage"] = exposure / equity.replace(0.0, np.nan)
    if "margin_used" in equity_df.columns:
        df["margin_util"] = equity_df["margin_used"].astype(float) / equity.replace(0.0, np.nan)
    for col in ("exposure_warn", "fuse_state"):
        if col in equity_df.columns:
            df[col] = equity_df[col]

    # ---------- VaR / CVaR（历史模拟，滚动；只用截至当日历史）----------
    rets = equity.pct_change()
    series: dict[str, list] = {}
    for conf in confidences:
        pct = int(round(conf * 100))
        series[f"var{pct}"] = []
        series[f"cvar{pct}"] = []
    for i in range(len(df)):
        hist = rets.iloc[max(0, i - var_window + 1) : i + 1].dropna()
        if len(hist) < 30:
            for col in series:
                series[col].append(np.nan)
            continue
        for conf in confidences:
            pct = int(round(conf * 100))
            q = float(np.quantile(hist, 1.0 - conf))
            var = max(0.0, -q)
            tail = hist[hist <= q]
            cvar = max(0.0, -float(tail.mean())) if len(tail) else np.nan
            series[f"var{pct}"].append(var)
            series[f"cvar{pct}"].append(cvar)
    for col, vals in series.items():
        df[col] = vals

    # ---------- 集中度（逐日名义占比）----------
    skip = {"exposure_total", "exposure_warn"}
    exp_cols = {
        c[len("exposure_") :]: c
        for c in equity_df.columns
        if c.startswith("exposure_") and c not in skip
    }
    df["top_symbol_share"] = np.nan
    df["top_sector_share"] = np.nan
    if exp_cols:
        exp = equity_df[list(exp_cols.values())].astype(float)
        total = exp.sum(axis=1).replace(0.0, np.nan)
        shares = exp.div(total, axis=0)
        df["top_symbol_share"] = shares.max(axis=1)
        if sectors:
            sec = pd.DataFrame(index=exp.index)
            for name, syms in sectors.items():
                cols = [exp_cols[s] for s in syms if s in exp_cols]
                if cols:
                    sec[name] = exp[cols].sum(axis=1) / total
            if len(sec.columns):
                df["top_sector_share"] = sec.max(axis=1)
    df["top_symbol_share"] = df["top_symbol_share"].fillna(0.0)
    df["top_sector_share"] = df["top_sector_share"].fillna(0.0)

    # ---------- 相关性预警（逐品种策略日收益）----------
    pnl_cols = [c for c in equity_df.columns if c.startswith("pnl_")]
    df["corr_mean"] = np.nan
    df["corr_warn"] = 0
    if len(pnl_cols) >= 2:
        rets_map = {c: equity_df[c].astype(float).diff() for c in pnl_cols}
        acc = None
        cnt = 0
        for i in range(len(pnl_cols)):
            for j in range(i + 1, len(pnl_cols)):
                rc = rets_map[pnl_cols[i]].rolling(corr_window).corr(rets_map[pnl_cols[j]])
                acc = rc if acc is None else acc + rc
                cnt += 1
        if cnt:
            mean_corr = acc / cnt
            df["corr_mean"] = mean_corr
            df["corr_warn"] = (mean_corr > corr_warn).fillna(False).astype(int)
    return df
