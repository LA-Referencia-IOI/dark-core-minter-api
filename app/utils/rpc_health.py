"""Lightweight JSON-RPC health checks for the blockchain endpoint."""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.config import get_settings


logger = logging.getLogger(__name__)

_state_lock = threading.Lock()
_last_ok_at: datetime | None = None
_last_error: str | None = None


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for JSON/DB consistency."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def check_rpc_health(
    rpc_url: str | None = None,
    timeout_seconds: float | None = None,
) -> Dict[str, Any]:
    """
    Check whether the configured JSON-RPC endpoint can answer eth_blockNumber.

    This deliberately avoids constructing DARKCoreClient so API/status paths can
    observe RPC availability without requiring contract initialization.
    """
    global _last_ok_at, _last_error

    settings = get_settings()
    url = rpc_url or settings.dark_rpc_url
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else float(settings.dark_rpc_health_timeout_seconds)
    )
    checked_at = _utc_now()
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
        body = json.loads(raw_body)
        if "error" in body:
            raise RuntimeError(body["error"])

        raw_block = body.get("result")
        if not raw_block:
            raise RuntimeError("RPC response did not include eth_blockNumber result")

        block_number = int(raw_block, 16)
        with _state_lock:
            _last_ok_at = checked_at
            _last_error = None
            last_ok_at = _last_ok_at

        return {
            "available": True,
            "state": "available",
            "checked_at": checked_at.isoformat(),
            "last_ok_at": _isoformat_or_none(last_ok_at),
            "last_error": None,
            "block_number": block_number,
        }

    except (OSError, URLError, TimeoutError, ValueError, RuntimeError) as exc:
        error = str(exc)
        logger.debug("RPC health check failed for %s: %s", url, error)
        with _state_lock:
            _last_error = error
            last_ok_at = _last_ok_at

        return {
            "available": False,
            "state": "unavailable",
            "checked_at": checked_at.isoformat(),
            "last_ok_at": _isoformat_or_none(last_ok_at),
            "last_error": error,
            "block_number": None,
        }
