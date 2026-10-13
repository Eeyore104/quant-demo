"""配置加载：全项目唯一的配置读取入口。

- ``load_config()``：读取 ``config.yaml``，并与 ``config/config.local.yaml``
  （可选、不进 git）做深度合并——本地文件优先，用于存放敏感信息（如 webhook URL）；
- ``get_webhook_url()``：企业微信 webhook URL（环境变量 > 本地配置 > None）。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# 项目根目录（src/utils/config_loader.py 往上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并（``override`` 优先；仅两边都是 dict 时递归，其余直接覆盖）。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict:
    """读取 config.yaml，返回配置字典。

    未指定 ``path`` 时（默认入口）：若存在 ``config/config.local.yaml``，
    与其做深度合并（本地覆盖优先）。指定 ``path`` 时不合并（测试 / 特殊用途）。
    """
    if path is not None:
        return _read_yaml(Path(path))
    cfg = _read_yaml(PROJECT_ROOT / "config" / "config.yaml")
    local = PROJECT_ROOT / "config" / "config.local.yaml"
    if local.exists():
        cfg = _deep_merge(cfg, _read_yaml(local))
    return cfg


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


def get_webhook_url(cfg: dict | None = None) -> str | None:
    """企业微信 webhook URL：环境变量 ``WECOM_WEBHOOK_URL`` > 配置（含本地覆盖）> None。

    模板占位值（``REPLACE_ME``）视为未配置（避免发起无效请求）。
    """
    env = (os.environ.get("WECOM_WEBHOOK_URL") or "").strip()
    if env:
        return env
    if cfg is None:
        cfg = load_config()
    url = (cfg.get("notify") or {}).get("wecom_webhook_url")
    if not url:
        return None
    url = str(url).strip()
    if not url or "REPLACE_ME" in url:
        return None
    return url
