"""v2.0 仿真链路 · 轮 1 测试：ctp_api 纯逻辑 + 探针辅助。

原则：
- 不依赖 openctp-ctp 库（CI 只装基础依赖，live 为可选组）；
- 不连接真实环境（start() 不调用；事件管道直接投喂队列）。
"""

import queue
import time

import pytest

from scripts import probe_simnow
from src.live import ctp_api


def _min_cfg(flow_dir) -> dict:
    return {
        "md_front": "tcp://127.0.0.1:1",
        "td_front": "tcp://127.0.0.1:2",
        "flow_dir": str(flow_dir),
        "broker_id": "9999",
        "investor_id": "123456",
        "password": "x",
        "app_id": "simnow_client_test",
        "auth_code": "0" * 16,
    }


# ---------------------------------------------------------------- 清洗与常量


def test_clean_number_valid_and_invalid():
    assert ctp_api._clean_number(3100.5) == 3100.5
    assert ctp_api._clean_number("2.5") == 2.5
    assert ctp_api._clean_number(None) == 0.0
    assert ctp_api._clean_number("abc") == 0.0
    # CTP 无效值（DBL_MAX 量级）归零
    assert ctp_api._clean_number(1.7976931348623157e308) == 0.0


def test_rsp_ok_three_states():
    assert ctp_api._rsp_ok(None) == (True, 0, "")

    class _Info:
        ErrorID = 0
        ErrorMsg = ""

    assert ctp_api._rsp_ok(_Info()) == (True, 0, "")

    class _Err:
        ErrorID = 42
        ErrorMsg = "boom"

    assert ctp_api._rsp_ok(_Err()) == (False, 42, "boom")


def test_status_and_direction_text_maps():
    assert ctp_api.STATUS_TEXT["0"] == "全部成交"
    assert ctp_api.STATUS_TEXT["5"] == "已撤单"
    assert ctp_api.DIRECTION_TEXT["0"] == "买"
    assert ctp_api.OFFSET_TEXT["3"] == "平今"


# ---------------------------------------------------------------- 事件模型


def test_event_kind_names():
    assert ctp_api.Tick().kind == "tick"
    assert ctp_api.MdConnected().kind == "mdconnected"
    assert ctp_api.OrderRtn().kind == "orderrtn"


def test_order_rtn_done_and_status_text():
    rtn = ctp_api.OrderRtn(status=ctp_api.ORDER_STATUS_NO_TRADE_QUEUEING)
    assert rtn.done is False
    assert rtn.status_text == "未成交(排队)"

    for s in ("0", "5", "2", "4"):
        assert ctp_api.OrderRtn(status=s).done is True


def test_position_direction_text():
    assert ctp_api.PositionRsp(posi_direction="2").direction_text == "多"
    assert ctp_api.PositionRsp(posi_direction="3").direction_text == "空"


# ---------------------------------------------------------------- 会话管道（不连网）


def test_session_config_plumbing(tmp_path):
    sess = ctp_api.CtpSession(_min_cfg(tmp_path))
    assert sess.md.front == "tcp://127.0.0.1:1"
    assert sess.td.front == "tcp://127.0.0.1:2"
    assert sess.td.broker_id == "9999"
    assert sess.td.app_id == "simnow_client_test"
    # 流文件目录：行情 / 交易两套独立
    assert sess.md.flow_dir.name == "md"
    assert sess.td.flow_dir.name == "td"


def test_session_tracks_ready_states(tmp_path):
    sess = ctp_api.CtpSession(_min_cfg(tmp_path))
    assert sess.md_ready is False and sess.td_ready is False

    sess.evq.put(ctp_api.MdLogin(ok=True))
    assert isinstance(sess.next_event(timeout=0.1), ctp_api.MdLogin)
    assert sess.md_ready is True

    sess.evq.put(ctp_api.TdLogin(ok=True))
    sess.evq.put(ctp_api.SettlementConfirmed(ok=True))
    sess.next_event(timeout=0.1)
    sess.next_event(timeout=0.1)
    assert sess.td_ready is True

    sess.evq.put(ctp_api.MdDisconnected(reason=3))
    sess.next_event(timeout=0.1)
    assert sess.md_ready is False


def test_wait_for_predicate_and_pending(tmp_path):
    sess = ctp_api.CtpSession(_min_cfg(tmp_path))
    sess.evq.put(ctp_api.Tick(instrument="a", last_price=1.0))
    sess.evq.put(ctp_api.Tick(instrument="b", last_price=2.0))

    ev = sess.wait_for(ctp_api.Tick, timeout=1.0, pred=lambda e: e.instrument == "b")
    assert ev is not None and ev.instrument == "b"
    # 不匹配的 tick 进入缓存，随后仍可取到（不丢事件）
    ev2 = sess.next_event(timeout=0.1)
    assert ev2 is not None and ev2.instrument == "a"


def test_wait_for_timeout_returns_none(tmp_path):
    sess = ctp_api.CtpSession(_min_cfg(tmp_path))
    t0 = time.time()
    assert sess.wait_for(ctp_api.Tick, timeout=0.2) is None
    assert time.time() - t0 < 2.0


def test_drain_collects_all(tmp_path):
    sess = ctp_api.CtpSession(_min_cfg(tmp_path))
    sess.evq.put(ctp_api.Tick(instrument="a"))
    sess.evq.put(ctp_api.Tick(instrument="b"))
    events = sess.drain()
    assert [e.instrument for e in events] == ["a", "b"]


# ---------------------------------------------------------------- 交易客户端辅助


def test_next_order_ref_increments(tmp_path):
    td = ctp_api.TdClient(
        front="tcp://x",
        flow_dir=tmp_path,
        evq=queue.Queue(),
        user_id="1",
        password="p",
    )
    assert td.next_order_ref() == "1"
    assert td.next_order_ref() == "2"
    assert td.next_order_ref() == "3"


def test_require_ctp_missing_raises(monkeypatch):
    monkeypatch.setattr(ctp_api, "HAS_CTP", False)
    with pytest.raises(RuntimeError, match="openctp-ctp"):
        ctp_api.require_ctp()


def test_require_ctp_present(monkeypatch):
    fake = (object(), object())
    monkeypatch.setattr(ctp_api, "HAS_CTP", True)
    monkeypatch.setattr(ctp_api, "_mdapi", fake[0])
    monkeypatch.setattr(ctp_api, "_tdapi", fake[1])
    mdapi, tdapi = ctp_api.require_ctp()
    assert mdapi is fake[0] and tdapi is fake[1]


# ---------------------------------------------------------------- 探针辅助


def test_probe_mask():
    assert probe_simnow.mask("123456") == "123**6"
    assert probe_simnow.mask("12") == "1**"


def test_probe_step_and_summary(capsys):
    p = probe_simnow.Probe()
    t0 = time.time()
    p.step("步骤A", True, "ok", t0)
    p.step("步骤B", False, "bad", t0)
    assert p.summary() is False
    out = capsys.readouterr().out
    assert "1/2 步通过" in out
    assert "未通过：步骤B" in out
