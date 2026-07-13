"""Structured (JSON) logging setup, shared by the CLI, worker, and webhook server."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Renders each log record as a single JSON line for easy grepping/ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if extra:
            payload["context"] = extra
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def resolve_log_level(name: str) -> int:
    """Maps a REEL_LOG_LEVEL / Settings.log_level string (e.g. "DEBUG") to a
    logging level int, falling back to INFO for an unrecognized value rather
    than raising - a typo'd env var shouldn't crash startup.
    """
    return logging.getLevelNamesMapping().get(name.upper(), logging.INFO)


def configure_logging(logs_dir: Path | None = None, level: int = logging.INFO) -> None:
    """Idempotently configure root logging with a console handler and, if logs_dir
    is given, a rotating-by-run JSON file handler under logs_dir/pipeline.log.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JsonFormatter())
    root.addHandler(console_handler)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        # Bounded like every other on-disk artifact this project maintains (data/tmp,
        # needs-attention.txt) - an always-on webhook server would otherwise grow this
        # file forever with no cleanup.
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "pipeline.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_context(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    """Log a message with structured context, e.g. log_context(logger, logging.INFO,
    "download complete", content_id=cid, duration_seconds=12.3).
    """
    logger.log(level, message, extra={"context": context})
