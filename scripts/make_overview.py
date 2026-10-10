"""手动刷新图表总览：扫描 output/figures → 生成 output/overview.png + overview.html。

用法（在项目根目录下执行）：
    uv run python scripts/make_overview.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.report.overview import build_overview  # noqa: E402
from src.utils.config_loader import load_config, resolve_path  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402


def main() -> None:
    log = get_logger("overview")
    cfg = load_config()
    fig_dir = resolve_path(cfg["output"]["figure_dir"])
    paths = build_overview(fig_dir, fig_dir.parent)
    if not paths:
        raise SystemExit(
            f"未找到任何图表（{fig_dir}），请先运行 run_backtest.py 或 run_portfolio.py"
        )
    for name, path in paths.items():
        log.info("%s 已生成：%s", name, path)
    print()
    for name, path in paths.items():
        print(f"{name}：{path}")


if __name__ == "__main__":
    main()
