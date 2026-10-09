"""研究体检（v1.1）：过拟合四件套补全 + Deflated Sharpe 校正。

模块划分：
- stats_utils        轻量统计工具（正态分布 / 矩统计，零新增依赖）
- deflated_sharpe    Deflated Sharpe（多重试验校正后的夏普显著性）
- param_sensitivity  参数邻域细检 + 悬崖式衰减检测
- monte_carlo        蒙特卡洛：信号重排随机对照 + 收益 Bootstrap
- cost_sensitivity   成本敏感性（×1.5 / ×2 / ×3 → 收益归零倍数）
- oos_usage          样本外使用次数登记（防止反复使用样本外）
- health             编排：汇总各项 → 体检报告（含总判定）
"""

from .health import HealthItem, HealthReport, run_health_check

__all__ = ["HealthItem", "HealthReport", "run_health_check"]
