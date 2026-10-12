"""SimNow 连接探针（v2.0 · docs/16 T2.0-1b）：分步自检全流程。

用法（项目根目录）：
    uv run python scripts/probe_simnow.py --env 724              # 7×24 环境（研发）
    uv run python scripts/probe_simnow.py --env first            # 第一套（盘中）
    uv run python scripts/probe_simnow.py --contract m2701       # 指定真实合约（默认取映射表 M0）
    uv run python scripts/probe_simnow.py --no-order             # 跳过下单冒烟

步骤（行情线与交易线独立，互不阻塞诊断）：
    [1] 配置检查（账号 / 前置 / 合约）
    [2] 行情连接 + 登录          [3] 订阅合约 + 收 tick
    [4] 交易连接（认证 → 登录 → 结算确认）
    [5] 查询资金                 [6] 查询持仓
    [7] 下单冒烟（跌停价挂单 → 撤单；默认执行，--no-order 跳过）
    [8] 退出清理

安全约定：挂单价格取「跌停价」（离成交价最远、绝不可能成交）；报单后立即撤单。
退出码：0 = 全部通过；1 = 有失败步骤。
"""

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live.ctp_api import (
    DIR_BUY,
    OFFSET_OPEN,
    AccountRsp,
    ActionRsp,
    CtpError,
    CtpSession,
    MdConnected,
    MdLogin,
    OrderRejected,
    OrderRtn,
    PositionRsp,
    QueryDone,
    SettlementConfirmed,
    TdAuth,
    TdConnected,
    TdDisconnected,
    TdLogin,
    Tick,
)
from src.utils.config_loader import load_config, resolve_path


class Probe:
    """分步结果收集器。"""

    def __init__(self):
        self.results: list[tuple[str, bool, str, float]] = []

    def step(self, title: str, ok: bool, detail: str, t0: float) -> bool:
        elapsed = time.time() - t0
        self.results.append((title, ok, detail, elapsed))
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}] {title}（{elapsed:.1f}s）  {detail}")
        return ok

    def summary(self) -> bool:
        passed = sum(1 for _, ok, _, _ in self.results if ok)
        total = len(self.results)
        print()
        print("=" * 68)
        print(f"探针结果：{passed}/{total} 步通过")
        for title, ok, _, _ in self.results:
            if not ok:
                print(f"  未通过：{title}")
        print("=" * 68)
        return passed == total and total > 0


def mask(account: str) -> str:
    if len(account) <= 3:
        return account[:1] + "**"
    return account[:3] + "**" + account[-1:]


def run_md(probe: Probe, sess, contracts: list[str], timeout: float) -> bool:
    """行情线：连接 → 登录 → 订阅 → 收 tick。"""
    t0 = time.time()
    ev = sess.wait_for(MdConnected, timeout=timeout)
    if ev is None:
        probe.step("行情连接", False, f"{timeout:.0f}s 内未连接（前置不可达？环境时段？）", t0)
        return False
    probe.step("行情连接", True, f"已连接 {ev.front}", t0)

    t0 = time.time()
    ev = sess.wait_for(MdLogin, timeout=timeout)
    if ev is None or not ev.ok:
        msg = "超时" if ev is None else f"错误 {ev.error_id}: {ev.error_msg}"
        probe.step("行情登录", False, msg, t0)
        return False
    probe.step("行情登录", True, f"交易日 {ev.trading_day}", t0)

    t0 = time.time()
    sess.subscribe(contracts)
    tick = sess.wait_for(Tick, timeout=timeout, pred=lambda e: e.instrument in contracts)
    if tick is None:
        msg = f"{timeout:.0f}s 内未收到 tick（非交易时段？合约代码？）"
        probe.step("订阅收行情", False, msg, t0)
        return False
    probe.step(
        "订阅收行情",
        True,
        f"{tick.instrument} 最新 {tick.last_price}（开 {tick.open_price} / "
        f"涨停 {tick.upper_limit} / 跌停 {tick.lower_limit}）",
        t0,
    )
    return True


