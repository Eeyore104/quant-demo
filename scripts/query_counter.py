"""只读排障：查询 SimNow 柜台资金与持仓（不下单，用于账实对账）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.live.ctp_api import CtpSession  # noqa: E402
from src.live.reconcile import query_counter  # noqa: E402
from src.utils.config_loader import load_config, resolve_path  # noqa: E402


def main() -> int:
    cfg = load_config()
    live = dict(cfg.get("live") or {})
    env_key = str(live.get("env", "first"))
    fronts = (live.get("envs") or {}).get(env_key) or {}
    scfg = {
        **live,
        "md_front": fronts["md_front"],
        "td_front": fronts["td_front"],
        "flow_dir": str(resolve_path(live.get("flow_dir", "output/live/ctp_flow"))),
    }
    session = CtpSession(scfg)
    session.start()
    try:
        snap = query_counter(session)
        print(json.dumps(snap.__dict__, ensure_ascii=False, indent=1, default=str))
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
