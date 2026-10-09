"""图表绘制：matplotlib → PNG（Windows 中文字体设置）。"""

from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # 无需 GUI，直接出图
import matplotlib.pyplot as plt

# Windows 中文字体，防止中文乱码
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False


def _titled(label: str, text: str) -> str:
    """给图表标题加上品种前缀（如「玉米 C0（主力连续） · 权益曲线」）。"""
    return f"{label} · {text}" if label else text


def plot_equity(equity_df, out_path, label: str = "") -> str:
    """权益曲线图（``label`` 为品种标注，如 "玉米 C0（主力连续）"）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        equity_df["date"], equity_df["equity"], color="#1976d2", linewidth=1.5, label="账户权益"
    )
    ax.axhline(
        y=equity_df["equity"].iloc[0],
        color="#9e9e9e",
        linestyle="--",
        linewidth=1,
        label="初始资金",
    )
    ax.set_title(_titled(label, "权益曲线"))
    ax.set_ylabel("权益（元）")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_signals(df, trades, out_path, label: str = "") -> str:
    """价格 + 均线/布林带 + 买卖点标注图。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(df["date"], df["close"], color="#455a64", linewidth=1.2, label="收盘价")

    # 策略指标线（有什么画什么）
    for col, color in (
        ("ma_fast", "#ef6c00"),
        ("ma_slow", "#6a1b9a"),
        ("boll_up", "#90a4ae"),
        ("boll_low", "#90a4ae"),
    ):
        if col in df.columns:
            ax.plot(df["date"], df[col], linewidth=1.0, color=color, label=col)

    # 买卖点：OPEN LONG / CLOSE SHORT = 买入；OPEN SHORT / CLOSE LONG = 卖出
    buys_x, buys_y, sells_x, sells_y = [], [], [], []
    for t in trades:
        is_buy = (t.direction == "LONG") == (t.action == "OPEN")
        (buys_x if is_buy else sells_x).append(t.date)
        (buys_y if is_buy else sells_y).append(t.price)
    if buys_x:
        ax.scatter(buys_x, buys_y, marker="^", color="#d32f2f", s=40, zorder=5, label="买入")
    if sells_x:
        ax.scatter(sells_x, sells_y, marker="v", color="#2e7d32", s=40, zorder=5, label="卖出")

    ax.set_title(_titled(label, "价格与买卖点"))
    ax.set_ylabel("价格（元/吨）")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_drawdown(equity_df, out_path, label: str = "") -> str:
    """回撤区间图：绘制回撤曲线（%）并高亮最大回撤区间。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    equity = equity_df["equity"].astype(float)
    dates = equity_df["date"]
    peak = equity.cummax()
    dd = (equity / peak - 1.0) * 100.0  # 百分比口径

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.fill_between(dates, dd, 0, color="#ef5350", alpha=0.30)
    ax.plot(dates, dd, color="#c62828", linewidth=1.2, label="回撤（%）")

    # 最大回撤区间：谷底为回撤最深处，起点取其之前的权益峰值
    dd_values = dd.to_numpy()
    trough_pos = int(dd_values.argmin())
    peak_pos = int(equity.iloc[: trough_pos + 1].to_numpy().argmax())
    ax.axvspan(dates.iloc[peak_pos], dates.iloc[trough_pos], color="#ffcc80", alpha=0.35)
    ax.scatter([dates.iloc[trough_pos]], [dd_values[trough_pos]], color="#b71c1c", s=35, zorder=5)
    ax.annotate(
        f"最大回撤 {dd_values[trough_pos]:.2f}%",
        xy=(dates.iloc[trough_pos], dd_values[trough_pos]),
        xytext=(8, -6),
        textcoords="offset points",
        color="#b71c1c",
        fontsize=9,
    )

    ax.axhline(0, color="#9e9e9e", linewidth=0.8)
    ax.set_title(_titled(label, "回撤区间图"))
    ax.set_ylabel("回撤（%）")
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_monthly_heatmap(equity_df, out_path, label: str = "") -> str:
    """月度收益热力图：按 年 × 月 排列（口径=权益月收益率，%）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    equity = equity_df["equity"].astype(float)
    idx = pd.to_datetime(equity_df["date"])
    series = pd.Series(equity.to_numpy(), index=idx).sort_index()

    daily_ret = series.pct_change().fillna(0.0)
    monthly = (1.0 + daily_ret).resample("ME").prod() - 1.0

    frame = monthly.to_frame("ret")
    frame["year"] = frame.index.year
    frame["month"] = frame.index.month
    pivot = frame.pivot_table(index="year", columns="month", values="ret")
    pivot = pivot.reindex(columns=range(1, 13))
    values = pivot.to_numpy(dtype=float) * 100.0

    fig, ax = plt.subplots(figsize=(10, 1.2 + 0.6 * len(pivot.index)))
    abs_vals = np.abs(values)
    vmax = float(np.nanmax(abs_vals)) if np.isfinite(abs_vals).any() else 1.0
    vmax = max(vmax, 1e-6)
    im = ax.imshow(values, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(12))
    ax.set_xticklabels([f"{m}月" for m in range(1, 13)])
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([str(y) for y in pivot.index])
    ax.set_xlabel("月份")
    ax.set_ylabel("年份")

    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            v = values[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:.1f}", ha="center", va="center", fontsize=7, color="#212121")

    fig.colorbar(im, ax=ax, label="月收益（%）", fraction=0.025, pad=0.02)
    ax.set_title(_titled(label, "月度收益热力图（口径：权益月收益率）"))
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


