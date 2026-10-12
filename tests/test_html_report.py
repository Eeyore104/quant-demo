"""HTML 报告测试：渲染 / base64 内嵌 / 转义 / 文件名。"""

from types import SimpleNamespace

from src.report import html_report


def _metrics(**kw):
    base = dict(
        total_return=0.08,
        max_drawdown=-0.2,
        sharpe=0.23,
        win_rate=0.37,
        trade_count=67,
        total_commission=405.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_render_backtest_html_contains_core(tmp_path):
    fig = tmp_path / "equity.png"
    fig.write_bytes(b"\x89PNG-fake-bytes")
    content = html_report.render_backtest_html(
        symbol="M0",
        label="豆粕 M0",
        strategy="dual_ma",
        params={"fast": 5, "slow": 20},
        start_date="2021-10-01",
        end_date="2026-10-01",
        initial_capital=100000,
        metrics=_metrics(),
        figures={"权益曲线": fig},
        text_report="报告正文 ABC",
        health=SimpleNamespace(overall="存疑"),
    )
    assert content.startswith("<!doctype html>")
    assert "单品种回测报告" in content
    assert "豆粕 M0" in content
    assert "8.00%" in content  # 总收益格式化
    assert "data:image/png;base64," in content
    assert "报告正文 ABC" in content
    assert "体检：存疑" in content


def test_render_backtest_escapes_html(tmp_path):
    content = html_report.render_backtest_html(
        symbol="X0",
        label="<script>alert(1)</script>",
        strategy="dual_ma",
        params={},
        start_date="a",
        end_date="b",
        initial_capital=1,
        metrics=_metrics(),
        figures={},
        text_report="<b>bold</b>",
    )
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content
    assert "&lt;b&gt;bold&lt;/b&gt;" in content


def test_render_portfolio_html_with_compare():
    content = html_report.render_portfolio_html(
        labels={"RB0": "螺纹钢 RB0", "M0": "豆粕 M0"},
        symbols=["RB0", "M0"],
        start_date="2021-10-01",
        end_date="2026-10-01",
        initial_capital=100000,
        metrics=_metrics(total_return=-0.0443),
        figures={},
        text_report="组合报告",
        compare={
            "bare": {
                "total_return": -0.98,
                "max_drawdown": -0.9896,
                "sharpe": -0.5,
                "worst_day": -0.42,
                "trade_count": 900,
                "stop_events": 0,
                "fuse_events": 0,
            },
            "risk": {
                "total_return": -0.0443,
                "max_drawdown": -0.2865,
                "sharpe": 0.1,
                "worst_day": -0.05,
                "trade_count": 515,
                "stop_events": 119,
                "fuse_events": 75,
            },
        },
        risk_enabled=True,
        sizing_desc="atr_risk（ATR 风险预算）",
    )
    assert "风控开 / 关对比" in content
    assert "裸奔（风控关）" in content
    assert "-98.00%" in content
    assert "风控：全开" in content


def test_report_filename_modes():
    assert html_report.report_filename("report_M0", keep_history=False) == "report_M0.html"
    fname = html_report.report_filename("report_M0", keep_history=True)
    assert fname.startswith("report_M0_") and fname.endswith(".html")
    assert len(fname) == len("report_M0_20261012_120000.html")


def test_save_html_creates_parent(tmp_path):
    out = html_report.save_html("<html></html>", tmp_path / "sub" / "r.html")
    assert (tmp_path / "sub" / "r.html").exists()
    assert out.endswith("r.html")
