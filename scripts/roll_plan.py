"""换月计划生成（v2.2 · docs/21 T2.2-3）：主力识别 + 到期驱动 → 待确认计划。

用法（项目根目录）：

    uv run python scripts/roll_plan.py --dry                  # 只看建议，不落盘
    uv run python scripts/roll_plan.py --write                # 落盘 plans/roll_<id>.json
    uv run python scripts/roll_plan.py --write --to AL0=al2612  # 人工指定目标合约

判定（两条触发取并集）：

1. **到期驱动**：距自然人最后了结日 ≤ ``live.lifecycle.roll_trigger_days``；
2. **持仓量切换**：候选合约持仓量 > 当前 × ``main_switch_ratio``，且连续
   ``main_switch_days`` 个交易日成立（用 ``oi_history.json`` 防抖）。

数据源：akshare ``futures_zh_realtime``（收盘后可用）；失败则降级为「只按到期驱动」，
并在输出里标注 OI 缺失——**不允许因为拿不到持仓量就不出计划**。

执行是半自动的：本脚本只「算」，落盘的计划必须由人
``uv run python run_live.py --confirm-roll <plan_id>`` 确认后才会被主程序执行。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live.contract_code import candidates, canonical, format_like  # noqa: E402
from src.live.lifecycle import LifecycleBook  # noqa: E402
from src.live.roll import (  # noqa: E402
    RollItem,
    RollPlan,
    next_plan_id,
    save_plan,
    summary_lines,
)
from src.utils.config_loader import load_config, resolve_path  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

log = get_logger("live.rollplan")

# 品种前缀 → 新浪品种名（akshare futures_zh_realtime 的入参）
MARK = {
    "RB": "螺纹钢",
    "I": "铁矿石",
    "M": "豆粕",
    "P": "棕榈",
    "SR": "白糖",
    "MA": "郑醇",
    "TA": "PTA",
    "AL": "沪铝",
}

CONTRACT_RE = re.compile(r"^[A-Z]+\d{3,4}$")


def base_of(symbol: str) -> str:
    return symbol.rstrip("0123456789").upper()


def read_ledger_size(base: str | Path | None = None) -> dict[str, int]:
    """读本地账本净持仓（只读，不改任何状态）。"""
    path = Path(base) if base is not None else resolve_path("output/live/state/ledger.json")
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(s): int((st or {}).get("size", 0))
        for s, st in (raw.get("states") or {}).items()
    }


def fetch_oi(codes: list[str]) -> dict[str, float]:
    """拉取各合约持仓量（拿不到就返回空，不阻塞计划生成）。"""
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001 - 可选依赖，缺了就走降级
        log.warning("akshare 不可用（%s），本次只按到期驱动", exc)
        return {}

    wanted = {canonical(c) for c in codes}
    out: dict[str, float] = {}
    for mark in sorted({MARK.get(base_of(c)) for c in wanted} - {None}):
        try:
            df = ak.futures_zh_realtime(symbol=mark)
        except Exception as exc:  # noqa: BLE001 - 单个品种失败不影响其他
            log.warning("持仓量拉取失败（%s）：%s", mark, exc)
            continue
        for _, row in df.iterrows():
            code = str(row.get("symbol") or "").upper()
            if not CONTRACT_RE.match(code):
                continue
            key = canonical(code)
            if key not in wanted:
                continue
            try:
                out[key] = float(row.get("position") or 0)
            except (TypeError, ValueError):
                continue
    return out


def _history_path() -> Path:
    return resolve_path("output/live/plans/oi_history.json")


def load_history() -> dict[str, dict[str, float]]:
    path = _history_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(history: dict[str, dict[str, float]], keep: int = 10) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for day in sorted(history)[:-keep]:
        history.pop(day, None)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")


def _prev_snapshot(history: dict[str, dict[str, float]], today: str) -> dict[str, float]:
    older = [d for d in sorted(history) if d < today]
    return history[older[-1]] if older else {}


def build_plan(
    cfg: dict,
    *,
    today: dt.date,
    overrides: dict[str, str] | None = None,
    oi: dict[str, float] | None = None,
    history: dict[str, dict[str, float]] | None = None,
    sizes: dict[str, int] | None = None,
    plan_base: str | Path | None = None,
) -> tuple[RollPlan, list[str]]:
    """按到期 + 持仓量生成计划（纯逻辑，便于测试）。"""
    live = dict(cfg.get("live") or {})
    lc = dict(live.get("lifecycle") or {})
    cmap = {str(k): str(v) for k, v in (live.get("contracts_map") or {}).items()}
    life = LifecycleBook.from_config(live, cmap, today=today)
    sizes = read_ledger_size() if sizes is None else dict(sizes)
    overrides = {k.upper(): v for k, v in (overrides or {}).items()}
    oi = dict(oi or {})
    history = history or {}
    prev = _prev_snapshot(history, today.isoformat())
    ratio = float(lc.get("main_switch_ratio", 1.2))
    switch_days = max(1, int(lc.get("main_switch_days", 2)))
    trigger_days = int(lc.get("roll_trigger_days", 10))

    items: list[RollItem] = []
    notes: list[str] = []
    if switch_days > 2:
        notes.append(f"main_switch_days={switch_days} 暂只支持 2 日防抖，本次按 2 日判定")
    for symbol, instrument in cmap.items():
        cur = canonical(instrument)
        days = life.days_left(symbol)
        pool = [
            canonical(c)
            for c in candidates(instrument)
            if canonical(c) in oi and oi[canonical(c)] > 0 and canonical(c) != cur
        ]
        best = max(pool, key=lambda c: oi[c]) if pool else ""

        switched_today = bool(best) and oi.get(best, 0) > oi.get(cur, 0) * ratio
        switched_prev = bool(best) and prev.get(best, 0) > prev.get(cur, 0) * ratio
        # 防抖：要求连续 2 个交易日成立；只有一天数据时按当日判定（宁可早提醒）
        switched = switched_today and (
            switched_prev if (switch_days >= 2 and prev) else True
        )

        expiring = days <= trigger_days
        if not (expiring or switched):
            continue

        target = overrides.get(symbol) or (format_like(instrument, best) if best else "")
        if not target:
            notes.append(f"{symbol}：无可用持仓量数据，请用 --to {symbol}=<合约> 指定目标")
            continue

        size = int(sizes.get(symbol, 0))
        items.append(
            RollItem(
                symbol=symbol,
                from_instrument=instrument,
                to_instrument=target,
                direction=("LONG" if size > 0 else "SHORT" if size < 0 else ""),
                lots=abs(size),
                reason=("expiry" if expiring else "oi_switch"),
                days_left=days,
                oi_from=float(oi.get(cur, 0)),
                oi_to=float(oi.get(canonical(target), 0)),
            )
        )

    plan = RollPlan(
        plan_id=next_plan_id(today, plan_base),
        date=today.isoformat(),
        items=items,
    )
    return plan, notes


def _print_plan(plan: RollPlan, notes: list[str], oi_ok: bool, saved: bool = False) -> None:
    print(f"\n换月计划 {plan.plan_id}（{plan.date}）｜ 状态：待确认")
    if not oi_ok:
        print("  ⚠ 持仓量数据缺失，本次仅按到期驱动")
    if not plan.items:
        print("  无需移仓：无品种进入触发窗口")
    for it in plan.items:
        side = it.direction or "空仓"
        print(
            f"  {it.symbol:4s} {it.from_instrument} → {it.to_instrument}"
            f" ｜ {side} {it.lots} 手 ｜ 剩 {it.days_left} 天 ｜ {it.reason}"
            f" ｜ OI {int(it.oi_from):,} → {int(it.oi_to):,}"
        )
    for note in notes:
        print(f"  ⚠ {note}")
    if plan.items and saved:
        print("\n确认后执行：uv run python run_live.py --confirm-roll " + plan.plan_id)
    elif plan.items:
        print("\n（未落盘）加 --write 生成计划后可确认执行")


def main() -> int:
    parser = argparse.ArgumentParser(description="换月计划生成（半自动：只算不执行）")
    parser.add_argument("--write", action="store_true", help="落盘计划（默认只打印）")
    parser.add_argument(
        "--dry", action="store_true", help="只打印计划（持仓量快照仍会记录，用于防抖）"
    )
    parser.add_argument(
        "--to", action="append", default=None, metavar="AL0=al2612",
        help="人工指定目标合约（可多次）",
    )
    parser.add_argument("--date", default=None, help="指定日期（默认今天，测试用）")
    args = parser.parse_args()

    overrides: dict[str, str] = {}
    for raw in args.to or []:
        if "=" not in raw:
            print(f"--to 格式应为 品种=合约，收到：{raw}")
            return 1
        sym, code = raw.split("=", 1)
        overrides[sym.strip().upper()] = code.strip()

    cfg = load_config()
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    live = dict(cfg.get("live") or {})
    cmap = {str(k): str(v) for k, v in (live.get("contracts_map") or {}).items()}

    codes: list[str] = []
    for instrument in cmap.values():
        codes.append(instrument.upper())
        codes.extend(c.upper() for c in candidates(instrument))
    oi = fetch_oi(codes)

    history = load_history()
    plan, notes = build_plan(cfg, today=today, overrides=overrides, oi=oi, history=history)

    if oi:
        history[today.isoformat()] = {k: float(v) for k, v in oi.items()}
        save_history(history)

    saved = False
    if args.write and not args.dry:
        if not plan.items:
            print("无移仓项，未生成计划文件")
            return 0
        path = save_plan(plan, resolve_path("output/live/plans"))
        print(f"已写入：{path}")
        for line in summary_lines(plan):
            print(line)
        saved = True

    _print_plan(plan, notes, oi_ok=bool(oi), saved=saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