_METRIC_COLS = {"total_return", "max_drawdown", "sharpe", "win_rate", "trade_count", "commission"}


def plot_param_heatmap(scan_df, out_path, metric: str = "sharpe", label: str = "") -> str:
    """参数扫描热力图：两个参数为平面、选定绩效指标为色阶。

    参数仅 1 个时退化为「按参数着色的柱状图」。无参数列时返回空串。
    ``label`` 为品种标注（如 "玉米 C0（主力连续）"）。
    """
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    param_cols = [c for c in scan_df.columns if c not in _METRIC_COLS and c != "symbol"]
    if not param_cols:
        return ""

    fig, ax = plt.subplots(figsize=(8, 5))
    if len(param_cols) >= 2:
        x_key, y_key = param_cols[0], param_cols[1]
        pivot = scan_df.pivot_table(index=y_key, columns=x_key, values=metric)
        values = pivot.to_numpy(dtype=float)
        im = ax.imshow(values, cmap="RdYlGn", aspect="auto")
        ax.set_xticks(range(pivot.shape[1]))
        ax.set_xticklabels([str(v) for v in pivot.columns])
        ax.set_yticks(range(pivot.shape[0]))
        ax.set_yticklabels([str(v) for v in pivot.index])
        ax.set_xlabel(x_key)
        ax.set_ylabel(y_key)
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                if np.isfinite(values[i, j]):
                    ax.text(j, i, f"{values[i, j]:.2f}", ha="center", va="center", fontsize=8)
        fig.colorbar(im, ax=ax, label=metric)
        ax.set_title(_titled(label, f"参数扫描热力图（{metric}）"))
    else:
        key = param_cols[0]
        data = scan_df.sort_values(key)
        vals = data[metric].to_numpy(dtype=float)
        colors = plt.cm.RdYlGn(plt.Normalize(vals.min(), vals.max())(vals))
        ax.bar([str(v) for v in data[key]], vals, color=colors)
        ax.set_xlabel(key)
        ax.set_ylabel(metric)
        ax.set_title(_titled(label, f"参数扫描（{metric}）"))
        ax.grid(alpha=0.3, axis="y")

    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_health_neighborhood(
    scan_df, focus: dict, out_path, metric: str = "total_return", label: str = ""
) -> str:
    """参数邻域热力图：★ 标记当前参数位置（收益为百分比口径，红=涨绿=跌）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    param_cols = [c for c in scan_df.columns if c not in _METRIC_COLS and c != "symbol"]
    if not param_cols:
        return ""

    fig, ax = plt.subplots(figsize=(8, 5))
    if len(param_cols) >= 2:
        x_key, y_key = param_cols[0], param_cols[1]
        pivot = scan_df.pivot_table(index=y_key, columns=x_key, values=metric)
        values = pivot.to_numpy(dtype=float) * 100.0
        abs_vals = np.abs(values)
        vmax = float(np.nanmax(abs_vals)) if np.isfinite(abs_vals).any() else 1.0
        vmax = max(vmax, 1e-6)
        im = ax.imshow(values, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
        ax.set_xticks(range(pivot.shape[1]))
        ax.set_xticklabels([str(v) for v in pivot.columns])
        ax.set_yticks(range(pivot.shape[0]))
        ax.set_yticklabels([str(v) for v in pivot.index])
        ax.set_xlabel(x_key)
        ax.set_ylabel(y_key)
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                if np.isfinite(values[i, j]):
                    ax.text(j, i, f"{values[i, j]:.1f}", ha="center", va="center", fontsize=8)
        fx, fy = focus.get(x_key), focus.get(y_key)
        cols, rows = list(pivot.columns), list(pivot.index)
        if fx in cols and fy in rows:
            ax.scatter(
                [cols.index(fx)],
                [rows.index(fy)],
                marker="*",
                s=260,
                color="#1a237e",
                edgecolor="white",
                zorder=5,
                label="当前参数",
            )
            ax.legend(loc="upper right", fontsize=8)
        fig.colorbar(im, ax=ax, label=f"{metric}（%）", fraction=0.04, pad=0.02)
        ax.set_title(_titled(label, f"参数邻域热力图（{metric}，★=当前参数）"))
    else:
        key = param_cols[0]
        data = scan_df.sort_values(key)
        vals = data[metric].to_numpy(dtype=float) * 100.0
        center = focus.get(key)
        colors = ["#c62828" if str(v) == str(center) else "#90a4ae" for v in data[key]]
        ax.bar([str(v) for v in data[key]], vals, color=colors)
        ax.axhline(0, color="#9e9e9e", linewidth=0.8)
        ax.set_xlabel(key)
        ax.set_ylabel(f"{metric}（%）")
        ax.set_title(_titled(label, f"参数邻域（{metric}，红色=当前参数）"))
        ax.grid(alpha=0.3, axis="y")

    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_health_monte_carlo(mc, out_path, label: str = "") -> str:
    """蒙特卡洛（信号重排）分布直方图：红色虚线 = 实际策略收益。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(9, 5))
    vals = mc.returns.astype(float) * 100.0
    bins = min(30, max(10, mc.runs // 5))
    ax.hist(vals, bins=bins, color="#90a4ae", edgecolor="white", alpha=0.9)
    ax.axvline(0, color="#9e9e9e", linewidth=0.8)
    ax.axvline(mc.actual_return * 100.0, color="#c62828", linewidth=1.8, linestyle="--")
    ax.text(
        0.98,
        0.95,
        f"实际收益 {mc.actual_return * 100:.2f}%\n优于 {mc.percentile:.0f}% 的随机对照",
        transform=ax.transAxes,
        ha="right",
        va="top",
        color="#c62828",
        fontsize=10,
    )
    ax.set_xlabel("累计收益（%）")
    ax.set_ylabel("频数")
    ax.set_title(_titled(label, f"蒙特卡洛对照 · 信号重排 {mc.runs} 次"))
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_health_cost(cs, out_path, label: str = "") -> str:
    """成本敏感性：收益 vs 成本倍数，标注收益归零点。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(8, 5))
    x = [float(r["multiplier"]) for r in cs.rows]
    y = [float(r["total_return"]) * 100.0 for r in cs.rows]
    ax.plot(x, y, marker="o", color="#1976d2", linewidth=1.5)
    ax.axhline(0, color="#9e9e9e", linewidth=0.8)
    if cs.zero_multiplier is not None:
        z = cs.zero_multiplier
        ax.axvline(z, color="#c62828", linestyle="--", linewidth=1.2)
        ax.annotate(
            f"归零 ≈ ×{z:.2f}",
            xy=(z, 0),
            xytext=(8, 10),
            textcoords="offset points",
            color="#c62828",
            fontsize=10,
        )
    ax.set_xticks(x)
    ax.set_xticklabels([f"×{v:g}" for v in x])
    ax.set_xlabel("成本倍数（手续费 / 滑点等比放大）")
    ax.set_ylabel("累计收益（%）")
    ax.set_title(_titled(label, "成本敏感性 · 收益归零倍数"))
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)
