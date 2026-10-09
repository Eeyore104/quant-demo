"""样本外使用次数登记：同一「品种|策略|分段」每跑一次样本外就计一次数。

动机：样本外一旦被反复使用（改参数 → 再看 → 再改），它就不再是真正的「样本外」。
超过阈值（默认 3 次）时，体检报告会把样本外结论降级为「存疑」并提示使用次数。
"""

import json
from pathlib import Path

from ..utils.logger import get_logger

log = get_logger("analysis")


def _load(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("样本外使用登记文件读取失败，将重建：%s", exc)
    return {}


def get_usage(path, key: str) -> int:
    """读取某 key 的累计使用次数（不存在返回 0）。"""
    return int(_load(Path(path)).get(key, 0))


def bump_usage(path, key: str) -> int:
    """使用次数 +1 并写回，返回更新后的次数。"""
    p = Path(path)
    data = _load(p)
    data[key] = int(data.get(key, 0)) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data[key]
