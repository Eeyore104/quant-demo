"""合约映射检查（v2.0 · docs/16 T2.0-1c）：订阅候选合约，核对行情活跃性。

用法（项目根目录）：
    uv run python scripts/check_contracts.py                # 验证 config 的 contracts_map
    uv run python scripts/check_contracts.py --env 724      # 指定环境
    uv run python scripts/check_contracts.py --collect 10   # 收集时长秒数（默认 10）
    uv run python scripts/check_contracts.py --add al2701   # 追加候选合约（可多次）

输出：每个映射合约是否收到实时行情 + 最新价 / 持仓量 / tick 数。
用途：轮 1「合约映射实测」——确认主力连续 → 真实合约的映射表；换月时复核。
"""

import argparse
import queue
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live.ctp_api import MdClient, MdLogin, Tick
from src.utils.config_loader import load_config, resolve_path


def main() -> int:
    parser = argparse.ArgumentParser(description="合约映射检查：订阅候选并核对行情活跃性")
    parser.add_argument("--env", choices=["first", "724"], default=None, help="环境（默认取配置）")
    parser.add_argument("--collect", type=float, default=10.0, help="收集 tick 秒数（默认 10）")
    parser.add_argument("--add", action="append", default=None, help="追加候选合约（可多次）")
    args = parser.parse_args()

    cfg = load_config()
    live = dict(cfg.get("live") or {})
    env_key = args.env or str(live.get("env", "first"))
    fronts = (live.get("envs") or {}).get(env_key)
    if not fronts:
        print(f"配置缺少 live.envs.{env_key}")
        return 1
    investor = str(live.get("investor_id") or "")
    password = str(live.get("password") or "")
    if not investor or not password:
        print("缺少 SimNow 账号（请在 config/config.local.yaml 填写）")
        return 1

    cmap = dict(live.get("contracts_map") or {})
    extra = [c for c in (args.add or []) if c not in set(cmap.values())]
    contracts = list(dict.fromkeys([*cmap.values(), *extra]))
    if not contracts:
        print("contracts_map 为空，且未追加 --add 候选")
        return 1

    evq: queue.Queue = queue.Queue()
    md = MdClient(
        front=fronts["md_front"],
        flow_dir=resolve_path(live.get("flow_dir", "output/live/ctp_flow")) / "md",
        evq=evq,
        broker_id=str(live.get("broker_id", "9999")),
        user_id=investor,
        password=password,
    )
    print(f"连接行情：{fronts['md_front']}（环境 {env_key}）")
    md.start()
    try:
        deadline = time.time() + 15
        logged = False
        while time.time() < deadline and not logged:
            try:
                ev = evq.get(timeout=1)
            except queue.Empty:
                continue
            if isinstance(ev, MdLogin) and ev.ok:
                logged = True
        if not logged:
            print("行情登录失败 / 超时")
            return 1

        md.subscribe(contracts)
        print(f"已订阅 {len(contracts)} 个合约，收集 {args.collect:.0f} 秒行情……\n")

        last: dict[str, Tick] = {}
        count: dict[str, int] = {}
        end = time.time() + args.collect
        while time.time() < end:
            try:
                ev = evq.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(ev, Tick):
                last[ev.instrument] = ev
                count[ev.instrument] = count.get(ev.instrument, 0) + 1
    finally:
        md.close()

    print("品种 | 合约 | 行情 | 最新价 | 持仓量 | tick数")
    print("-" * 64)
    missing: list[str] = []
    for sym, contract in cmap.items():
        t = last.get(contract)
        if t:
            print(
                f"{sym} | {contract} | OK | {t.last_price} | "
                f"{t.open_interest:.0f} | {count[contract]}"
            )
        else:
            print(f"{sym} | {contract} | 未收到行情（需核对候选月份）")
            missing.append(contract)
    for contract in extra:
        t = last.get(contract)
        if t:
            print(
                f"(候选) | {contract} | OK | {t.last_price} | "
                f"{t.open_interest:.0f} | {count[contract]}"
            )
        else:
            print(f"(候选) | {contract} | 未收到行情")

    print()
    if missing:
        print(f"结论：{len(missing)} 个映射未收到行情 → 用 --add 试备选月份后定稿。")
        return 1
    print(f"结论：全部 {len(cmap)} 个映射合约均收到实时行情。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
