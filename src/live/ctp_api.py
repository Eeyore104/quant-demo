"""CTP 薄封装（v2.0 · docs/16 §3.2）：openctp-ctp 的行情 / 交易接口管理。

设计要点：
- **隔离层**：本模块是 openctp-ctp（第三方库）与自研业务层的唯一接触点；
  业务层（探针 / 适配器）只消费「事件对象」，不直接触 CTP 字段与常量；
- **回调 → 队列**：CTP 回调发生在 C++ 线程，回调内只做「组装事件 + 投递队列」，
  业务逻辑全部由主线程消费队列完成（无锁设计）；
- **可选依赖**：openctp-ctp 属于可选依赖组 ``live``（``uv sync --extra live``）；
  未安装时本模块仍可 import（常量与事件模型不依赖库），仅在 ``start()`` 时报出
  带安装指引的清晰错误——保证 CI 与既有测试零影响；
- **连接编排**：行情 = 连接 → 登录；交易 = 连接 → 认证 → 登录 → 结算确认；
  全部在回调链内驱动（CTP 标准模式），就绪状态以事件形式对外广播。

CTP 常量已对照 openctp-ctp 6.7.11.0 实测核对（方向 '0'/'1'、开平 '0'/'1'/'3'/'4'、
限价 '2'、GFD '3'、AV '1'、Immediately '1'、订单状态 '0'~'5'/'a' 等）。
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..utils.logger import get_logger

log = get_logger("live.ctp_api")

# ---------------------------------------------------------------- 可选依赖

try:  # pragma: no cover - 依运行环境而定
    from openctp_ctp import mdapi as _mdapi
    from openctp_ctp import tdapi as _tdapi

    HAS_CTP = True
except ImportError:  # pragma: no cover
    _mdapi = None
    _tdapi = None
    HAS_CTP = False


def require_ctp():
    """确保 openctp-ctp 可用；返回 (mdapi, tdapi) 模块。"""
    if not HAS_CTP:
        raise RuntimeError(
            "未安装 openctp-ctp（SimNow 仿真依赖）。"
            "请执行：uv sync --extra live"
        )
    return _mdapi, _tdapi


# ---------------------------------------------------------------- CTP 常量
# 与 CTP 官方枚举值一致（已对照 openctp-ctp 6.7.11.0 核对）；内联定义使本模块
# 在未安装库的环境下仍可被 import 与单测。

# 买卖方向
DIR_BUY = "0"
DIR_SELL = "1"

# 开平标志
OFFSET_OPEN = "0"
OFFSET_CLOSE = "1"
OFFSET_CLOSE_TODAY = "3"
OFFSET_CLOSE_YESTERDAY = "4"

# 价格类型 / 有效期 / 数量条件 / 触发条件 / 强平原因
PRICE_TYPE_LIMIT = "2"
TIME_CONDITION_GFD = "3"
VOLUME_CONDITION_AV = "1"
CONTINGENT_IMMEDIATELY = "1"
FORCE_CLOSE_NOT = "0"
HEDGE_SPECULATION = "1"

# 报单操作标志
ACTION_DELETE = "0"

# 订单状态（OnRtnOrder / OrderStatus）
ORDER_STATUS_ALL_TRADED = "0"  # 全部成交
ORDER_STATUS_PART_TRADED = "1"  # 部分成交还在队列中
ORDER_STATUS_PART_TRADED_NOT_QUEUEING = "2"  # 部分成交不在队列中
ORDER_STATUS_NO_TRADE_QUEUEING = "3"  # 未成交还在队列中
ORDER_STATUS_NO_TRADE_NOT_QUEUEING = "4"  # 未成交不在队列中
ORDER_STATUS_CANCELED = "5"  # 撤单
ORDER_STATUS_UNKNOWN = "a"  # 未知

STATUS_TEXT = {
    "0": "全部成交",
    "1": "部分成交(排队)",
    "2": "部分成交(不在队列)",
    "3": "未成交(排队)",
    "4": "未成交(不在队列)",
    "5": "已撤单",
    "a": "未知",
}

DIRECTION_TEXT = {"0": "买", "1": "卖"}
OFFSET_TEXT = {"0": "开仓", "1": "平仓", "3": "平今", "4": "平昨"}


def _clean_number(value) -> float:
    """清洗 CTP 数值：无效值（DBL_MAX 量级）归零。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if abs(f) > 1e300 else f


