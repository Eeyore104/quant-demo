"""SimNow 仿真常驻进程（v2.0 · docs/16 T2.0-3e）。

用法（项目根目录）：
    uv run python run_live.py --once          # 联调模式：单轮执行后退出（好用小步验证）
    uv run python run_live.py                 # 常驻（按 config 的 dry_run 决定是否真下单）
    uv run python run_live.py --real          # 真实下单二次确认（须 config dry_run=false）
    uv run python run_live.py --dry           # 强制干跑（覆盖 config）

主循环（骨架）：
    事件泵（tick → 账本盯市 + 止损监控；回报 → 订单簿 / 账本）→
    目标执行（**每天首次进入交易时段即执行**：夜盘 21:00 在线即 21:00 执行；
    夜间离线的运行模式下由次日日盘开盘后自动补执行）→
    追单（周期）→ 日终（15:05 后一次：移动止损 + 熔断判定 + 对账 + 日报 + 状态落盘）→
    行情心跳监控（交易时段内超时告警）。

    运行窗口说明：夜盘（21:00-23:00）离线时——① 目标执行顺延至次日日盘补执行；
    ② 该时段止损监控缺失，相关偏差在验收报告如实记录。

安全约定：``live.enabled`` 必须为 true；``dry_run=true`` 时只算信号不下单；
真实下单需 config ``dry_run=false`` 且命令行 ``--real`` 双重确认。
断线重连由 CTP 内置重连 + 回调链重登接管；进程守护由任务计划（--install --live）负责。
"""

import argparse
import datetime as dt
import socket
import sys
import time
from pathlib import Path

from src.data import store
from src.engine.contracts import build_contracts
from src.live import roll as roll_mod
from src.live.adapter import ExecNote, Executor
from src.live.alert import alert
from src.live.ctp_api import (
    ActionRejected,
    ActionRsp,
    CtpError,
    CtpSession,
    MdConnected,
    MdDisconnected,
    MdLogin,
    OrderQryRsp,
    OrderRejected,
    OrderRtn,
    QueryDone,
    SettlementConfirmed,
    TdAuth,
    TdConnected,
    TdDisconnected,
    TdLogin,
    Tick,
    TradeRtn,
)
from src.live.journal import Journal, build_daily_report, push_report, save_report
from src.live.ledger import VirtualLedger
from src.live.lifecycle import LifecycleBook
from src.live.orders import OrderBook
from src.live.reconcile import query_counter, reconcile_all
from src.live.risk_guard import LiveRiskGuard
from src.live.schedule import is_trading_time
from src.live.signals import SignalBridge
from src.results import db as results_db
from src.utils import logger as logger_mod
from src.utils.config_loader import load_config, resolve_path
from src.utils.logger import get_logger

log = get_logger("live.run")
PROJECT_ROOT = Path(__file__).resolve().parent


def prev_workday(today: dt.date | None = None) -> dt.date:
    """最近一个工作日（不含今天；仅跳过周末，节假日由数据事实自然兜底）。"""
    d = (today or dt.date.today()) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def parse_force_targets(text: str, symbols: list[str]) -> dict[str, int]:
    """解析联调参数 ``M0=+1,RB0=-2`` → {品种: 带符号手数}（v2.1 · 仅 --once 使用）。"""
    out: dict[str, int] = {}
    allowed = {s.upper() for s in symbols}
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        sym, sep, raw = part.partition("=")
        if not sep:
            raise ValueError(f"格式应为「品种=手数」，如 M0=+1：{part}")
        sym = sym.strip().upper()
        if sym not in allowed:
            raise ValueError(f"品种 {sym} 不在 live.contracts_map 中")
        try:
            out[sym] = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"手数须为整数：{part}") from exc
    return out


def advance_day(ledger, guard) -> None:
    """日终状态推进（在结算与日报之后调用，顺序与回测主循环一致）。

    ① 交易日序 +1（止损 ``entry_row`` 的 armed 基准）；
    ② 次日「开盘前权益」= 当前权益（日报 ``day_pnl`` 的单日基准）；
    ③ 清「当日止损」标记（否则一次止损后该品种永久禁止开仓）。
    """
    ledger.day_idx += 1
    ledger.day_ref_equity = ledger.equity
    guard.new_day()


