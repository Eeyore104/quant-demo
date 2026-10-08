"""实盘适配抽象接口（MVP 只留接口不实现）。

设计意图：回测引擎用历史 bar 驱动策略；接入实盘时，由本层把 CTP 实时行情
组装成同样的上下文去调用策略的 on_bar()，策略代码不改 ——
「回测 → 仿真 → 实盘」共用同一套策略，这是预留的执行层插槽。
"""

from abc import ABC, abstractmethod


class ExecutionAdapter(ABC):
    """执行适配器基类。"""

    @abstractmethod
    def connect(self, cfg: dict) -> None:
        """连接交易/行情前置（SimNow：BrokerID/InvestorID/密码/前置地址）。"""

    @abstractmethod
    def subscribe(self, symbol: str) -> None:
        """订阅行情。"""

    @abstractmethod
    def send_order(
        self, symbol: str, direction: str, offset: str, price: float, volume: int
    ) -> str:
        """下单：direction=LONG/SHORT，offset=OPEN/CLOSE，返回订单号。"""

    @abstractmethod
    def on_market_data(self, bar) -> None:
        """收到行情后：调用策略 on_bar() 并执行目标仓位调整。"""


# SimNowAdapter（P1）与 CtpAdapter（P2）按此接口实现。
# 接入要点见 docs/03-系统设计与任务分解.md 第 10.3 / 10.4 节。
