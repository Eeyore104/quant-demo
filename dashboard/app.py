"""Streamlit 看板（v1.4）：历史回测一览 / 详情 / 对比 / 图表。

启动（项目根目录）：
    uv run --extra dashboard streamlit run dashboard/app.py

说明：指标与明细来自结果库（output/db/quant.sqlite3）；图表为**最近一次跑批**产物
（历史 run 的完整图见其 HTML 报告文件）。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from src.results import db as results_db
from src.utils.config_loader import load_config, resolve_path

st.set_page_config(page_title="quant-demo 看板", layout="wide")


@st.cache_resource
def get_conn():
    cfg = load_config()
    storage = cfg.get("storage") or {}
    db_path = resolve_path(storage.get("db_path", "output/db/quant.sqlite3"))
    if not db_path.exists():
        return None
    return results_db.open_db(db_path)


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.2%}"


def fmt_num(v, nd: int = 2) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def runs_frame(runs: list[dict]) -> pd.DataFrame:
    rows = []
    for r in runs:
        rows.append(
            {
                "ID": r["id"],
                "时间": r["created_at"],
                "类型": "组合" if r["kind"] == "portfolio" else "单品种",
                "品种": r["symbol"] or "（组合池）",
                "策略 / 规模": r["strategy"] or "—",
                "收益": fmt_pct(r["total_return"]),
                "回撤": fmt_pct(r["max_drawdown"]),
                "夏普": fmt_num(r["sharpe"]),
                "交易数": r["trade_count"],
                "体检": r["health_verdict"] or "—",
                "报告文件": Path(str(r["report_html"])).name if r["report_html"] else "—",
            }
        )
    return pd.DataFrame(rows)


def run_label(r: dict) -> str:
    who = r["symbol"] or "组合池"
    return f"#{r['id']} · {r['created_at']} · {who}"


def main() -> None:
    st.title("quant-demo · 回测看板")
    conn = get_conn()
    if conn is None:
        st.warning("结果库不存在。先运行一次回测生成：`uv run python run_portfolio.py`")
        st.stop()

    runs = results_db.list_runs(conn, limit=200)
    if not runs:
        st.info("还没有回测记录。运行 `uv run python run_portfolio.py` 后刷新本页。")
        st.stop()

    labels = {run_label(r): r for r in runs}
    tab_overview, tab_detail, tab_compare, tab_figures = st.tabs(
        ["概览", "单次详情", "对比", "图表（最近跑批）"]
    )

    with tab_overview:
        latest = runs[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("最近一次", run_label(latest))
        c2.metric("收益", fmt_pct(latest["total_return"]))
        c3.metric("最大回撤", fmt_pct(latest["max_drawdown"]))
        c4.metric("夏普", fmt_num(latest["sharpe"]))
        st.dataframe(runs_frame(runs), hide_index=True, use_container_width=True)

    with tab_detail:
        sel = st.selectbox("选择回测记录", list(labels), index=0)
        run = labels[sel]
        rid = int(run["id"])
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("收益", fmt_pct(run["total_return"]))
        c2.metric("最大回撤", fmt_pct(run["max_drawdown"]))
        c3.metric("夏普", fmt_num(run["sharpe"]))
        c4.metric("胜率", fmt_pct(run["win_rate"]))
        c5.metric("交易数", run["trade_count"])
        c6.metric("手续费", fmt_num(run["total_commission"]))
        if run["report_html"]:
            st.caption(f"完整 HTML 报告（双击打开 / 发送）：{run['report_html']}")

        trades = results_db.get_trades(conn, rid)
        st.subheader(f"交易明细（{len(trades)} 笔）")
        if trades:
            tdf = pd.DataFrame(trades).drop(columns=["id", "run_id"])
            st.dataframe(tdf, hide_index=True, use_container_width=True, height=320)
        else:
            st.caption("无交易记录。")

        events = results_db.get_risk_events(conn, rid)
        if events:
            st.subheader(f"风控事件（{len(events)} 条）")
            edf = pd.DataFrame(events).drop(columns=["id", "run_id"])
            st.dataframe(edf, hide_index=True, use_container_width=True, height=240)

    with tab_compare:
        picks = st.multiselect(
            "选择 2 条以上记录对比", list(labels), default=list(labels)[: min(2, len(labels))]
        )
        if len(picks) >= 2:
            ids = [int(labels[p]["id"]) for p in picks]
            st.dataframe(
                runs_frame(results_db.compare_runs(conn, ids)),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption("至少选择 2 条记录。")

    with tab_figures:
        cfg = load_config()
        fig_dir = resolve_path((cfg.get("output") or {}).get("figure_dir", "output/figures"))
        pngs = [p for p in sorted(fig_dir.glob("*.png")) if not p.name.startswith("overview")]
        st.caption(
            "以下为最近一次跑批生成的图表（每次跑批刷新）；"
            "历史 run 的完整图见其 HTML 报告。"
        )
        if not pngs:
            st.info("还没有图表。先运行一次回测（run_backtest.py / run_portfolio.py）。")
        for p in pngs:
            st.image(str(p), caption=p.name, use_container_width=True)


main()
