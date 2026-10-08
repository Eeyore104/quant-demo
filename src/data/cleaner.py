"""数据清洗：去重、去无效 bar、排序、重排索引。"""

import pandas as pd


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """清洗规则：
    ① date 解析为 datetime
    ② 按日期排序 + 同日去重（保留最后一条）
    ③ 剔除 volume <= 0 的无效 bar
    ④ 数值列类型规整
    """
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    out = out[out["volume"] > 0]
    out["volume"] = out["volume"].astype(int)
    out["open_interest"] = out["open_interest"].fillna(0).astype(int)
    out = out.sort_values("date").reset_index(drop=True)
    return out
