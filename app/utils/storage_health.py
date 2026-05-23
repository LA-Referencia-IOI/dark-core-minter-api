"""Lightweight metadata storage readiness checks."""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dark_core_lib.metadata import MetadataStorage

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


def _store_api_health(timeout_seconds: float) -> Dict[str, Any]:
    """Read Store API health with detail useful for metadata worker pauses."""
    settings = get_settings()
    url = f"{settings.metadata_store_api_url.rstrip('/')}/health"
    request = Request(url, method="GET")

    with urlopen(request, timeout=timeout_seconds) as response:
        raw_body = response.read().decode("utf-8")

    body = json.loads(raw_body)
    backend_healthy = bool(body.get("backend_healthy"))
    service_healthy = body.get("status") == "healthy"
    available = service_healthy and backend_healthy
    error = None
    if not available:
        error = (
            body.get("error")
            or body.get("message")
            or body.get("detail")
            or f"Store API health is {body.get('status', 'unknown')}"
        )

    return {
        "available": available,
        "backend": body.get("backend"),
        "backend_healthy": backend_healthy,
        "available_peers": body.get("available_cluster_peers"),
        "min_peers": body.get("min_cluster_peers"),
        "last_error": error,
    }


def check_metadata_storage_health(
    storage: MetadataStorage | None = None,
    timeout_seconds: float | None = None,
) -> Dict[str, Any]:
    """Check whether metadata storage is ready to accept writes."""
    global _last_ok_at, _last_error

    settings = get_settings()
    checked_at = _utc_now()
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else float(settings.metadata_store_api_timeout_seconds)
    )

    try:
        if settings.metadata_storage_type.lower() == "store_api":
            result = _store_api_health(timeout)
            available = bool(result["available"])
            backend = result.get("backend") or "store_api"
            error = result.get("last_error")
            available_peers = result.get("available_peers")
            min_peers = result.get("min_peers")
            backend_healthy = result.get("backend_healthy")
        else:
            if storage is None:
                raise RuntimeError("Metadata storage instance is required for non-store_api health checks")
            available = bool(storage.health_check())
            backend = settings.metadata_storage_type.lower()
            error = None if available else "Metadata storage health check returned false"
            available_peers = None
            min_peers = None
            backend_healthy = available

        if available:
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
                "backend": backend,
                "backend_healthy": backend_healthy,
                "available_peers": available_peers,
                "min_peers": min_peers,
            }

        raise RuntimeError(error or "Metadata storage is unavailable")

    except (OSError, HTTPError, URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        error = str(exc)
        logger.debug("Metadata storage health check failed: %s", error)
        with _state_lock:
            _last_error = error
            last_ok_at = _last_ok_at

        return {
            "available": False,
            "state": "unavailable",
            "checked_at": checked_at.isoformat(),
            "last_ok_at": _isoformat_or_none(last_ok_at),
            "last_error": error,
            "backend": settings.metadata_storage_type.lower(),
            "backend_healthy": False,
            "available_peers": None,
            "min_peers": None,
        }
