# 更新日志（Changelog）

本文件记录 quant-demo 的重要变更。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- **图表总览（overview）**：`run_backtest.py` / `run_portfolio.py` 结束时自动刷新 `output/overview.png`（全部分区拼图，一眼扫完）与 `output/overview.html`（单页图表浏览，原尺寸、自包含、可直接分享）；新增手动刷新脚本 `scripts/make_overview.py`。
- **品种参数全量表（自动生成 + 多源交叉校验）**：新增 `scripts/build_symbol_params.py` —— 按「新浪品种清单 × 主力合约每跳毛利 × 东财合约详情 × openctp 费用表」生成 `config/symbol_params.yaml`（**覆盖 81/82 个品种**，含校验标签 verified / adjusted / fees）；`run_backtest.py` 三层解析（手动覆盖 → 自动全量表 → 默认值 + 醒目告警）——单品种链路换品种**只改 `data.symbol` 一行**，未覆盖品种会告警而非静默算错。

## [1.3.0] - 2026-10-10

### Added
- **风控体系（v1.3）**：新增 `src/engine/risk.py` ——
  - **ATR 风险预算**（第 4 头寸模式 `atr_risk`）：手数 = 权益 × 单笔风险 ÷（止损倍数 × ATR × 乘数），入场锁定、期间不随波动漂移；
  - **止损止盈**：固定 2×ATR（入场冻结）+ 移动 3×ATR（逐日更新）+ 止盈（开关，默认关）；**盘中触价**口径（跳空按开盘价、盘中按触发价；新仓次日生效）；止损后重入锁定（signal_reset / cooldown / immediate）；
  - **组合熔断**：三级阶梯（单日亏损→暂停 / 回撤→新开仓减半 / 连续亏损→冷却），收盘判定 → 次日生效，全平为最高级开关（默认关）；
  - **敞口上限**（单品种/组合名义，开仓事前检查）、**涨跌停方向感知**（跌停拒卖 / 涨停拒买，含「止不掉」如实呈现）、**成交量参与率上限**、保证金不足**降级执行**（v1.2 遗留承接）。
- **压力测试**（`src/analysis/stress.py`）：历史情景自动识别（单日/5 日跌幅 Top3 + 波动率最高 3 段；双口径：当时实际损失 + 1 手/品种标准化重估）；假设情景（跳空 −2σ/−3σ、连续 3 日跌停、相关性跳升分散化失效）；蒙特卡洛回撤分布（iid bootstrap ×500，P50~P99）。
- **风险指标**（`src/analysis/risk_metrics.py`）：日频 VaR/CVaR（历史模拟、滚动窗口、95%/99%，只用截至当日历史）、集中度（品种/板块名义占比）、相关性预警（滚动平均相关）。
- **报告与图表**：组合报告新增「风险与压力测试」章节 + **风控开 / 关对比**（裸奔 vs 全开自动双跑）；新增 3 张图（`risk_compare` / `risk_stress` / `risk_metrics`）与 4 个明细 CSV（`risk_events` / `risk_daily` / `stress_results` / `risk_compare`）。
- 配置新增 `risk` 段（**全部机制独立开关**）与 `portfolio.sizing` 的 `atr_risk` 参数；风控为 opt-in（`risk_cfg` 缺省时与 v1.2 逐笔一致）。
- 新增 49 个单元测试（总数 57 → **106**）：头寸 / 止损止盈 / 熔断 / 敞口 / 停板参与率 / 压力测试 / 风险指标 / 端到端 / 报告。

### Changed
- `run_portfolio.py` 升级为「风控全开 + 裸奔对比」双跑入口；组合报告标注风控口径。
- `pyproject.toml` 版本号 `1.2.0` → `1.3.0`。

## [1.2.0] - 2026-10-10

### Added
- **组合引擎（v1.2）**：新增 `run_portfolio.py` 一键入口与 `src/engine/{contracts,account,portfolio_engine,sizing}.py` —— 多品种并行、**共享资金池**（保证金占用按收盘价逐日重估、可用资金不足**拒绝开仓并记事件**）、逐日盯市；单品种链路零改动。
- **头寸规模三模式**：`fixed`（默认，每品种 1 手）/ `equal_weight` / `inv_vol`（波动率倒数），`config.portfolio.sizing` 配置。
- **组合报告与图表**：`src/report/{portfolio_report,portfolio_plot}.py` —— 组合汇总（收益 / 回撤 / 夏普 / 保证金占用与峰值利用率 / 约束事件）、逐品种汇总、相关性矩阵；4 张组合图表与 3 个明细 CSV。
- 品种池：8 个中小合约 · 4 板块（RB0 / I0 / M0 / P0 / SR0 / MA0 / TA0 / AL0）；合约参数表按 akshare 2026-10-10 数据核对（交易所保证金口径近似，可覆盖）。
- 新增 18 个单元测试（账户 / 头寸规模 / 组合引擎 / 组合报告），含**财务闭合**与**约束前逐笔一致性**断言。

