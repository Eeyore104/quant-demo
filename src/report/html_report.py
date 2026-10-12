"""HTML 报告（v1.4）：每次回测一份自包含报告页（可发送、可存档）。

- 单品种：``render_backtest_html(...)``；组合：``render_portfolio_html(...)``；
- 图片 base64 内嵌 → 单文件自包含（断网可看、双击即开）；
- 完整文本报告（体检 / 样本外 / 风控章节）以「完整记录」折叠区原样收录（口径与终端一致）；
- **红涨绿跌**（中国习惯）；零新增依赖（字符串模板 + 标准库）。
"""

from __future__ import annotations

import base64
import datetime as dt
import html as html_mod
from pathlib import Path

_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f5f6f7; color: #1f2328;
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; }
header { background: #fff; border-bottom: 1px solid #e3e5e8; padding: 18px 24px; }
header h1 { margin: 0 0 6px; font-size: 19px; font-weight: 500; }
header .meta { color: #8a8f98; font-size: 13px; line-height: 1.7; }
main { max-width: 1080px; margin: 0 auto; padding: 18px 20px 40px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; }
.card { flex: 1 1 140px; background: #fff; border: 1px solid #e3e5e8;
  border-radius: 10px; padding: 12px 14px; }
.card .k { color: #8a8f98; font-size: 12px; }
.card .v { font-size: 20px; margin-top: 4px; }
.v.up { color: #d64545; } .v.down { color: #2e9e5b; }
.badge { display: inline-block; margin-left: 8px; padding: 2px 10px;
  border-radius: 999px; font-size: 12px; vertical-align: 2px; }
.badge.pass { background: #e8f6ee; color: #1d7a46; }
.badge.warn { background: #fdf3dd; color: #9a6b00; }
.badge.fail { background: #fdeaea; color: #b03030; }
h2 { font-size: 15px; font-weight: 500; margin: 26px 0 10px;
  padding-left: 10px; border-left: 4px solid #1976d2; }
figure.fig { margin: 0 0 16px; background: #fff; border: 1px solid #e3e5e8;
  border-radius: 10px; overflow: hidden; }
figure.fig figcaption { padding: 8px 14px; font-size: 13px;
  border-bottom: 1px solid #eef0f2; color: #3d4450; }
figure.fig img { width: 100%; height: auto; display: block; }
table.cmp { border-collapse: collapse; width: 100%; background: #fff;
  border: 1px solid #e3e5e8; border-radius: 10px; overflow: hidden; }
table.cmp th, table.cmp td { padding: 8px 14px; font-size: 13px;
  border-bottom: 1px solid #eef0f2; text-align: left; }
table.cmp th { color: #8a8f98; font-weight: 500; }
details { background: #fff; border: 1px solid #e3e5e8; border-radius: 10px;
  padding: 10px 14px; }
summary { cursor: pointer; font-size: 13px; color: #185fa5; }
pre { white-space: pre-wrap; font-family: Consolas, "Courier New", monospace;
  font-size: 12.5px; line-height: 1.55; color: #24292f; }
footer { max-width: 1080px; margin: 0 auto; padding: 4px 20px 36px;
  color: #9aa0a8; font-size: 12px; line-height: 1.7; }
"""

_HEAD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>{title}{badge}</h1>
  <div class="meta">{meta}</div>
</header>
<main>
"""

_TAIL = """<h2>完整文本报告</h2>
<details>
  <summary>展开 / 折叠（与终端输出一致：体检表 · 样本外 · 风控章节全文）</summary>
  <pre>{text}</pre>
</details>
</main>
<footer>本页由 quant-demo 自动生成（{ts}）· 图表为 PNG 内嵌，单文件自包含。<br>
投资有风险，本内容仅供研究与教育，不构成投资建议；回测结果不等于实盘表现，历史业绩不代表未来。</footer>
</body>
</html>
"""


def _esc(value) -> str:
    return html_mod.escape(str(value))


def _pct(v) -> str:
    return "—" if v is None else f"{v:.2%}"


def _num(v, nd: int = 2) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _tone(v) -> str:
    """红涨绿跌：正数 → up（红），负数 → down（绿）。"""
    if v is None or v == 0:
        return ""
    return "up" if v > 0 else "down"


def _card(key: str, value: str, tone: str = "") -> str:
    cls = f"v {tone}".strip()
    return (
        f'<div class="card"><div class="k">{_esc(key)}</div>'
        f'<div class="{cls}">{value}</div></div>'
    )


def _health_badge(verdict: str | None) -> str:
    if not verdict:
        return ""
    cls = {"通过": "pass", "存疑": "warn", "不通过": "fail"}.get(verdict, "")
    return f'<span class="badge {cls}">体检：{_esc(verdict)}</span>'


def _figure(path: str | Path, title: str) -> str:
    """图片 base64 内嵌（自包含）。"""
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return (
        '<figure class="fig">'
        f"<figcaption>{_esc(title)}</figcaption>"
        f'<img src="data:image/png;base64,{data}" alt="{_esc(title)}">'
        "</figure>"
    )


def render_backtest_html(
    *,
    symbol: str,
    label: str,
    strategy: str,
    params: dict,
    start_date: str,
    end_date: str,
    initial_capital: float,
    metrics,
    figures: dict,
    text_report: str,
    health=None,
) -> str:
    """渲染单品种回测报告（HTML 字符串）。"""
    title = "quant-demo · 单品种回测报告"
    params_desc = ", ".join(f"{k}={v}" for k, v in (params or {}).items())
    meta = " · ".join(
        [
            _esc(label),
            _esc(f"{strategy}({params_desc})" if params_desc else strategy),
            _esc(f"{start_date} ~ {end_date}"),
            _esc(f"初始资金 {initial_capital:,.0f} 元"),
        ]
    )
    tr = getattr(metrics, "total_return", None)
    cards = "".join(
        [
            _card("总收益率", _pct(tr), _tone(tr)),
            _card("最大回撤", _pct(getattr(metrics, "max_drawdown", None))),
            _card("夏普比率", _num(getattr(metrics, "sharpe", None))),
            _card("胜率", _pct(getattr(metrics, "win_rate", None))),
            _card("交易笔数", _esc(getattr(metrics, "trade_count", "—"))),
            _card("手续费（元）", _esc(getattr(metrics, "total_commission", "—"))),
        ]
    )
    verdict = getattr(health, "overall", None) if health is not None else None
    parts = [
        _HEAD.format(title=_esc(title), css=_CSS, badge=_health_badge(verdict), meta=meta),
        f'<section class="cards">{cards}</section>',
    ]
    if figures:
        parts.append("<h2>图表</h2>")
        parts.extend(_figure(p, name) for name, p in figures.items())
    tail = _TAIL.format(text=_esc(text_report), ts=f"{dt.datetime.now():%Y-%m-%d %H:%M}")
    parts.append(tail)
    return "\n".join(parts)


def render_portfolio_html(
    *,
    labels: dict,
    symbols: list,
    start_date: str,
    end_date: str,
    initial_capital: float,
    metrics,
    figures: dict,
    text_report: str,
    compare: dict | None = None,
    risk_enabled: bool = False,
    sizing_desc: str | None = None,
) -> str:
    """渲染组合回测报告（HTML 字符串）。"""
    title = "quant-demo · 组合回测报告"
    names = " / ".join(labels.get(s, s) for s in symbols)
    meta = " · ".join(
        [
            _esc(f"{len(symbols)} 品种：{names}"),
            _esc(sizing_desc or "组合"),
            _esc(f"{start_date} ~ {end_date}"),
            _esc(f"初始资金 {initial_capital:,.0f} 元"),
            _esc(f"风控：{'全开' if risk_enabled else '关闭'}"),
        ]
    )
    tr = getattr(metrics, "total_return", None)
    cards = "".join(
        [
            _card("组合总收益率", _pct(tr), _tone(tr)),
            _card("最大回撤", _pct(getattr(metrics, "max_drawdown", None))),
            _card("夏普比率", _num(getattr(metrics, "sharpe", None))),
            _card("胜率", _pct(getattr(metrics, "win_rate", None))),
            _card("交易笔数", _esc(getattr(metrics, "trade_count", "—"))),
            _card("手续费（元）", _esc(getattr(metrics, "total_commission", "—"))),
        ]
    )
    parts = [_HEAD.format(title=_esc(title), css=_CSS, badge="", meta=meta)]
    parts.append(f'<section class="cards">{cards}</section>')

    if compare and compare.get("bare") and compare.get("risk"):
        b, r = compare["bare"], compare["risk"]
        rows = [
            ("累计收益", _pct(b.get("total_return")), _pct(r.get("total_return"))),
            ("最大回撤", _pct(b.get("max_drawdown")), _pct(r.get("max_drawdown"))),
            ("夏普", _num(b.get("sharpe")), _num(r.get("sharpe"))),
            ("最差单日", _pct(b.get("worst_day")), _pct(r.get("worst_day"))),
            ("交易笔数", _esc(b.get("trade_count")), _esc(r.get("trade_count"))),
            ("止损次数", _esc(b.get("stop_events")), _esc(r.get("stop_events"))),
            ("熔断次数", _esc(b.get("fuse_events")), _esc(r.get("fuse_events"))),
        ]
        body = "".join(
            f"<tr><td>{_esc(k)}</td><td>{v1}</td><td>{v2}</td></tr>" for k, v1, v2 in rows
        )
        parts.append(
            "<h2>风控开 / 关对比</h2>"
            '<table class="cmp"><tr><th>指标</th><th>裸奔（风控关）</th><th>风控全开</th></tr>'
            f"{body}</table>"
        )

    if figures:
        parts.append("<h2>图表</h2>")
        parts.extend(_figure(p, name) for name, p in figures.items())
    tail = _TAIL.format(text=_esc(text_report), ts=f"{dt.datetime.now():%Y-%m-%d %H:%M}")
    parts.append(tail)
    return "\n".join(parts)


def report_filename(
    prefix: str, *, keep_history: bool = True, ts: dt.datetime | None = None
) -> str:
    """报告文件名：``<prefix>_<时间戳>.html``；``keep_history=False`` 时省略时间戳（覆盖式）。"""
    if not keep_history:
        return f"{prefix}.html"
    ts = ts or dt.datetime.now()
    return f"{prefix}_{ts:%Y%m%d_%H%M%S}.html"


def save_html(content: str, out_path: str | Path) -> str:
    """写出 HTML 报告文件；返回路径字符串。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)
