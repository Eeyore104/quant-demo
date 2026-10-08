# 更新日志（Changelog）

本文件记录 quant-demo 的重要变更。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- 全部输出标注品种（如「玉米 C0（主力连续）」）：绩效报告头、样本外对比表、5 张图表标题、参数扫描控制台与 CSV（新增 `symbol` 列）。
- 新增 `scripts/list_symbols.py`：一键列出全部可回测品种（83 个主力连续合约）；品种中文名映射补齐至全覆盖。

### Fixed
- 样本外验证边界兜底：训练段为空（如 `split_date` 早于数据起点）时改为 WARN 并跳过，不再抛异常崩溃；空串 `split_date` 视为未提供，回退按 `ratio` 切分。
- 修复 `walkforward` 日志重复打印：复用 `engine` logger，避免子 logger 向父 logger 冒泡导致每条日志打两遍。

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

[Unreleased]: https://github.com/Eeyore104/quant-demo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Eeyore104/quant-demo/releases/tag/v1.0.0
[0.1.0]: https://github.com/Eeyore104/quant-demo/releases/tag/v0.1.0
