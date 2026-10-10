"""组合图表（v1.2）：权益与回撤 / 相关性矩阵 / 品种贡献 / 保证金占用。"""

from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")  # 无需 GUI，直接出图
import matplotlib.pyplot as plt

from .plotter import _titled


def plot_portfolio_equity(equity_df: pd.DataFrame, out_path, label: str = "") -> str:
    """组合权益曲线 + 回撤（双面板）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    equity = equity_df["equity"].astype(float)
    dates = equity_df["date"]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )
    ax1.plot(dates, equity, color="#1976d2", linewidth=1.5, label="组合权益")
    ax1.axhline(equity.iloc[0], color="#9e9e9e", linestyle="--", linewidth=1, label="初始资金")
    ax1.set_ylabel("权益（元）")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(alpha=0.3)
    ax1.set_title(_titled(label, "组合权益与回撤"))

    dd = (equity / equity.cummax() - 1.0) * 100.0
    ax2.fill_between(dates, dd, 0, color="#ef5350", alpha=0.30)
    ax2.plot(dates, dd, color="#c62828", linewidth=1.0)
    ax2.axhline(0, color="#9e9e9e", linewidth=0.8)
    ax2.set_ylabel("回撤（%）")
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_portfolio_correlation(corr: pd.DataFrame, out_path, label: str = "") -> str:
    """品种相关性热力图（逐品种策略日收益）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    values = corr.to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(7.5, 6))
    im = ax.imshow(values, cmap="RdYlGn", vmin=-1.0, vmax=1.0, aspect="auto")
    ax.set_xticks(range(len(corr.columns)))
    ax.set_xticklabels(list(corr.columns), fontsize=8)
    ax.set_yticks(range(len(corr.index)))
    ax.set_yticklabels(list(corr.index), fontsize=8)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            ax.text(j, i, f"{values[i, j]:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, label="相关系数", fraction=0.045, pad=0.02)
    ax.set_title(_titled(label, "品种相关性矩阵（策略日收益）"))
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_portfolio_contribution(sym_df: pd.DataFrame, out_path, label: str = "") -> str:
    """品种盈亏贡献条形图（红=盈利 / 绿=亏损，中国习惯）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    names = [str(v).split("（")[0] for v in sym_df["name"]]
    pnl = sym_df["pnl"].to_numpy(dtype=float)
    colors = ["#c62828" if v >= 0 else "#2e7d32" for v in pnl]

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.bar(range(len(names)), pnl, color=colors)
    ax.axhline(0, color="#9e9e9e", linewidth=0.8)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=8, rotation=20)
    ax.set_ylabel("累计盈亏（元）")
    ax.set_title(_titled(label, "品种盈亏贡献"))
    for bar, v in zip(bars, pnl, strict=True):
        ax.annotate(
            f"{v:,.0f}",
            xy=(bar.get_x() + bar.get_width() / 2, v),
            xytext=(0, 4 if v >= 0 else -12),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_portfolio_margin(
    equity_df: pd.DataFrame, initial_capital: float, out_path, label: str = ""
) -> str:
    """保证金占用与可用资金曲线（含资金线）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    dates = equity_df["date"]
    margin = equity_df["margin_used"].astype(float)
    available = equity_df["available"].astype(float)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.fill_between(dates, margin, 0, color="#90a4ae", alpha=0.55, label="保证金占用")
    ax.plot(dates, available, color="#1976d2", linewidth=1.2, label="可用资金")
    ax.axhline(initial_capital, color="#9e9e9e", linestyle="--", linewidth=1, label="初始资金")
    ax.set_ylabel("金额（元）")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)
    ax.set_title(_titled(label, "保证金占用与可用资金"))
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)
