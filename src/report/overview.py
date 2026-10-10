"""图表总览：把 ``output/figures`` 下的全部图表汇总为两个入口文件。

- ``overview.png``  —— 分区拼图（缩略网格，快速扫视 / 分享截图）；
- ``overview.html`` —— 单页图表浏览（原尺寸、自包含、双击滚完全部图表）。

生成时机：
- 自动：``run_backtest.py`` / ``run_portfolio.py`` 结束时刷新；
- 手动：``uv run python scripts/make_overview.py``。
"""

from __future__ import annotations

import base64
import datetime as dt
import html as html_mod
from pathlib import Path

import matplotlib
import matplotlib.image as mpimg

matplotlib.use("Agg")  # 无需 GUI，直接出图
import matplotlib.pyplot as plt

from .plotter import _titled  # 同时引入 plotter，启用中文字体配置

# 分区与文件名匹配规则（(文件名或前缀, 标题)；按前缀命中，多余后缀并入标题）
SECTIONS: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    (
        "① 单品种回测",
        (
            ("equity", "权益曲线"),
            ("signals", "价格与买卖点"),
            ("drawdown", "回撤区间"),
            ("monthly_heatmap", "月度收益热力图"),
        ),
    ),
    (
        "② 研究体检与参数扫描",
        (
            ("health_param_neighborhood", "参数邻域细检"),
            ("health_monte_carlo", "蒙特卡洛对照"),
            ("health_cost_sensitivity", "成本敏感性"),
            ("param_heatmap_", "参数扫描热力图"),
        ),
    ),
    (
        "③ 组合回测",
        (
            ("portfolio_equity", "组合权益与回撤"),
            ("portfolio_correlation", "品种相关性矩阵"),
            ("portfolio_contribution", "品种盈亏贡献"),
            ("portfolio_margin", "保证金占用与可用资金"),
        ),
    ),
    (
        "④ 风控体系",
        (
            ("risk_compare", "风控开/关对比"),
            ("risk_stress", "压力测试"),
            ("risk_metrics", "风险指标"),
        ),
    ),
]

_OTHER = "⑤ 其他"


def _match(stem: str) -> tuple[str, str, int]:
    """文件名 → (分区, 标题, 分区内排序号)。未命中的归入「其他」。"""
    best: tuple[str, str, int, int] | None = None  # (分区, 标题, 排序号, 命中 key 长度)
    order = 0
    for sec, specs in SECTIONS:
        for key, title in specs:
            order += 1
            if stem == key or stem.startswith(key):
                extra = stem[len(key) :].strip("_")
                t = f"{title} · {extra}" if extra else title
                # 更长的 key 视为更具体的命中
                if best is None or len(key) > best[3]:
                    best = (sec, t, order, len(key))
    if best is None:
        return _OTHER, stem, 1000
    return best[0], best[1], best[2]


def collect_overview_entries(fig_dir) -> list[dict]:
    """扫描图表目录，返回按分区/次序整理好的条目列表。"""
    fig_dir = Path(fig_dir)
    entries: list[dict] = []
    for f in sorted(fig_dir.glob("*.png")):
        if f.stem.startswith("overview"):
            continue  # 总览自身不参与汇总
        sec, title, order = _match(f.stem)
        entries.append(
            {
                "section": sec,
                "title": title,
                "path": f,
                "mtime": dt.datetime.fromtimestamp(f.stat().st_mtime),
                "order": order,
            }
        )
    entries.sort(key=lambda e: (e["section"], e["order"], e["title"]))
    return entries


def _group(entries: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["section"], []).append(e)
    return groups


# ---------------------------------------------------------------- 拼图