def _rsp_ok(p_rsp_info) -> tuple[bool, int, str]:
    """解析 CTP 响应错误对象 → (是否成功, 错误码, 错误信息)。"""
    if p_rsp_info is None:
        return True, 0, ""
    error_id = int(getattr(p_rsp_info, "ErrorID", 0) or 0)
    error_msg = str(getattr(p_rsp_info, "ErrorMsg", "") or "")
    return error_id == 0, error_id, error_msg


# ---------------------------------------------------------------- 事件模型


@dataclass
class CtpEvent:
    """事件基类（所有事件带时间戳；kind 取子类类名的小写）。"""

    ts: float = field(default_factory=time.time)

    @property
    def kind(self) -> str:
        return type(self).__name__.lower()


@dataclass
class MdConnected(CtpEvent):
    front: str = ""


@dataclass
class MdDisconnected(CtpEvent):
    reason: int = 0


@dataclass
class MdLogin(CtpEvent):
    ok: bool = False
    error_id: int = 0
    error_msg: str = ""
    trading_day: str = ""


@dataclass
class Tick(CtpEvent):
    """行情快照（500ms 级）。"""

    instrument: str = ""
    last_price: float = 0.0
    open_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    pre_settlement: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    volume: int = 0
    open_interest: float = 0.0
    bid_price1: float = 0.0
    bid_volume1: int = 0
    ask_price1: float = 0.0
    ask_volume1: int = 0
    update_time: str = ""
    update_millisec: int = 0
    trading_day: str = ""


@dataclass
class TdConnected(CtpEvent):
    front: str = ""


@dataclass
class TdDisconnected(CtpEvent):
    reason: int = 0


@dataclass
class TdAuth(CtpEvent):
    ok: bool = False
    error_id: int = 0
    error_msg: str = ""


@dataclass
class TdLogin(CtpEvent):
    ok: bool = False
    error_id: int = 0
    error_msg: str = ""
    trading_day: str = ""
    session_id: int = 0
    front_id: int = 0
    max_order_ref: int = 0


@dataclass
class SettlementConfirmed(CtpEvent):
    ok: bool = False
    error_id: int = 0
    error_msg: str = ""


@dataclass
class OrderRtn(CtpEvent):
    """订单状态回报（含未成交 / 部成 / 全成 / 撤单）。"""

    order_ref: str = ""
    instrument: str = ""
    direction: str = ""
    offset: str = ""
    limit_price: float = 0.0
    volume_total: int = 0
    volume_traded: int = 0
    status: str = ""
    status_msg: str = ""
    order_sys_id: str = ""
    insert_time: str = ""

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)

    @property
    def done(self) -> bool:
        return self.status in (
            ORDER_STATUS_ALL_TRADED,
            ORDER_STATUS_CANCELED,
            ORDER_STATUS_PART_TRADED_NOT_QUEUEING,
            ORDER_STATUS_NO_TRADE_NOT_QUEUEING,
        )


@dataclass
class TradeRtn(CtpEvent):
    """成交回报。"""

    order_ref: str = ""
    instrument: str = ""
    direction: str = ""
    offset: str = ""
    price: float = 0.0
    volume: int = 0
    trade_id: str = ""
    trade_date: str = ""
    trade_time: str = ""
    trading_day: str = ""


@dataclass
class OrderRejected(CtpEvent):
    """报单被拒（OnRspOrderInsert / OnErrRtnOrderInsert）。"""

    order_ref: str = ""
    instrument: str = ""
    error_id: int = 0
    error_msg: str = ""


@dataclass
class ActionRsp(CtpEvent):
    """撤单响应（OnRspOrderAction）。"""

    order_ref: str = ""
    instrument: str = ""
    ok: bool = False
    error_id: int = 0
    error_msg: str = ""


