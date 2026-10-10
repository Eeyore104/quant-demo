"""风控图表（v1.3）：风控开/关对比 / 压力测试 / 风险指标。"""

from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")  # 无需 GUI，直接出图
import matplotlib.pyplot as plt

from .plotter import _titled


def plot_risk_compare(
    eq_bare: pd.DataFrame, eq_risk: pd.DataFrame, out_path, label: str = ""
) -> str:
    """风控开/关对比：权益曲线 + 回撤（双面板，双口径对照）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=False, gridspec_kw={"height_ratios": [2, 1]}
    )
    d_bare, e_bare = eq_bare["date"], eq_bare["equity"].astype(float)
    d_risk, e_risk = eq_risk["date"], eq_risk["equity"].astype(float)

    ax1.plot(d_bare, e_bare, color="#ef5350", linewidth=1.3, label="裸奔（fixed 1 手 · 风控全关）")
    ax1.plot(
        d_risk, e_risk, color="#1976d2", linewidth=1.5, label="风控全开（风险预算 + 止损 + 熔断）"
    )
    ax1.axhline(e_risk.iloc[0], color="#9e9e9e", linestyle="--", linewidth=1, label="初始资金")
    ax1.set_ylabel("权益（元）")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(alpha=0.3)
    ax1.set_title(_titled(label, "风控开 / 关对比：组合权益"))

    dd_bare = (e_bare / e_bare.cummax() - 1.0) * 100.0
    dd_risk = (e_risk / e_risk.cummax() - 1.0) * 100.0
    ax2.plot(d_bare, dd_bare, color="#ef5350", linewidth=1.1, label="裸奔回撤")
    ax2.plot(d_risk, dd_risk, color="#1976d2", linewidth=1.3, label="风控回撤")
    ax2.axhline(0, color="#9e9e9e", linewidth=0.8)
    ax2.set_ylabel("回撤（%）")
    ax2.legend(loc="lower left", fontsize=8)
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_risk_stress(stress, out_path, label: str = "") -> str:
    """压力测试：情景重估损失条形 + 蒙特卡洛回撤分布（双面板）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5.2), gridspec_kw={"width_ratios": [1.3, 1]})

    names = [s.name.split("（")[0] for s in stress.scenarios]
    values = [(s.replay_loss or 0.0) for s in stress.scenarios]  # 正 = 亏损
    colors = ["#2e7d32" if v >= 0 else "#c62828" for v in values]  # 中国习惯：绿=亏损
    ax1.barh(range(len(names)), values, color=colors)
    ax1.axvline(0, color="#9e9e9e", linewidth=0.8)
    ax1.set_yticks(range(len(names)))
    ax1.set_yticklabels(names, fontsize=8)
    ax1.invert_yaxis()
    for i, v in enumerate(values):
        ax1.annotate(
            f"{v:,.0f}",
            xy=(v, i),
            xytext=(4 if v >= 0 else -4, 0),
            textcoords="offset points",
            ha="left" if v >= 0 else "right",
            va="center",
            fontsize=8,
        )
    left = min(0.0, min(values) * 1.35) if values else 0.0
    right = max(0.0, max(values) * 1.18) if values else 1.0
    ax1.set_xlim(left - (0 if left < 0 else 1.0), right + 1.0)
    ax1.set_xlabel("标准化重估损失（元 · 1 手/品种 · 正 = 亏损）")
    ax1.set_title(_titled(label, "压力测试：情景损失"))
    ax1.grid(alpha=0.3, axis="x")

    if stress.mc:
        m = stress.mc
        samples = m.get("samples")
        if samples is None:
            import numpy as np

            rng = np.random.default_rng(0)
            mean = (m["p50"] + m["p95"]) / 2.0
            std = max((m["p95"] - m["p50"]) / 1.645, 1e-4)
            samples = np.abs(rng.normal(mean, std, 2000))
        ax2.hist(samples * 100.0, bins=40, color="#90a4ae", alpha=0.85)
        for val, name, color in (
            (m["p50"], f"P50 {m['p50']:.1%}", "#546e7a"),
            (m["p95"], f"P95 {m['p95']:.1%}", "#ef6c00"),
            (m["p99"], f"P99 {m['p99']:.1%}", "#c62828"),
        ):
            ax2.axvline(val * 100.0, color=color, linewidth=1.2, linestyle="--", label=name)
        ax2.set_xlabel("最大回撤（%）")
        ax2.set_ylabel("路径数")
        ax2.legend(fontsize=8)
        ax2.set_title(_titled(label, f"蒙特卡洛回撤分布（×{m.get('runs', 0)}）"))
        ax2.grid(alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "样本不足，未计算", ha="center", va="center", transform=ax2.transAxes)

    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_risk_metrics(risk_daily: pd.DataFrame, out_path, label: str = "") -> str:
    """风险指标：VaR/CVaR 曲线 + 杠杆与保证金利用率（双面板）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [1.4, 1]}
    )
    d = risk_daily["date"]

    for col, name, color in (
        ("var95", "VaR 95%", "#1976d2"),
        ("var99", "VaR 99%", "#5e35b1"),
        ("cvar95", "CVaR 95%", "#ef6c00"),
    ):
        if col in risk_daily:
            ax1.plot(
                d, risk_daily[col].astype(float) * 100.0, linewidth=1.2, label=name, color=color
            )
    ax1.set_ylabel("日损失幅度（%）")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(alpha=0.3)
    ax1.set_title(_titled(label, "滚动风险指标（历史模拟）"))

    if "leverage" in risk_daily:
        ax2.plot(
            d,
            risk_daily["leverage"].astype(float),
            linewidth=1.2,
            color="#00838f",
            label="杠杆（名义/权益）",
        )
    if "margin_util" in risk_daily:
        ax2.plot(
            d,
            risk_daily["margin_util"].astype(float),
            linewidth=1.2,
            color="#9e9e9e",
            label="保证金利用率",
        )
    ax2.set_ylabel("倍数 / 占比")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)
