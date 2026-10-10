"""组合报告：汇总统计 / 逐品种表 / 相关性矩阵 / 约束事件 / 口径说明 + 明细 CSV。"""

from pathlib import Path

import pandas as pd

from . import metrics as metrics_mod
from .report import _pad


def symbol_summary(result, labels: dict[str, str], initial_capital: float) -> pd.DataFrame:
    """逐品种汇总：交易次数 / 胜率 / 累计盈亏 / 贡献占比 / 回撤* / 夏普*。

    * 回撤与夏普基于各品种**逐日盈亏曲线**（近似口径，非独立账户）。
    """
    eq = result.equity_df
    total_pnl = sum(result.account.symbol_pnl(s) for s in result.symbols)
    rows: list[dict] = []
    for s in result.symbols:
        series = eq[f"pnl_{s}"].astype(float)
        pnl = float(series.iloc[-1]) if len(series) else 0.0
        mdd = float((series - series.cummax()).min()) if len(series) else 0.0
        sharpe = metrics_mod.sharpe_ratio(series.diff().dropna())
        closes = [t for t in result.trades if t.symbol == s and t.action == "CLOSE"]
        wins = sum(1 for t in closes if t.pnl > 0)
        rows.append(
            {
                "symbol": s,
                "name": labels.get(s, s),
                "trades": len(closes),
                "win_rate": (wins / len(closes)) if closes else 0.0,
                "pnl": pnl,
                "contribution": (pnl / total_pnl) if abs(total_pnl) > 1e-9 else float("nan"),
                "max_drawdown": mdd,
                "sharpe": sharpe,
                "commission": result.account.state(s).commission_paid,
            }
        )
    return pd.DataFrame(rows)


def correlation_matrix(result) -> pd.DataFrame:
    """逐品种「策略日收益」（逐日盈亏变化）的相关性矩阵。"""
    cols = [f"pnl_{s}" for s in result.symbols]
    daily = result.equity_df[cols].astype(float).diff().dropna()
    daily.columns = [c.removeprefix("pnl_") for c in daily.columns]
    return daily.corr()


