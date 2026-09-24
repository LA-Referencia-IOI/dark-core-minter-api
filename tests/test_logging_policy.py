"""Tests for production-oriented minter logging controls."""

import json
import logging
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.utils.logging import JsonFormatter, WarningRateLimiter


@pytest.mark.parametrize("value", ["debug", "INFO", "Warning", "ERROR", "critical"])
def test_settings_accept_valid_log_levels(value):
    assert Settings(_env_file=None, minter_log_level=value).minter_log_level == value.upper()


def test_settings_reject_invalid_log_level():
    with pytest.raises(ValueError, match="MINTER_LOG_LEVEL"):
        Settings(_env_file=None, minter_log_level="verbose")


def test_warning_rate_limiter_emits_first_and_then_throttles():
    logger = Mock(spec=logging.Logger)
    limiter = WarningRateLimiter(repeat_seconds=60)
    with patch("app.utils.logging.time.monotonic", side_effect=[10, 20, 71]):
        limiter.warning(logger, "storage", "Storage unavailable")
        limiter.warning(logger, "storage", "Storage unavailable")
        limiter.warning(logger, "storage", "Storage unavailable")

    assert logger.warning.call_count == 2


def test_json_formatter_emits_machine_readable_record():
    record = logging.LogRecord("minter.worker", logging.WARNING, "", 0, "storage %s", ("paused",), None)
    body = json.loads(JsonFormatter().format(record))

    assert body["level"] == "WARNING"
    assert body["logger"] == "minter.worker"
    assert body["message"] == "storage paused"
