"""「风险与压力测试」章节渲染（v1.3）+ 风控明细 CSV 导出。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

from .report import _pad


def _fmt_pct(v, nd=2) -> str:
    if v is None or v != v:  # NaN
        return "—"
    return f"{v:.{nd}%}"


def _fmt_money(v) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:,.0f}"


def _counts_line(counts: Counter, names: list[tuple[str, str]]) -> str:
    return " · ".join(f"{label} {counts.get(kind, 0)}" for kind, label in names)


def _fuse_desc(risk_cfg: dict) -> str:
    fu = risk_cfg.get("fuse") or {}
    dl = fu.get("daily_loss") or {}
    dd = fu.get("drawdown") or {}
    ls = fu.get("losing_streak") or {}
    lq = fu.get("liquidate") or {}
    parts = [
        f"单日亏损 -{float(dl.get('threshold', 0.03)):.0%}（冷却 {dl.get('cooldown_days', 3)} 日）",
        f"回撤 -{float(dd.get('threshold', 0.15)):.0%}（新开仓减半）",
        f"连亏 {ls.get('count', 5)} 笔（冷却 {ls.get('cooldown_days', 10)} 日）",
        f"全平 {'开' if lq.get('enabled', False) else '关'}",
    ]
    return " · ".join(parts)


def _stops_desc(risk_cfg: dict) -> str:
    s = risk_cfg.get("stops") or {}
    f = s.get("fixed") or {}
    t = s.get("trailing") or {}
    tp = s.get("take_profit") or {}
    fixed = f"固定 {f.get('atr_mult', 2.0):g}×ATR" if f.get("enabled", True) else "固定 关"
    trail = f"移动 {t.get('atr_mult', 3.0):g}×ATR" if t.get("enabled", True) else "移动 关"
    take = f"止盈 {float(tp.get('pct', 0.04)):.0%}" if tp.get("enabled", False) else "止盈 关"
    return f"{fixed} · {trail} · {take}"


def build_risk_section(
    result,
    *,
    risk_cfg: dict,
    stress=None,
    risk_daily: pd.DataFrame | None = None,
    compare: dict | None = None,
    initial_capital: float,
) -> str:
    """渲染「风险与压力测试」章节（含风控事件、指标、压力测试、开/关对比）。"""
    events = result.risk_events
    counts = Counter(e.kind for e in events)
    lines: list[str] = ["============= 风险与压力测试（v1.3）============="]

    # ---------- 配置摘要 ----------
    ex = risk_cfg.get("exposure") or {}
    pl = risk_cfg.get("price_limit") or {}
    lq = risk_cfg.get("liquidity") or {}
    e = risk_cfg.get("execution") or {}
    r = risk_cfg.get("reentry") or {}
    lines += [
        "  【风控配置摘要】",
        f"    止损止盈 ：{_stops_desc(risk_cfg)}",
        f"    熔断     ：{_fuse_desc(risk_cfg)}",
        f"    敞口上限 ：单品种 {float(ex.get('max_symbol_pct', 1.5)):.0%} / "
        f"组合 {float(ex.get('max_total_pct', 4.0)):.0%}（名义，开仓事前检查）",
        f"    涨跌停   ：{'方向感知（跌停拒卖 / 涨停拒买）' if pl.get('enabled', True) else '关'}"
        f" · 参与率 ≤ {float(lq.get('max_volume_share', 0.05)):.0%}",
        "    执行口径 ："
        + ("盘中触价（跳空按开盘价）" if e.get("intrabar", True) else "收盘判定（次日开盘）")
        + " · "
        + ("资金不足按可负担手数降级" if e.get("margin_degrade", True) else "资金不足整笔拒绝")
        + f" · 重入 {r.get('mode', 'signal_reset')}",
    ]

    # ---------- 风控事件汇总 ----------
    lines.append(f"  【风控事件汇总】（共 {len(events)} 条）")
    lines.append(
        "    止损/止盈："
        + _counts_line(
            counts,
            [
                ("stop_fixed", "固定止损"),
                ("stop_trailing", "移动止损"),
                ("take_profit", "止盈"),
                ("stop_refused", "停板拒单"),
            ],
        )
    )
    lines.append(
        "    熔断     ："
        + _counts_line(
            counts,
            [
                ("fuse_pause", "暂停"),
                ("fuse_degrade", "降规模"),
                ("fuse_liquidate", "全平"),
                ("fuse_resume", "恢复"),
            ],
        )
    )
    lines.append(
        "    约束     ："
        + _counts_line(
            counts,
            [
                ("margin_degrade", "降级开仓"),
                ("exposure_trim", "敞口截断"),
                ("exposure_refuse", "敞口拒绝"),
                ("limit_refuse", "停板拒单(信号)"),
                ("participation_trim", "参与率截断"),
            ],
        )
    )
    if events:
        lines.append("    前 5 条明细：")
        for ev in events[:5]:
            lines.append("      - " + ev.describe())

    # ---------- 敞口与杠杆 ----------
    if risk_daily is not None and len(risk_daily):
        rd = risk_daily
        lines.append("  【敞口与杠杆】")
        lev = rd["leverage"].dropna() if "leverage" in rd else pd.Series(dtype=float)
        if len(lev):
            d_peak = str(rd["date"].iloc[lev.idxmax()])[:10]
            lines.append(
                f"    杠杆（名义/权益）：均值 {lev.mean():.2f} · 峰值 {lev.max():.2f}（{d_peak}）"
            )
        mu = rd["margin_util"].dropna() if "margin_util" in rd else pd.Series(dtype=float)
        if len(mu):
            lines.append(f"    保证金利用率峰值：{mu.max():.1%}")
        warn_days = int((rd.get("exposure_warn", pd.Series(dtype=float)) > 0).sum())
        lines.append(f"    敞口超限记录（仅记录、不强制减仓）：{warn_days} 天")
        tss = rd["top_symbol_share"].dropna()
        tsec = rd["top_sector_share"].dropna()
        if len(tss) and len(tsec):
            lines.append(
                f"    集中度（逐日名义占比）：单品种 均值 {tss.mean():.0%} / 峰值 {tss.max():.0%}"
                f" · 板块 均值 {tsec.mean():.0%} / 峰值 {tsec.max():.0%}"
            )

        # ---------- 尾部风险 ----------
        lines.append("  【尾部风险（历史模拟）】")
        eq = rd["equity"].astype(float)
        rets = eq.pct_change().dropna()
        if len(rets):
            d_worst = str(rd["date"].iloc[rets.idxmin()])[:10]
            lines.append(
                f"    最差单日：{rets.min():.2%}（{d_worst}）"
                f"· 样本期最大回撤：{rd['drawdown'].min():.2%}"
            )
        var95 = rd["var95"].dropna() if "var95" in rd else pd.Series(dtype=float)
        var99 = rd["var99"].dropna() if "var99" in rd else pd.Series(dtype=float)
        cvar95 = rd["cvar95"].dropna() if "cvar95" in rd else pd.Series(dtype=float)
        if len(var95):
            lines.append(
                f"    VaR（均值）：95% {var95.mean():.2%} · 99% {var99.mean():.2%}"
                f" · CVaR 95% {cvar95.mean():.2%}"
            )
        else:
            lines.append("    VaR/CVaR：样本不足，未计算（需 ≥30 个收益日）")

        # ---------- 相关性预警 ----------
        cm = rd["corr_mean"].dropna() if "corr_mean" in rd else pd.Series(dtype=float)
        warn_days_corr = int(rd["corr_warn"].sum()) if "corr_warn" in rd else 0
        if len(cm):
            d_corr = str(rd["date"].iloc[cm.idxmax()])[:10]
            lines.append("  【相关性预警】")
            lines.append(
                f"    滚动平均相关：峰值 {cm.max():.2f}（{d_corr}）· 预警天数 {warn_days_corr}"
                "（阈值见 config.risk.metrics.corr_warn）"
            )

    # ---------- 压力测试 ----------
    if stress is not None:
        lines.append("  【压力测试】")
        lines.append(
            _pad("    情景", 42) + _pad("区间", 26) + _pad("当时实际", 12) + "重估(1手/品种)"
        )
        for s in stress.scenarios:
            actual = _fmt_money(s.actual_loss) if s.actual_loss is not None else "—"
            replay = _fmt_money(s.replay_loss) if s.replay_loss is not None else "—"
            lines.append(_pad("    " + s.name, 42) + _pad(s.window, 26) + _pad(actual, 12) + replay)
            if s.note:
                lines.append("      └ " + s.note)
        if stress.mc:
            m = stress.mc
            lines.append(
                f"    蒙特卡洛回撤分布（bootstrap × {m.get('runs', 0)}）："
                f"P50 {m['p50']:.2%} · P90 {m['p90']:.2%} · P95 {m['p95']:.2%}"
                f" · P99 {m['p99']:.2%} · 最差 {m['worst']:.2%}"
            )

    # ---------- 风控开 / 关对比 ----------
    if compare:
        lines.append("  【风控开 / 关对比】")
        bare, risk_r = compare["bare"], compare["risk"]
        lines.append(
            _pad("    指标", 26) + _pad("裸奔（fixed 1 手）", 22) + "风控全开（风险预算 + 止损）"
        )

        def row(name, a, b):
            return _pad("    " + name, 26) + _pad(a, 22) + b

        b_tr, r_tr = bare["total_return"], risk_r["total_return"]
        lines.append(row("累计收益", f"{b_tr:.2%}", f"{r_tr:.2%}"))
        lines.append(
            row("最大回撤", f"{bare['max_drawdown']:.2%}", f"{risk_r['max_drawdown']:.2%}")
        )
        lines.append(row("夏普比率", f"{bare['sharpe']:.2f}", f"{risk_r['sharpe']:.2f}"))
        lines.append(row("最差单日", f"{bare['worst_day']:.2%}", f"{risk_r['worst_day']:.2%}"))
        lines.append(row("年化波动率", f"{bare['volatility']:.2%}", f"{risk_r['volatility']:.2%}"))
        detail = f"止损 {risk_r['stop_events']} · 熔断 {risk_r['fuse_events']}"
        lines.append(
            row(
                "交易次数 / 事件",
                f"{bare['trade_count']}",
                f"{risk_r['trade_count']}（{detail}）",
            )
        )

    # ---------- 口径说明 ----------
    lines += [
        "  【口径说明】",
        "  - 止损止盈为盘中触价近似：跳空穿过按开盘价、盘中触及按触发价成交（新仓次日生效）",
        "  - 熔断为组合级：每日收盘判定 → 次日开盘生效；阈值为建议默认值（见 config.risk.fuse）",
        "  - 涨跌停为日线近似（无结算价）：按当根 bar 涨跌幅判定，方向感知拒单；参与率为容量设施",
        "  - VaR/CVaR 为历史模拟法（滚动窗口），对极端行情有滞后；只使用截至当日历史",
        "  - 压力测试为重估近似：1 手/品种、多头口径、σ 取近 60 日；蒙特卡洛为 iid bootstrap",
        "  - 对比实验同时变更「头寸方法」与「风控机制」，效果构成不作独立归因",
        "  - 回测结果不等于实盘表现；主连拼接跳空与模拟撮合差异均已登记为口径风险",
        "====================================================",
    ]
    return "\n".join(lines)


def save_risk_artifacts(
    result,
    stress,
    risk_daily: pd.DataFrame | None,
    compare: dict | None,
    *,
    out_dir,
) -> dict[str, str]:
    """保存风控明细 CSV：事件 / 逐日指标 / 压力测试 / 开-关对比。返回 {名称: 路径}。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    ev_path = out / "risk_events.csv"
    ev_df = pd.DataFrame(
        [
            {
                "date": str(e.date)[:10],
                "kind": e.kind,
                "symbol": e.symbol,
                "detail": e.detail,
            }
            for e in result.risk_events
        ],
        columns=["date", "kind", "symbol", "detail"],  # 空事件也保留表头
    )
    ev_df.to_csv(ev_path, index=False, encoding="utf-8-sig")
    paths["风控事件"] = str(ev_path)

    if risk_daily is not None:
        rd_path = out / "risk_daily.csv"
        rd = risk_daily.copy()
        rd["date"] = rd["date"].astype(str).str.slice(0, 10)
        rd.to_csv(rd_path, index=False, encoding="utf-8-sig")
        paths["逐日风险指标"] = str(rd_path)

    if stress is not None:
        st_path = out / "stress_results.csv"
        cols = [
            "category",
            "scenario",
            "window",
            "actual_loss",
            "actual_pct",
            "replay_loss_1lot",
            "replay_pct_1lot",
            "note",
        ]
        pd.DataFrame(stress.as_rows(), columns=cols).to_csv(
            st_path, index=False, encoding="utf-8-sig"
        )
        paths["压力测试"] = str(st_path)

    if compare:
        cmp_path = out / "risk_compare.csv"
        bare, risk_r = compare["bare"], compare["risk"]
        rows = [
            {"metric": k, "bare_fixed_1lot": bare.get(k), "risk_full": risk_r.get(k)}
            for k in (
                "total_return",
                "max_drawdown",
                "sharpe",
                "worst_day",
                "volatility",
                "trade_count",
                "stop_events",
                "fuse_events",
            )
        ]
        pd.DataFrame(rows).to_csv(cmp_path, index=False, encoding="utf-8-sig")
        paths["风控对比"] = str(cmp_path)

    return paths
