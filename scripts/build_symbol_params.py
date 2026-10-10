"""生成全量品种参数表 ``config/symbol_params.yaml``（多源交叉校验，自动生成）。

数据源：
① 新浪主力连续清单（品种范围与交易所）；
② akshare ``futures_comm_info``（主力合约：每跳毛利、手续费）；
③ akshare ``futures_contract_detail_em``（交易单位、最小变动价格）；
④ akshare ``futures_fees_info``（openctp 费用参照表：合约乘数、最小跳动、费用）。

校验规则：
- ``verified``：东财「最小变动 × 交易单位」== 主力合约「每跳毛利」（±2%）；
- ``adjusted``：不通过时按「每跳毛利 ÷ 最小变动」折算合约乘数（报价单位口径修正）；
- ``fees``：无主力合约时取 openctp 费用表（单源，标注）。
未覆盖品种不会写入，运行时由 ``run_backtest.py`` 走默认值 + 告警。

用法（项目根目录下，需要联网）：
    uv run python scripts/build_symbol_params.py
"""

import re
import time
from datetime import datetime
from pathlib import Path

import akshare as ak

OUT_PATH = Path(__file__).resolve().parents[1] / "config" / "symbol_params.yaml"

_EXCHANGE_NAMES = {
    "shfe": "上期所",
    "ine": "能源中心",
    "dce": "大商所",
    "czce": "郑商所",
    "cffex": "中金所",
    "gfex": "广期所",
}
_ORDER = ["shfe", "ine", "dce", "czce", "cffex", "gfex"]


def _num(value):
    """从「10吨/手」「0.02元/克」之类的文本里提取数值。"""
    if value is None:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value))
    return float(m.group(1)) if m else None


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def resolve_params(sina_row, main_row, detail_kv, fees_row) -> dict | None:
    """解析单个品种的参数与校验标签；无法可靠解析时返回 None。"""
    tick = _num(detail_kv.get("最小变动价格")) if detail_kv else None
    mult_em = _num(detail_kv.get("交易单位")) if detail_kv else None
    jump = _num(main_row.get("每跳毛利")) if main_row is not None else None
    fee = _num(main_row.get("手续费")) if main_row is not None else None
    fees_tick = _num(fees_row.get("最小跳动")) if fees_row is not None else None
    fees_mult = _num(fees_row.get("合约乘数")) if fees_row is not None else None
    fees_fee = _num(fees_row.get("开仓费用/手")) if fees_row is not None else None

    info = {"tick": None, "mult": None, "fee": fee if fee is not None else fees_fee, "note": None}

    if tick and mult_em and jump:
        calc = tick * mult_em
        if abs(calc - jump) <= max(0.02 * jump, 1e-6):
            info.update(tick=tick, mult=int(round(mult_em)), note="verified")
            return info
        # 口径修正：按每跳毛利折算合约乘数
        derived = jump / tick
        mult_r = round(derived)
        if abs(derived - mult_r) / max(derived, 1e-9) < 0.01:
            info.update(tick=tick, mult=int(mult_r), note="adjusted")
            return info
        return None
    if tick and jump:
        derived = jump / tick
        mult_r = round(derived)
        if abs(derived - mult_r) / max(derived, 1e-9) < 0.01:
            info.update(tick=tick, mult=int(mult_r), note="adjusted")
            return info
        return None
    if fees_tick and fees_mult:
        info.update(tick=fees_tick, mult=int(round(fees_mult)), note="fees")
        return info
    return None


