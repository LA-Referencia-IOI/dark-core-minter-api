"""
Worker status endpoints backed by DB heartbeat.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import ARKRecord
from app.dependencies import get_db
from app.models.states import ARKState
from app.repositories import WorkerRuntimeRepository
from app.repositories.ark_repository import _is_ready_for_publish

router = APIRouter(tags=["Worker"])


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _isoformat_or_none(value: datetime | None) -> str | None:
    """Return ISO string for datetime values used in JSON responses."""
    return value.isoformat() if value else None


def _build_worker_config(settings) -> Dict[str, Any]:
    """Return effective worker settings relevant for publication cycles."""
    return {
        "worker_interval_seconds": settings.worker_interval_seconds,
        "worker_batch_size": settings.worker_batch_size,
        "worker_max_retries": settings.worker_max_retries,
        "worker_retry_backoff_base": settings.worker_retry_backoff_base,
    }


def _build_queue_summary(db: Session, settings, now: datetime) -> Dict[str, Any]:
    """Build read-only queue and error counters from ARK records."""
    state_labels = {
        ARKState.RESERVED.value: "reserved",
        ARKState.DRAFT.value: "draft",
        ARKState.UPDATE.value: "update",
        ARKState.PUBLISHED.value: "published",
        ARKState.TOMBSTONE.value: "tombstone",
    }
    by_state = {label: 0 for label in state_labels.values()}

    state_counts = (
        db.query(ARKRecord.state, func.count(ARKRecord.id))
        .group_by(ARKRecord.state)
        .all()
    )
    for state, count in state_counts:
        label = state_labels.get(state)
        if label:
            by_state[label] = int(count or 0)

    pending_records = (
        db.query(ARKRecord)
        .filter(
            ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]),
            ARKRecord.publish_permanently_failed == 0,
        )
        .all()
    )
    ready_now = sum(
        1
        for record in pending_records
        if _is_ready_for_publish(
            record,
            now,
            settings.worker_retry_backoff_base,
            settings.worker_max_retries,
        )
    )
    pending_total = len(pending_records)
    oldest_pending_at = min(
        (record.created_at for record in pending_records if record.created_at),
        default=None,
    )

    retrying = (
        db.query(func.count(ARKRecord.id))
        .filter(
            ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]),
            ARKRecord.publish_permanently_failed == 0,
            ARKRecord.publish_retry_count > 0,
        )
        .scalar()
        or 0
    )
    permanent = (
        db.query(func.count(ARKRecord.id))
        .filter(ARKRecord.publish_permanently_failed == 1)
        .scalar()
        or 0
    )
    oldest_error_at = (
        db.query(func.min(ARKRecord.publish_last_attempt_at))
        .filter(
            (ARKRecord.publish_retry_count > 0)
            | (ARKRecord.publish_permanently_failed == 1)
            | (ARKRecord.publish_last_error.isnot(None))
        )
        .scalar()
    )

    return {
        "queue": {
            "pending_total": pending_total,
            "ready_now": ready_now,
            "delayed_by_backoff": max(pending_total - ready_now, 0),
            "oldest_pending_at": _isoformat_or_none(oldest_pending_at),
        },
        "errors": {
            "retrying": int(retrying),
            "permanent": int(permanent),
            "oldest_error_at": _isoformat_or_none(oldest_error_at),
        },
        "by_state": by_state,
    }


@router.get("/status", response_model=Dict[str, Any])
async def get_worker_status(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Get standalone worker status using DB heartbeat.

    This endpoint does not query worker process internals directly.
    It reads the shared runtime heartbeat persisted by the worker.
    """
    settings = get_settings()
    now = _utc_now()
    queue_summary = _build_queue_summary(db, settings, now)
    config_summary = _build_worker_config(settings)
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
            **queue_summary,
            "config": config_summary,
        }

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
        **queue_summary,
        "config": config_summary,
    }
