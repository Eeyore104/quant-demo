"""信号桥测试（v2.0 轮 2）：重放目标 / ATR 手数 / latch 锁定 / 重启粘合。"""

import pandas as pd

from src.engine.contracts import ContractSpec
from src.live.ledger import VirtualLedger
from src.live.signals import SignalBridge


def _write_clean(tmp_path, symbol, closes, hl=5):
    rows = []
    for i, c in enumerate(closes):
        rows.append(
            {
                "date": f"2026-09-{i + 1:02d}",
                "open": c,
                "high": c + hl,
                "low": c - hl,
                "close": c,
                "volume": 100000,
                "open_interest": 500000,
            }
        )
    df = pd.DataFrame(rows)
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(clean_dir / f"{symbol}_clean.csv", index=False)


def _cfg(tmp_path, symbols, mode="fixed", lots=1):
    return {
        "data": {"clean_dir": str(tmp_path / "clean")},
        "portfolio": {
            "symbols": symbols,
            "sizing": {
                "mode": mode,
                "lots": lots,
                "atr_window": 3,
                "risk_pct": 0.015,
                "stop_atr_mult": 2.0,
                "max_lots_per_symbol": 10,
            },
        },
    }


def _contracts():
    return {
        "A0": ContractSpec("A0", 10, 1.0, 0.07, 1.2),
        "B0": ContractSpec("B0", 10, 1.0, 0.07, 1.2),
    }


def test_fixed_target_follows_signal(tmp_path):
    _write_clean(tmp_path, "A0", [100 + i for i in range(10)])  # 一路上涨 → +1
    # 先涨后跌（6 涨 + 4 跌）→ 快线跌破慢线 → -1
    _write_clean(tmp_path, "B0", [100 + i for i in range(6)] + [100, 95, 90, 85])
    cfg = _cfg(
        tmp_path,
        [
            {"code": "A0", "strategy": "dual_ma", "params": {"fast": 2, "slow": 3}},
            {"code": "B0", "strategy": "dual_ma", "params": {"fast": 2, "slow": 3}},
        ],
    )
    contracts = _contracts()
    ledger = VirtualLedger(100000, contracts, {"a2701": "A0", "b2701": "B0"})
    bridge = SignalBridge(cfg, contracts, ledger)
    targets = bridge.compute()

    assert targets["A0"].signal == 1
    assert targets["A0"].direction == 1 and targets["A0"].lots == 1
    assert targets["B0"].signal == -1
    assert targets["B0"].direction == -1 and targets["B0"].lots == 1
    # 副产品：收盘价 / ATR（供风控收盘更新）
    assert bridge.last_close["A0"] == 109.0
    assert bridge.last_atr["A0"] > 0


def test_atr_risk_lots_and_latch_hold(tmp_path):
    # 缓涨（每行 +1，TR 恒 10）→ +1 信号；ATR(3)=10
    # lots = int(100000 × 0.015 / (2.0 × 10 × 10)) = 7
    closes = [990 + i for i in range(12)]
    _write_clean(tmp_path, "A0", closes)
    cfg = _cfg(
        tmp_path,
        [{"code": "A0", "strategy": "dual_ma", "params": {"fast": 2, "slow": 3}}],
        mode="atr_risk",
    )
    contracts = _contracts()
    ledger = VirtualLedger(100000, contracts, {"a2701": "A0"})
    bridge = SignalBridge(cfg, contracts, ledger)
    t1 = bridge.compute()
    assert t1["A0"].direction == 1 and t1["A0"].lots == 7

    # 持仓后 ATR 放大（hl=30 → TR=60 → ATR=60）→ 锁定不刷新（非 fresh）
    ledger.apply_fill(
        instrument="a2701", direction="LONG", offset="OPEN", price=1000.0,
        volume=7, trading_day="2026-09-13",
    )
    _write_clean(tmp_path, "A0", closes, hl=30)
    t2 = bridge.compute()
    assert t2["A0"].direction == 1 and t2["A0"].lots == 7  # 保持锁定


def test_latch_rebuild_from_position_on_restart(tmp_path):
    # 重启场景：账本有持仓、latch 空 → 以持仓手数粘合（不会误平/误调）
    closes = [990 + i for i in range(12)]
    _write_clean(tmp_path, "A0", closes)
    cfg = _cfg(
        tmp_path,
        [{"code": "A0", "strategy": "dual_ma", "params": {"fast": 2, "slow": 3}}],
        mode="atr_risk",
    )
    contracts = _contracts()
    ledger = VirtualLedger(100000, contracts, {"a2701": "A0"})
    ledger.apply_fill(
        instrument="a2701", direction="LONG", offset="OPEN", price=1000.0,
        volume=3, trading_day="2026-09-13",
    )
    bridge = SignalBridge(cfg, contracts, ledger)  # 新实例，latch 为空
    t = bridge.compute()
    assert t["A0"].direction == 1 and t["A0"].lots == 3  # 与持仓一致


def test_latch_persistence_roundtrip(tmp_path):
    closes = [990 + i for i in range(12)]
    _write_clean(tmp_path, "A0", closes)
    cfg = _cfg(
        tmp_path,
        [{"code": "A0", "strategy": "dual_ma", "params": {"fast": 2, "slow": 3}}],
        mode="atr_risk",
    )
    contracts = _contracts()
    ledger = VirtualLedger(100000, contracts, {"a2701": "A0"})
    bridge = SignalBridge(cfg, contracts, ledger)
    bridge.compute()  # 生成 latch
    assert bridge.latch["A0"] == 7

    path = tmp_path / "signal_state.json"
    bridge.save(path)
    bridge2 = SignalBridge(cfg, contracts, ledger)
    assert bridge2.load(path) is True
    assert bridge2.latch["A0"] == 7
