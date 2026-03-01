"""
Structured logging setup.

- Console: coloured, human-readable.
- File: JSON-lines for machine parsing.
- Rotating file handler to cap disk usage.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from config import LoggingConfig


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        import time

        entry = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


class _ConsoleFormatter(logging.Formatter):
    """Coloured console formatter."""

    COLORS = {
        "DEBUG": "\033[90m",  # grey
        "INFO": "\033[36m",  # cyan
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",  # red
        "CRITICAL": "\033[41m",  # red bg
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = self.formatTime(record, "%H:%M:%S")
        return (
            f"{color}{ts} {record.levelname:<8}{self.RESET} "
            f"[{record.name}] {record.getMessage()}"
        )


def setup_logging(config: LoggingConfig) -> None:
    """Configure root logger with console + rotating file handlers."""
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.level.upper(), logging.INFO))

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    # Rotating file handler (JSON)
    file_path = log_dir / config.log_file
    file_handler = logging.handlers.RotatingFileHandler(
        file_path,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
    )
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)

    # Quieten noisy third-party loggers
    for name in ("aiohttp", "asyncio", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.info("Logging initialised — level=%s dir=%s", config.level, log_dir)
