# quant-demo：个人量化交易 Demo（期货回测最小闭环）

> 一个结构清晰、可一键运行、能产出专业回测报告的个人期货量化研究项目。
> 第一阶段目标：**回测最小闭环** —— 数据获取 → 清洗 → 策略 → 回测（含手续费/滑点）→ 绩效报告 → 参数对比。

---

## 快速开始

```powershell
# 1. 安装 uv（若已安装可跳过）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 进入项目目录
cd C:\Users\eeyor\WorkBuddy\demo\quant-demo

# 3. 安装依赖（自动创建虚拟环境，锁定 Python 3.12）
uv sync

# 4. 一键跑通回测（首次运行联网拉数据，之后走本地缓存）
uv run python run_backtest.py
```

> 若提示 "uv 不是命令"：重开一个终端，或改用完整路径 `%USERPROFILE%\.local\bin\uv.exe`。

跑完在 `output/` 下得到：

| 产物 | 说明 |
|---|---|
| `output/figures/equity.png` | 权益曲线图 |
| `output/figures/signals.png` | 价格与买卖点标注图 |
| `output/reports/backtest_report.txt` | 绩效报告（≥5 项指标 + 成本明细） |

参数对比（需先跑过一次上面的回测）：

```powershell
uv run python scripts/run_param_scan.py
```

运行单元测试：

```powershell
uv run pytest
```

---

## 目录结构

```
quant-demo/
├── config/config.yaml      # 全部可调参数（品种/周期/策略/成本）
├── src/
│   ├── data/               # 数据层：akshare 拉取 / CSV 缓存 / 清洗
│   ├── strategy/           # 策略层：基类 + 双均线 + 布林带
│   ├── engine/             # 引擎层：事件驱动回测 / 撮合 / 持仓
│   ├── report/             # 报告层：绩效指标 / 图表 / 报告
│   ├── execution/          # 【预留】SimNow/CTP 实盘适配接口
│   └── utils/              # 配置加载 / 日志
├── scripts/run_param_scan.py   # 参数扫描入口
├── run_backtest.py             # ★ 一键入口
├── tests/                      # 单元测试
├── data/                       # 数据缓存（gitignore）
└── output/                     # 报告与图表（gitignore）
```

## 改成你自己的策略 / 品种

全部在 `config/config.yaml` 里改，不用动代码：

| 想改什么 | 改哪里 | 示例 |
|---|---|---|
| 换品种 | `data.symbol` | `C0`（玉米主连）→ `M0`（豆粕主连） |
| 换回测区间 | `data.start_date` / `end_date` | `2021-10-01` ~ `2026-10-01` |
| 换策略 | `strategy.name` | `dual_ma` / `bollinger` |
| 调参数 | `strategy.params` | 双均线 `fast: 5, slow: 20` |
| 调成本 | `backtest.commission_per_lot` / `slippage_ticks` | 手续费 1.2 元/手、滑点 1 跳 |

---

## 设计要点（为什么这么做）

- **成交约定（防未来函数）**：T 日收盘产生信号，T+1 日开盘价 ± 滑点成交
- **自研轻量回测引擎**：bar 推进 / 撮合 / 成本逻辑全部可查、可讲；与未来 CTP 实盘
  共用同一策略接口（见 `src/execution/base.py` 预留的 ExecutionAdapter）
- **数据可离线复用**：`data/raw/` 缓存原始数据、`data/clean/` 缓存清洗结果，
  二次运行不重复联网
- **成本必计入**：报告明确打印累计手续费与累计滑点，回测结论不虚高

## 合规提示（重要）

- 当前阶段仅为**本地回测研究**，不涉及实盘，无合规风险
- 未来接实盘前：需开立期货账户，且**程序化交易必须先向期货公司报备、收到确认后**
  才能实盘交易（《期货市场程序化交易管理规定（试行）》，证监会公告〔2025〕12 号）
- 详见 `docs/01-量化背景与前置需求.md` 第 4 章「合规必读」

## 项目文档

| 文档 | 内容 |
|---|---|
| `docs/01-量化背景与前置需求.md` | 背景科普、路径对比、SimNow 科普、合规必读、前置需求 |
| `docs/02-PRD.md` | 产品需求、验收标准、里程碑 |
| `docs/03-系统设计与任务分解.md` | 架构设计、数据模型、任务分解、落地路线 |

## 常见问题

- **图表中文乱码**：已默认使用 Microsoft YaHei；若系统无此字体，改
  `src/report/plotter.py` 顶部字体列表
- **PowerShell 激活虚拟环境报错**：可跳过激活，直接用 `uv run <命令>` 运行
- **想加数据源（如 tushare）**：在 `src/data/loader.py` 的扩展位加实现即可，
  上层不感知
