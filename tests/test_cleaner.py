"""清洗逻辑单元测试。"""

import pandas as pd

from src.data.cleaner import clean


def _make_df() -> pd.DataFrame:
    # 乱序 + 重复日期（保留最后一条 close=1.5）+ 0 成交量无效 bar
    return pd.DataFrame(
        {
            "date": ["2024-01-03", "2024-01-02", "2024-01-02", "2024-01-04"],
            "symbol": ["C0"] * 4,
            "open": [2.0, 1.0, 1.5, 3.0],
            "high": [2.0, 1.0, 1.5, 3.0],
            "low": [2.0, 1.0, 1.5, 3.0],
            "close": [2.0, 1.0, 1.5, 3.0],
            "volume": [10, 10, 20, 0],
            "open_interest": [1, 1, 1, 1],
        }
    )


def test_clean_dedup_and_zero_volume():
    df = clean(_make_df())
    assert len(df) == 2  # 同日期去重保留最后一条 + 剔除 0 成交量 bar
    assert df["date"].is_monotonic_increasing
    assert (df["volume"] > 0).all()
    assert str(df["date"].iloc[0].date()) == "2024-01-02"
    assert df["close"].iloc[0] == 1.5  # 重复日期保留的是后一条