def run_td(probe: Probe, sess, timeout: float) -> bool:
    """交易线：连接 → 认证 → 登录 → 结算确认 → 查询资金 / 持仓。"""
    t0 = time.time()
    ev = sess.wait_for(TdConnected, timeout=timeout)
    if ev is None:
        probe.step("交易连接", False, f"{timeout:.0f}s 内未连接", t0)
        return False
    probe.step("交易连接", True, f"已连接 {ev.front}", t0)

    t0 = time.time()
    ev = sess.wait_for(TdAuth, timeout=timeout)
    if ev is None or not ev.ok:
        msg = "超时" if ev is None else f"错误 {ev.error_id}: {ev.error_msg}"
        probe.step("交易认证", False, msg, t0)
        return False
    probe.step("交易认证", True, "AppID/AuthCode 有效", t0)

    t0 = time.time()
    ev = sess.wait_for(TdLogin, timeout=timeout)
    if ev is None or not ev.ok:
        msg = "超时" if ev is None else f"错误 {ev.error_id}: {ev.error_msg}"
        probe.step("交易登录", False, msg, t0)
        return False
    probe.step(
        "交易登录",
        True,
        f"交易日 {ev.trading_day}（FrontID={ev.front_id} SessionID={ev.session_id}）",
        t0,
    )

    t0 = time.time()
    ev = sess.wait_for(SettlementConfirmed, timeout=timeout)
    if ev is None or not ev.ok:
        msg = "超时" if ev is None else f"错误 {ev.error_id}: {ev.error_msg}"
        probe.step("结算确认", False, msg, t0)
        return False
    probe.step("结算确认", True, "交易接口就绪", t0)

    # 资金查询
    t0 = time.time()
    sess.query_account()
    acc = sess.wait_for(AccountRsp, timeout=timeout)
    sess.wait_for(QueryDone, timeout=5)
    if acc is None:
        probe.step("查询资金", False, "未收到资金响应", t0)
        return False
    probe.step(
        "查询资金",
        True,
        f"权益 {acc.balance:,.0f} / 可用 {acc.available:,.0f} / 保证金 {acc.curr_margin:,.0f}",
        t0,
    )

    # 持仓查询
    t0 = time.time()
    sess.query_position()
    positions: list[PositionRsp] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = sess.wait_for((PositionRsp, QueryDone), timeout=2)
        if isinstance(ev, PositionRsp):
            positions.append(ev)
        elif isinstance(ev, QueryDone) and ev.query == "position":
            break
    held = [p for p in positions if p.position > 0]
    detail = f"{len(held)} 个持仓" if held else "无持仓（空仓）"
    if held:
        detail += "：" + "；".join(
            f"{p.instrument} {p.direction_text}{p.position}手" for p in held[:4]
        )
    probe.step("查询持仓", True, detail, t0)
    return True