def build_portfolio_report(
    result,
    *,
    labels: dict[str, str],
    initial_capital: float,
    start_date: str,
    end_date: str,
    sizing_desc: str,
    risk_enabled: bool = False,
) -> str:
    """渲染组合回测报告（文本）。``risk_enabled`` 时标注风控口径（v1.3）。"""
    eq = result.equity_df
    m = metrics_mod.analyze(eq, result.trades, initial_capital)

    margin_mean = float(eq["margin_used"].mean())
    margin_peak = float(eq["margin_used"].max())
    util = (eq["margin_used"] / eq["equity"].replace(0, pd.NA)).max()
    util_peak = float(util) if pd.notna(util) else 0.0

    pool_line = " / ".join(labels.get(s, s).split("（")[0] for s in result.symbols)
    risk_line = (
        "已启用（止损 / 熔断 / 敞口 / 停板 · 详见「风险与压力测试」）"
        if risk_enabled
        else "未启用（v1.2 口径）"
    )

    lines = [
        "=================== 组合回测报告 ===================",
        f"  资金       : {initial_capital:,.0f} 元（共享资金池）",
        f"  区间       : {start_date} ~ {end_date}",
        f"  品种池     : {len(result.symbols)} 个 · {pool_line}",
        f"  头寸规模   : {sizing_desc}",
        f"  风控体系   : {risk_line}",
        "  成交约定   : T 日收盘信号 → T+1 开盘价 ± 1 跳滑点",
        "---------------------------------------------------",
        "  【组合汇总】",
        f"  累计收益率 : {m.total_return:>9.2%}",
        f"  年化收益率 : {m.annual_return:>9.2%}",
        f"  最大回撤   : {m.max_drawdown:>9.2%}",
        f"  夏普比率   : {m.sharpe:>9.2f}",
        f"  交易次数   : {m.trade_count:>9d}",
        f"  累计手续费 : {m.total_commission:>9.2f} 元",
        f"  累计滑点   : {m.total_slippage:>9.2f} 元",
        f"  保证金占用 : 均值 {margin_mean:,.0f} 元 / 峰值 {margin_peak:,.0f} 元"
        f"（峰值利用率 {util_peak:.1%}）",
        f"  约束事件   : 拒绝开仓 {len(result.events)} 次",
        "---------------------------------------------------",
        "  【逐品种汇总】",
    ]

    sym_df = symbol_summary(result, labels, initial_capital)
    lines.append(
        _pad("品种", 20)
        + _pad("交易", 6)
        + _pad("胜率", 8)
        + _pad("累计盈亏", 13)
        + _pad("贡献占比", 10)
        + _pad("回撤*", 13)
        + "夏普*"
    )
    for _, r in sym_df.iterrows():
        share = "—" if pd.isna(r["contribution"]) else f"{r['contribution']:>7.1%}"
        lines.append(
            _pad(str(r["name"]), 20)
            + _pad(str(int(r["trades"])), 6)
            + _pad(f"{r['win_rate']:>6.1%}", 8)
            + _pad(f"{r['pnl']:>11,.0f}", 13)
            + _pad(share, 10)
            + _pad(f"{r['max_drawdown']:>11,.0f}", 13)
            + f"{r['sharpe']:>6.2f}"
        )
    lines.append("  * 回撤 / 夏普基于各品种逐日盈亏曲线（近似口径，非独立账户）")

    lines.append("---------------------------------------------------")
    lines.append("  【相关性矩阵（逐品种策略日收益）】")
    corr = correlation_matrix(result)
    head = _pad("", 10) + "".join(_pad(c, 10) for c in corr.columns)
    lines.append(head)
    for s in corr.index:
        row = _pad(s, 10) + "".join(_pad(f"{corr.loc[s, c]:>8.2f}", 10) for c in corr.columns)
        lines.append(row)

    lines.append("---------------------------------------------------")
    lines.append(f"  【约束事件（共 {len(result.events)} 条，前 10 条）】")
    if result.events:
        for ev in result.events[:10]:
            lines.append("  - " + ev.describe())
        if len(result.events) > 10:
            lines.append(f"  … 其余 {len(result.events) - 10} 条见 portfolio_events.csv")
    else:
        lines.append("  无")

    lines += [
        "---------------------------------------------------",
        "  【口径说明】",
        "  - 保证金率 / 手续费为近似口径（2026-10-10 akshare 核对，交易所口径；实盘通常上浮）",
        "  - 保证金占用每日按收盘价重估（近似盯市口径）；可用资金 = 权益 - 保证金占用",
        (
            "  - 平仓永不拒绝（停板除外）；开仓资金不足时按可负担手数降级执行（v1.3）"
            if risk_enabled
            else "  - 平仓永不拒绝；开仓保证金不足时整笔拒绝并记录事件"
        ),
        "    （同日多品种竞争资金时，按品种池配置顺序先到先得）",
        "  - 回测使用主力连续（拼接）序列，换月点存在跳空，损益含拼接噪声（已登记为数据口径风险）",
        "  - 手续费按手、滑点 1 跳（逐品种参数化），口径与单品种回测一致",
        "===================================================",
    ]
    return "\n".join(lines)


def save_portfolio_artifacts(
    result, *, labels: dict[str, str], initial_capital: float, out_dir
) -> dict[str, str]:
    """保存明细 CSV：逐日 / 逐品种 / 约束事件。返回 {名称: 路径}。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    daily_path = out / "portfolio_daily.csv"
    result.equity_df.to_csv(daily_path, index=False, encoding="utf-8-sig")

    sym_path = out / "portfolio_symbols.csv"
    symbol_summary(result, labels, initial_capital).to_csv(
        sym_path, index=False, encoding="utf-8-sig"
    )

    ev_path = out / "portfolio_events.csv"
    events_df = pd.DataFrame(
        [
            {
                "date": e.date,
                "symbol": e.symbol,
                "needed_margin": round(e.needed_margin, 2),
                "available": round(e.available, 2),
            }
            for e in result.events
        ],
        columns=["date", "symbol", "needed_margin", "available"],  # 空事件也保留表头
    )
    events_df.to_csv(ev_path, index=False, encoding="utf-8-sig")

    return {"逐日明细": str(daily_path), "逐品种汇总": str(sym_path), "约束事件": str(ev_path)}
