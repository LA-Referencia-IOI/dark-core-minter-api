"""
Worker status endpoints backed by DB heartbeat.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dependencies import get_db
from app.repositories import WorkerRuntimeRepository

router = APIRouter(tags=["Worker"])


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/status", response_model=Dict[str, Any])
async def get_worker_status(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Get standalone worker status using DB heartbeat.

    This endpoint does not query worker process internals directly.
    It reads the shared runtime heartbeat persisted by the worker.
    """
    settings = get_settings()
    repo = WorkerRuntimeRepository(db)
    record = repo.get_by_name(settings.worker_runtime_name)

    if record is None:
        return {
            "enabled": False,
            "running": False,
            "status": "UNKNOWN",
            "worker_name": settings.worker_runtime_name,
            "source": "db_heartbeat",
            "message": "No worker heartbeat found",
        }

    now = _utc_now()
    age_seconds = max((now - record.last_heartbeat_at).total_seconds(), 0.0)
    stale_after = settings.worker_heartbeat_stale_after_seconds
    stale = age_seconds > stale_after

    active_states = {"STARTING", "RUNNING", "IDLE"}
    running = (record.status in active_states) and (not stale)

    return {
        "enabled": True,
        "running": running,
        "stale": stale,
        "status": record.status,
        "worker_name": record.worker_name,
        "instance_id": record.instance_id,
        "host": record.host,
        "pid": record.pid,
        "heartbeat_age_seconds": age_seconds,
        "stale_after_seconds": stale_after,
        "last_heartbeat_at": record.last_heartbeat_at.isoformat() if record.last_heartbeat_at else None,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "last_cycle_at": record.last_cycle_at.isoformat() if record.last_cycle_at else None,
        "stats": {
            "total_processed": record.total_processed,
            "total_succeeded": record.total_succeeded,
            "total_failed": record.total_failed,
            "total_permanent_failures": record.total_permanent_failures,
        },
        "last_error": record.last_error,
        "source": "db_heartbeat",
    }