def run_order(probe: Probe, sess, contract: str, timeout: float) -> bool:
    """下单冒烟：跌停价买开 1 手 → 等回报 → 撤单 → 等撤单回报。"""
    tick = sess.wait_for(Tick, timeout=timeout, pred=lambda e: e.instrument == contract)
    if tick is None:
        probe.step("下单冒烟", False, f"未取到 {contract} 行情，跳过", time.time())
        return False

    price = tick.lower_limit if tick.lower_limit > 0 else round(tick.last_price * 0.9, 1)
    t0 = time.time()
    order_ref = sess.order_insert(contract, DIR_BUY, OFFSET_OPEN, price, 1)
    print(f"      已报单：买开 {contract} 1 手 @{price}（跌停价，不会成交）OrderRef={order_ref}")

    # 等本单回报（可能先 OrderRejected）
    rejected = None
    rtn = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = sess.wait_for(
            (OrderRtn, OrderRejected), timeout=2, pred=lambda e: e.order_ref == order_ref
        )
        if isinstance(ev, OrderRejected):
            rejected = ev
            break
        if isinstance(ev, OrderRtn):
            rtn = ev
            break
    if rejected is not None:
        probe.step("下单受理", False, f"被拒 {rejected.error_id}: {rejected.error_msg}", t0)
        return False
    if rtn is None:
        probe.step("下单受理", False, "未收到订单回报", t0)
        return False
    probe.step("下单受理", True, f"状态：{rtn.status_text}", t0)

    # 撤单
    t0 = time.time()
    time.sleep(1.0)  # 等柜台侧处理
    sess.order_action(order_ref, contract, order_sys_id=rtn.order_sys_id)
    action_ok, canceled = False, False
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = sess.wait_for(
            (ActionRsp, OrderRtn), timeout=2, pred=lambda e: e.order_ref == order_ref
        )
        if isinstance(ev, ActionRsp) and ev.ok:
            action_ok = True
        if isinstance(ev, OrderRtn) and ev.status == "5":
            canceled = True
        if action_ok or canceled:
            break
    ok = action_ok or canceled
    probe.step("撤单", ok, "撤单成功" if ok else "撤单未确认", t0)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="SimNow 连接探针（分步自检）")
    parser.add_argument("--env", choices=["first", "724"], default=None, help="环境（默认取配置）")
    parser.add_argument(
        "--contract",
        action="append",
        default=None,
        help="真实合约代码（可多次；默认取 live.contracts_map 的 M0）",
    )
    parser.add_argument("--no-order", action="store_true", help="跳过下单冒烟")
    parser.add_argument("--timeout", type=float, default=20.0, help="单步超时秒数（默认 20）")
    args = parser.parse_args()

    cfg = load_config()
    live = dict(cfg.get("live") or {})

    # ---------- [1] 配置检查 ----------
    t0 = time.time()
    env_key = args.env or str(live.get("env", "first"))
    envs = live.get("envs") or {}
    if env_key not in envs:
        print(f"[1] 配置检查失败：config.yaml 的 live.envs 缺少环境「{env_key}」")
        return 1
    fronts = envs[env_key]
    investor = str(live.get("investor_id") or "").strip()
    password = str(live.get("password") or "").strip()
    if not investor or not password:
        print("[1] 配置检查失败：缺少 SimNow 账号。")
        print()
        print("  请在 config/config.local.yaml 中填写（该文件不进 git）：")
        print("    live:")
        print('      investor_id: "你的投资者代码（6 位）"')
        print('      password: "你的登录密码"')
        print()
        print("  模板见 config/config.local.yaml.example；填好后重新运行本脚本。")
        return 1
    contracts = args.contract
    if not contracts:
        cmap = live.get("contracts_map") or {}
        default = cmap.get("M0") or (next(iter(cmap.values())) if cmap else "")
        if not default:
            print("[1] 配置检查失败：未指定 --contract，且 config 的 live.contracts_map 为空。")
            print("  示例：uv run python scripts/probe_simnow.py --contract m2701")
            return 1
        contracts = [str(default)]

    probe = Probe()
    probe.step(
        "配置检查",
        True,
        f"环境 {env_key}｜账号 {mask(investor)}｜合约 {'、'.join(contracts)}",
        t0,
    )

    live_cfg = {
        **live,
        "md_front": fronts["md_front"],
        "td_front": fronts["td_front"],
        "flow_dir": str(resolve_path(live.get("flow_dir", "output/live/ctp_flow"))),
    }

    sess = CtpSession(live_cfg)
    print(f"\n开始探针：环境 {env_key}（{fronts['td_front']} / {fronts['md_front']}）\n")
    try:
        sess.start()
        run_md(probe, sess, contracts, args.timeout)
        td_ok = run_td(probe, sess, args.timeout)
        if args.no_order:
            probe.step("下单冒烟", True, "已跳过（--no-order）", time.time())
        elif not td_ok:
            probe.step("下单冒烟", False, "交易线未就绪，跳过", time.time())
        else:
            run_order(probe, sess, contracts[0], args.timeout)
        # 非阻塞看一眼两侧是否有断连 / 错误事件
        for ev in sess.drain():
            if isinstance(ev, (TdDisconnected, CtpError)):
                print(f"  [提示] 事件：{ev}")
    except KeyboardInterrupt:
        print("\n  [中断] 用户终止")
    finally:
        t0 = time.time()
        sess.close()
        probe.step("退出清理", True, "接口已释放", t0)

    ok = probe.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
