"""合约代码解析（v2.2）：`al2611` → 2026-11，`SR701` → 2027-01。

只做「代码 ↔ 年月」的纯运算，不查行情、不查交易所规则——
到期日仍以 ``config/contracts_lifecycle.yaml`` 为准（此处只用来生成候选月份）。

格式差异：
- 上期所 / 大商所：4 位 ``YYMM``（rb2701 = 2027-01）
- 郑商所：3 位 ``YMM``（SR701 = 2027-01，年份取末位）
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"^([A-Za-z]+)(\d{3,4})$")


def parse(code: str) -> tuple[str, int, int, int]:
    """解析合约代码 → (品种前缀, 年, 月, 位数)。非法代码抛 ValueError。"""
    m = _PATTERN.match(str(code).strip())
    if not m:
        raise ValueError(f"无法解析合约代码：{code}")
    base, digits = m.group(1), m.group(2)
    if len(digits) == 4:
        year, month = 2000 + int(digits[:2]), int(digits[2:])
    else:
        year, month = 2020 + int(digits[0]), int(digits[1:])
    if not 1 <= month <= 12:
        raise ValueError(f"合约月份非法：{code}")
    return base, year, month, len(digits)


def shift(code: str, months: int) -> str:
    """在合约代码上加减月份（保持原位数与大小写）。"""
    base, year, month, width = parse(code)
    total = year * 12 + (month - 1) + int(months)
    year2, month2 = divmod(total, 12)
    month2 += 1
    if width == 4:
        return f"{base}{year2 % 100:02d}{month2:02d}"
    return f"{base}{year2 % 10}{month2:02d}"


def candidates(code: str, offsets: tuple[int, ...] = (1, 2, 3, 4, 6, 9)) -> list[str]:
    """候选月份（默认未来 1/2/3/4/6/9 个月）。"""
    return [shift(code, n) for n in offsets]


def same_product(a: str, b: str) -> bool:
    return parse(a)[0].upper() == parse(b)[0].upper()


def canonical(code: str) -> str:
    """统一成 4 位口径（SR701 → SR2701），用于跨数据源比对。

    郑商所行情源用 4 位（SR2701），CTP / 本项目配置用 3 位（SR701）；
    两者指向同一合约，比较前必须先归一。
    """
    base, year, month, _ = parse(code)
    return f"{base.upper()}{year % 100:02d}{month:02d}"


def format_like(sample: str, code: str) -> str:
    """按 ``sample`` 的位数与大小写风格输出 ``code``。

    ``format_like("SR701", "SR2702")`` → ``"SR702"``；
    ``format_like("al2611", "AL2612")`` → ``"al2612"``（沿用配置里的小写风格）。
    """
    _, year, month, _ = parse(code)
    sample_base, _, _, sample_width = parse(sample)
    if sample_width == 4:
        return f"{sample_base}{year % 100:02d}{month:02d}"
    return f"{sample_base}{year % 10}{month:02d}"
