"""结果库（SQLite）测试：schema / 登记 / 查询 / 明细 / 空数据兜底。"""

from types import SimpleNamespace

import pytest

from src.results import db


@pytest.fixture()
def conn(tmp_path):
    c = db.open_db(tmp_path / "test.sqlite3")
    yield c
    c.close()


def _metrics(**kw):
    base = dict(
        total_return=0.1,
        max_drawdown=-0.2,
        sharpe=0.5,
        win_rate=0.4,
        trade_count=3,
        total_commission=9.9,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _trade(
    date="2025-01-02",
    symbol="M0",
    direction="LONG",
    action="OPEN",
    price=3000.0,
    volume=1,
    commission=1.5,
    pnl=0.0,
):
    return SimpleNamespace(
        date=date,
        symbol=symbol,
        direction=direction,
        action=action,
        price=price,
        volume=volume,
        commission=commission,
        pnl=pnl,
    )


def test_open_db_creates_tables(conn):
    names = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "trades", "risk_events", "data_quality"} <= names


def test_record_backtest_run_and_query(conn):
    trades = [_trade(), _trade(action="CLOSE", pnl=120.0)]
    run_id = db.record_backtest_run(
        conn,
        symbol="M0",
        strategy="dual_ma",
        params={"fast": 5, "slow": 20},
        start_date="2021-10-01",
        end_date="2026-10-01",
        initial_capital=100000.0,
        metrics=_metrics(),
        trades=trades,
        health_verdict="存疑",
        meta={"data_bars": 1211},
    )
    run = db.get_run(conn, run_id)
    assert run["kind"] == "backtest"
    assert run["symbol"] == "M0"
    assert run["health_verdict"] == "存疑"
    assert run["trade_count"] == 3
    assert abs(run["total_return"] - 0.1) < 1e-12
    rows = db.get_trades(conn, run_id)
    assert len(rows) == 2
    assert rows[1]["action"] == "CLOSE"
    assert abs(rows[1]["pnl"] - 120.0) < 1e-12
    assert "data_bars" in (run["meta_json"] or "")  # 附加 meta 已入库


def test_record_portfolio_run_with_events(conn):
    events = [
        SimpleNamespace(date="2025-05-22", kind="fuse_pause", symbol="", detail="暂停新开仓"),
        SimpleNamespace(date="2025-05-23", kind="stop_fixed", symbol="RB0", detail="止损成交"),
    ]
    constraints = [
        SimpleNamespace(
            date="2025-05-22",
            symbol="RB0",
            needed_margin=5000.0,
            available=3000.0,
            describe=lambda: "2025-05-22 RB0：需要保证金 5,000 元 ＞ 可用资金 3,000 元",
        )
    ]
    run_id = db.record_portfolio_run(
        conn,
        symbols=["RB0", "M0"],
        labels={"RB0": "螺纹钢 RB0", "M0": "豆粕 M0"},
        start_date="2021-10-01",
        end_date="2026-10-01",
        initial_capital=100000.0,
        metrics=_metrics(),
        trades=[_trade(symbol="RB0")],
        risk_events=events,
        constraint_events=constraints,
        risk_enabled=True,
        sizing_desc="atr_risk（ATR 风险预算）",
    )
    run = db.get_run(conn, run_id)
    assert run["kind"] == "portfolio"
    assert run["symbol"] is None
    assert run["risk_enabled"] == 1
    assert "RB0" in (run["params_json"] or "")
    rows = db.get_risk_events(conn, run_id)
    assert len(rows) == 3
    assert {r["kind"] for r in rows} == {"fuse_pause", "stop_fixed", "constraint"}


def test_empty_inputs_are_ok(conn):
    run_id = db.record_backtest_run(
        conn,
        symbol="C0",
        strategy="dual_ma",
        params=None,
        start_date="2021-10-01",
        end_date="2026-10-01",
        initial_capital=100000.0,
        metrics=_metrics(trade_count=0),
        trades=[],
    )
    assert db.get_trades(conn, run_id) == []
    assert db.latest_run(conn)["id"] == run_id
    assert db.latest_run(conn, kind="portfolio") is None
    assert db.get_run(conn, 99999) is None


def test_list_runs_order_and_limit(conn):
    for i in range(3):
        db.record_backtest_run(
            conn,
            symbol=f"S{i}",
            strategy="dual_ma",
            params={},
            start_date="2021-10-01",
            end_date="2026-10-01",
            initial_capital=100000.0,
            metrics=_metrics(),
            trades=[],
        )
    runs = db.list_runs(conn, limit=2)
    assert [r["symbol"] for r in runs] == ["S2", "S1"]
    assert len(db.list_runs(conn)) == 3
    assert len(db.compare_runs(conn, [1, 3, 999])) == 2  # 不存在 id 被跳过


def test_quality_rows(conn):
    n = db.record_quality_rows(
        conn,
        "batch-1",
        [
            {"symbol": "M0", "check": "freshness", "status": "pass", "detail": "ok"},
            {"symbol": "M0", "check": "gap", "status": "warn", "detail": "间隔 12 天"},
        ],
    )
    assert n == 2
    rows = conn.execute(
        "SELECT * FROM data_quality WHERE batch_id = ?", ("batch-1",)
    ).fetchall()
    assert len(rows) == 2
    assert rows[1]["status"] == "warn"
