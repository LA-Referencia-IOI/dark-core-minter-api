"""
Worker status endpoints backed by DB heartbeat.
"""

from datetime import datetime, timezone
from math import ceil
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import ARKMetadata, ARKRecord
from app.dependencies import get_db
from app.models.states import ARKState
from app.repositories import WorkerRuntimeRepository
from app.repositories.ark_repository import _is_ready_for_publish
from app.utils.rpc_health import check_rpc_health

router = APIRouter(tags=["Worker"])


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _isoformat_or_none(value: datetime | None) -> str | None:
    """Return ISO string for datetime values used in JSON responses."""
    return value.isoformat() if value else None


def _metadata_complete(metadata: ARKMetadata | None) -> bool:
    """Return True when both persisted metadata CIDs exist."""
    return bool(metadata and metadata.level1_cid and metadata.original_cid)


def _build_worker_config(settings) -> Dict[str, Any]:
    """Return effective worker settings relevant for publication cycles."""
    return {
        "metadata": {
            "worker_page_size": settings.metadata_worker_page_size,
            "worker_sleep_seconds": settings.metadata_worker_sleep_seconds,
            "worker_max_retries": settings.metadata_worker_max_retries,
            "worker_retry_backoff_base": settings.metadata_worker_retry_backoff_base,
            "worker_runtime_name": settings.metadata_worker_runtime_name,
        },
        "chain": {
            "worker_page_size": settings.chain_worker_page_size,
            "worker_sleep_seconds": settings.chain_worker_sleep_seconds,
            "worker_rpc_retry_seconds": settings.chain_worker_rpc_retry_seconds,
            "worker_max_retries": settings.chain_worker_max_retries,
            "worker_retry_backoff_base": settings.chain_worker_retry_backoff_base,
            "worker_runtime_name": settings.chain_worker_runtime_name,
        },
        "rpc": {
            "health_timeout_seconds": settings.dark_rpc_health_timeout_seconds,
            "connect_retry_seconds": settings.dark_rpc_connect_retry_seconds,
            "connect_retry_interval_seconds": settings.dark_rpc_connect_retry_interval_seconds,
        },
    }


def _empty_queue() -> Dict[str, Any]:
    return {
        "pending_total": 0,
        "ready_now": 0,
        "delayed_by_backoff": 0,
        "oldest_pending_at": None,
    }


def _empty_errors() -> Dict[str, Any]:
    return {
        "retrying": 0,
        "permanent": 0,
        "oldest_error_at": None,
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

    queues = {
        "metadata": _empty_queue(),
        "chain": _empty_queue(),
    }
    errors = {
        "metadata": _empty_errors(),
        "chain": _empty_errors(),
    }

    pending_rows = (
        db.query(ARKRecord, ARKMetadata)
        .outerjoin(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
        .filter(ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]))
        .all()
    )

    oldest_pending = {"metadata": None, "chain": None}
    oldest_error = {"metadata": None, "chain": None}

    for record, metadata in pending_rows:
        stage = "chain" if _metadata_complete(metadata) else "metadata"

        if record.publish_permanently_failed == 1:
            errors[stage]["permanent"] += 1
        else:
            queues[stage]["pending_total"] += 1
            if _is_ready_for_publish(
                record,
                now,
                (
                    settings.chain_worker_retry_backoff_base
                    if stage == "chain"
                    else settings.metadata_worker_retry_backoff_base
                ),
                (
                    settings.chain_worker_max_retries
                    if stage == "chain"
                    else settings.metadata_worker_max_retries
                ),
            ):
                queues[stage]["ready_now"] += 1

            current_oldest = oldest_pending[stage]
            if record.created_at and (current_oldest is None or record.created_at < current_oldest):
                oldest_pending[stage] = record.created_at

        if record.publish_retry_count > 0 and record.publish_permanently_failed == 0:
            errors[stage]["retrying"] += 1

        if record.publish_last_attempt_at and (
            record.publish_retry_count > 0
            or record.publish_permanently_failed == 1
            or record.publish_last_error is not None
        ):
            current_oldest_error = oldest_error[stage]
            if current_oldest_error is None or record.publish_last_attempt_at < current_oldest_error:
                oldest_error[stage] = record.publish_last_attempt_at

    for stage in ("metadata", "chain"):
        queues[stage]["delayed_by_backoff"] = max(
            queues[stage]["pending_total"] - queues[stage]["ready_now"],
            0,
        )
        queues[stage]["oldest_pending_at"] = _isoformat_or_none(oldest_pending[stage])
        errors[stage]["oldest_error_at"] = _isoformat_or_none(oldest_error[stage])

    return {
        "queues": queues,
        "errors": errors,
        "by_state": by_state,
    }


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    """Round a float for stable JSON output."""
    return round(value, digits) if value is not None else None