def main() -> None:
    print("拉取数据源……")
    sina = ak.futures_display_main_sina()[["symbol", "exchange", "name"]]
    comm = ak.futures_comm_info(symbol="所有")
    main_rows = comm[comm["备注"].fillna("").str.contains("主力")].copy()
    main_rows["prod"] = main_rows["合约代码"].str.lower().str.replace(r"\d+$", "", regex=True)
    try:
        fees = ak.futures_fees_info()
        fees["prod"] = fees["品种代码"].str.upper()
    except Exception as e:  # noqa: BLE001
        print("费用参照表获取失败（跳过，不影响主校验）:", str(e)[:60])
        fees = None
    print(f"品种清单 {len(sina)} 个 · 主力合约记录 {len(main_rows)} 条")

    records = []
    for idx, (_, s) in enumerate(sina.iterrows(), 1):
        sym, ex, name = s["symbol"], s["exchange"], str(s["name"]).removesuffix("连续")
        prod = re.sub(r"[0-9]+$", "", sym).lower()
        m = main_rows[main_rows["prod"] == prod]
        main_row = m.iloc[0] if len(m) else None
        contract = str(main_row["合约代码"]) if main_row is not None else None
        fees_row = None
        if fees is not None:
            f = fees[fees["prod"] == prod.upper()]
            fees_row = f.iloc[0] if len(f) else None

        detail_kv = None
        if contract:
            for _ in range(2):  # 失败重试一次
                try:
                    d = ak.futures_contract_detail_em(symbol=contract)
                    detail_kv = dict(zip(d["item"], d["value"], strict=False))
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.6)

        info = resolve_params(s, main_row, detail_kv, fees_row)
        if info:
            records.append({"symbol": sym, "exchange": ex, "name": name, **info})
            print(
                f"[{idx}/{len(sina)}] {sym:5s} {name:8s} "
                f"tick={info['tick']:g} 乘数={info['mult']:g} "
                f"费={info['fee']:g} [{info['note']}]"
            )
        else:
            records.append({"symbol": sym, "exchange": ex, "name": name, "note": "uncovered"})
            print(f"[{idx}/{len(sina)}] {sym:5s} {name:8s} —— 未能解析（运行时走默认值 + 告警）")
        time.sleep(0.12)

    covered = [r for r in records if r["note"] != "uncovered"]
    uncovered = [r for r in records if r["note"] == "uncovered"]

    # ---------- 写出 YAML（按交易所分组，含校验标签）----------
    lines = [
        "# ================================================================",
        "# 品种参数全量表（自动生成，勿手改——需要覆盖时用 config.yaml 的",
        "# backtest.params_by_symbol 手动层，优先级更高）",
        f"# 生成: scripts/build_symbol_params.py ｜ 时间: {datetime.now():%Y-%m-%d %H:%M}",
        "# note 含义: verified 三重校验 / adjusted 按每跳毛利折算(报价口径修正)"
        " / fees openctp费用表(单源)",
        f"# 覆盖 {len(covered)}/{len(records)} 个品种；"
        f"未覆盖: {', '.join(r['symbol'] for r in uncovered) or '无'}",
        "# 手续费为交易所近似口径（元/手，可覆盖）",
        "# ================================================================",
    ]
    grouped: dict[str, list] = {}
    for r in covered:
        grouped.setdefault(r["exchange"], []).append(r)
    for ex in _ORDER + [e for e in grouped if e not in _ORDER]:
        if ex not in grouped:
            continue
        items = sorted(grouped[ex], key=lambda r: r["symbol"])
        lines.append(f"# —— {_EXCHANGE_NAMES.get(ex, ex)}（{len(items)}）——")
        for r in items:
            lines.append(
                f'"{r["symbol"]}": {{ tick_size: {_fmt(r["tick"])}, '
                f"contract_multiplier: {_fmt(r['mult'])}, "
                f"commission_per_lot: {_fmt(r['fee'])}, note: {r['note']} }}"
            )
        lines.append("")
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print()
    print(f"已生成：{OUT_PATH}")
    print(f"覆盖 {len(covered)}/{len(records)} 个品种")
    if uncovered:
        print("未覆盖（运行时走默认值 + 告警）：")
        for r in uncovered:
            print(f"  {r['symbol']:5s} {r['name']}")


if __name__ == "__main__":
    main()
