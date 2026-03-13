"""日志配置：同时输出到控制台和按天轮转的日志文件。
Logging config: output to both console and daily-rotated log file.
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config import settings


def setup_logger(name='app', log_level=logging.INFO):
    """设置 logger，同时输出到控制台和文件。
    Setup logger with both console and file handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    if logger.handlers:
        return logger

    Path(settings.LOG_PATH).mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 按天轮转，保留 10 天 / Daily rotation, keep 10 days
    file_handler = TimedRotatingFileHandler(
        os.path.join(settings.LOG_PATH, f'{name}.log'),
        when='midnight',
        backupCount=10,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger("mini-deploy")