@dataclass
class ActionRejected(CtpEvent):
    """撤单被拒（OnErrRtnOrderAction）。"""

    order_ref: str = ""
    instrument: str = ""
    error_id: int = 0
    error_msg: str = ""


@dataclass
class AccountRsp(CtpEvent):
    """资金账户查询响应。"""

    balance: float = 0.0
    available: float = 0.0
    curr_margin: float = 0.0
    close_profit: float = 0.0
    position_profit: float = 0.0
    commission: float = 0.0
    pre_balance: float = 0.0


@dataclass
class PositionRsp(CtpEvent):
    """持仓查询响应（逐条）。"""

    instrument: str = ""
    posi_direction: str = ""  # '2'=多头 '3'=空头
    position: int = 0
    today_position: int = 0
    yd_position: int = 0
    use_margin: float = 0.0
    position_cost: float = 0.0
    position_profit: float = 0.0

    @property
    def direction_text(self) -> str:
        return {"2": "多", "3": "空"}.get(self.posi_direction, self.posi_direction)


@dataclass
class QueryDone(CtpEvent):
    """查询批次结束标记（bIsLast）。"""

    query: str = ""  # "account" / "position"
    ok: bool = True
    error_id: int = 0
    error_msg: str = ""


@dataclass
class CtpError(CtpEvent):
    """通用错误响应（OnRspError）。"""

    error_id: int = 0
    error_msg: str = ""
    source: str = ""


# ---------------------------------------------------------------- 行情客户端


class MdClient:
    """行情客户端：连接 → 登录 → 订阅；tick 回调 → 事件队列。"""

    def __init__(
        self,
        front: str,
        flow_dir: str | Path,
        evq: queue.Queue,
        broker_id: str = "9999",
        user_id: str = "",
        password: str = "",
    ):
        self.front = front
        self.flow_dir = Path(flow_dir)
        self.evq = evq
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self._api = None
        self._spi = None
        self._subscribed: list[str] = []  # 订阅记录（断线重连重登后自动重订阅）

    def start(self) -> None:
        mdapi, _ = require_ctp()
        self.flow_dir.mkdir(parents=True, exist_ok=True)
        flow = str(self.flow_dir).replace("\\", "/") + "/"
        api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(flow)
        spi = _MdSpi(self, api)
        api.RegisterSpi(spi)
        api.RegisterFront(self.front)
        api.Init()
        self._api = api
        self._spi = spi  # 持引用防 GC
        log.info("行情接口已启动：%s（流文件 %s）", self.front, flow)

    def subscribe(self, instruments: list[str]) -> None:
        if self._api is None:
            raise RuntimeError("行情接口未启动（先调用 start()）")
        self._subscribed = list(dict.fromkeys(self._subscribed + list(instruments)))
        codes = [x.encode("utf-8") for x in instruments]
        self._api.SubscribeMarketData(codes, len(codes))
        log.info("已订阅行情：%s", ", ".join(instruments))

    def resubscribe(self) -> None:
        """重连重登后重新订阅（行情断线恢复的关键一步；无订阅记录时跳过）。"""
        if self._api is None or not self._subscribed:
            return
        codes = [x.encode("utf-8") for x in self._subscribed]
        self._api.SubscribeMarketData(codes, len(codes))
        log.info("行情已重新订阅：%s", ", ".join(self._subscribed))

    def close(self) -> None:
        if self._api is not None:
            self._api.Release()
            self._api = None
            log.info("行情接口已释放")


