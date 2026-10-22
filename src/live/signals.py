"""信号桥（v2.0 · docs/16 T2.0-3c）：收盘重放日线 → 目标仓位表。

设计（与回测组合引擎逐项对齐）：
- 收盘后用**全量 clean CSV 重放策略**（复用策略注册表，与回测同一份
  ``generate_signals`` 代码）→ 取最后一根 bar 的信号与参数；
- 手数复用 ``compute_target_lots``（固定 / 等权 / 波动率倒数 / ATR 风险预算），
  权益取虚拟账本当前权益（≈ 回测 ``day_ref_equity`` 语义）；
- ATR 按「截至信号日」计算（防未来函数），并保留供当日收盘的风控更新使用；
- **atr_risk 入场锁定（latch）**：仅在「新开方向」时计算手数并锁定，持仓期间
  不随波动漂移（与回测主循环一致）；重启后以当前持仓手数懒重建锁定值。

输出的 ``TargetPosition`` 为「信号日收盘后的原始目标」，熔断 / 重入 / 敞口等
约束由 ``risk_guard`` 在执行时逐腿施加（与回测的事件时序一致）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ..data import store
from ..engine.contracts import ContractSpec
from ..engine.risk import atr_series
from ..engine.sizing import compute_target_lots
from ..strategy.registry import get_strategy
from ..utils.config_loader import resolve_path
from ..utils.logger import get_logger
from .ledger import VirtualLedger

log = get_logger("live.signals")


@dataclass
class TargetPosition:
    """单品种目标仓位（信号日收盘后的原始目标）。"""

    symbol: str
    signal: int  # 策略原始信号：+1 / -1 / 0
    direction: int  # 目标方向：+1 多 / -1 空 / 0 平
    lots: int  # 目标手数（绝对值）
    ref_price: float  # 信号日收盘价（参考价）


class SignalBridge:
    """策略重放 → 目标仓位（含 latch 锁定与 ATR 输出）。"""

    def __init__(self, cfg: dict, contracts: dict[str, ContractSpec], ledger: VirtualLedger):
        self.cfg = cfg
        self.contracts = contracts
        self.ledger = ledger
        pcfg = cfg.get("portfolio") or {}
        self.sizing_cfg = dict(pcfg.get("sizing") or {})
        self.items = list(pcfg.get("symbols") or [])
        self.latch: dict[str, int] = {}  # 入场锁定手数（atr_risk）
        # compute() 的副产品（供风控收盘更新）
        self.last_atr: dict[str, float] = {}
        self.last_close: dict[str, float] = {}

    # -------------------------------------------------- 数据与重放

    def _load(self, symbol: str) -> pd.DataFrame:
        clean_dir = (self.cfg.get("data") or {}).get("clean_dir", "data/clean")
        path = resolve_path(f"{clean_dir}/{symbol}_clean.csv")
        if not path.exists():
            raise FileNotFoundError(f"缺少清洗数据：{path}（请先更新数据）")
        return store.load_csv(path)

    def compute(self) -> dict[str, TargetPosition]:
        """重放全部品种 → 目标仓位表（同时刷新 last_atr / last_close）。"""
        mode = str(self.sizing_cfg.get("mode", "fixed"))
        atr_window = int(self.sizing_cfg.get("atr_window", 20))
        prices: dict[str, float] = {}
        closes: dict[str, pd.Series] = {}
        atrs: dict[str, float] = {}
        sigs: dict[str, int] = {}

        for item in self.items:
            sym = str(item["code"])
            df = self._load(sym)
            cls = get_strategy(str(item["strategy"]))
            out = cls(dict(item.get("params") or {})).generate_signals(df)
            prices[sym] = float(out["close"].iloc[-1])
            closes[sym] = out["close"]
            sigs[sym] = int(out["signal"].iloc[-1])
            av = float(atr_series(out, atr_window).iloc[-1])
            if av == av and av > 0:  # 非 NaN 且为正
                atrs[sym] = av
            log.info(
                "重放 %s：信号 %+d（收盘 %.4f，ATR %s）",
                sym,
                sigs[sym],
                prices[sym],
                f"{atrs.get(sym, 0):.4f}",
            )

        capital = float(self.ledger.equity)
        lots_all = compute_target_lots(
            mode,
            self.contracts,
            prices,
            closes,
            capital,
            self.sizing_cfg,
            atr_by_symbol=atrs,
        )

        targets: dict[str, TargetPosition] = {}
        for item in self.items:
            sym = str(item["code"])
            sig = sigs[sym]
            cur = self.ledger.net_size(sym)
            # 重启恢复：持仓在手但锁定缺失 → 以当前持仓手数为锁定值
            if cur != 0 and sym not in self.latch:
                self.latch[sym] = abs(cur)
            is_fresh = sig != 0 and (cur == 0 or (cur > 0) != (sig > 0))
            if is_fresh:
                self.latch[sym] = int(lots_all.get(sym, 0))
            t = sig * self.latch.get(sym, 0) if sig != 0 else 0
            direction = 1 if t > 0 else (-1 if t < 0 else 0)
            targets[sym] = TargetPosition(
                symbol=sym,
                signal=sig,
                direction=direction,
                lots=abs(int(t)),
                ref_price=prices[sym],
            )

        self.last_atr = atrs
        self.last_close = prices
        return targets

    # -------------------------------------------------- 持久化（latch）

    def to_dict(self) -> dict:
        return {
            "latch": dict(self.latch),
            "last_atr": dict(self.last_atr),
            "last_close": dict(self.last_close),
        }

    def load(self, path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        d = json.loads(p.read_text(encoding="utf-8"))
        self.latch = {k: int(v) for k, v in (d.get("latch") or {}).items()}
        # v2.1：ATR / 前收盘持久化（重启后启动补建与涨跌停判定不再依赖当日重放）
        self.last_atr = {k: float(v) for k, v in (d.get("last_atr") or {}).items()}
        self.last_close = {k: float(v) for k, v in (d.get("last_close") or {}).items()}
        return True

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
