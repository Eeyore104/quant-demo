"""报告汇总：指标 + 图片路径 → 文本报告（并打印到控制台）。"""

from pathlib import Path

from ..utils.logger import get_logger

log = get_logger("report")


def _fmt_num(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def build_report_text(m, header_lines: list[str], figure_paths: dict) -> str:
    """拼装文本报告。"""
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
    return "\n".join(lines)


def save_report(text: str, out_path) -> str:
    """保存报告文本。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)
