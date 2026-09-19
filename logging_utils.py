"""Centralized, rotating application logging."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_directory: Path, filename: str, level_name: str) -> logging.Logger:
    """Configure one process logger without recording search result contents."""
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("telegram_csv_search")
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False

    # Avoid duplicate handlers when a test or an embedding process initializes twice.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(process)d %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = RotatingFileHandler(
        log_directory / filename,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
