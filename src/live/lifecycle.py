"""合约生命周期（v2.2 · docs/21 T2.2-1）：到期状态机。

为什么单独一个模块：

- 到期判定是纯数据问题（合约 + 日期 → 状态），不依赖 CTP / 账本 / 风控，
  独立出来才能全量单测——这类「差一天就出事」的逻辑不能只靠实盘验证；
- 判定用「自然日倒计时」而非交易日历：引入日历要维护节假日表，误差风险大于收益，
  阈值本身保守（默认 warn=5 / force_close=1 天），宁可早禁一天。

状态机（阈值可配，见 ``config.yaml`` 的 ``live.lifecycle``）：

===== ========================================== ==========================
状态   条件（距 ``individual_last`` 自然日）        动作
===== ========================================== ==========================
ok      > ``warn_days``                            正常
warn    ≤ ``warn_days``                           禁新开 + 告警
force   ≤ ``force_close_days``                    自动全平（保命优先）
unknown 合约未登记                                 禁新开（不允许未知合约）
===== ========================================== ==========================

元数据：``config/contracts_lifecycle.yaml``（``scripts/roll_plan.py`` 出建议、人工确认）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..utils.config_loader import resolve_path
from ..utils.logger import get_logger

log = get_logger("live.lifecycle")

# 未登记合约的 days_left 哨兵值（不要让调用方拿到负数）
UNKNOWN_DAYS = 99999

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_FORCE = "force_close"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContractLife:
    """单个真实合约的生命周期条目。"""

    instrument: str  # al2611
    symbol: str  # AL0
    exchange: str  # SHFE / DCE / CZCE
    last_trade: dt.date  # 最后交易日（参考值）
    individual_last: dt.date  # 自然人最后了结日（硬约束）

    @classmethod
    def from_dict(cls, instrument: str, raw: dict) -> ContractLife:
        def _d(key: str) -> dt.date:
            value = raw.get(key)
            if not value:
                raise ValueError(f"{instrument}: 缺少 {key}")
            return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))

        return cls(
            instrument=instrument,
            symbol=str(raw.get("symbol") or instrument).upper(),
            exchange=str(raw.get("exchange") or "").upper(),
            last_trade=_d("last_trade"),
            individual_last=_d("individual_last"),
        )


class LifecycleBook:
    """合约生命周期台账：symbol → ContractLife，提供状态与开仓许可。

    ``contracts_map`` 是「品种 → 真实合约」（如 ``AL0: al2611``）。
    品种在表里查不到 → ``unknown``（禁新开），因为「新加品种忘登记」是最可能的失误模式。
    """

    def __init__(
        self,
        items: dict[str, ContractLife],
        *,
        contracts_map: dict[str, str] | None = None,
        warn_days: int = 5,
        force_close_days: int = 1,
        roll_trigger_days: int = 10,
        enabled: bool = True,
        today: dt.date | None = None,
    ) -> None:
        self.items = {str(k): v for k, v in items.items()}
        self.contracts_map = {str(k): str(v) for k, v in (contracts_map or {}).items()}
        self.warn_days = int(warn_days)
        self.force_close_days = int(force_close_days)
        self.roll_trigger_days = int(roll_trigger_days)
        self.enabled = bool(enabled)
        self._fixed_today = today

    # -------------------------------------------------- 组装

    @classmethod
    def from_config(
        cls,
        live_cfg: dict,
        contracts_map: dict[str, str],
        *,
        today: dt.date | None = None,
        path: str | Path | None = None,
    ) -> LifecycleBook:
        """按 ``live.lifecycle`` 配置加载台账（文件缺失 → 空表 → 全部 unknown）。"""
        lc = dict(live_cfg.get("lifecycle") or {})
        target = Path(path) if path is not None else resolve_path(
            lc.get("path", "config/contracts_lifecycle.yaml")
        )
        items: dict[str, ContractLife] = {}
        if target.exists():
            raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            for instrument, entry in (raw.get("contracts") or {}).items():
                try:
                    life = ContractLife.from_dict(str(instrument), dict(entry or {}))
                except ValueError as exc:  # 单条坏数据不拖垮启动，但要留痕
                    log.error("生命周期表条目无效（已跳过）：%s", exc)
                    continue
                items[life.instrument] = life
        else:
            log.warning("生命周期表不存在：%s（全部品种将按 unknown 处理）", target)
        return cls(
            items,
            contracts_map=contracts_map,
            warn_days=int(lc.get("warn_days", 5)),
            force_close_days=int(lc.get("force_close_days", 1)),
            roll_trigger_days=int(lc.get("roll_trigger_days", 10)),
            enabled=bool(lc.get("enabled", True)),
            today=today,
        )

    # -------------------------------------------------- 查询

    @property
    def today(self) -> dt.date:
        return self._fixed_today or dt.date.today()

    def contract_of(self, symbol: str) -> ContractLife | None:
        """品种 → 生命周期条目（未登记返回 None）。"""
        instrument = self.contracts_map.get(str(symbol))
        if instrument:
            return self.items.get(instrument)
        # 兜底：按 symbol 字段反查（配置里没写 contracts_map 时也能用）
        for life in self.items.values():
            if life.symbol == str(symbol).upper():
                return life
        return None

    def days_left(self, symbol: str) -> int:
        """距自然人最后了结日的自然日数（未登记返回 ``UNKNOWN_DAYS``）。"""
        life = self.contract_of(symbol)
        if life is None:
            return UNKNOWN_DAYS
        return int((life.individual_last - self.today).days)

    def state(self, symbol: str) -> str:
        """状态机：ok / warn / force_close / unknown。"""
        if not self.enabled:
            return STATE_OK
        life = self.contract_of(symbol)
        if life is None:
            return STATE_UNKNOWN
        left = (life.individual_last - self.today).days
        if left <= self.force_close_days:
            return STATE_FORCE
        if left <= self.warn_days:
            return STATE_WARN
        return STATE_OK

    def can_open(self, symbol: str) -> tuple[bool, str]:
        """开仓许可：(是否允许, 拒绝原因)。平仓腿不受此闸约束。"""
        if not self.enabled:
            return True, ""
        state = self.state(symbol)
        life = self.contract_of(symbol)
        if state == STATE_UNKNOWN:
            return False, "无生命周期元数据（config/contracts_lifecycle.yaml 未登记）"
        left = self.days_left(symbol)
        assert life is not None  # state != unknown 时必有 life
        if state == STATE_FORCE:
            return False, (
                f"{life.instrument} 距自然人最后了结日 {left} 天"
                f"（≤{self.force_close_days}），禁止开仓并应清仓"
            )
        if state == STATE_WARN:
            return False, (
                f"{life.instrument} 距自然人最后了结日 {left} 天"
                f"（≤{self.warn_days}），禁止新开仓"
            )
        return True, ""

    # -------------------------------------------------- 批量

    def override(self, symbol: str, instrument: str) -> None:
        """换月后把品种的当前合约指向新合约（到期闸随之按新合约判定）。

        必须在**开新仓之前**调用，否则闸门仍按旧合约（临期）拒绝开仓。
        """
        self.contracts_map[str(symbol)] = str(instrument)

    def missing(self) -> list[str]:
        """有 mapping 但无生命周期条目的品种（启动告警用）。"""
        return [s for s in self.contracts_map if self.contract_of(s) is None]

    def states(self) -> dict[str, str]:
        return {s: self.state(s) for s in self.contracts_map}

    def expiring(self) -> list[str]:
        """处于 warn / force_close 的品种（日报与告警用）。"""
        return [s for s in self.contracts_map if self.state(s) in (STATE_WARN, STATE_FORCE)]

    def force_close_symbols(self) -> list[str]:
        return [s for s in self.contracts_map if self.state(s) == STATE_FORCE]

    def describe(self, symbol: str) -> str:
        """一行摘要（日志 / 日报）。"""
        life = self.contract_of(symbol)
        if life is None:
            return f"{symbol}：未登记生命周期"
        return (
            f"{symbol}（{life.instrument}）：距自然人最后了结 {self.days_left(symbol)} 天，"
            f"状态 {self.state(symbol)}"
        )
