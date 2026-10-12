"""统一日志。"""

import logging
import sys
from datetime import datetime
from pathlib import Path

_file_handler: logging.FileHandler | None = None


def get_logger(name: str = "quant") -> logging.Logger:
    """获取带统一格式的 logger（重复调用不会重复加 handler）。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def enable_file_logging(log_dir: str | Path, *, date_str: str | None = None) -> Path:
    """把全部日志同时写入 ``logs/daily_<YYYYMMDD>.log``（重复调用不重复添加）。

    仅每日流水线（``daily_run``）场景启用——通过 root logger 收集全部模块日志；
    控制台输出行为不变。返回日志文件路径。
    """
    global _file_handler
    logs_dir = Path(log_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    day = date_str or datetime.now().strftime("%Y%m%d")
    path = logs_dir / f"daily_{day}.log"
    if _file_handler is not None:
        return Path(_file_handler.baseFilename)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _file_handler = handler
    return path
