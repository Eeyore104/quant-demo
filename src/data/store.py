"""CSV 数据存取（原始缓存 + 清洗结果）。"""

from pathlib import Path

import pandas as pd


def exists(path: str | Path) -> bool:
    """判断文件是否存在（缓存命中判断）。"""
    return Path(path).exists()


def save_csv(df: pd.DataFrame, path: str | Path) -> Path:
    """保存为 CSV（自动创建父目录）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return p


def load_csv(path: str | Path) -> pd.DataFrame:
    """读取 CSV。"""
    return pd.read_csv(path)
