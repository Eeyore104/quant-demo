"""事件流 / 日报 / 结果库 live_* 表测试（v2.0 轮 3）。"""

import json
import time

from src.engine.contracts import ContractSpec
from src.live.ctp_api import TradeRtn
from src.live.journal import (
    Journal,
    build_daily_report,
    positions_brief,
    save_report,
)
from src.live.ledger import VirtualLedger
from src.live.orders import ST_FILLED, LiveOrder
from src.results import db as results_db


def _ledger() -> VirtualLedger:
    contracts = {"M0": ContractSpec("M0", 10, 1.0, 0.07, 1.2)}
    lg = VirtualLedger(100000, contracts, {"m2701": "M0"})
    lg.begin_day()
    return lg


def _order() -> LiveOrder:
    return LiveOrder(
        order_ref="1", symbol="M0", instrument="m2701", direction="SHORT",
        offset="OPEN", price=3379.0, volume=1, status=ST_FILLED, filled=1,
        created_ts=time.time(), updated_ts=time.time(),
    )


def test_journal_writes_jsonl(tmp_path):
    j = Journal(tmp_path / "journal")
    e1 = j.log("signal", symbol="M0", detail="信号 -1 → 目标 1 手")
    j.log("open", symbol="M0", detail="OPEN SHORT 1 手", order_ref="1")
    assert e1["kind"] == "signal" and len(j.entries) == 2

    files = list((tmp_path / "journal").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    assert rows[0]["detail"] == "信号 -1 → 目标 1 手"
    assert rows[1]["order_ref"] == "1"


def test_journal_db_integration(tmp_path):
    conn = results_db.open_db(tmp_path / "db.sqlite3")
    sid = results_db.record_live_session(conn, env="first", mode="once")
    j = Journal(tmp_path / "journal", conn=conn, session_id=sid)
    j.log("signal", symbol="M0", detail="测试信号")

    events = results_db.get_live_events(conn, sid)
    assert len(events) == 1 and events[0]["kind"] == "signal"
    assert "测试信号" in events[0]["detail_json"]

    # 会话关闭
    results_db.close_live_session(conn, sid, status="closed")
    sessions = results_db.list_live_sessions(conn)
    assert sessions[0]["status"] == "closed" and sessions[0]["env"] == "first"


def test_record_order_and_fill_db(tmp_path):
    conn = results_db.open_db(tmp_path / "db.sqlite3")
    sid = results_db.record_live_session(conn, env="first", mode="once")
    j = Journal(tmp_path / "journal", conn=conn, session_id=sid)

    j.record_order_terminal(_order())
    orders = results_db.get_live_orders(conn, sid)
    assert len(orders) == 1
    assert orders[0]["status"] == "filled" and orders[0]["direction"] == "SHORT"

    ev = TradeRtn(
        order_ref="1", instrument="m2701", direction="1", offset="0",
        price=3379.0, volume=1, trade_id="T1",
        trade_date="2026-10-13", trade_time="09:30:00", trading_day="2026-10-13",
    )
    j.record_trade(ev, commission=1.5)
    fills = results_db.get_live_fills(conn, sid)
    assert len(fills) == 1 and fills[0]["commission"] == 1.5


def test_positions_and_equity_tables(tmp_path):
    conn = results_db.open_db(tmp_path / "db.sqlite3")
    n = results_db.record_live_positions(
        conn,
        "2026-10-13 15:05:00",
        [
            {
                "instrument": "m2701", "direction": "空", "volume": 1,
                "avg_price": 3379.0, "margin": 2365.3,
            }
        ],
        source="local",
    )
    assert n == 1
    n0 = results_db.record_live_positions(conn, "2026-10-13 15:05:00", [], source="counter")
    assert n0 == 0
    results_db.record_live_equity(
        conn, "2026-10-13 15:05:00", nominal_equity=99998.5, counter_equity=20_000_000.0
    )
    eq = results_db.latest_live_equity(conn)
    assert len(eq) == 1 and eq[0]["nominal_equity"] == 99998.5


def test_build_daily_report_sections():
    lg = _ledger()
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="OPEN", price=3379.0,
        volume=1, trading_day="2026-10-13",
    )
    entries = [
        {"date": "2026-10-13", "kind": "signal", "symbol": "M0", "detail": "M0 信号 -1"},
        {"date": "2026-10-13", "kind": "open", "symbol": "M0", "detail": "OPEN SHORT 1 手"},
        {"date": "2026-10-13", "kind": "fill", "symbol": "m2701", "detail": "m2701 成交 1 手"},
        {"date": "2026-10-12", "kind": "signal", "symbol": "RB0", "detail": "昨日信号（不应出现）"},
    ]
    md = build_daily_report(date="2026-10-13", ledger=lg, entries=entries, fuse_state="正常")
    assert "SimNow 仿真日报" in md
    assert "M0 信号 -1" in md
    assert "m2701 成交 1 手" in md
    assert "M0-1" in md  # 持仓简述
    assert "昨日信号" not in md  # 跨日过滤
    assert "模拟 ≠ 实盘" in md


def test_positions_brief_and_save_report(tmp_path):
    lg = _ledger()
    assert positions_brief(lg) == "空仓"
    lg.apply_fill(
        instrument="m2701", direction="SHORT", offset="OPEN", price=3379.0,
        volume=1, trading_day="2026-10-13",
    )
    assert positions_brief(lg) == "M0-1"

    path = save_report("**测试日报**", tmp_path / "reports", "2026-10-13")
    assert path.exists() and "测试日报" in path.read_text(encoding="utf-8")