class _MdSpi(_mdapi.CThostFtdcMdSpi if _mdapi else object):  # pragma: no cover
    """行情 SPI：回调 → 事件（C++ 线程内只投递，不做业务）。"""

    def __init__(self, client: MdClient, api):
        super().__init__()
        self.client = client
        self.api = api
        self._req_seq = itertools.count(1)

    def OnFrontConnected(self):
        log.info("行情前置已连接（%s），登录中……", self.client.front)
        self.client.evq.put(MdConnected(front=self.client.front))
        # 行情登录（SimNow 行情前置需要登录）
        mdapi, _ = require_ctp()
        req = mdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = self.client.broker_id
        req.UserID = self.client.user_id
        req.Password = self.client.password
        self.api.ReqUserLogin(req, next(self._req_seq))

    def OnFrontDisconnected(self, nReason):
        log.warning("行情前置断开（reason=%s），等待 API 自动重连……", nReason)
        self.client.evq.put(MdDisconnected(reason=int(nReason)))

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(
            MdLogin(
                ok=ok,
                error_id=eid,
                error_msg=emsg,
                trading_day=str(getattr(pRspUserLogin, "TradingDay", "") or ""),
            )
        )
        if ok:
            # 断线重连重登后自动重订阅（否则行情永久静默——实战教训）
            self.client.resubscribe()
        else:
            log.error("行情登录失败：%s %s", eid, emsg)

    def OnRtnDepthMarketData(self, pDepthMarketData):
        d = pDepthMarketData
        self.client.evq.put(
            Tick(
                instrument=str(getattr(d, "InstrumentID", "") or ""),
                last_price=_clean_number(getattr(d, "LastPrice", 0)),
                open_price=_clean_number(getattr(d, "OpenPrice", 0)),
                highest_price=_clean_number(getattr(d, "HighestPrice", 0)),
                lowest_price=_clean_number(getattr(d, "LowestPrice", 0)),
                pre_settlement=_clean_number(getattr(d, "PreSettlementPrice", 0)),
                upper_limit=_clean_number(getattr(d, "UpperLimitPrice", 0)),
                lower_limit=_clean_number(getattr(d, "LowerLimitPrice", 0)),
                volume=int(_clean_number(getattr(d, "Volume", 0))),
                open_interest=_clean_number(getattr(d, "OpenInterest", 0)),
                bid_price1=_clean_number(getattr(d, "BidPrice1", 0)),
                bid_volume1=int(_clean_number(getattr(d, "BidVolume1", 0))),
                ask_price1=_clean_number(getattr(d, "AskPrice1", 0)),
                ask_volume1=int(_clean_number(getattr(d, "AskVolume1", 0))),
                update_time=str(getattr(d, "UpdateTime", "") or ""),
                update_millisec=int(_clean_number(getattr(d, "UpdateMillisec", 0))),
                trading_day=str(getattr(d, "TradingDay", "") or ""),
            )
        )

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        _, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(CtpError(error_id=eid, error_msg=emsg, source="md"))


# ---------------------------------------------------------------- 交易客户端


