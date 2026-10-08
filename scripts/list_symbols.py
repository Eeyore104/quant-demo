"""列出全部可回测品种（新浪财经主力连续合约，共 80+ 个）。

用法（项目根目录下）：

    uv run python scripts/list_symbols.py

换品种：把 ``config/config.yaml`` 里 ``data.symbol`` 改为清单中的任一代码后重跑即可。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import akshare as ak
import pandas as pd

from src.data.symbols import SYMBOL_NAMES
from src.utils.config_loader import resolve_path

_EXCHANGE_NAMES = {
    "shfe": "上期所",
    "ine": "能源中心",
    "dce": "大商所",
    "czce": "郑商所",
    "cffex": "中金所",
    "gfex": "广期所",
}


def main() -> None:
    df = ak.futures_display_main_sina()
    df = df[["symbol", "exchange", "name"]].copy()
    df["exchange_name"] = df["exchange"].map(_EXCHANGE_NAMES).fillna(df["exchange"])
    df["标注名"] = df["symbol"].map(SYMBOL_NAMES).fillna("")

    order = ["shfe", "ine", "dce", "czce", "cffex", "gfex"]
    df["_order"] = pd.Categorical(df["exchange"], categories=order, ordered=True)
    df = df.sort_values(["_order", "symbol"]).drop(columns="_order").reset_index(drop=True)

    print(f"共 {len(df)} 个可回测品种（数据源：新浪财经主力连续合约）\n")
    for ex_code, group in df.groupby("exchange", sort=False):
        ex_name = _EXCHANGE_NAMES.get(ex_code, ex_code)
        print(f"【{ex_name}】{len(group)} 个")
        for sym, name, label in zip(group["symbol"], group["name"], group["标注名"], strict=True):
            base = str(name).removesuffix("连续")
            suffix = f"（{label}）" if label and label != base else ""
            print(f"  {sym:<6} {base}{suffix}")
        print()

    out_path = resolve_path("output/reports/symbols_list.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"完整清单已保存：{out_path}")
    print("换品种：把 config/config.yaml 里 data.symbol 改为上述任一代码后重跑即可。")


if __name__ == "__main__":
    main()