def _build_cadence_metrics(
    record,
    *,
    page_size: int,
    sleep_seconds: int,
    rpc_retry_seconds: int | None = None,
    queue: Dict[str, Any],
    running: bool,
    stale: bool,
    now: datetime,
) -> Dict[str, Any]:
    """Build worker cycle metrics for the continuous page loop."""
    seconds_since_last_cycle = None
    if record and record.last_cycle_at:
        seconds_since_last_cycle = max((now - record.last_cycle_at).total_seconds(), 0.0)

    duration = record.last_cycle_duration_seconds if record else None
    utilization = None
    if duration is not None and sleep_seconds > 0:
        utilization = float(duration) / float(sleep_seconds)

    ready_now = int(queue.get("ready_now") or 0)
    safe_page_size = max(int(page_size or 0), 1)
    ready_pages = ceil(ready_now / safe_page_size) if ready_now else 0
    estimated_drain_seconds = ready_pages * sleep_seconds if ready_pages else 0
    last_cycle_processed = record.last_cycle_processed if record else None
    last_cycle_full_page = bool(
        last_cycle_processed is not None
        and page_size > 0
        and last_cycle_processed >= page_size
    )
    paused_rpc = bool(record and record.status == "PAUSED_RPC_UNAVAILABLE")
    if paused_rpc:
        next_action = "pause_rpc"
        sleep_seconds_next = int(rpc_retry_seconds or sleep_seconds)
    elif running and last_cycle_full_page:
        next_action = "continue"
        sleep_seconds_next = 0
    else:
        next_action = "sleep"
        sleep_seconds_next = sleep_seconds

    if record is None:
        pressure = "unknown"
    elif stale:
        pressure = "stale"
    elif paused_rpc:
        pressure = "rpc_unavailable"
    elif not running:
        pressure = "stopped"
    elif last_cycle_full_page:
        pressure = "backlogged"
    elif ready_now > safe_page_size:
        pressure = "backlogged"
    elif ready_now > 0:
        pressure = "working"
    else:
        pressure = "idle"

    return {
        "page_size": page_size,
        "sleep_seconds": sleep_seconds,
        "rpc_retry_seconds": rpc_retry_seconds,
        "last_cycle_duration_seconds": _round_or_none(duration),
        "last_cycle_processed": last_cycle_processed,
        "last_cycle_succeeded": record.last_cycle_succeeded if record else None,
        "last_cycle_failed": record.last_cycle_failed if record else None,
        "last_cycle_full_page": last_cycle_full_page,
        "next_action": next_action,
        "sleep_seconds_next": sleep_seconds_next,
        "seconds_since_last_cycle": _round_or_none(seconds_since_last_cycle),
        "cycle_utilization_ratio": _round_or_none(utilization),
        "ready_pages": ready_pages,
        "estimated_seconds_to_drain_ready": estimated_drain_seconds,
        "pressure": pressure,
    }


def _build_runtime_status(
    record,
    worker_name: str,
    enabled: bool,
    stale_after: int,
    now: datetime,
    *,
    page_size: int,
    sleep_seconds: int,
    rpc_retry_seconds: int | None = None,
    queue: Dict[str, Any],
) -> Dict[str, Any]:
    """Build one worker runtime status block from DB heartbeat."""
    if record is None:
        return {
            "enabled": enabled,
            "running": False,
            "status": "UNKNOWN",
            "worker_name": worker_name,
            "source": "db_heartbeat",
            "message": "No worker heartbeat found",
            "cadence": _build_cadence_metrics(
                record,
                page_size=page_size,
                sleep_seconds=sleep_seconds,
                rpc_retry_seconds=rpc_retry_seconds,
                queue=queue,
                running=False,
                stale=False,
                now=now,
            ),
        }

    age_seconds = max((now - record.last_heartbeat_at).total_seconds(), 0.0)
    stale = age_seconds > stale_after
    active_states = {"STARTING", "RUNNING", "IDLE", "PAUSED_RPC_UNAVAILABLE"}
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
        "last_heartbeat_at": _isoformat_or_none(record.last_heartbeat_at),
        "started_at": _isoformat_or_none(record.started_at),
        "last_cycle_at": _isoformat_or_none(record.last_cycle_at),
        "cadence": _build_cadence_metrics(
            record,
            page_size=page_size,
            sleep_seconds=sleep_seconds,
            rpc_retry_seconds=rpc_retry_seconds,
            queue=queue,
            running=running,
            stale=stale,
            now=now,
        ),
        "stats": {
            "total_processed": record.total_processed,
            "total_succeeded": record.total_succeeded,
            "total_failed": record.total_failed,
            "total_permanent_failures": record.total_permanent_failures,
        },
        "last_error": record.last_error,
        "source": "db_heartbeat",
    }