### Changed
- `pyproject.toml` 版本号 `1.1.0` → `1.2.0`；`config.yaml` 新增 `portfolio` 配置段。

## [1.1.0] - 2026-10-09

### Added
- **过拟合体检（v1.1 · 四件套 + DSR）**：新增 `src/analysis/` —— ① 参数邻域细检（悬崖式衰减 / 孤峰检测、稳健区域占比）；② 蒙特卡洛对照（信号随机重排 × N 次 + 收益 Bootstrap 分布）；③ 成本敏感性（×1.5/×2/×3 → 收益归零倍数）；④ Deflated Sharpe 多重试验校正（Bailey & López de Prado）。
- 回测报告新增「过拟合体检」章节（逐项判定 通过/存疑/不通过 + 总判定）与 3 张新图表（参数邻域热力图 / 蒙特卡洛分布 / 成本敏感性）；`config.yaml` 新增 `health_check` 配置段（开关 / 次数 / 判定阈值全部可调）。
- **样本外使用次数登记**（`src/analysis/oos_usage.py`）：同一「品种 | 策略 | 分段」反复使用样本外时累计计数，超过 3 次自动将体检中的样本外结论降级为「存疑」。
- 统计函数全部自研（`stats_utils.py`：正态 CDF / 分位函数 / 偏度 / 峰度），**未新增任何第三方依赖**。
- 全部输出标注品种（如「玉米 C0（主力连续）」）：绩效报告头、样本外对比表、5 张图表标题、参数扫描控制台与 CSV（新增 `symbol` 列）。
- 新增 `scripts/list_symbols.py`：一键列出全部可回测品种（83 个主力连续合约）；品种中文名映射补齐至全覆盖。

### Fixed
- 体检报告悬崖描述：整数参数不再显示小数点（`4.0 → 5.0` 修复为 `4 → 5`），并补充对应单测断言。
- 样本外验证边界兜底：训练段为空（如 `split_date` 早于数据起点）时改为 WARN 并跳过，不再抛异常崩溃；空串 `split_date` 视为未提供，回退按 `ratio` 切分。
- 修复 `walkforward` 日志重复打印：复用 `engine` logger，避免子 logger 向父 logger 冒泡导致每条日志打两遍。
- 品种代码防呆：结尾字母 `O`（零/O 混淆笔误）自动纠正为数字 `0` 并告警；无效代码的报错信息会指向品种清单脚本。

### Changed
- `pyproject.toml` 版本号 `1.0.0` → `1.1.0`；回测一键运行现在额外输出体检章节（可用 `health_check.enabled` 关闭）。

## [1.0.0] - 2026-10-08

### Added
- **工程门面**：GitHub Actions CI（`uv sync --frozen` + `ruff check` + `pytest`）、MIT `LICENSE`、`CHANGELOG.md`、`ruff` 代码规范（lint + format）。
- **样本外验证**：新增 `src/engine/walkforward.py`，训练段参数择优 → 测试段检验，输出"样本内 / 样本外"对比表，`config.yaml` 新增 `backtest.oos` 开关。
- **策略扩展**：新增唐奇安通道策略 `src/strategy/donchian.py` 与策略自动注册表 `src/strategy/registry.py` —— **新增策略 = 新增一个文件**。
- **图表升级**：新增回撤区间图、月度收益热力图、参数扫描热力图。
- **工程卫生**：`.gitattributes` 统一 LF 行尾；修复文档格式化残留瑕疵。

### Changed
- `pyproject.toml` 版本号 `0.1.0` → `1.0.0`；新增 `[tool.ruff]` 配置。
- `run_backtest.py` / `scripts/run_param_scan.py` 改用策略注册表，参数扫描改由 `config.strategy.grid` 驱动。

## [0.1.0] - 2026-10-01

### Added
- 首个可用版本（MVP）：**期货回测最小闭环**。
- 数据层：akshare 拉取期货日线 + CSV 本地缓存 + 自动清洗（去重 / 去无效 bar）。
- 策略层：统一策略接口 `StrategyBase` + 双均线 / 布林带两个示例策略。
- 引擎层：自研轻量事件驱动回测引擎（T 日收盘出信号、T+1 开盘价 ± 滑点成交，内置手续费 / 滑点）。
- 报告层：绩效指标（收益 / 回撤 / 夏普 / 胜率 / 盈亏比 / 成本）+ 权益曲线 / 买卖点图 + 参数扫描对比表。
- 工程化：`uv` 依赖管理、`pytest` 单元测试、`config.yaml` 配置驱动。

[Unreleased]: https://github.com/Eeyore104/quant-demo/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/Eeyore104/quant-demo/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Eeyore104/quant-demo/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Eeyore104/quant-demo/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Eeyore104/quant-demo/releases/tag/v1.0.0
[0.1.0]: https://github.com/Eeyore104/quant-demo/releases/tag/v0.1.0
