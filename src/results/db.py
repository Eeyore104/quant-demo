"""结果库（SQLite）：回测结果 / 交易明细 / 风控事件 / 数据质量记录。

定位（v1.4 设计锁定「结果库优先」）：行情维持 CSV 链路不变，本模块只承载
「跑批产物」——每次跑批一行总记录（``runs``）+ 交易与事件明细（``trades`` /
``risk_events``）+ 数据质量批次（``data_quality``）。

- 数据库文件位于 ``output/``（不进 git）；WAL 模式（单机多读单写安全）；
- 登记失败由调用方兜底（不影响回测结果）；查询函数供看板 / 命令行共用。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,              -- backtest | portfolio
    symbol            TEXT,                       -- 单品种代码；组合为 NULL
    strategy          TEXT,                       -- 单品种策略名；组合为规模口径描述
    params_json       TEXT,                       -- 策略参数 / 组合摘要（品种池等）
    start_date        TEXT,
    end_date          TEXT,
    initial_capital   REAL,
    total_return      REAL,
    max_drawdown      REAL,
    sharpe            REAL,
    win_rate          REAL,
    trade_count       INTEGER,
    total_commission  REAL,
    health_verdict    TEXT,                       -- 体检总判定（单品种）
    risk_enabled      INTEGER,                    -- 风控开关状态（组合）
    report_html       TEXT,                       -- HTML 报告路径（轮 2 起填充）
    meta_json         TEXT,                       -- 环境与数据摘要（git / python / bar 数…）
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    symbol      TEXT,
    direction   TEXT,                             -- LONG / SHORT
    action      TEXT,                             -- OPEN / CLOSE
    date        TEXT,
    price       REAL,
    volume      INTEGER,
    commission  REAL,
    pnl         REAL                              -- 平仓已实现盈亏（开仓为 0）
);
CREATE TABLE IF NOT EXISTS risk_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    date     TEXT,
    symbol   TEXT,
    kind     TEXT,                                -- 止损/熔断/停板/constraint（保证金）…
    detail   TEXT
);
CREATE TABLE IF NOT EXISTS data_quality (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT,
    symbol      TEXT,
    check_item  TEXT,                             -- 检查项名（避让 SQL 关键字 check）
    status      TEXT,                             -- pass | warn | error
    detail      TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_run    ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run    ON risk_events(run_id);
CREATE INDEX IF NOT EXISTS idx_quality_batch ON data_quality(batch_id);

-- ============ v2.0 仿真链路（live_*，docs/16 §3.9）============
CREATE TABLE IF NOT EXISTS live_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT,
    env         TEXT,                             -- first | 724
    mode        TEXT,                             -- live | dry_run | once
    status      TEXT,                             -- running | closed
    meta_json   TEXT
);
CREATE TABLE IF NOT EXISTS live_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(id),
    order_ref   TEXT,
    instrument  TEXT,
    direction   TEXT,                             -- 业务方向：LONG / SHORT
    offset      TEXT,                             -- OPEN/CLOSE/CLOSE_TODAY/CLOSE_YESTERDAY
    price       REAL,
    volume      INTEGER,
    status      TEXT,                             -- 本地订单状态（见 live/orders）
    ts_submit   TEXT,
    ts_update   TEXT,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS live_fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(id),
    order_ref   TEXT,
    trade_id    TEXT,
    instrument  TEXT,
    direction   TEXT,
    offset      TEXT,
    price       REAL,
    volume      INTEGER,
    ts          TEXT,
    commission  REAL
);
CREATE TABLE IF NOT EXISTS live_positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts  TEXT,
    instrument   TEXT,
    direction    TEXT,                            -- 多 / 空
    volume       INTEGER,
    avg_price    REAL,
    margin       REAL,
    source       TEXT                             -- local | counter
);
CREATE TABLE IF NOT EXISTS live_equity (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts      TEXT,
    nominal_equity   REAL,                        -- 本地虚拟账本（10 万口径）
    counter_equity   REAL,                        -- 柜台（2000 万口径）
    margin_used      REAL,
    available        REAL,
    day_pnl          REAL
);
CREATE TABLE IF NOT EXISTS live_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES live_sessions(id),
    ts          TEXT,
    kind        TEXT,                             -- 事件类型（signal / open / fill / …）
    symbol      TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_orders_session ON live_orders(session_id);
CREATE INDEX IF NOT EXISTS idx_live_fills_session  ON live_fills(session_id);
CREATE INDEX IF NOT EXISTS idx_live_events_session ON live_events(session_id);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）结果库：自动建表 + WAL 模式。

    ``check_same_thread=False``：连接允许跨线程使用（看板场景——Streamlit
    重跑脚本可能发生在不同线程）。本项目 SQLite 为 SERIALIZED 线程模式
    （``threadsafety == 3``，模块内部自带互斥），跨线程共享连接是安全的。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------- 登记


def _num(obj, name: str, cast=float):
    """从对象安全取数值（缺失 / None → None）。"""
    value = getattr(obj, name, None)
    return None if value is None else cast(value)


def _env_meta(extra: dict | None = None) -> dict:
    """环境摘要：python 版本 / git 短哈希（尽力而为）+ 调用方附加字段。"""
    meta: dict = {"python": sys.version.split()[0], "created_at": _now()}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = result.stdout.strip()
        if sha:
            meta["git"] = sha
    except Exception:  # noqa: BLE001 — 环境信息缺失不影响登记
        pass
    if extra:
        meta.update(extra)
    return meta


def _trade_rows(run_id: int, trades) -> list[tuple]:
    rows = []
    for t in trades or ():
        rows.append(
            (
                run_id,
                str(getattr(t, "symbol", "") or ""),
                str(getattr(t, "direction", "") or ""),
                str(getattr(t, "action", "") or ""),
                str(getattr(t, "date", ""))[:10],
                float(getattr(t, "price", 0.0) or 0.0),
                int(getattr(t, "volume", 0) or 0),
                float(getattr(t, "commission", 0.0) or 0.0),
                float(getattr(t, "pnl", 0.0) or 0.0),
            )
        )
    return rows


def _event_rows(run_id: int, risk_events, constraint_events) -> list[tuple]:
    rows = []
    for e in risk_events or ():
        rows.append(
            (
                run_id,
                str(getattr(e, "date", ""))[:10],
                str(getattr(e, "symbol", "") or ""),
                str(getattr(e, "kind", "") or ""),
                str(getattr(e, "detail", "") or ""),
            )
        )
    for e in constraint_events or ():
        detail = e.describe() if hasattr(e, "describe") else str(e)
        rows.append(
            (
                run_id,
                str(getattr(e, "date", ""))[:10],
                str(getattr(e, "symbol", "") or ""),
                "constraint",  # v1.2 保证金「拒绝开仓」事件统一归入
                detail,
            )
        )
    return rows


def record_backtest_run(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    strategy: str,
    params: dict | None,
    start_date: str,
    end_date: str,
    initial_capital: float,
    metrics,
    trades=(),
    health_verdict: str | None = None,
    report_html: str | None = None,
    meta: dict | None = None,
) -> int:
    """登记一次单品种回测（含交易明细）；返回 ``run_id``。"""
    cur = conn.execute(
        """INSERT INTO runs (kind, symbol, strategy, params_json, start_date, end_date,
               initial_capital, total_return, max_drawdown, sharpe, win_rate, trade_count,
               total_commission, health_verdict, risk_enabled, report_html, meta_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "backtest",
            symbol,
            strategy,
            json.dumps(params or {}, ensure_ascii=False),
            start_date,
            end_date,
            float(initial_capital),
            _num(metrics, "total_return"),
            _num(metrics, "max_drawdown"),
            _num(metrics, "sharpe"),
            _num(metrics, "win_rate"),
            _num(metrics, "trade_count", int),
            _num(metrics, "total_commission"),
            health_verdict,
            None,
            report_html,
            json.dumps(_env_meta(meta), ensure_ascii=False),
            _now(),
        ),
    )
    run_id = int(cur.lastrowid)
    rows = _trade_rows(run_id, trades)
    if rows:
        conn.executemany(
            "INSERT INTO trades (run_id, symbol, direction, action, date, price, "
            "volume, commission, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()
    return run_id


def record_portfolio_run(
    conn: sqlite3.Connection,
    *,
    symbols: list[str],
    labels: dict[str, str] | None = None,
    start_date: str,
    end_date: str,
    initial_capital: float,
    metrics,
    trades=(),
    risk_events=(),
    constraint_events=(),
    risk_enabled: bool = False,
    sizing_desc: str | None = None,
    report_html: str | None = None,
    meta: dict | None = None,
) -> int:
    """登记一次组合回测（含交易明细 + 风控 / 保证金事件）；返回 ``run_id``。"""
    summary = {"symbols": list(symbols), "labels": labels or {}}
    cur = conn.execute(
        """INSERT INTO runs (kind, symbol, strategy, params_json, start_date, end_date,
               initial_capital, total_return, max_drawdown, sharpe, win_rate, trade_count,
               total_commission, health_verdict, risk_enabled, report_html, meta_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "portfolio",
            None,
            sizing_desc,
            json.dumps(summary, ensure_ascii=False),
            start_date,
            end_date,
            float(initial_capital),
            _num(metrics, "total_return"),
            _num(metrics, "max_drawdown"),
            _num(metrics, "sharpe"),
            _num(metrics, "win_rate"),
            _num(metrics, "trade_count", int),
            _num(metrics, "total_commission"),
            None,
            1 if risk_enabled else 0,
            report_html,
            json.dumps(_env_meta(meta), ensure_ascii=False),
            _now(),
        ),
    )
    run_id = int(cur.lastrowid)
    rows = _trade_rows(run_id, trades)
    if rows:
        conn.executemany(
            "INSERT INTO trades (run_id, symbol, direction, action, date, price, "
            "volume, commission, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    events = _event_rows(run_id, risk_events, constraint_events)
    if events:
        conn.executemany(
            "INSERT INTO risk_events (run_id, date, symbol, kind, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            events,
        )
    conn.commit()
    return run_id


def record_quality_rows(conn: sqlite3.Connection, batch_id: str, rows) -> int:
    """登记一批数据质量检查结果（轮 2 质量模块调用）。

    ``rows``：``[{symbol, check, status, detail}, ...]``；返回写入行数。
    """
    now = _now()
    payload = [
        (
            batch_id,
            str(r.get("symbol", "")),
            str(r.get("check", "")),
            str(r.get("status", "")),
            str(r.get("detail", "")),
            now,
        )
        for r in rows or ()
    ]
    if payload:
        conn.executemany(
            "INSERT INTO data_quality (batch_id, symbol, check_item, status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            payload,
        )
        conn.commit()
    return len(payload)


# ---------------------------------------------------------------- 查询


def list_runs(conn: sqlite3.Connection, *, limit: int = 50, kind: str | None = None) -> list[dict]:
    """按 id 倒序返回跑批记录（最新在前）。"""
    sql = "SELECT * FROM runs"
    args: list = []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


def latest_run(conn: sqlite3.Connection, kind: str | None = None) -> dict | None:
    """最近一次跑批记录（可指定 kind）。"""
    runs = list_runs(conn, limit=1, kind=kind)
    return runs[0] if runs else None


def get_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (int(run_id),)).fetchone()
    return dict(row) if row else None


def get_trades(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE run_id = ? ORDER BY id", (int(run_id),)
    )
    return [dict(r) for r in rows]


def get_risk_events(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM risk_events WHERE run_id = ? ORDER BY id", (int(run_id),)
    )
    return [dict(r) for r in rows]


def compare_runs(conn: sqlite3.Connection, run_ids: list[int]) -> list[dict]:
    """多 run 对比视图（按传入顺序返回指定字段行）。"""
    out = []
    for rid in run_ids:
        run = get_run(conn, int(rid))
        if run:
            out.append(run)
    return out


# ---------------------------------------------------------------- v2.0 仿真（live_*）


def record_live_session(
    conn: sqlite3.Connection,
    *,
    env: str,
    mode: str,
    start_ts: str | None = None,
    meta: dict | None = None,
) -> int:
    """登记一次仿真会话（进程启动时）；返回 session_id。"""
    cur = conn.execute(
        "INSERT INTO live_sessions (start_ts, env, mode, status, meta_json)"
        " VALUES (?, ?, ?, 'running', ?)",
        (start_ts or _now(), env, mode, json.dumps(meta or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def close_live_session(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    status: str = "closed",
    end_ts: str | None = None,
    meta: dict | None = None,
) -> None:
    """关闭仿真会话（进程退出时）。"""
    if meta is not None:
        conn.execute(
            "UPDATE live_sessions SET end_ts = ?, status = ?, meta_json = ? WHERE id = ?",
            (end_ts or _now(), status, json.dumps(meta, ensure_ascii=False), int(session_id)),
        )
    else:
        conn.execute(
            "UPDATE live_sessions SET end_ts = ?, status = ? WHERE id = ?",
            (end_ts or _now(), status, int(session_id)),
        )
    conn.commit()


def record_live_order(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    order_ref: str,
    instrument: str,
    direction: str,
    offset: str,
    price: float,
    volume: int,
    status: str,
    ts_submit: str = "",
    ts_update: str = "",
    detail: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO live_orders (session_id, order_ref, instrument, direction, offset,"
        " price, volume, status, ts_submit, ts_update, detail_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(session_id),
            order_ref,
            instrument,
            direction,
            offset,
            float(price),
            int(volume),
            status,
            ts_submit,
            ts_update,
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_live_fill(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    order_ref: str,
    trade_id: str,
    instrument: str,
    direction: str,
    offset: str,
    price: float,
    volume: int,
    ts: str,
    commission: float = 0.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO live_fills (session_id, order_ref, trade_id, instrument, direction,"
        " offset, price, volume, ts, commission) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(session_id),
            order_ref,
            trade_id,
            instrument,
            direction,
            offset,
            float(price),
            int(volume),
            ts,
            float(commission),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_live_event(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    ts: str,
    kind: str,
    symbol: str = "",
    detail: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO live_events (session_id, ts, kind, symbol, detail_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (int(session_id), ts, kind, symbol, json.dumps(detail or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_live_positions(
    conn: sqlite3.Connection, snapshot_ts: str, rows: list[dict], *, source: str
) -> int:
    """批量登记持仓快照；``rows``：{instrument, direction, volume, avg_price?, margin?}。"""
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO live_positions (snapshot_ts, instrument, direction, volume,"
        " avg_price, margin, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                snapshot_ts,
                r["instrument"],
                r["direction"],
                int(r["volume"]),
                float(r.get("avg_price", 0.0)),
                float(r.get("margin", 0.0)),
                source,
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def record_live_equity(
    conn: sqlite3.Connection,
    snapshot_ts: str,
    *,
    nominal_equity: float,
    counter_equity: float = 0.0,
    margin_used: float = 0.0,
    available: float = 0.0,
    day_pnl: float = 0.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO live_equity (snapshot_ts, nominal_equity, counter_equity,"
        " margin_used, available, day_pnl) VALUES (?, ?, ?, ?, ?, ?)",
        (
            snapshot_ts,
            float(nominal_equity),
            float(counter_equity),
            float(margin_used),
            float(available),
            float(day_pnl),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_live_sessions(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM live_sessions ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]


def get_live_orders(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM live_orders WHERE session_id = ? ORDER BY id", (int(session_id),)
    )
    return [dict(r) for r in rows]


def get_live_fills(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM live_fills WHERE session_id = ? ORDER BY id", (int(session_id),)
    )
    return [dict(r) for r in rows]


def get_live_events(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM live_events WHERE session_id = ? ORDER BY id", (int(session_id),)
    )
    return [dict(r) for r in rows]


def latest_live_equity(conn: sqlite3.Connection, *, limit: int = 240) -> list[dict]:
    """最近的权益快照（跨会话，按时间正序返回）。"""
    rows = conn.execute(
        "SELECT * FROM live_equity ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    return [dict(r) for r in reversed(list(rows))]
