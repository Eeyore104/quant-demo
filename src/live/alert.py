"""盘中告警（v2.2 · docs/21 T2.2-8）：去重 + 日限流 + 企业微信。

为什么单独一层：

- 日报要等 15:05，盘中出事（拒单 / 断线 / 对账失败 / 合约临期）必须立刻知道；
- 但把每条日志都推企微会变成噪音，所以统一在这里做「同 key 冷却 + 每日上限」。

降级：未配置 webhook 时只落 journal + WARNING 日志，不抛异常（沿用 ``send_markdown`` 语义）。
"""

from __future__ import annotations

import datetime as dt

from ..utils.logger import get_logger
from ..utils.notify import send_markdown

log = get_logger("live.alert")

DEFAULT_TTL = 300  # 同 key 冷却秒数
DEFAULT_DAILY_CAP = 20  # 每日推送上限（超出只落盘）

_sent: dict[str, float] = {}
_day = {"date": "", "count": 0}


def reset() -> None:
    """清空去重表与当日计数（测试用）。"""
    _sent.clear()
    _day["date"] = ""
    _day["count"] = 0


def alert(
    kind: str,
    text: str,
    *,
    symbol: str = "",
    ttl: int = DEFAULT_TTL,
    daily_cap: int = DEFAULT_DAILY_CAP,
    journal=None,
    now: dt.datetime | None = None,
) -> bool:
    """推送一条盘中告警；返回是否真的发出。

    返回 ``False`` 的三种情况：同 key 冷却中 / 当日额度用尽 / webhook 未配置。
    三种情况都会留下痕迹（journal 或日志），便于事后核对「当时到底报没报」。
    """
    moment = now or dt.datetime.now()
    key = f"{kind}:{symbol}" if symbol else kind
    last = _sent.get(key)
    if last is not None and moment.timestamp() - last < ttl:
        return False

    day = moment.strftime("%Y-%m-%d")
    if _day["date"] != day:
        _day["date"] = day
        _day["count"] = 0
    if _day["count"] >= daily_cap:
        log.info("告警日限额已满（%d 条），仅落盘：%s", daily_cap, text)
        if journal is not None:
            journal.log("alert_dropped", symbol=symbol, detail=text)
        return False

    _sent[key] = moment.timestamp()
    _day["count"] += 1
    if journal is not None:
        journal.log(f"alert_{kind}", symbol=symbol, detail=text)
    ok = send_markdown(f"**[{kind}]** {text}")
    if not ok:
        log.warning("[%s] %s", kind, text)
    return ok