def acquire_single_instance(port: int = 49517) -> socket.socket | None:
    """单实例互斥：绑定本地回环端口，已被占用（已有实例）则返回 None。

    用于兜底「任务多重触发 / 进程孤儿」场景——无论上游如何重复启动，
    同一时间只允许一个 run_live 实例。进程退出（含被强杀）时操作系统自动
    释放端口，不存在「僵尸持锁」问题。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


class LiveRuntime:
    """仿真运行器：组装组件 + 事件泵 + 时点调度 + 状态持久化。"""

    def __init__(
        self,
        cfg: dict,
        *,
        dry_run: bool,
        only: list[str] | None = None,
        force_targets: dict[str, int] | None = None,
    ):
        self.cfg = cfg
        self.live_cfg = dict(cfg.get("live") or {})
        self.dry_run = dry_run
        self.only = [str(x) for x in (only or [])]  # 联调限制品种（空 = 全部）
        self.force_targets = dict(force_targets or {})  # 联调覆盖目标仓位（仅 --once）

        self.contracts = build_contracts(cfg["portfolio"]["contracts"])
        cmap = dict(self.live_cfg.get("contracts_map") or {})
        self.instrument_map = {str(v): str(k) for k, v in cmap.items()}  # 合约 → 品种
        self.symbols = [str(k) for k in cmap]

        nominal = float(self.live_cfg.get("nominal_capital", 100000))
        self.ledger = VirtualLedger(nominal, self.contracts, dict(self.instrument_map))
        self.guard = LiveRiskGuard(cfg.get("risk") or {}, self.contracts, self.ledger)
        self.book = OrderBook()
        self.session = CtpSession(self._session_cfg())
        # v2.2 合约生命周期（到期闸 / 强平）；None 之外的情况都按台账判定
        self.lifecycle = LifecycleBook.from_config(self.live_cfg, cmap)
        self._life_alerted: set[str] = set()  # 「品种:日期:状态」已告警（避免刷屏）
        self.executor = Executor(
            self.session, self.book, self.ledger, self.guard, self.live_cfg,
            lifecycle=self.lifecycle,
        )
        self.bridge = SignalBridge(cfg, self.contracts, self.ledger)

        self.state_dir = resolve_path("output/live/state")
        self.plan_dir = resolve_path("output/live/plans")  # v2.2 换月计划目录
        self.exec_date = ""  # 最近一次目标执行的自然日
        self.settle_date = ""  # 最近一次日终结算的自然日
        self.tick_date_today = ""  # 最近收到行情 tick 的自然日（判「今日行情已就绪」）
        self.last_tick_ts = 0.0  # 行情心跳监控
        self._gap_reported = False  # 心跳失联是否已告警（每次失联只报一条 + 恢复一条）
        self._conn_down_since = 0.0  # 连接失联起始时刻（健康监控）
        self._was_disconnected = False  # 是否经历过断线（判定「链路恢复」用）
        self._trading_prev = False  # 上一轮是否处于交易时段（识别时段起点）
        self._td_wait_reported = False  # 「交易未登录、暂缓执行」是否已告警

        # 结果库（storage 开关；关闭则仅文件事件流）
        storage_cfg = cfg.get("storage") or {}
        self.conn = (
            results_db.open_db(resolve_path(storage_cfg.get("db_path", "output/db/quant.sqlite3")))
            if storage_cfg.get("enabled", True)
            else None
        )
        self.session_id = 0
        self.journal: Journal | None = None
        self._journaled_orders: set[str] = set()

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
        orphans = self._clear_orphan_orders()  # v2.2 R11：先清孤儿单，再订阅行情
        self.session.subscribe(list(self.instrument_map.keys()))
        # 会话登记 + 事件流
        if self.conn is not None:
            self.session_id = results_db.record_live_session(
                self.conn,
                env=str(self.live_cfg.get("env", "first")),
                mode=("dry_run" if self.dry_run else "live"),
                meta={"symbols": self.symbols, "only": self.only},
            )
        self.journal = Journal(
            resolve_path("output/live/journal"), conn=self.conn, session_id=self.session_id
        )
        self.journal.log(
            "connect", detail=f"启动：env={self.live_cfg.get('env')} dry_run={self.dry_run}"
        )
        for line in orphans:
            self.journal.log("orphan_cancel", detail=line)
            alert("orphan_cancel", line, journal=self.journal)
        self._check_lifecycle(startup=True)  # v2.2：到期巡检（启动即知，早于补建）
        self._backfill_stops()  # v2.1：持仓有、状态无 → 补建（立即受保护）
        self._warn_settle_missed()  # v2.1：漏结算告警（不自动补做，避免跨日口径静默失真）
        log.info(
            "仿真就绪：%d 品种（%s）｜ dry_run=%s ｜ 权益 %.2f",
            len(self.symbols),
            "、".join(self.instrument_map.keys()),
            self.dry_run,
            self.ledger.equity,
        )

    def close(self) -> None:
        try:
            self.drain_orders()  # v2.2 R8：退出前撤在途委托（先收尾再存盘）
            self.save_state()
            if self.conn is not None and self.session_id:
                results_db.close_live_session(self.conn, self.session_id)
        finally:
            self.session.close()

    def save_state(self) -> None:
        self.ledger.save(self.state_dir / "ledger.json")
        self.guard.save(self.state_dir / "risk.json")
        self.bridge.save(self.state_dir / "signal_state.json")

    # -------------------------------------------------- v2.1 状态补建

    def _backfill_stops(self) -> None:
        """启动补建：持仓在手但止损状态缺失 → 按账本均价 + 最近 ATR 重建。

        ``row_idx = day_idx - 1`` 把既有持仓视为「昨日入场」→ 补建后**立即受保护**；
        若价格已越过止损位，下一 tick 即平仓——这是正确暴露，记 ``stop_backfill`` 事件。
        """
        self.guard.atr_hint = dict(self.bridge.last_atr)
        date = self.today()
        for symbol in self.symbols:
            if self.ledger.net_size(symbol) == 0 or self.guard.stop_state(symbol) is not None:
                continue
            for ev in self.guard.sync_stops(
                symbol, row_idx=self.ledger.day_idx - 1, date=date
            ):
                log.warning("启动补建：%s", ev.describe())
                if self.journal is not None:
                    self.journal.log("stop_backfill", symbol=symbol, detail=ev.detail)

    def _warn_settle_missed(self) -> None:
        """上次结算落后于上一工作日 → 记事件（day_pnl / 冷却天数按跨日口径）。"""
        last = self.ledger.last_settle_date
        if not last:
            return
        need = prev_workday().isoformat()
        if last < need:
            msg = f"上次结算 {last} 落后于上一工作日 {need}（当日盈亏按跨日口径）"
            log.warning("漏结算：%s", msg)
            if self.journal is not None:
                self.journal.log("settle_missed", detail=msg)

    # -------------------------------------------------- v2.2 到期闸

    def _check_lifecycle(self, *, startup: bool = False) -> None:
        """到期状态巡检：warn / force_close / unknown 每品种每日各记一次事件。

        ``startup=True`` 时额外打印一行台账摘要（便于从日志判断「今天系统怎么看合约」）。
        """
        if startup:
            log.info(
                "生命周期台账：%d 个合约已登记（%s）",
                len(self.lifecycle.items),
                "、".join(f"{s}={st}" for s, st in self.lifecycle.states().items()),
            )
        today = self.today()
        for symbol in self.symbols:
            state = self.lifecycle.state(symbol)
            if state == "ok":
                continue
            key = f"{symbol}:{today}:{state}"
            if key in self._life_alerted:
                continue
            self._life_alerted.add(key)
            detail = self.lifecycle.describe(symbol)
            if state == "force_close":
                log.error("到期强制平仓窗口：%s", detail)
            elif state == "warn":
                log.warning("合约临近到期（禁止新开）：%s", detail)
            else:
                log.error("合约未登记生命周期（禁止开仓）：%s", detail)
            if self.journal is not None:
                self.journal.log(f"expiry_{state}", symbol=symbol, detail=detail)
            alert(f"expiry_{state}", detail, symbol=symbol, journal=self.journal)

    def _force_close_expiring(self) -> None:
        """``force_close`` 状态且仍有持仓 → 强制平仓（只平不开，每小时重试）。"""
        for symbol in self.lifecycle.force_close_symbols():
            if self.ledger.net_size(symbol) == 0:
                continue
            detail = self.lifecycle.describe(symbol)
            if self.dry_run:
                log.warning("dry_run：应到期强平但不下单（%s）", detail)
                continue
            notes = self.executor.force_flat(symbol, date=self.today())
            if notes:
                self.log_notes(notes)
                log.error(
                    "到期强平：%s → %s",
                    detail,
                    "；".join(n.detail for n in notes),
                )
            else:
                log.error("到期强平未产生委托（检查行情 / 合约映射）：%s", detail)

    # -------------------------------------------------- v2.2 启动清理 / 退出收尾

    def _clear_orphan_orders(self, timeout: float = 8.0) -> list[str]:
        """启动清理：柜台当日未终态委托全部撤掉（R11）。

        判据：本地订单簿是内存态，重启后为空 →「柜台有、本地无」= 孤儿。
        CTP 只返回当日委托，跨日单已随日终失效，无需处理历史。
        调用时点：交易登录之后、订阅行情之前（此时队列里只有查询响应，不会丢 tick）。
        """
        if self.dry_run:
            return []
        try:
            self.session.query_order()
        except Exception as e:  # noqa: BLE001 - 清理失败不阻塞启动
            log.warning("委托查询失败（跳过孤儿清理）：%s", e)
            return []

        deadline = time.time() + timeout
        found: list[OrderQryRsp] = []
        while time.time() < deadline:
            ev = self.session.next_event(timeout=0.3)
            if ev is None:
                continue
            if isinstance(ev, OrderQryRsp):
                found.append(ev)
            elif isinstance(ev, QueryDone) and ev.query == "order":
                break
            elif isinstance(ev, CtpError):
                log.warning("委托查询错误：%s %s", ev.error_id, ev.error_msg)

        active = [o for o in found if o.is_active]
        lines: list[str] = []
        for o in active:
            try:
                self.session.order_action(o.order_ref, o.instrument, o.order_sys_id)
                lines.append(f"撤销孤儿委托：{o.describe()}")
                log.warning("撤销孤儿委托：%s", o.describe())
            except Exception as e:  # noqa: BLE001 - 单笔失败不阻塞
                log.warning("撤单失败（%s）：%s", o.order_ref, e)
        if not active:
            log.info("启动检查：柜台无未终态委托（当日 %d 笔记录）", len(found))
        return lines

    def drain_orders(self, timeout: float = 15.0) -> list[ExecNote]:
        """退出前处置在途委托（R8）：全撤 → 等终态（超时则告警）。"""
        try:
            active = self.book.active_orders()
        except Exception:  # noqa: BLE001 - 退出路径不因清理失败而卡住
            return []
        if not active:
            return []
        log.warning("退出前撤在途委托 %d 笔", len(active))
        for o in active:
            try:
                self.session.order_action(o.order_ref, o.instrument, o.order_sys_id)
            except Exception as e:  # noqa: BLE001
                log.warning("撤单失败（%s）：%s", o.order_ref, e)
        deadline = time.time() + timeout
        while time.time() < deadline and self.book.active_orders():
            try:
                self.pump(0.5)
            except Exception:  # noqa: BLE001
                break
        left = self.book.active_orders()
        notes = [
            ExecNote("exit_pending", o.symbol, f"退出仍在途：{o.describe()}", o.order_ref)
            for o in left
        ]
        for n in notes:
            log.error("%s", n.detail)
        return notes

    # -------------------------------------------------- v2.2 移仓执行

    def _maybe_execute_roll(self) -> None:
        """当日已确认、未执行的移仓计划 → 执行（每日最多一次，执行后打标）。"""
        plan = roll_mod.latest_plan(self.plan_dir)
        if plan is None or not plan.confirmed or plan.executed_at:
            return
        if plan.date != self.today():
            log.warning("移仓计划 %s 不是当日的（%s），不执行", plan.plan_id, plan.date)
            return
        if self.dry_run:
            log.warning("dry_run：移仓计划 %s 已确认，但本次不下单", plan.plan_id)
            return
        self._execute_roll(plan)

    def _wait_flat(self, symbol: str, timeout: float = 60.0) -> bool:
        """等平旧仓完成（净持仓归零）；无在途委托且约 2 秒不动 → 判失败。"""
        deadline = time.time() + timeout
        idle = 0
        while time.time() < deadline:
            if self.ledger.net_size(symbol) == 0:
                return True
            if self.book.active_orders(symbol):
                idle = 0
            else:
                idle += 1
                if idle >= 4:
                    break
            self.pump(0.5)
        return self.ledger.net_size(symbol) == 0

    def _wait_position(self, symbol: str, target: int, timeout: float = 60.0) -> bool:
        """等开新仓到位（净持仓 == target）。"""
        deadline = time.time() + timeout
        idle = 0
        while time.time() < deadline:
            if self.ledger.net_size(symbol) == target:
                return True
            if self.book.active_orders(symbol):
                idle = 0
            else:
                idle += 1
                if idle >= 4:
                    break
            self.pump(0.5)
        return self.ledger.net_size(symbol) == target

    def _execute_roll(self, plan) -> None:
        """逐项执行移仓：平旧 → 等归零 → 登记新合约 → 开新 → 重挂防护 → 迁移收尾。"""
        log.info("开始执行移仓计划 %s（%d 项）", plan.plan_id, len(plan.items))
        ok: list[str] = []
        failed: list[str] = []
        for item in plan.items:
            symbol = item.symbol
            try:
                if symbol not in self.symbols:
                    failed.append(f"{symbol} 不在品种池")
                    continue
                if self.lifecycle.contract_of(item.to_instrument) is None:
                    failed.append(f"{symbol} 目标合约 {item.to_instrument} 未登记生命周期")
                    continue
                if item.direction == "LONG":
                    to_target = 1
                elif item.direction == "SHORT":
                    to_target = -1
                else:
                    to_target = 0
                old_instrument = self.ledger.symbol_to_instrument.get(symbol, "")
                old_avg = float(self.ledger.state(symbol).avg_price)
                if item.lots > 0:
                    self.log_notes(self.executor.force_flat(symbol, date=self.today()))
                    if not self._wait_flat(symbol):
                        pos = self.ledger.net_size(symbol)
                        failed.append(f"{symbol} 平旧未完成（净持仓 {pos}）")
                        continue
                self.ledger.register_instrument(symbol, item.to_instrument)
                self.lifecycle.override(symbol, item.to_instrument)
                if item.lots > 0:
                    self.log_notes(
                        self.executor.open_for_roll(
                            symbol, item.direction, item.lots, date=self.today()
                        )
                    )
                    want = to_target * item.lots
                    if not self._wait_position(symbol, want):
                        pos = self.ledger.net_size(symbol)
                        failed.append(f"{symbol} 开新未完成（净持仓 {pos}）")
                        if self.journal is not None:
                            self.journal.log("roll_failed", symbol=symbol,
                                             detail=f"开新未到位，目标 {want}")
                        continue
                    ev = self.guard.rearm_stop(symbol, date=self.today())
                    if ev is not None:
                        self.log_notes([ExecNote("risk", symbol, ev.detail)])
                volume = abs(self.ledger.net_size(symbol))
                rec = self.ledger.commit_roll(
                    symbol,
                    old_instrument=old_instrument,
                    new_instrument=item.to_instrument,
                    old_avg_price=old_avg,
                    volume=volume,
                    direction=item.direction or "LONG",
                    trading_day=self.today(),
                )
                ok.append(
                    f"{symbol} {old_instrument}→{item.to_instrument} "
                    f"价差成本 {rec['roll_cost']:+.2f}"
                )
                if self.journal is not None:
                    self.journal.log(
                        "roll", symbol=symbol,
                        detail=(
                            f"{rec['from']} → {rec['to']}（{volume} 手，"
                            f"价差 {rec['roll_cost']:+.2f}）"
                        ),
                    )
            except Exception as e:  # noqa: BLE001 - 单项失败不拖垮其余品种
                log.exception("移仓失败（%s）：%s", symbol, e)
                failed.append(f"{symbol} 异常：{e}")
        plan.executed_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        roll_mod.save_plan(plan, self.plan_dir)
        log.info("移仓计划 %s 执行结束：成功 %d 项，失败 %d 项", plan.plan_id, len(ok), len(failed))
        for line in ok:
            log.info("  ✓ %s", line)
        for line in failed:
            log.error("  ✗ %s", line)
            alert("roll_failed", line, journal=self.journal)
        self.save_state()

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
                self.tick_date_today = self.today()
                self.last_tick_ts = time.time()
                self.ledger.account.mark_price(symbol, ev.last_price)
                self.log_notes(self.executor.on_tick(symbol, ev, date=self.today()))
        elif isinstance(ev, (OrderRtn, TradeRtn, OrderRejected, ActionRsp, ActionRejected)):
            self.log_notes(self.executor.on_event(ev))
            self._journal_trade_event(ev)
            if isinstance(ev, TradeRtn):
                self.save_state()  # 成交即时落盘（防意外关机 / 断电丢账）
        elif isinstance(ev, (MdDisconnected, TdDisconnected)):
            log.warning("连接断开：%s（CTP 内置重连 + 回调链重登将自动恢复）", ev.kind)
            self._was_disconnected = True
            if self.journal is not None:
                self.journal.log(
                    "disconnect", detail=f"{ev.kind} reason={getattr(ev, 'reason', '')}"
                )
        elif isinstance(ev, (MdConnected, TdConnected)):
            log.info("前置已连接：%s（等待登录完成）", ev.kind)
        elif isinstance(ev, (MdLogin, TdLogin, TdAuth)):
            if getattr(ev, "ok", False):
                log.info("登录成功：%s（交易日 %s）", ev.kind, getattr(ev, "trading_day", ""))
            else:
                log.warning("登录失败：%s %s", ev.kind, getattr(ev, "error_msg", ""))
        elif isinstance(ev, SettlementConfirmed):
            if ev.ok and self._was_disconnected:
                self._was_disconnected = False
                log.info("交易链路已恢复（重连重登完成）")
                if self.journal is not None:
                    self.journal.log("connect", detail="交易链路恢复（重连重登完成）")
        elif isinstance(ev, CtpError) and ev.error_id:
            log.warning("CTP 错误 [%s]：%s %s", ev.source, ev.error_id, ev.error_msg)

    def log_notes(self, notes) -> None:
        for n in notes or []:
            if n.kind in ("stop", "reject", "abandon", "refuse"):
                log.warning("[%s] %s：%s", n.kind, n.symbol, n.detail)
            else:
                log.info("[%s] %s：%s", n.kind, n.symbol, n.detail)
        for n in notes or []:
            if n.kind == "reject":
                alert("order_reject", n.detail, symbol=n.symbol, journal=self.journal)
        if self.journal is not None:
            self.journal.record_notes(notes)

    def _journal_trade_event(self, ev) -> None:
        """回报入库：成交 → live_fills；订单终态 → live_orders（去重）。"""
        if self.journal is None:
            return
        if isinstance(ev, TradeRtn):
            symbol = self.instrument_map.get(ev.instrument, "")
            commission = (
                self.contracts[symbol].commission_per_lot * int(ev.volume) if symbol else 0.0
            )
            self.journal.record_trade(ev, commission=commission)
        elif isinstance(ev, (OrderRtn, OrderRejected)):
            order = self.book.get(ev.order_ref)
            if (
                order is not None
                and not order.is_active
                and order.order_ref not in self._journaled_orders
            ):
                self._journaled_orders.add(order.order_ref)
                self.journal.record_order_terminal(order)

    @staticmethod
    def today() -> str:
        return dt.date.today().strftime("%Y-%m-%d")

    # -------------------------------------------------- 数据新鲜度

    def _data_is_fresh(self) -> bool:
        """clean 数据是否已覆盖「最近一个工作日（不含今天）」的收盘。"""
        need = prev_workday().isoformat()
        clean_dir = (self.cfg.get("data") or {}).get("clean_dir", "data/clean")
        for sym in self.symbols:
            path = resolve_path(f"{clean_dir}/{sym}_clean.csv")
            if not path.exists():
                return False
            try:
                last = str(store.load_csv(path)["date"].iloc[-1])[:10]
            except Exception:  # noqa: BLE001 - 读取异常按「不新鲜」处理
                return False
            if last < need:
                return False
        return True

    def _ensure_fresh_data(self) -> None:
        """数据陈旧时自动刷新（复用每日流水线的全区间重拉链；失败不阻塞执行）。"""
        try:
            if self._data_is_fresh():
                return
            log.info("行情数据陈旧，自动刷新中……")
            from scripts.daily_run import refresh_data

            refresh_data(self.cfg, self.symbols, log)
            if self.journal is not None:
                self.journal.log("data", detail="行情数据已自动刷新")
        except Exception as e:  # noqa: BLE001 - 刷新失败继续用现有数据
            log.exception("数据自动刷新失败（继续用现有数据）：%s", e)
            if self.journal is not None:
                self.journal.log("data", detail=f"数据刷新失败：{e}")

    # -------------------------------------------------- 执行与日终

    def execute_round(self) -> None:
        """信号重放 → 目标仓位 → （非干跑时）逐腿执行。"""
        self._ensure_fresh_data()  # 执行前数据保新（陈旧则自动刷新）
        self._maybe_execute_roll()  # v2.2：已确认的移仓计划先执行（换月后再算目标）
        targets = self.bridge.compute()
        if self.only:
            targets = {s: t for s, t in targets.items() if s in self.only}
            log.info("联调限制品种：%s", "、".join(self.only))
        self.guard.prev_close = dict(self.bridge.last_close)  # 涨跌停判定的前收盘依据
        self.guard.atr_hint = dict(self.bridge.last_atr)  # 止损注册的 ATR（截至信号日收盘）
        for s in targets:
            if self.ledger.net_size(s) != 0:  # 每日对账式校准（补 ATR / 校准加仓均价）
                for risk_ev in self.guard.sync_stops(s, date=self.today()):
                    if risk_ev.kind != "stop_update":
                        log.info("止损状态校准：%s", risk_ev.describe())
        for s, t in targets.items():
            self.guard.update_signal(s, t.signal)  # 释放 signal_reset 锁定（回测每日调用）
        if self.force_targets:
            self._apply_force_targets(targets)
        if self.journal is not None:
            for s, t in targets.items():
                self.journal.log(
                    "signal",
                    symbol=s,
                    detail=f"{s} 信号 {t.signal:+d}，目标 {t.direction:+d}×{t.lots} 手",
                )
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
            notes = self.executor.execute_target(targets, date=self.today())
            self.log_notes(notes)
            log.info(
                "目标执行完成：%s",
                f"{len(notes)} 条执行记录" if notes else "全部品种差额为 0（无调仓）",
            )
            self._force_close_expiring()  # v2.2：到期强平（在目标执行之后，只平不开）
        self.exec_date = self.today()

    def _apply_force_targets(self, targets: dict) -> None:
        """联调覆盖目标仓位（保留原 ``signal`` 供重入锁定释放；仅 --once 使用）。"""
        for symbol, signed in self.force_targets.items():
            tp = targets.get(symbol)
            if tp is None:
                log.warning("force_target：品种 %s 无目标仓位（检查 only / 配置）", symbol)
                continue
            tp.direction = 1 if signed > 0 else (-1 if signed < 0 else 0)
            tp.lots = abs(int(signed))
            log.info("force_target 覆盖：%s → %+d × %d 手", symbol, tp.direction, tp.lots)

    def settle_day(self) -> None:
        """日终：移动止损 + 熔断 + 对账 + 快照 + 日报。"""
        date = self.today()
        if self.ledger.last_settle_date == date:
            log.info("今日已完成日终结算（%s），跳过", date)
            self.settle_date = date  # 本进程不再重复尝试（防「跳过」刷屏）
            return
        res = self.guard.on_day_close(date, atr_map=self.bridge.last_atr)
        log.info(
            "日终结算：权益 %.2f ｜ 熔断状态 %s ｜ 事件 %d 条",
            self.ledger.equity,
            res["fuse_state"],
            len(res["events"]),
        )
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ① 柜台对账（失败不阻塞日终）
        counter_equity: float | None = None
        reconciles: list = []
        try:
            snap = query_counter(self.session)
            if snap.ok:
                reconciles = reconcile_all(self.ledger, snap)
                counter_equity = float(snap.account.balance) if snap.account else None
                if self.conn is not None:
                    rows = [
                        {
                            "instrument": p.instrument,
                            "direction": ("多" if str(p.posi_direction) == "2" else "空"),
                            "volume": int(p.position),
                        }
                        for p in snap.positions
                        if p.position > 0
                    ]
                    results_db.record_live_positions(self.conn, ts, rows, source="counter")
            else:
                log.warning("对账跳过：%s", snap.detail)
        except Exception as e:  # noqa: BLE001 - 对账失败不阻塞日终
            log.exception("对账失败（不阻塞日终）：%s", e)

        # ② 本地快照
        if self.conn is not None:
            local_rows = []
            for s in self.symbols:
                n = self.ledger.net_size(s)
                if n:
                    local_rows.append(
                        {
                            "instrument": self.ledger.symbol_to_instrument.get(s, s),
                            "direction": "多" if n > 0 else "空",
                            "volume": abs(n),
                            "avg_price": float(self.ledger.state(s).avg_price),
                        }
                    )
            results_db.record_live_positions(self.conn, ts, local_rows, source="local")
            results_db.record_live_equity(
                self.conn,
                ts,
                nominal_equity=self.ledger.equity,
                counter_equity=counter_equity or 0.0,
                margin_used=self.ledger.margin_used,
                available=self.ledger.available,
                day_pnl=self.ledger.equity - self.ledger.day_ref_equity,
            )

        # ③ 日报（构建 → 落地 → 推送）
        entries = self.journal.entries if self.journal is not None else []
        md = build_daily_report(
            date=date,
            ledger=self.ledger,
            entries=entries,
            fuse_state=res["fuse_state"],
            counter_equity=counter_equity,
            reconciles=reconciles,
            expiry_notes=[self.lifecycle.describe(s) for s in self.lifecycle.expiring()],
        )
        path = save_report(md, resolve_path("output/live/reports"), date)
        log.info("日报已生成：%s", path)
        push_report(md)
        if self.journal is not None:
            ok_n = sum(1 for r in reconciles if r.match)
            self.journal.log("reconcile", detail=f"对账 {ok_n}/{len(reconciles)} 项一致")
            if reconciles and ok_n < len(reconciles):
                alert(
                    "reconcile_fail",
                    f"对账不一致 {ok_n}/{len(reconciles)}",
                    journal=self.journal,
                )

        self.ledger.last_settle_date = date
        advance_day(self.ledger, self.guard)  # day_idx+1 → 次日基准 → 清「当日止损」
        self.save_state()
        self.settle_date = date

    # -------------------------------------------------- 主循环

    def run_forever(self) -> None:
        self.start()  # 状态恢复 + 连接 + 就绪等待 + 订阅 + 会话登记
        self._ensure_fresh_data()  # 启动时数据保新（陈旧则自动刷新）
        last_chase = 0.0
        last_beat = 0.0
        last_life_check = 0.0
        while True:
            self.pump(0.5)
            now = dt.datetime.now()
            t = now.time()

            # v2.2 到期巡检（每小时）：跨过 T-warn / T-force 边界时即时生效；
            # force_close 且有持仓 → 强平（交易时段内，每小时重试直到平掉）
            if time.time() - last_life_check >= 3600:
                self._check_lifecycle()
                if is_trading_time(now, self.symbols):
                    self._force_close_expiring()
                last_life_check = time.time()

            # 目标执行（每日一次）：每天首次进入交易时段、且今日行情已就绪（首个 tick 已到）
            # 即执行——夜盘 21:00 在线（标准口径）即 21:00 执行；夜间离线的运行模式下，
            # 由次日日盘开盘后自动补执行（防重复由 exec_date 标记保证）。
            if (
                self.exec_date != self.today()
                and self.tick_date_today == self.today()
                and is_trading_time(now, self.symbols)
                and not self.session.td_ready
                and not self._td_wait_reported
            ):
                # v2.1：只有行情就绪不够——交易未登录时报单会静默丢失（10-23 09:00 实证）
                log.warning("行情已就绪但交易通道未登录，暂缓执行目标仓位")
                self._td_wait_reported = True
            if (
                self.exec_date != self.today()
                and is_trading_time(now, self.symbols)
                and self.tick_date_today == self.today()
                and self.session.td_ready
            ):
                log.info("进入交易时段且行情就绪，开始执行目标仓位……")
                try:
                    self.execute_round()
                except Exception as e:  # noqa: BLE001 - 常驻进程需容错并继续
                    log.exception("目标执行失败：%s", e)

            # 追单（每 10 秒）
            if time.time() - last_chase >= 10:
                self.log_notes(self.executor.process_chase(date=self.today()))
                last_chase = time.time()

            # 行情心跳：交易时段内超时告警（每次失联只报一条 + 恢复报一条，避免刷屏）
            hb_limit = float(self.live_cfg.get("heartbeat_seconds", 60))
            trading_now = is_trading_time(now, self.symbols)
            if trading_now and not self._trading_prev:
                # v2.1：时段起点（10:30 / 13:30 / 夜盘）重置行情基准——否则 10:15-10:30
                # 与午休的空档会被当成「行情停滞」而主动退出（10-23 10:30 实证）
                self.last_tick_ts = time.time()
                self._gap_reported = False
            self._trading_prev = trading_now
            gap = time.time() - self.last_tick_ts if self.last_tick_ts > 0 else 0.0
            if self._gap_reported and self.last_tick_ts > 0 and gap <= hb_limit:
                self._gap_reported = False
                log.info("行情心跳恢复")
                if self.journal is not None:
                    self.journal.log("heartbeat", detail="行情心跳恢复")
            elif (
                trading_now
                and self.last_tick_ts > 0
                and gap > hb_limit
                and not self._gap_reported
            ):
                self._gap_reported = True
                msg = f"行情心跳超时 {gap:.0f}s（限 {hb_limit:.0f}s）"
                log.warning(msg)
                if self.journal is not None:
                    self.journal.log("heartbeat", detail=msg)

            # 连接健康：交易时段内失联（断线 / 行情停滞）持续超阈值 → 主动退出交自愈重启
            if trading_now:
                conn_down = (not self.session.md_ready) or (not self.session.td_ready)
                if conn_down:
                    if self._conn_down_since == 0.0:
                        self._conn_down_since = time.time()
                    elif time.time() - self._conn_down_since > 180:
                        log.error(
                            "连接失联超过 3 分钟（md_ready=%s td_ready=%s），主动退出等待自愈重启",
                            self.session.md_ready,
                            self.session.td_ready,
                        )
                        if self.journal is not None:
                            self.journal.log("connect", detail="连接失联超 3 分钟，主动退出待自愈")
                        self.save_state()
                        raise SystemExit(2)
                else:
                    self._conn_down_since = 0.0
                    if gap > 600:
                        log.error(
                            "行情停滞超过 10 分钟（连接就绪但无 tick），主动退出等待自愈重启"
                        )
                        if self.journal is not None:
                            self.journal.log("connect", detail="行情停滞超 10 分钟，主动退出待自愈")
                        self.save_state()
                        raise SystemExit(3)

            # 日终（15:05-21:00 窗口内每日一次；当日有行情、或已过收盘均可）
            if (
                self.settle_date != self.today()
                and dt.time(15, 5) <= t < dt.time(21, 0)
                and (self.tick_date_today == self.today() or t >= dt.time(15, 0))
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
    parser.add_argument(
        "--confirm-roll",
        default="",
        metavar="PLAN_ID",
        help="确认换月计划（只置确认位，不立即下单；下次执行轮生效）",
    )
    parser.add_argument(
        "--force-target",
        default="",
        metavar="M0=+1,RB0=-2",
        help="联调覆盖目标仓位（仅与 --once 同用；走完整下单链路）",
    )
    args = parser.parse_args()

    # 换月确认：不需要连柜台，也不受单实例锁限制（常驻进程在跑时也能确认）
    if args.confirm_roll:
        base = resolve_path("output/live/plans")
        plan = roll_mod.confirm_plan(args.confirm_roll, base=base)
        if plan is None:
            print(f"计划不存在：{args.confirm_roll}（目录 {base}）")
            return 1
        print(f"已确认移仓计划 {plan.plan_id}（{plan.date}，{len(plan.items)} 项）")
        for line in roll_mod.summary_lines(plan):
            print(f"  {line}")
        print("生效时点：下一个交易时段的目标执行轮（需重启进程加载新代码/配置）")
        return 0

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

    # 单实例互斥（防任务多重触发 / 进程孤儿导致的多实例并存）
    force: dict[str, int] = {}
    if args.force_target:
        if not args.once:
            print("--force-target 仅允许与 --once 同时使用（防误用）")
            return 1
        try:
            force = parse_force_targets(
                args.force_target, [str(k) for k in (live_cfg.get("contracts_map") or {})]
            )
        except ValueError as e:
            print(f"--force-target 解析失败：{e}")
            return 1
    lock = acquire_single_instance()
    if lock is None:
        print("已有仿真实例在运行（单实例互斥），本次启动退出")
        return 0

    logger_mod.enable_file_logging(PROJECT_ROOT / "logs" / "live")
    log.info(
        "SimNow 仿真启动：环境 %s ｜ dry_run=%s ｜ 模式 %s",
        live_cfg.get("env"),
        dry_run,
        "联调单轮" if args.once else "常驻",
    )
    rt = LiveRuntime(cfg, dry_run=dry_run, only=args.only, force_targets=force)
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
