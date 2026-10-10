"""风险指标单元测试（v1.3）：VaR/CVaR 性质与防未来 / 集中度 / 相关性预警。"""

import numpy as np
import pandas as pd
import pytest

from src.analysis.risk_metrics import build_risk_daily


def _equity_df(vals):
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(vals), freq="D"),
            "equity": [float(v) for v in vals],
            "margin_used": [0.0] * len(vals),
        }
    )


def test_var_cvar_properties_and_no_future():
    rng = np.random.default_rng(0)
    vals = [100000.0]
    for r in rng.normal(0, 0.01, 100):
        vals.append(vals[-1] * (1.0 + r))
    eq = _equity_df(vals)
    df = build_risk_daily(eq, var_window=250)

    assert df["var95"].iloc[:30].isna().all()  # 样本不足 30 个收益日
    assert not pd.isna(df["var95"].iloc[30])  # 第 31 行起（30 个收益样本）有效
    both = df[["var95", "var99", "cvar95"]].dropna()
    assert (both["var95"] >= 0).all()
    assert (both["var99"] >= both["var95"]).all()  # 更极端分位 → 更大损失
    assert (both["cvar95"] >= both["var95"] - 1e-12).all()  # 尾部均值 ≥ 分位点

    # 防未来：修改最后一行不影响此前读数
    eq2 = eq.copy()
    eq2.loc[len(eq2) - 1, "equity"] = 1.0
    df2 = build_risk_daily(eq2, var_window=250)
    mid = len(df) // 2
    assert df2["var95"].iloc[mid] == df["var95"].iloc[mid]
    assert df2["cvar95"].iloc[mid] == df["cvar95"].iloc[mid]


def test_concentration_and_corr_warn():
    n = 80
    a = np.cumsum(np.sin(np.arange(n)))
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "equity": [100000.0] * n,
            "margin_used": [10000.0] * n,
            "exposure_total": [30000.0] * n,
            "exposure_A0": [20000.0] * n,
            "exposure_B0": [10000.0] * n,
            "exposure_warn": [0] * n,
            "fuse_state": ["normal"] * n,
            "pnl_A0": a,
            "pnl_B0": a * 2.0,  # 与 A 完全相关
        }
    )
    out = build_risk_daily(df, corr_window=20, sectors={"黑色": ["A0", "B0"]})
    assert out["top_symbol_share"].iloc[-1] == pytest.approx(20000 / 30000)
    assert out["top_sector_share"].iloc[-1] == pytest.approx(1.0)
    assert out["leverage"].iloc[-1] == pytest.approx(0.3)
    assert out["margin_util"].iloc[-1] == pytest.approx(0.1)
    assert out["corr_warn"].iloc[-1] == 1
    assert out["corr_mean"].dropna().iloc[-1] == pytest.approx(1.0, abs=1e-9)


def test_running_without_risk_columns():
    # 无风控附加列（v1.2 风格 equity_df）时也能构建（缺列自动跳过）
    n = 40
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "equity": [100000.0] * n,
            "margin_used": [0.0] * n,
            "pnl_A0": [0.0] * n,
        }
    )
    out = build_risk_daily(df)
    assert "leverage" not in out.columns
    assert "corr_mean" in out.columns  # 单品种无配对 → 留空
    assert out["corr_mean"].isna().all()
