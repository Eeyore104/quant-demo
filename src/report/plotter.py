"""图表绘制：matplotlib → PNG（Windows 中文字体设置）。"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无需 GUI，直接出图
import matplotlib.pyplot as plt

# Windows 中文字体，防止中文乱码
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False


def plot_equity(equity_df, out_path) -> str:
    """权益曲线图。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(equity_df["date"], equity_df["equity"], color="#1976d2", linewidth=1.5, label="账户权益")
    ax.axhline(
        y=equity_df["equity"].iloc[0], color="#9e9e9e", linestyle="--", linewidth=1, label="初始资金"
    )
    ax.set_title("权益曲线")
    ax.set_ylabel("权益（元）")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def plot_signals(df, trades, out_path) -> str:
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

    ax.set_title("价格与买卖点")
    ax.set_ylabel("价格（元/吨）")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)