def build_contact_sheet(
    entries: list[dict], out_path, *, cols: int = 3, title: str | None = None
) -> str:
    """把全部图表拼成一张分区大图（PNG）。"""
    title = title or _titled("quant-demo", "图表总览")
    groups = _group(entries)
    cell_w, cell_h = 4.35, 2.62
    top_h, header_h, bottom_h = 0.66, 0.46, 0.30
    img_rows = sum((len(v) + cols - 1) // cols for v in groups.values())
    width = cell_w * cols
    height = top_h + bottom_h + header_h * len(groups) + cell_h * max(img_rows, 1)

    fig = plt.figure(figsize=(width, height), dpi=110)
    fig.patch.set_facecolor("white")
    fig.text(0.012, 1 - 0.24 / height, title, fontsize=16, va="top", color="#1a1a1a")
    fig.text(
        0.988,
        1 - 0.27 / height,
        f"{len(entries)} 张图 · 生成于 {dt.datetime.now():%Y-%m-%d %H:%M}",
        fontsize=10,
        va="top",
        ha="right",
        color="#8a8a8a",
    )

    y = height - top_h
    for sec, items in groups.items():
        fig.text(0.012, (y - 0.06) / height, sec, fontsize=13, va="top", color="#333333")
        y -= header_h
        for start in range(0, len(items), cols):
            chunk = items[start : start + cols]
            for j, e in enumerate(chunk):
                left = (j * cell_w + 0.05) / width
                bottom = (y - cell_h + 0.10) / height
                w = (cell_w - 0.10) / width
                h = (cell_h - 0.48) / height
                ax = fig.add_axes([left, bottom, w, h])
                ax.imshow(mpimg.imread(e["path"]))
                ax.set_axis_off()
                ax.set_title(e["title"], fontsize=9, pad=3, color="#444444")
            y -= cell_h

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return str(out)


# ---------------------------------------------------------------- 单页浏览

_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f5f6f7; color: #1f2328;
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; }
header { position: sticky; top: 0; z-index: 9; background: rgba(255,255,255,.94);
  border-bottom: 1px solid #e3e5e8; backdrop-filter: blur(4px); }
.hd { max-width: 1180px; margin: 0 auto; padding: 12px 20px; display: flex;
  align-items: baseline; gap: 14px; flex-wrap: wrap; }
h1 { font-size: 17px; margin: 0; font-weight: 500; }
.meta { color: #8a8f98; font-size: 12px; }
nav { display: flex; gap: 10px; flex-wrap: wrap; }
nav a { color: #185fa5; text-decoration: none; font-size: 13px; }
nav a:hover { text-decoration: underline; }
main { max-width: 1180px; margin: 0 auto; padding: 8px 20px 48px; }
h2 { font-size: 15px; font-weight: 500; margin: 26px 0 10px;
  padding-left: 10px; border-left: 4px solid #1976d2; }
figure.card { margin: 0 0 18px; background: #fff; border: 1px solid #e3e5e8;
  border-radius: 10px; overflow: hidden; }
figcaption { display: flex; justify-content: space-between; gap: 10px; padding: 9px 14px;
  font-size: 13px; border-bottom: 1px solid #eef0f2; }
figcaption .f { color: #9aa0a8; font-size: 12px; }
figure.card img { width: 100%; height: auto; display: block; }
footer { max-width: 1180px; margin: 0 auto; padding: 0 20px 40px;
  color: #9aa0a8; font-size: 12px; }
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
<header><div class="hd">
  <h1>{title}</h1>
  <span class="meta">{n} 张图 · 生成于 {ts}</span>
  <nav>{nav}</nav>
</div></header>
<main>
"""


def build_gallery_html(entries: list[dict], out_path, *, title: str | None = None) -> str:
    """生成单页图表浏览（自包含 HTML，图片以 base64 内嵌）。"""
    title = title or _titled("quant-demo", "图表总览")
    groups = _group(entries)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    nav = "".join(f'<a href="#sec{i}">{html_mod.escape(sec)}</a>' for i, sec in enumerate(groups))
    parts = [_HEAD.format(title=html_mod.escape(title), css=_CSS, n=len(entries), ts=ts, nav=nav)]
    for i, (sec, items) in enumerate(groups.items()):
        parts.append(f'<h2 id="sec{i}">{html_mod.escape(sec)}</h2>')
        for e in items:
            b64 = base64.b64encode(Path(e["path"]).read_bytes()).decode("ascii")
            parts.append(
                '<figure class="card">'
                f"<figcaption><span>{html_mod.escape(e['title'])}</span>"
                f'<span class="f">{html_mod.escape(e["path"].name)}'
                f" · {e['mtime']:%Y-%m-%d %H:%M}</span></figcaption>"
                f'<img src="data:image/png;base64,{b64}" '
                f'alt="{html_mod.escape(e["title"])}"></figure>'
            )
    parts.append(
        "</main><footer>本页由 run_backtest.py / run_portfolio.py / "
        "scripts/make_overview.py 自动生成；图表明细位于 output/figures/。</footer>"
        "</body></html>"
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------- 一键入口


def build_overview(fig_dir, out_dir=None, *, cols: int = 3) -> dict[str, str]:
    """扫描 ``fig_dir`` 生成 overview.png + overview.html；返回 {名称: 路径}。"""
    entries = collect_overview_entries(fig_dir)
    if not entries:
        return {}
    out = Path(out_dir) if out_dir is not None else Path(fig_dir).parent
    return {
        "总览拼图": build_contact_sheet(entries, out / "overview.png", cols=cols),
        "总览页": build_gallery_html(entries, out / "overview.html"),
    }
