import logging
import sys
from logging.handlers import RotatingFileHandler

from kusamushiri.paths import get_log_file_path

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] %(message)s"


def setup_logger(name: str = "kusamushiri") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    try:
        file_handler = RotatingFileHandler(
            get_log_file_path(),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError as error:
        logger.warning("File logging is disabled: %s", error)
    else:
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger


logger = setup_logger()
