"""配置加载：全项目唯一的配置读取入口。"""

from pathlib import Path

import yaml

# 项目根目录（src/utils/config_loader.py 往上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> dict:
    """读取 config.yaml，返回配置字典。"""
    cfg_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbol_params() -> dict:
    """读取自动生成的品种参数全量表（``config/symbol_params.yaml``）。

    由 ``scripts/build_symbol_params.py`` 生成；文件缺失时返回空字典
    （此时 run_backtest 对未手动登记的品种走默认值 + 告警）。
    """
    path = PROJECT_ROOT / "config" / "symbol_params.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_path(rel: str) -> Path:
    """把配置里的相对路径解析为项目根下的绝对路径。"""
    return PROJECT_ROOT / rel
