"""图表总览单元测试（overview.png / overview.html）。"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.report.overview import build_overview, collect_overview_entries


def _png(path, color):
    fig = plt.figure(figsize=(4, 2))
    fig.patch.set_facecolor(color)
    fig.savefig(path)
    plt.close(fig)


def test_collect_sections_and_titles(tmp_path):
    fig_dir = tmp_path / "figures"
    fig_dir.mkdir()
    _png(fig_dir / "equity.png", "#ffcccc")
    _png(fig_dir / "portfolio_equity.png", "#ccccff")
    _png(fig_dir / "risk_compare.png", "#ccffcc")
    _png(fig_dir / "param_heatmap_dual_ma.png", "#ffeecc")
    _png(fig_dir / "mystery.png", "#eeeeee")

    entries = collect_overview_entries(fig_dir)
    by_stem = {e["path"].stem: e for e in entries}
    assert by_stem["equity"]["section"] == "① 单品种回测"
    assert by_stem["portfolio_equity"]["section"] == "③ 组合回测"
    assert by_stem["risk_compare"]["section"] == "④ 风控体系"
    assert by_stem["mystery"]["section"] == "⑤ 其他"
    assert by_stem["param_heatmap_dual_ma"]["title"] == "参数扫描热力图 · dual_ma"


def test_build_overview_outputs(tmp_path):
    fig_dir = tmp_path / "figures"
    fig_dir.mkdir()
    _png(fig_dir / "equity.png", "#ffcccc")
    _png(fig_dir / "risk_stress.png", "#ccffcc")

    paths = build_overview(fig_dir, tmp_path)
    png = Path(paths["总览拼图"])
    html = Path(paths["总览页"])
    assert png.exists() and png.stat().st_size > 5000  # 拼图非空
    text = html.read_text(encoding="utf-8")
    assert "data:image/png;base64" in text
    assert "单品种回测" in text and "压力测试" in text


def test_build_overview_empty_dir(tmp_path):
    fig_dir = tmp_path / "figures"
    fig_dir.mkdir()
    assert build_overview(fig_dir, tmp_path) == {}
