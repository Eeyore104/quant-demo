"""报告汇总：指标 + 图片路径 → 文本报告（并打印到控制台）。

v1.0 增量：新增 ``build_oos_table`` —— 样本外验证的「训练段 / 测试段」对比表渲染。
"""

import unicodedata
from pathlib import Path

import pandas as pd

from ..utils.logger import get_logger

log = get_logger("report")


def _fmt_num(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _display_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（中文等宽字符按 2 计）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    return text + " " * max(0, width - _display_width(text))


def _fmt_range(rng: tuple) -> str:
    """把 (起, 止) 时间戳格式化为 ``YYYY-MM-DD ~ YYYY-MM-DD``。"""
    start, end = rng
    s = "-" if start is None else pd.Timestamp(start).strftime("%Y-%m-%d")
    e = "-" if end is None else pd.Timestamp(end).strftime("%Y-%m-%d")
    return f"{s} ~ {e}"


def build_oos_table(oos, label: str = "") -> str:
    """渲染样本外验证对比表（明确标注：训练段=样本内、测试段=样本外）。"""
    tm = oos.train_metrics
    sm = oos.test_metrics
    params_desc = ", ".join(f"{k}={v}" for k, v in oos.best_params.items()) or "-"

    header = (
        _pad("阶段", 14) + _pad("区间", 28) + _pad("累计收益", 12) + _pad("最大回撤", 12) + "夏普"
    )
    row_train = (
        _pad("样本内(训练)", 14)
        + _pad(_fmt_range(oos.train_range), 28)
        + _pad(f"{tm.total_return:>8.2%}", 12)
        + _pad(f"{tm.max_drawdown:>8.2%}", 12)
        + f"{tm.sharpe:>6.2f}   ← 用于择优"
    )
    row_test = (
        _pad("样本外(测试)", 14)
        + _pad(_fmt_range(oos.test_range), 28)
        + _pad(f"{sm.total_return:>8.2%}", 12)
        + _pad(f"{sm.max_drawdown:>8.2%}", 12)
        + f"{sm.sharpe:>6.2f}   ← 检验"
    )
    title = "样本外验证（训练段择优 → 测试段检验）"
    if label:
        title = f"样本外验证 · {label}（训练段择优 → 测试段检验）"
    return "\n".join(
        [
            f"============= {title} =============",
            header,
            row_train,
            row_test,
            f"择优参数     {params_desc}",
            "===============================================================",
        ]
    )


def build_report_text(
    m, header_lines: list[str], figure_paths: dict, extra_sections: list[str] | None = None
) -> str:
    """拼装文本报告；``extra_sections`` 用于追加（如样本外验证对比表）。"""
    lines = ["=================== 绩效报告 ==================="]
    lines += [f"  {h}" for h in header_lines]
    lines += [
        f"  累计收益率 : {m.total_return:>9.2%}",
        f"  年化收益率 : {m.annual_return:>9.2%}",
        f"  最大回撤   : {m.max_drawdown:>9.2%}",
        f"  夏普比率   : {m.sharpe:>9.2f}",
        f"  胜率       : {m.win_rate:>9.2%}",
        f"  盈亏比     : {_fmt_num(m.profit_factor):>9}",
        f"  交易次数   : {m.trade_count:>9d}",
        f"  累计手续费 : {m.total_commission:>9.2f} 元",
        f"  累计滑点   : {m.total_slippage:>9.2f} 元",
        "================================================",
    ]
    for name, path in figure_paths.items():
        lines.append(f"  {name}: {path}")
    if extra_sections:
        lines.append("")
        lines.extend(extra_sections)
    return "\n".join(lines)


def save_report(text: str, out_path) -> str:
    """保存报告文本。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)