class TdClient:
    """交易客户端：连接 → 认证 → 登录 → 结算确认；回报 / 查询回调 → 事件队列。"""

    def __init__(
        self,
        front: str,
        flow_dir: str | Path,
        evq: queue.Queue,
        broker_id: str = "9999",
        user_id: str = "",
        password: str = "",
        app_id: str = "simnow_client_test",
        auth_code: str = "0000000000000000",
    ):
        self.front = front
        self.flow_dir = Path(flow_dir)
        self.evq = evq
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self.app_id = app_id
        self.auth_code = auth_code
        self._api = None
        self._spi = None
        self._req_seq = itertools.count(1)
        self._order_ref_seq = 0
        self._last_query_ts = 0.0
        self._query_lock = threading.Lock()
        # 登录后回填（撤单需 FrontID + SessionID 定位订单）
        self.session_id = 0
        self.front_id = 0
        self.max_order_ref = 0

    # ------------------------------------------------ 生命周期

    def start(self) -> None:
        _, tdapi = require_ctp()
        self.flow_dir.mkdir(parents=True, exist_ok=True)
        flow = str(self.flow_dir).replace("\\", "/") + "/"
        api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(flow)
        spi = _TdSpi(self, api)
        api.RegisterSpi(spi)
        api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
        api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
        api.RegisterFront(self.front)
        api.Init()
        self._api = api
        self._spi = spi
        log.info("交易接口已启动：%s（流文件 %s）", self.front, flow)

    def close(self) -> None:
        if self._api is not None:
            self._api.Release()
            self._api = None
            log.info("交易接口已释放")

    # ------------------------------------------------ 请求辅助

    def next_request_id(self) -> int:
        return next(self._req_seq)

    def next_order_ref(self) -> str:
        """本地报单引用（会话内唯一递增；定位订单用 FrontID+SessionID+OrderRef）。"""
        self._order_ref_seq += 1
        return str(self._order_ref_seq)

    def _throttle_query(self) -> None:
        """CTP 查询流控：两次查询至少间隔 1.05 秒（官方限制 1 秒 1 次）。"""
        with self._query_lock:
            gap = time.time() - self._last_query_ts
            if gap < 1.05:
                time.sleep(1.05 - gap)
            self._last_query_ts = time.time()

    # ------------------------------------------------ 查询

    def query_account(self) -> None:
        tdapi, _ = require_ctp()
        self._throttle_query()
        q = tdapi.CThostFtdcQryTradingAccountField()
        q.BrokerID = self.broker_id
        q.InvestorID = self.user_id
        self._api.ReqQryTradingAccount(q, self.next_request_id())
        log.debug("已发资金查询请求")

    def query_position(self, instrument: str = "") -> None:
        tdapi, _ = require_ctp()
        self._throttle_query()
        q = tdapi.CThostFtdcQryInvestorPositionField()
        q.BrokerID = self.broker_id
        q.InvestorID = self.user_id
        q.InstrumentID = instrument
        self._api.ReqQryInvestorPosition(q, self.next_request_id())
        log.debug("已发持仓查询请求：%s", instrument or "全部")

    # ------------------------------------------------ 报单 / 撤单

    def order_insert(
        self,
        instrument: str,
        direction: str,
        offset: str,
        price: float,
        volume: int,
        order_ref: str,
    ) -> None:
        """限价报单（direction: DIR_BUY/DIR_SELL；offset: OFFSET_*）。"""
        tdapi, _ = require_ctp()
        o = tdapi.CThostFtdcInputOrderField()
        o.BrokerID = self.broker_id
        o.InvestorID = self.user_id
        o.InstrumentID = instrument
        o.OrderRef = order_ref
        o.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        o.Direction = direction
        o.CombOffsetFlag = offset
        o.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation
        o.LimitPrice = float(price)
        o.VolumeTotalOriginal = int(volume)
        o.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        o.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        o.MinVolume = 1
        o.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
        o.StopPrice = 0.0
        o.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose
        o.IsAutoSuspend = 0
        o.UserForceClose = 0
        self._api.ReqOrderInsert(o, self.next_request_id())
        log.info(
            "已报单：%s %s %s %s手 @%.4f（OrderRef=%s）",
            instrument,
            DIRECTION_TEXT.get(direction, direction),
            OFFSET_TEXT.get(offset, offset),
            volume,
            price,
            order_ref,
        )

    def order_action(self, order_ref: str, instrument: str, order_sys_id: str = "") -> None:
        """撤单（以 OrderRef + FrontID + SessionID 定位；OrderSysID 可选）。"""
        tdapi, _ = require_ctp()
        a = tdapi.CThostFtdcInputOrderActionField()
        a.BrokerID = self.broker_id
        a.InvestorID = self.user_id
        a.InstrumentID = instrument
        a.OrderRef = order_ref
        a.OrderSysID = order_sys_id
        a.FrontID = self.front_id
        a.SessionID = self.session_id
        a.ActionFlag = tdapi.THOST_FTDC_AF_Delete
        self._api.ReqOrderAction(a, self.next_request_id())
        log.info("已发撤单请求：%s（OrderRef=%s）", instrument, order_ref)


