"""数据质量校验（v1.4）：缺口 / 异常值 / 新鲜度 / 一致性 → 报告 + 入库。

- 输入：清洗后的行情 DataFrame（date/open/high/low/close/volume）；
- 交易日历：akshare ``tool_trade_date_hist_sina`` → 缓存 ``data/raw/trade_dates.csv``；
  拉取失败时降级为「相邻间隔」启发式（gap_max_days / recent_days）；
- 输出：检查行 ``[{symbol, check, status, detail}]`` + Markdown 报告；
- 主连换月跳空属**已知口径**（非数据错误），跳变检查的明细中注明。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..utils.logger import get_logger
from . import store

log = get_logger("data.quality")

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_ERROR = "error"

_ORDER = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_ERROR: 2}
_ICON = {STATUS_PASS: "✅", STATUS_WARN: "⚠️", STATUS_ERROR: "❌"}


@dataclass
class QualityReport:
    """一次数据质量检查的完整结果。"""

    batch_id: str
    symbols: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_ERROR: 0}
        for r in self.rows:
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    def ok(self) -> bool:
        """无 error 即视为可用（warn 为提示）。"""
        return not any(r["status"] == STATUS_ERROR for r in self.rows)

    def worst_by_symbol(self) -> dict[str, str]:
        worst: dict[str, str] = {}
        for r in self.rows:
            cur = worst.get(r["symbol"])
            if cur is None or _ORDER[r["status"]] > _ORDER[cur]:
                worst[r["symbol"]] = r["status"]
        return worst


def _row(symbol: str, check: str, status: str, detail: str) -> dict:
    return {"symbol": symbol, "check": check, "status": status, "detail": detail}


def load_trade_dates(raw_dir: str | Path) -> set[str] | None:
    """交易日历（缓存优先；拉取失败返回 None → 调用方降级为间隔启发式）。"""
    path = Path(raw_dir) / "trade_dates.csv"
    if path.exists():
        df = pd.read_csv(path)
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        return {str(v)[:10] for v in df[col]}
    try:
        import akshare as ak  # 延迟导入：离线场景不拖慢启动

        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        df = pd.DataFrame({col: pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d")})
        store.save_csv(df, path)
        log.info("交易日历已缓存：%s（%d 个交易日）", path, len(df))
        return {str(v) for v in df[col]}
    except Exception as exc:  # noqa: BLE001 — 日历不可得时降级
        log.warning("交易日历拉取失败（降级为间隔启发式）：%s", exc)
        return None


def check_symbol(
    df: pd.DataFrame,
    symbol: str,
    *,
    trade_dates: set[str] | None = None,
    gap_max_days: int = 10,
    jump_pct_warn: float = 0.15,
    recent_days: int = 10,
) -> list[dict]:
    """单品种四类检查（结构 / 跳变 / 缺口 / 新鲜度）+ 汇总行。"""
    rows: list[dict] = []
    if df is None or len(df) == 0:
        return [_row(symbol, "structure", STATUS_ERROR, "数据为空")]

    eps = 1e-6
    o, h, low, c, vol = df["open"], df["high"], df["low"], df["close"], df["volume"]
    bad = (
        (low > o + eps)
        | (low > c + eps)
        | (h < o - eps)
        | (h < c - eps)
        | (low <= 0)
        | (c <= 0)
    )
    n_bad = int(bad.sum())
    if n_bad:
        first = str(df.loc[bad, "date"].iloc[0])[:10]
        rows.append(
            _row(symbol, "structure", STATUS_ERROR, f"OHLC 逻辑异常 {n_bad} 处（首例 {first}）")
        )
    n_neg = int((vol < 0).sum())
    if n_neg:
        rows.append(_row(symbol, "structure", STATUS_ERROR, f"成交量为负 {n_neg} 处"))

    dates = df["date"].dt.strftime("%Y-%m-%d")
    start_d, end_d = str(dates.iloc[0])[:10], str(dates.iloc[-1])[:10]

    rets = c.pct_change().abs()
    jump_pos = rets[rets > jump_pct_warn].index
    if len(jump_pos):
        samples = ", ".join(str(dates.loc[i])[:10] for i in jump_pos[:5])
        rows.append(
            _row(
                symbol,
                "jump",
                STATUS_WARN,
                f"单日跳变（>{jump_pct_warn:.0%}）{len(jump_pos)} 处：{samples}；"
                "主连换月跳空属已知口径，非数据错误",
            )
        )

    if trade_dates:
        have = set(dates)
        should = {d for d in trade_dates if start_d <= d <= end_d}
        missing = sorted(should - have)
        if missing:
            samples = ", ".join(missing[:5])
            more = "…" if len(missing) > 5 else ""
            detail = f"区间内缺失 {len(missing)} 个交易日（{samples}{more}）"
            rows.append(_row(symbol, "gap", STATUS_WARN, detail))
        today = dt.date.today().strftime("%Y-%m-%d")
        past = sorted(d for d in trade_dates if d <= today)
        lag_days = [d for d in past if d > end_d]
        if len(lag_days) > 2:
            shown = "、".join(lag_days[:5]) + ("…" if len(lag_days) > 5 else "")
            rows.append(
                _row(
                    symbol,
                    "freshness",
                    STATUS_WARN,
                    f"数据滞后 {len(lag_days)} 个交易日（最新 {end_d}；缺失 {shown}）",
                )
            )
    else:
        dts = df["date"].reset_index(drop=True)
        gaps = dts.diff().dt.days.dropna()
        big = gaps[gaps > gap_max_days]
        if len(big):
            samples = ", ".join(str(dts[i])[:10] for i in big.index[:5])
            detail = f"相邻间隔 > {gap_max_days} 天共 {len(big)} 处（{samples}）"
            rows.append(_row(symbol, "gap", STATUS_WARN, detail))
        last = dts.iloc[-1]
        lag_days = (pd.Timestamp.today().normalize() - last).days
        if lag_days > recent_days * 2:
            rows.append(_row(symbol, "freshness", STATUS_WARN, f"最新数据距今 {lag_days} 天"))

    rows.append(_row(symbol, "summary", STATUS_PASS, f"{len(df)} 行 | {start_d} ~ {end_d}"))
    return rows


def check_dataset(
    items: list[tuple[str, pd.DataFrame]],
    *,
    trade_dates: set[str] | None = None,
    gap_max_days: int = 10,
    jump_pct_warn: float = 0.15,
    recent_days: int = 10,
    align_tolerance_days: int = 5,
) -> QualityReport:
    """多品种批量检查（含跨品种新鲜度对齐）。"""
    batch_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = QualityReport(batch_id=batch_id, symbols=[s for s, _ in items])
    ends: dict[str, pd.Timestamp] = {}
    for symbol, df in items:
        report.rows.extend(
            check_symbol(
                df,
                symbol,
                trade_dates=trade_dates,
                gap_max_days=gap_max_days,
                jump_pct_warn=jump_pct_warn,
                recent_days=recent_days,
            )
        )
        if df is not None and len(df):
            ends[symbol] = pd.Timestamp(df["date"].iloc[-1])
    if len(ends) > 1:
        ref = max(ends.values())
        for symbol, last in ends.items():
            lag = (ref - last).days
            if lag > align_tolerance_days:
                report.rows.append(
                    _row(
                        symbol,
                        "align",
                        STATUS_WARN,
                        f"最新数据 {last:%Y-%m-%d}，落后于组合最新 {ref:%Y-%m-%d}（{lag} 天）",
                    )
                )
    return report


def render_markdown(report: QualityReport, labels: dict | None = None) -> str:
    """质量报告（Markdown）：先总览后明细。"""
    labels = labels or {}
    counts = report.counts()
    lines = [
        "# 数据质量报告（v1.4）",
        "",
        f"- **批次**：`{report.batch_id}`",
        f"- **检查品种**：{len(report.symbols)} 个",
        (
            f"- **总体**：✅ {counts[STATUS_PASS]} · ⚠️ {counts[STATUS_WARN]}"
            f" · ❌ {counts[STATUS_ERROR]}"
        ),
        "",
        "## 品种汇总",
        "",
        "| 品种 | 检查结论 | 摘要 |",
        "|---|---|---|",
    ]
    worst = report.worst_by_symbol()
    summaries = {r["symbol"]: r["detail"] for r in report.rows if r["check"] == "summary"}
    for symbol in report.symbols:
        status = worst.get(symbol, STATUS_PASS)
        lines.append(
            f"| {labels.get(symbol, symbol)} | {_ICON[status]} {status} "
            f"| {summaries.get(symbol, '—')} |"
        )
    issues = [r for r in report.rows if r["status"] != STATUS_PASS]
    lines += ["", "## 告警与错误明细", ""]
    if issues:
        for r in issues:
            name = labels.get(r["symbol"], r["symbol"])
            lines.append(f"- {_ICON[r['status']]} **{name} / {r['check']}**：{r['detail']}")
    else:
        lines.append("- 无：全部检查通过。")
    if issues:
        lines += [
            "",
            "> **排查提示（常见原因与处理）**",
            "> - **数据滞后（freshness）**：最常见原因是行情缓存未更新——回测与检查只读已有缓存、"
            "不会自动追新；拉取区间上限（`config.data.end_date`）固定在过去时同样如此。"
            "处理：刷新数据缓存后重新检查。",
            "> - **缺口（gap）**：长假前后的较大间隔属正常；连续多日缺失需核查数据源。",
            "> - **跳变（jump）**：主连换月跳空属已知口径（非数据错误）；同日异常需核查原始数据。",
            "> - **结构异常（structure）**：请核查原始缓存与数据源。",
            "> - **对齐（align）**：部分品种缓存未同步更新，刷新数据后复检。",
        ]
    lines += [
        "",
        "> 说明：阈值见 `config.quality`；交易日历缓存于 `data/raw/trade_dates.csv`；"
        "主连拼接序列的换月跳空为已知口径（非数据错误）。",
        "",
    ]
    return "\n".join(lines)
