"""Production-oriented logging configuration and warning rate limiting."""

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any

from app.config import Settings, get_settings


_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class JsonFormatter(logging.Formatter):
    """Compact JSON formatter for collectors that ingest stdout records."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings | None = None) -> None:
    """Configure root logging once per process from minter environment settings."""
    settings = settings or get_settings()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.minter_log_file:
        handlers.append(
            RotatingFileHandler(
                settings.minter_log_file,
                maxBytes=settings.minter_log_file_max_bytes,
                backupCount=settings.minter_log_file_backup_count,
                encoding="utf-8",
            )
        )
    formatter: logging.Formatter = JsonFormatter() if settings.minter_log_json else logging.Formatter(_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, settings.minter_log_level),
        handlers=handlers,
    )
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if settings.minter_uvicorn_access_log else logging.WARNING
    )


class WarningRateLimiter:
    """Emit a repeated operational warning at most once per key and interval."""

    def __init__(self, repeat_seconds: int):
        self.repeat_seconds = repeat_seconds
        self._last_emitted: dict[str, float] = {}

    def warning(
        self,
        logger: logging.Logger,
        key: str,
        message: str,
        *args: Any,
    ) -> None:
        now = time.monotonic()
        last = self._last_emitted.get(key)
        if last is None or now - last >= self.repeat_seconds:
            logger.warning(message, *args)
            self._last_emitted[key] = now


_warning_limiter = WarningRateLimiter(get_settings().minter_log_warning_repeat_seconds)


def rate_limited_warning(
    logger: logging.Logger,
    key: str,
    message: str,
    *args: Any,
) -> None:
    """Log an operational warning once per key and configured interval."""
    _warning_limiter.warning(logger, key, message, *args)
