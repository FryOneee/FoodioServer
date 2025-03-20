# logging_config.py
import logging
from logging.handlers import RotatingFileHandler


def setup_logging():
    logger = logging.getLogger("server_logger")
    logger.setLevel(logging.INFO)

    # Handler do pliku
    file_handler = RotatingFileHandler("server.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Handler do konsoli
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger