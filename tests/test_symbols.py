"""品种显示标注单元测试。"""

from src.data.symbols import instrument_label


def test_known_symbol_gets_chinese_name():
    assert instrument_label("C0") == "玉米 C0（主力连续）"
    assert instrument_label("M0") == "豆粕 M0（主力连续）"


def test_unknown_symbol_falls_back_to_code():
    assert instrument_label("XX0") == "XX0（主力连续）"
    assert instrument_label("600519") == "600519"