class _TdSpi(_tdapi.CThostFtdcTraderSpi if _tdapi else object):  # pragma: no cover
    """交易 SPI：认证 → 登录 → 结算确认链 + 回报 / 查询转发。"""

    def __init__(self, client: TdClient, api):
        super().__init__()
        self.client = client
        self.api = api

    # ------------------------------------------------ 连接与登录链

    def OnFrontConnected(self):
        log.info("交易前置已连接（%s），认证中……", self.client.front)
        self.client.evq.put(TdConnected(front=self.client.front))
        tdapi, _ = require_ctp()
        auth = tdapi.CThostFtdcReqAuthenticateField()
        auth.BrokerID = self.client.broker_id
        auth.UserID = self.client.user_id
        auth.AppID = self.client.app_id
        auth.AuthCode = self.client.auth_code
        self.api.ReqAuthenticate(auth, self.client.next_request_id())

    def OnFrontDisconnected(self, nReason):
        log.warning("交易前置断开（reason=%s），等待 API 自动重连……", nReason)
        self.client.evq.put(TdDisconnected(reason=int(nReason)))

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(TdAuth(ok=ok, error_id=eid, error_msg=emsg))
        if not ok:
            return
        tdapi, _ = require_ctp()
        req = tdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = self.client.broker_id
        req.UserID = self.client.user_id
        req.Password = self.client.password
        self.api.ReqUserLogin(req, self.client.next_request_id())

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        if ok and pRspUserLogin is not None:
            self.client.session_id = int(getattr(pRspUserLogin, "SessionID", 0) or 0)
            self.client.front_id = int(getattr(pRspUserLogin, "FrontID", 0) or 0)
            self.client.max_order_ref = int(
                _clean_number(getattr(pRspUserLogin, "MaxOrderRef", 0))
            )
        self.client.evq.put(
            TdLogin(
                ok=ok,
                error_id=eid,
                error_msg=emsg,
                trading_day=str(getattr(pRspUserLogin, "TradingDay", "") or ""),
                session_id=self.client.session_id,
                front_id=self.client.front_id,
                max_order_ref=self.client.max_order_ref,
            )
        )
        if not ok:
            return
        tdapi, _ = require_ctp()
        s = tdapi.CThostFtdcSettlementInfoConfirmField()
        s.BrokerID = self.client.broker_id
        s.InvestorID = self.client.user_id
        self.api.ReqSettlementInfoConfirm(s, self.client.next_request_id())

    def OnRspSettlementInfoConfirm(self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(SettlementConfirmed(ok=ok, error_id=eid, error_msg=emsg))

    # ------------------------------------------------ 订单 / 成交回报

    def OnRtnOrder(self, pOrder):
        self.client.evq.put(
            OrderRtn(
                order_ref=str(getattr(pOrder, "OrderRef", "") or ""),
                instrument=str(getattr(pOrder, "InstrumentID", "") or ""),
                direction=str(getattr(pOrder, "Direction", "") or ""),
                offset=str(getattr(pOrder, "CombOffsetFlag", "") or "")[:1],
                limit_price=_clean_number(getattr(pOrder, "LimitPrice", 0)),
                volume_total=int(_clean_number(getattr(pOrder, "VolumeTotalOriginal", 0))),
                volume_traded=int(_clean_number(getattr(pOrder, "VolumeTraded", 0))),
                status=str(getattr(pOrder, "OrderStatus", "") or ""),
                status_msg=str(getattr(pOrder, "StatusMsg", "") or ""),
                order_sys_id=str(getattr(pOrder, "OrderSysID", "") or ""),
                insert_time=str(getattr(pOrder, "InsertTime", "") or ""),
            )
        )

    def OnRtnTrade(self, pTrade):
        self.client.evq.put(
            TradeRtn(
                order_ref=str(getattr(pTrade, "OrderRef", "") or ""),
                instrument=str(getattr(pTrade, "InstrumentID", "") or ""),
                direction=str(getattr(pTrade, "Direction", "") or ""),
                offset=str(getattr(pTrade, "OffsetFlag", "") or ""),
                price=_clean_number(getattr(pTrade, "Price", 0)),
                volume=int(_clean_number(getattr(pTrade, "Volume", 0))),
                trade_id=str(getattr(pTrade, "TradeID", "") or ""),
                trade_date=str(getattr(pTrade, "TradeDate", "") or ""),
                trade_time=str(getattr(pTrade, "TradeTime", "") or ""),
                trading_day=str(getattr(pTrade, "TradingDay", "") or ""),
            )
        )

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        # 成功（ErrorID=0）表示前置已接受报单，后续 OnRtnOrder 回报状态；
        # 仅错误时作为「报单被拒」事件投递。
        ok, eid, emsg = _rsp_ok(pRspInfo)
        if ok:
            return
        self.client.evq.put(
            OrderRejected(
                order_ref=str(getattr(pInputOrder, "OrderRef", "") or ""),
                instrument=str(getattr(pInputOrder, "InstrumentID", "") or ""),
                error_id=eid,
                error_msg=emsg,
            )
        )

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        _, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(
            OrderRejected(
                order_ref=str(getattr(pInputOrder, "OrderRef", "") or ""),
                instrument=str(getattr(pInputOrder, "InstrumentID", "") or ""),
                error_id=eid,
                error_msg=emsg or "报单被拒",
            )
        )

    def OnRspOrderAction(self, pInputOrderAction, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(
            ActionRsp(
                order_ref=str(getattr(pInputOrderAction, "OrderRef", "") or ""),
                instrument=str(getattr(pInputOrderAction, "InstrumentID", "") or ""),
                ok=ok,
                error_id=eid,
                error_msg=emsg,
            )
        )

    def OnErrRtnOrderAction(self, pOrderAction, pRspInfo):
        _, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(
            ActionRejected(
                order_ref=str(getattr(pOrderAction, "OrderRef", "") or ""),
                instrument=str(getattr(pOrderAction, "InstrumentID", "") or ""),
                error_id=eid,
                error_msg=emsg or "撤单被拒",
            )
        )

    # ------------------------------------------------ 查询响应

    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        if pTradingAccount is not None and ok:
            a = pTradingAccount
            self.client.evq.put(
                AccountRsp(
                    balance=_clean_number(getattr(a, "Balance", 0)),
                    available=_clean_number(getattr(a, "Available", 0)),
                    curr_margin=_clean_number(getattr(a, "CurrMargin", 0)),
                    close_profit=_clean_number(getattr(a, "CloseProfit", 0)),
                    position_profit=_clean_number(getattr(a, "PositionProfit", 0)),
                    commission=_clean_number(getattr(a, "Commission", 0)),
                    pre_balance=_clean_number(getattr(a, "PreBalance", 0)),
                )
            )
        if bIsLast:
            self.client.evq.put(
                QueryDone(query="account", ok=ok, error_id=eid, error_msg=emsg)
            )

    def OnRspQryInvestorPosition(self, pInvestorPosition, pRspInfo, nRequestID, bIsLast):
        ok, eid, emsg = _rsp_ok(pRspInfo)
        if pInvestorPosition is not None and ok:
            p = pInvestorPosition
            self.client.evq.put(
                PositionRsp(
                    instrument=str(getattr(p, "InstrumentID", "") or ""),
                    posi_direction=str(getattr(p, "PosiDirection", "") or ""),
                    position=int(_clean_number(getattr(p, "Position", 0))),
                    today_position=int(_clean_number(getattr(p, "TodayPosition", 0))),
                    yd_position=int(_clean_number(getattr(p, "YdPosition", 0))),
                    use_margin=_clean_number(getattr(p, "UseMargin", 0)),
                    position_cost=_clean_number(getattr(p, "PositionCost", 0)),
                    position_profit=_clean_number(getattr(p, "PositionProfit", 0)),
                )
            )
        if bIsLast:
            self.client.evq.put(
                QueryDone(query="position", ok=ok, error_id=eid, error_msg=emsg)
            )

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        _, eid, emsg = _rsp_ok(pRspInfo)
        self.client.evq.put(CtpError(error_id=eid, error_msg=emsg, source="td"))


# ---------------------------------------------------------------- 会话（组合）


class CtpSession:
    """行情 + 交易一体会话：统一事件队列 + 状态跟踪 + 便捷请求入口。

    消费模式：``next_event()`` 取事件；``wait_for(...)`` 等待特定事件
    （期间不匹配的事件缓存，保证不丢 tick / 回报）。
    """

    def __init__(self, cfg: dict):
        """cfg 为 ``config.yaml`` 的 ``live`` 段（含本地覆盖后的账号信息）。"""
        self.cfg = cfg
        self.evq: queue.Queue = queue.Queue()
        self.md = MdClient(
            front=cfg["md_front"],
            flow_dir=Path(cfg.get("flow_dir", "output/live/ctp_flow")) / "md",
            evq=self.evq,
            broker_id=str(cfg.get("broker_id", "9999")),
            user_id=str(cfg.get("investor_id", "")),
            password=str(cfg.get("password", "")),
        )
        self.td = TdClient(
            front=cfg["td_front"],
            flow_dir=Path(cfg.get("flow_dir", "output/live/ctp_flow")) / "td",
            evq=self.evq,
            broker_id=str(cfg.get("broker_id", "9999")),
            user_id=str(cfg.get("investor_id", "")),
            password=str(cfg.get("password", "")),
            app_id=str(cfg.get("app_id", "simnow_client_test")),
            auth_code=str(cfg.get("auth_code", "0000000000000000")),
        )
        self.md_ready = False
        self.td_ready = False
        self._pending: list[CtpEvent] = []

    # ------------------------------------------------ 生命周期

    def start(self) -> None:
        self.md.start()
        self.td.start()

    def close(self) -> None:
        self.md.close()
        self.td.close()

    # ------------------------------------------------ 事件消费

    def next_event(self, timeout: float = 0.5) -> CtpEvent | None:
        """取下一个事件（先吐缓存）；超时返回 None。"""
        if self._pending:
            return self._pending.pop(0)
        try:
            ev = self.evq.get(timeout=timeout)
        except queue.Empty:
            return None
        self._track(ev)
        return ev

    def drain(self) -> list[CtpEvent]:
        """非阻塞取出当前全部事件。"""
        events: list[CtpEvent] = []
        while True:
            try:
                ev = self.evq.get_nowait()
            except queue.Empty:
                break
            self._track(ev)
            events.append(ev)
        return events

    def wait_for(
        self,
        kinds: tuple[type, ...] | type,
        timeout: float = 10.0,
        pred=None,
    ) -> CtpEvent | None:
        """等待指定类型的事件（其余事件进入缓存待后续消费）。

        ``pred``：可选谓词（如 ``lambda e: e.instrument == "m2701"``）。
        注意：实现上直接消费底层队列（不走 ``next_event`` 的缓存优先逻辑），
        避免「取出 → 不匹配 → 放回缓存」与缓存优先互相打转。
        """
        classes = kinds if isinstance(kinds, tuple) else (kinds,)
        deadline = time.time() + timeout

        def _match(ev: CtpEvent) -> bool:
            return isinstance(ev, classes) and (pred is None or bool(pred(ev)))

        # 先查缓存（此前被缓存的不匹配事件可能已经匹配本次条件）
        for i, ev in enumerate(self._pending):
            if _match(ev):
                return self._pending.pop(i)
        while time.time() < deadline:
            try:
                ev = self.evq.get(timeout=min(0.5, max(0.05, deadline - time.time())))
            except queue.Empty:
                continue
            self._track(ev)
            if _match(ev):
                return ev
            self._pending.append(ev)
        return None

    def _track(self, ev: CtpEvent) -> None:
        if isinstance(ev, MdLogin) and ev.ok:
            self.md_ready = True
        elif isinstance(ev, SettlementConfirmed) and ev.ok:
            # 交易就绪 = 认证 + 登录 + 结算确认全部完成
            self.td_ready = True
        elif isinstance(ev, MdDisconnected):
            self.md_ready = False
        elif isinstance(ev, TdDisconnected):
            self.td_ready = False

    # ------------------------------------------------ 便捷请求

    def subscribe(self, instruments: list[str]) -> None:
        self.md.subscribe(instruments)

    def query_account(self) -> None:
        self.td.query_account()

    def query_position(self, instrument: str = "") -> None:
        self.td.query_position(instrument)

    def order_insert(
        self,
        instrument: str,
        direction: str,
        offset: str,
        price: float,
        volume: int,
    ) -> str:
        """报单并返回本地 OrderRef。"""
        order_ref = self.td.next_order_ref()
        self.td.order_insert(instrument, direction, offset, price, volume, order_ref)
        return order_ref

    def order_action(self, order_ref: str, instrument: str, order_sys_id: str = "") -> None:
        self.td.order_action(order_ref, instrument, order_sys_id)
