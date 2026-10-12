"""SimNow 仿真链路（v2.0 · 设计见 docs/16）。

模块地图（分轮落地）：
- ``ctp_api``：openctp-ctp 薄封装（行情 / 交易生命周期、回调 → 事件队列）【轮 1】
- ``orders`` / ``ledger`` / ``risk_guard`` / ``signals`` / ``adapter`` /
  ``reconcile`` / ``journal`` / ``schedule``【轮 2 / 轮 3】

约定：本包不 import 回测链路（src/engine 等）的既有文件；引擎与策略零改动。
"""
