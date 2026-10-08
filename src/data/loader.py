"""数据获取：从 akshare 拉取期货日线，统一列名与格式。"""

import akshare as ak
import pandas as pd

from ..utils.logger import get_logger
from .symbols import normalize_symbol

log = get_logger("data.loader")

# akshare 返回的中文列名 -> 项目统一英文列名
_COLUMNS = {
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "持仓量": "open_interest",
}

_ORDER = ["date", "symbol", "open", "high", "low", "close", "volume", "open_interest"]


def fetch_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """拉取主力连续日线（akshare 的 futures_main_sina）。

    start_date / end_date 支持 "2021-10-01" 或 "20211001" 两种写法。
    品种代码结尾的字母 O（零/O 混淆）会自动纠正为数字 0。
    """
    fixed = normalize_symbol(symbol)
    if fixed != symbol:
        log.warning("品种代码 %r 结尾是字母 O（应为数字 0），已自动纠正为 %r", symbol, fixed)
        symbol = fixed

    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    log.info("akshare 拉取 %s 日线：%s ~ %s", symbol, start, end)

    try:
        raw = ak.futures_main_sina(symbol=symbol, start_date=start, end_date=end)
    except TypeError:
        # 兼容旧版接口签名（不接受日期参数）
        raw = ak.futures_main_sina(symbol=symbol)
    except ValueError as exc:
        raise ValueError(
            f"品种代码 {symbol!r} 拉取失败（代码无效？）。"
            f"请先运行 uv run python scripts/list_symbols.py 查看可用清单；"
            f"主力连续代码 = 字母 + 数字 0（如 V0 / RB0 / M0）"
        ) from exc

    df = raw.rename(columns=_COLUMNS)
    missing = [c for c in ("date", "open", "high", "low", "close", "volume") if c not in df.columns]
    if missing:
        raise ValueError(f"akshare 返回缺少列: {missing}，实际列: {list(raw.columns)}")
    if "open_interest" not in df.columns:
        df["open_interest"] = 0

    df = df[["date", "open", "high", "low", "close", "volume", "open_interest"]].copy()
    df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    # 统一过滤到目标区间（ISO 日期字符串可直接比较）
    lo = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    hi = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    df = df[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)

    log.info("拉取完成：%d 行（%s ~ %s）", len(df), lo, hi)
    return df[_ORDER]
