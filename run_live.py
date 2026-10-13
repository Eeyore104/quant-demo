"""SimNow 仿真常驻进程（v2.0 · docs/16 T2.0-3e）。

用法（项目根目录）：
    uv run python run_live.py --once          # 联调模式：单轮执行后退出（好用小步验证）
    uv run python run_live.py                 # 常驻（按 config 的 dry_run 决定是否真下单）
    uv run python run_live.py --real          # 真实下单二次确认（须 config dry_run=false）
    uv run python run_live.py --dry           # 强制干跑（覆盖 config）

主循环（骨架）：
    事件泵（tick → 账本盯市 + 止损监控；回报 → 订单簿 / 账本）→
    执行时点（每日一次：信号重放 → 目标仓位 → 逐腿风控 → 报单）→
    追单（周期）→ 日终（15:05 后一次：移动止损 + 熔断判定 + 状态落盘）→
    心跳输出（10 分钟一次）。

安全约定：``live.enabled`` 必须为 true；``dry_run=true`` 时只算信号不下单；
真实下单需 config ``dry_run=false`` 且命令行 ``--real`` 双重确认。
断线重连 / 进程守护 / 日报推送属轮 3（T2.0-5），本骨架仅记录断连事件。
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from src.engine.contracts import build_contracts
from src.live.adapter import Executor
from src.live.ctp_api import (
    ActionRejected,
    ActionRsp,
    CtpError,
    CtpSession,
    MdDisconnected,
    MdLogin,
    OrderRejected,
    OrderRtn,
    SettlementConfirmed,
    TdDisconnected,
    Tick,
    TradeRtn,
)
from src.live.ledger import VirtualLedger
from src.live.orders import OrderBook
from src.live.risk_guard import LiveRiskGuard
from src.live.schedule import is_trading_time, parse_hms
from src.live.signals import SignalBridge
from src.utils import logger as logger_mod
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger

log = get_logger("live.run")
PROJECT_ROOT = Path(__file__).resolve().parent


class LiveRuntime:
    """仿真运行器：组装组件 + 事件泵 + 时点调度 + 状态持久化。"""

    def __init__(self, cfg: dict, *, dry_run: bool, only: list[str] | None = None):
        self.cfg = cfg
        self.live_cfg = dict(cfg.get("live") or {})
        self.dry_run = dry_run
        self.only = [str(x) for x in (only or [])]  # 联调限制品种（空 = 全部）

        self.contracts = build_contracts(cfg["portfolio"]["contracts"])
        cmap = dict(self.live_cfg.get("contracts_map") or {})
        self.instrument_map = {str(v): str(k) for k, v in cmap.items()}  # 合约 → 品种
        self.symbols = [str(k) for k in cmap]

        nominal = float(self.live_cfg.get("nominal_capital", 100000))
        self.ledger = VirtualLedger(nominal, self.contracts, dict(self.instrument_map))
        self.guard = LiveRiskGuard(cfg.get("risk") or {}, self.contracts, self.ledger)
        self.book = OrderBook()
        self.session = CtpSession(self._session_cfg())
        self.executor = Executor(self.session, self.book, self.ledger, self.guard, self.live_cfg)
        self.bridge = SignalBridge(cfg, self.contracts, self.ledger)

        self.state_dir = resolve_path("output/live/state")
        self.exec_date = ""  # 最近一次目标执行的自然日
        self.settle_date = ""  # 最近一次日终结算的自然日
        self.got_tick_today = False

    # -------------------------------------------------- 组装

    def _session_cfg(self) -> dict:
        env_key = str(self.live_cfg.get("env", "first"))
        fronts = (self.live_cfg.get("envs") or {}).get(env_key) or {}
        if "md_front" not in fronts or "td_front" not in fronts:
            raise SystemExit(f"live.envs.{env_key} 缺少 md_front / td_front")
        return {
            **self.live_cfg,
            "md_front": fronts["md_front"],
            "td_front": fronts["td_front"],
            "flow_dir": str(resolve_path(self.live_cfg.get("flow_dir", "output/live/ctp_flow"))),
        }

    # -------------------------------------------------- 生命周期

    def start(self) -> None:
        # 状态恢复（重启不丢账 / 不丢风控状态）
        self.ledger.load(self.state_dir / "ledger.json")
        self.guard.load(self.state_dir / "risk.json")
        self.bridge.load(self.state_dir / "signal_state.json")

        self.session.start()
        md = self.session.wait_for(MdLogin, timeout=20)
        if md is None or not md.ok:
            raise SystemExit("行情登录失败（检查账号 / 环境时段）")
        td = self.session.wait_for(SettlementConfirmed, timeout=30)
        if td is None or not td.ok:
            raise SystemExit("交易登录 / 结算确认失败")
        self.session.subscribe(list(self.instrument_map.keys()))
        log.info(
            "仿真就绪：%d 品种（%s）｜ dry_run=%s ｜ 权益 %.2f",
            len(self.symbols),
            "、".join(self.instrument_map.keys()),
            self.dry_run,
            self.ledger.equity,
        )

    def close(self) -> None:
        try:
            self.save_state()
        finally:
            self.session.close()

    def save_state(self) -> None:
        self.ledger.save(self.state_dir / "ledger.json")
        self.guard.save(self.state_dir / "risk.json")
        self.bridge.save(self.state_dir / "signal_state.json")

    # -------------------------------------------------- 事件泵

    def pump(self, seconds: float = 0.5) -> None:
        deadline = time.time() + seconds
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            ev = self.session.next_event(timeout=min(0.2, remain))
            if ev is not None:
                self.dispatch(ev)

    def dispatch(self, ev) -> None:
        if isinstance(ev, Tick):
            symbol = self.instrument_map.get(ev.instrument)
            if symbol and ev.last_price > 0:
                self.got_tick_today = True
                self.ledger.account.mark_price(symbol, ev.last_price)
                self.log_notes(self.executor.on_tick(symbol, ev, date=self.today()))
        elif isinstance(ev, (OrderRtn, TradeRtn, OrderRejected, ActionRsp, ActionRejected)):
            self.log_notes(self.executor.on_event(ev))
        elif isinstance(ev, (MdDisconnected, TdDisconnected)):
            log.warning("连接断开：%s（轮 3 的自动重连将接管）", ev.kind)
        elif isinstance(ev, CtpError) and ev.error_id:
            log.warning("CTP 错误 [%s]：%s %s", ev.source, ev.error_id, ev.error_msg)

    def log_notes(self, notes) -> None:
        for n in notes or []:
            if n.kind in ("stop", "reject", "abandon", "refuse"):
                log.warning("[%s] %s：%s", n.kind, n.symbol, n.detail)
            else:
                log.info("[%s] %s：%s", n.kind, n.symbol, n.detail)

    @staticmethod
    def today() -> str:
        return dt.date.today().strftime("%Y-%m-%d")

    # -------------------------------------------------- 执行与日终

    def execute_round(self) -> None:
        """信号重放 → 目标仓位 → （非干跑时）逐腿执行。"""
        targets = self.bridge.compute()
        if self.only:
            targets = {s: t for s, t in targets.items() if s in self.only}
            log.info("联调限制品种：%s", "、".join(self.only))
        self.guard.prev_close = dict(self.bridge.last_close)  # 涨跌停判定的前收盘依据
        if self.dry_run:
            log.info("dry_run：目标仓位（不下单）——")
            for s, t in targets.items():
                log.info(
                    "  %s：目标 %+d × %d 手（信号 %+d，参考价 %.4f）",
                    s,
                    t.direction,
                    t.lots,
                    t.signal,
                    t.ref_price,
                )
        else:
            self.log_notes(self.executor.execute_target(targets, date=self.today()))
        self.exec_date = self.today()

    def settle_day(self) -> None:
        """日终：移动止损更新 + 熔断判定 + 状态落盘。"""
        res = self.guard.on_day_close(self.today(), atr_map=self.bridge.last_atr)
        log.info(
            "日终结算：权益 %.2f ｜ 熔断状态 %s ｜ 事件 %d 条",
            self.ledger.equity,
            res["fuse_state"],
            len(res["events"]),
        )
        self.save_state()
        self.settle_date = self.today()

    # -------------------------------------------------- 主循环

    def run_forever(self) -> None:
        exec_t = parse_hms(str((self.live_cfg.get("execute") or {}).get("time", "21:00:05")))
        last_chase = 0.0
        last_beat = 0.0
        while True:
            self.pump(0.5)
            now = dt.datetime.now()
            t = now.time()

            # 执行时点（每日一次；须处于交易时段）
            if (
                self.exec_date != self.today()
                and t >= exec_t
                and is_trading_time(now, self.symbols)
            ):
                log.info("到达执行时点 %s，开始执行目标仓位……", exec_t)
                try:
                    self.execute_round()
                except Exception as e:  # noqa: BLE001 - 常驻进程需容错并继续
                    log.exception("目标执行失败：%s", e)

            # 追单（每 10 秒）
            if time.time() - last_chase >= 10:
                self.log_notes(self.executor.process_chase(date=self.today()))
                last_chase = time.time()

            # 日终（15:05-21:00 窗口内每日一次；当日需有行情）
            if (
                self.settle_date != self.today()
                and dt.time(15, 5) <= t < dt.time(21, 0)
                and self.got_tick_today
            ):
                try:
                    self.settle_day()
                except Exception as e:  # noqa: BLE001
                    log.exception("日终结算失败：%s", e)

            # 心跳（每 10 分钟）
            if time.time() - last_beat >= 600:
                log.info(
                    "运行中：权益 %.2f ｜ 持仓 %s ｜ 挂单 %d ｜ 熔断 %s",
                    self.ledger.equity,
                    self.positions_brief(),
                    len(self.book.active_orders()),
                    self.guard.fuse_state(),
                )
                last_beat = time.time()

    def run_once(self, wait_seconds: float = 20.0) -> None:
        """联调模式：启动 → （等行情）→ 执行一轮 → 等回报 → 输出 → 退出。"""
        self.start()
        try:
            log.info("联调模式：等待行情与回报 ……")
            self.pump(3)
            self.execute_round()
            self.pump(wait_seconds)
            self.log_notes(self.executor.process_chase(date=self.today()))
            self.pump(3)
            self.print_status()
        finally:
            self.close()

    # -------------------------------------------------- 输出

    def positions_brief(self) -> str:
        parts = []
        for s in self.symbols:
            n = self.ledger.net_size(s)
            if n:
                parts.append(f"{s}{n:+d}")
        return " ".join(parts) or "空仓"

    def print_status(self) -> None:
        log.info("=" * 56)
        log.info(
            "账本：权益 %.2f ｜ 可用 %.2f ｜ 保证金 %.2f ｜ 手续费累计 %.2f",
            self.ledger.equity,
            self.ledger.available,
            self.ledger.margin_used,
            self.ledger.total_commission,
        )
        log.info("持仓：%s", self.positions_brief())
        log.info("订单：")
        for o in self.book.orders.values():
            log.info("  %s", o.describe())
        log.info("=" * 56)


def main() -> int:
    parser = argparse.ArgumentParser(description="SimNow 仿真常驻进程（v2.0）")
    parser.add_argument("--once", action="store_true", help="联调模式：单轮执行后退出")
    parser.add_argument("--wait", type=float, default=20.0, help="联调模式下等待回报秒数")
    parser.add_argument(
        "--only", action="append", default=None, help="联调限制品种（可多次，如 --only M0）"
    )
    parser.add_argument(
        "--real", action="store_true", help="真实下单二次确认（须 config dry_run=false）"
    )
    parser.add_argument("--dry", action="store_true", help="强制干跑（不下单）")
    args = parser.parse_args()

    cfg = load_config()
    live_cfg = dict(cfg.get("live") or {})
    if not bool(live_cfg.get("enabled", False)):
        print("live.enabled=false——请先将 config.yaml 的 live.enabled 设为 true")
        return 1

    cfg_dry = bool(live_cfg.get("dry_run", True))
    if args.dry or cfg_dry:
        dry_run = True  # 干跑：--dry 强制，或 config 默认
    else:
        # config 明确要求真实下单（dry_run=false）→ 需 --real 二次确认
        if not args.real:
            print("config dry_run=false（真实下单模式）。二次确认：请加 --real 参数显式允许")
            return 1
        dry_run = False

    logger_mod.enable_file_logging(PROJECT_ROOT / "logs" / "live")
    log.info(
        "SimNow 仿真启动：环境 %s ｜ dry_run=%s ｜ 模式 %s",
        live_cfg.get("env"),
        dry_run,
        "联调单轮" if args.once else "常驻",
    )
    rt = LiveRuntime(cfg, dry_run=dry_run, only=args.only)
    try:
        if args.once:
            rt.run_once(wait_seconds=args.wait)
        else:
            rt.run_forever()
    except KeyboardInterrupt:
        log.info("收到中断，保存状态并退出 ……")
        rt.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