def _build_full_status(db: Session) -> Dict[str, Any]:
    """Build the full worker status payload used for detailed debugging."""
    settings = get_settings()
    now = _utc_now()
    queue_summary = _build_queue_summary(db, settings, now)
    config_summary = _build_worker_config(settings)
    repo = WorkerRuntimeRepository(db)
    stale_after = settings.worker_heartbeat_stale_after_seconds

    metadata_record = repo.get_by_name(settings.metadata_worker_runtime_name)
    chain_record = repo.get_by_name(settings.chain_worker_runtime_name)
    metadata_status = _build_runtime_status(
        metadata_record,
        settings.metadata_worker_runtime_name,
        settings.metadata_worker_enabled,
        stale_after,
        now,
        page_size=settings.metadata_worker_page_size,
        sleep_seconds=settings.metadata_worker_sleep_seconds,
        queue=queue_summary["queues"]["metadata"],
    )
    chain_status = _build_runtime_status(
        chain_record,
        settings.chain_worker_runtime_name,
        settings.chain_worker_enabled,
        stale_after,
        now,
        page_size=settings.chain_worker_page_size,
        sleep_seconds=settings.chain_worker_sleep_seconds,
        rpc_retry_seconds=settings.chain_worker_rpc_retry_seconds,
        queue=queue_summary["queues"]["chain"],
    )

    metadata_state = _state_from_worker_status(metadata_status, queue_summary["queues"]["metadata"])
    chain_state = _state_from_worker_status(chain_status, queue_summary["queues"]["chain"])
    metadata_status["state"] = metadata_state
    metadata_status["activity"] = metadata_state if metadata_state in {"idle", "working", "backlogged", "delayed"} else None
    chain_status["state"] = chain_state
    chain_status["activity"] = chain_state if chain_state in {"idle", "working", "backlogged", "delayed"} else None

    rpc_status = check_rpc_health()
    error_totals = _build_error_totals(queue_summary["errors"])
    queue_totals = _build_queue_totals(queue_summary["queues"])
    worker_states = {metadata_state, chain_state}
    overall = _derive_overall_status(worker_states, error_totals, rpc_status)

    response = {
        "overall": overall,
        "message": _build_status_message(
            {"metadata": _simple_worker_status(metadata_status, queue_summary["queues"]["metadata"]),
             "chain": _simple_worker_status(chain_status, queue_summary["queues"]["chain"])},
            error_totals,
            rpc_status,
        ),
        "rpc": rpc_status,
        "workers": {
            "metadata": metadata_status,
            "chain": chain_status,
        },
        "queues": {
            **queue_summary["queues"],
            "total": queue_totals,
        },
        "errors": {
            **queue_summary["errors"],
            "total": error_totals,
        },
        "by_state": queue_summary["by_state"],
        "config": config_summary,
    }
    return response


def _state_from_worker_status(status: Dict[str, Any], queue: Dict[str, Any]) -> str:
    """Derive a compact human-facing worker state."""
    if not status.get("enabled", False):
        return "disabled"
    if status.get("status") == "UNKNOWN":
        return "unknown"
    if status.get("stale", False):
        return "stale"
    if status.get("status") == "ERROR":
        return "error"
    if status.get("status") == "PAUSED_RPC_UNAVAILABLE":
        return "paused_rpc_unavailable"
    if not status.get("running", False):
        return "stopped"

    pending = int(queue.get("pending_total") or 0)
    ready = int(queue.get("ready_now") or 0)
    delayed = int(queue.get("delayed_by_backoff") or 0)
    page_size = int((status.get("cadence") or {}).get("page_size") or 1)

    if pending > 0 and ready == 0 and delayed > 0:
        return "delayed"
    if ready > page_size:
        return "backlogged"
    if ready > 0 or int((status.get("cadence") or {}).get("last_cycle_processed") or 0) > 0:
        return "working"
    return "idle"


def _simple_worker_status(
    status: Dict[str, Any],
    queue: Dict[str, Any],
) -> Dict[str, Any]:
    """Reduce a detailed worker status block to the operational essentials."""
    cadence = status.get("cadence") or {}
    state = status.get("state") or _state_from_worker_status(status, queue)
    alive = bool(status.get("running", False))
    return {
        "state": state,
        "activity": state if state in {"idle", "working", "backlogged", "delayed"} else None,
        "status": "running" if alive else state,
        "alive": alive,
        "last_heartbeat_seconds": (
            int(round(status["heartbeat_age_seconds"]))
            if status.get("heartbeat_age_seconds") is not None
            else None
        ),
        "last_cycle": {
            "processed": cadence.get("last_cycle_processed"),
            "succeeded": cadence.get("last_cycle_succeeded"),
            "failed": cadence.get("last_cycle_failed"),
            "duration_seconds": cadence.get("last_cycle_duration_seconds"),
            "page_size": cadence.get("page_size"),
            "full_page": cadence.get("last_cycle_full_page"),
            "next_action": cadence.get("next_action"),
        },
        "queue": {
            "pending": int(queue.get("pending_total") or 0),
            "ready": int(queue.get("ready_now") or 0),
            "delayed": int(queue.get("delayed_by_backoff") or 0),
        },
    }


