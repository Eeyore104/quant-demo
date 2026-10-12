"""日志文件输出测试（daily 场景）。"""

import logging

from src.utils import logger as logger_mod


def test_enable_file_logging_writes_and_is_idempotent(tmp_path):
    logger_mod._file_handler = None  # 重置模块级状态，保证测试独立
    root = logging.getLogger()
    before = list(root.handlers)
    old_level = root.level
    try:
        path = logger_mod.enable_file_logging(tmp_path, date_str="20990101")
        logging.getLogger("test.file").info("关键日志关键字 ABC123")
        again = logger_mod.enable_file_logging(tmp_path, date_str="20990202")
        assert again == path  # 已启用：不重复添加、返回首次路径
        text = path.read_text(encoding="utf-8")
        assert "ABC123" in text
        n_file = sum(
            1
            for h in root.handlers
            if isinstance(h, logging.FileHandler)
            and not type(h).__module__.startswith("_pytest")
        )
        assert n_file == 1
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(old_level)
        logger_mod._file_handler = None
