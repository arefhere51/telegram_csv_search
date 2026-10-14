"""Centralized, rotating application logging."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import traceback


class RedactionFilter(logging.Filter):
    """Remove configured secrets from log messages and formatted tracebacks."""

    def __init__(self, secrets_to_redact: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets_to_redact = tuple(secret for secret in secrets_to_redact if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets_to_redact:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        if record.exc_info:
            formatted = "".join(traceback.format_exception(*record.exc_info))
            for secret in self.secrets_to_redact:
                formatted = formatted.replace(secret, "[REDACTED]")
            record.exc_text = formatted.rstrip()
            record.exc_info = None
        return True


def configure_logging(
    log_directory: Path,
    filename: str,
    level_name: str,
    *,
    secrets_to_redact: tuple[str, ...] = (),
) -> logging.Logger:
    """Configure one process logger without recording search result contents."""
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("telegram_csv_search")
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False

    # Avoid duplicate handlers when a test or an embedding process initializes twice.
    if logger.handlers:
        for handler in logger.handlers:
            redaction_filter = next(
                (item for item in handler.filters if isinstance(item, RedactionFilter)),
                None,
            )
            if redaction_filter is None and secrets_to_redact:
                handler.addFilter(RedactionFilter(secrets_to_redact))
            elif redaction_filter is not None:
                redaction_filter.secrets_to_redact = tuple(dict.fromkeys(
                    (*redaction_filter.secrets_to_redact, *(secret for secret in secrets_to_redact if secret))
                ))
        root = logging.getLogger()
        root.setLevel(logging.WARNING)
        if not any(getattr(handler, "_telegram_csv_search_root_handler", False) for handler in root.handlers):
            for handler in logger.handlers:
                handler._telegram_csv_search_root_handler = True  # type: ignore[attr-defined]
                root.addHandler(handler)
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s pid=%(process)d thread=%(threadName)s "
        "%(name)s %(filename)s:%(lineno)d | %(message)s",
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
    redaction_filter = RedactionFilter(secrets_to_redact)
    file_handler.addFilter(redaction_filter)
    console_handler.addFilter(redaction_filter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Capture warnings and errors from the Telegram/runtime libraries in CMD and bot.log.
    # The application logger has propagate=False, so its records are not duplicated.
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    if not any(getattr(handler, "_telegram_csv_search_root_handler", False) for handler in root.handlers):
        for handler in (file_handler, console_handler):
            handler._telegram_csv_search_root_handler = True  # type: ignore[attr-defined]
            root.addHandler(handler)
    return logger