def _build_error_totals(errors: Dict[str, Any]) -> Dict[str, int]:
    """Aggregate stage error counters."""
    return {
        "retrying": int(errors["metadata"]["retrying"] or 0)
        + int(errors["chain"]["retrying"] or 0),
        "permanent": int(errors["metadata"]["permanent"] or 0)
        + int(errors["chain"]["permanent"] or 0),
    }


def _build_queue_totals(queues: Dict[str, Any]) -> Dict[str, int]:
    """Aggregate stage queue counters."""
    return {
        "pending": int(queues["metadata"]["pending_total"] or 0)
        + int(queues["chain"]["pending_total"] or 0),
        "ready": int(queues["metadata"]["ready_now"] or 0)
        + int(queues["chain"]["ready_now"] or 0),
        "delayed": int(queues["metadata"]["delayed_by_backoff"] or 0)
        + int(queues["chain"]["delayed_by_backoff"] or 0),
    }


def _derive_overall_status(
    worker_states: set[str],
    error_summary: Dict[str, int],
    rpc: Dict[str, Any],
) -> str:
    """Derive overall health from workers, RPC, and blocked ARKs."""
    hard_failure_states = {"unknown", "stale", "error", "stopped"}
    degraded_states = {"paused_rpc_unavailable", "backlogged"}
    if worker_states and worker_states.issubset(hard_failure_states | {"disabled"}):
        return "down"
    if (
        worker_states & hard_failure_states
        or worker_states & degraded_states
        or error_summary["permanent"] > 0
        or not rpc["available"]
    ):
        return "degraded"
    return "ok"


def _build_status_message(
    workers: Dict[str, Any],
    error_summary: Dict[str, int],
    rpc: Dict[str, Any],
) -> str:
    """Build a compact operational message from worker and error blocks."""
    labels = {"metadata": "Metadata worker", "chain": "Chain worker"}
    parts = []
    for key in ("metadata", "chain"):
        worker = workers[key]
        state = worker["state"]
        pending = worker["queue"]["pending"]
        if state == "paused_rpc_unavailable":
            parts.append(f"{labels[key]} is paused because RPC is unavailable")
        elif state in {"unknown", "stale", "error", "stopped", "disabled"}:
            parts.append(f"{labels[key]} is {state}")
        elif pending > 0:
            parts.append(f"{labels[key]} is {state} with {pending} pending")
        else:
            parts.append(f"{labels[key]} is {state}")
    if error_summary["permanent"] > 0:
        parts.append(f"{error_summary['permanent']} permanent ARK errors require review")
    if error_summary["retrying"] > 0:
        parts.append(f"{error_summary['retrying']} ARKs are retrying")
    if not rpc["available"]:
        parts.append("RPC is unavailable")
    return "; ".join(parts) + "."


def _build_simple_status(full_status: Dict[str, Any]) -> Dict[str, Any]:
    """Build the default human-readable worker status payload."""
    metadata = _simple_worker_status(
        full_status["workers"]["metadata"],
        full_status["queues"]["metadata"],
    )
    chain = _simple_worker_status(
        full_status["workers"]["chain"],
        full_status["queues"]["chain"],
    )
    workers = {"metadata": metadata, "chain": chain}

    error_summary = _build_error_totals(full_status["errors"])
    worker_states = {metadata["state"], chain["state"]}
    overall = _derive_overall_status(worker_states, error_summary, full_status["rpc"])

    return {
        "overall": overall,
        "message": _build_status_message(workers, error_summary, full_status["rpc"]),
        "rpc": full_status["rpc"],
        "workers": workers,
        "errors": error_summary,
        "arks": full_status["by_state"],
    }


@router.get("/status", response_model=Dict[str, Any])
async def get_worker_status(
    detail: str = Query(
        default="simple",
        pattern="^(simple|full)$",
        description="Use detail=full for the complete debug payload.",
    ),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get worker status using DB heartbeat.

    By default this returns a compact operational summary. Use `detail=full`
    to inspect the full heartbeat, queue, cadence, config, and host/process
    fields.
    """
    full_status = _build_full_status(db)
    if detail == "full":
        return full_status
    return _build_simple_status(full_status)
