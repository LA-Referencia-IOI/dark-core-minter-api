"""
Worker status endpoints backed by DB heartbeat.
"""

from datetime import datetime, timezone
from math import ceil
from typing import Any, Dict

from dark_core_lib import ARKPublishOperation, DARKCoreClient
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import ARKMetadata, ARKRecord
from app.dependencies import get_corelib_client, get_db
from app.middleware.auth import enforce_authority_match, require_authority_identity
from app.models.states import ARKState
from app.repositories import WorkerRuntimeRepository
from app.repositories.ark_repository import ARKRepository
from app.repositories.ark_repository import _is_ready_for_publish
from app.utils.rpc_health import check_rpc_health
from app.utils.storage_health import check_metadata_storage_health
from app.workers.publisher import ChainPublisherWorker

router = APIRouter(tags=["Worker"])

ERROR_TYPES = (
    "transaction_reverted",
    "gas_limit",
    "authority",
    "metadata_storage",
    "infrastructure",
    "reconcile_conflict",
    "max_retries",
    "unknown",
)


class RescueErrorsRequest(BaseModel):
    """Request body for filtered permanent-error rescue."""

    authority_id: str = Field(..., min_length=1)
    stage: str = Field(default="chain", pattern="^(all|metadata|chain)$")
    error_type: str = Field(
        default="all",
        pattern=(
            "^(all|transaction_reverted|gas_limit|authority|metadata_storage|"
            "infrastructure|reconcile_conflict|max_retries|unknown)$"
        ),
    )
    limit: int | None = Field(default=None, ge=1)


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _isoformat_or_none(value: datetime | None) -> str | None:
    """Return ISO string for datetime values used in JSON responses."""
    return value.isoformat() if value else None


def _metadata_complete(metadata: ARKMetadata | None) -> bool:
    """Return True when both persisted metadata CIDs exist."""
    return bool(metadata and metadata.level1_cid and metadata.original_cid)


def _metadata_stage(metadata: ARKMetadata | None) -> str:
    """Infer the active worker stage from persisted metadata CIDs."""
    return "chain" if _metadata_complete(metadata) else "metadata"


def _state_label(state: str | None) -> str | None:
    """Return a readable state label from the compact DB state code."""
    labels = {
        ARKState.RESERVED.value: "reserved",
        ARKState.DRAFT.value: "draft",
        ARKState.UPDATE.value: "update",
        ARKState.PUBLISHED.value: "published",
        ARKState.TOMBSTONE.value: "tombstone",
    }
    return labels.get(state) if state else None


def _error_status(record: ARKRecord) -> str:
    """Return the operational error status for an ARK record."""
    if record.publish_permanently_failed == 1:
        return "permanent"
    if record.publish_retry_count > 0:
        return "retrying"
    return "none"


def _classify_error_type(record: ARKRecord, metadata: ARKMetadata | None = None) -> str:
    """Classify an ARK publish error from persisted error text."""
    message = (record.publish_last_error or "").lower()
    if not message:
        return "unknown"

    if "gas limit" in message or "out of gas" in message or "gas x2" in message:
        return "gas_limit"
    if "reconcile conflict" in message or "on-chain ark differs" in message:
        return "reconcile_conflict"
    if "max retries exceeded, permanent" in message:
        return "max_retries"
    if "metadata storage" in message:
        return "metadata_storage"
    if "authority error" in message or "authorization" in message or "not authorized" in message:
        return "authority"
    if any(
        token in message
        for token in (
            "infrastructure",
            "ambiguous",
            "send_failed",
            "not_sent",
            "rpc",
            "timeout",
            "timed out",
            "connection",
            "transport",
            "unavailable",
            "connect",
            "refused",
        )
    ):
        return "infrastructure"
    if "reverted" in message or "transaction error" in message:
        return "transaction_reverted"
    return "unknown"


def _is_infrastructure_error_message(message: str) -> bool:
    """Return True when an error message looks like RPC/transport failure."""
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "infrastructure",
            "ambiguous",
            "rpc",
            "timeout",
            "timed out",
            "connection",
            "transport",
            "unavailable",
            "connect",
            "refused",
        )
    )


def _build_worker_config(settings) -> Dict[str, Any]:
    """Return effective worker settings relevant for publication cycles."""
    return {
        "metadata": {
            "worker_page_size": settings.metadata_worker_page_size,
            "worker_sleep_seconds": settings.metadata_worker_sleep_seconds,
            "worker_storage_retry_seconds": settings.metadata_worker_storage_retry_seconds,
            "worker_max_retries": settings.metadata_worker_max_retries,
            "worker_retry_backoff_base": settings.metadata_worker_retry_backoff_base,
            "worker_runtime_name": settings.metadata_worker_runtime_name,
        },
        "chain": {
            "worker_page_size": settings.chain_worker_page_size,
            "worker_sleep_seconds": settings.chain_worker_sleep_seconds,
            "worker_rpc_retry_seconds": settings.chain_worker_rpc_retry_seconds,
            "worker_congestion_retry_seconds": settings.chain_worker_congestion_retry_seconds,
            "worker_block_stall_seconds": settings.chain_worker_block_stall_seconds,
            "worker_adaptive_page_enabled": settings.chain_worker_adaptive_page_enabled,
            "worker_min_page_size": settings.chain_worker_min_page_size,
            "worker_recovery_success_cycles": settings.chain_worker_recovery_success_cycles,
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


def _chain_capacity_to_dict(capacity, max_page_size: int) -> Dict[str, Any]:
    """Return a JSON-safe chain capacity snapshot."""
    return {
        "available": bool(getattr(capacity, "available", False)),
        "state": str(getattr(capacity, "state", "unknown") or "unknown"),
        "recommended_page_size": int(getattr(capacity, "recommended_page_size", 0) or 0),
        "max_page_size": int(getattr(capacity, "max_page_size", max_page_size) or max_page_size),
        "reason": str(getattr(capacity, "reason", "") or ""),
        "block_number": getattr(capacity, "block_number", None),
        "txpool_pending": getattr(capacity, "txpool_pending", None),
    }


def _build_chain_capacity_summary(settings) -> Dict[str, Any]:
    """Build chain capacity through dark-core-lib, without direct txpool RPC calls."""
    max_page_size = settings.chain_worker_page_size
    try:
        capacity = get_corelib_client().get_chain_capacity(max_page_size=max_page_size)
        return _chain_capacity_to_dict(capacity, max_page_size)
    except Exception as exc:
        return {
            "available": False,
            "state": "unknown",
            "recommended_page_size": 0,
            "max_page_size": max_page_size,
            "reason": f"Failed to read chain capacity from core-lib: {exc}",
            "block_number": None,
            "txpool_pending": None,
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
        stage = _metadata_stage(metadata)

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


def _serialize_worker_error(record: ARKRecord, metadata: ARKMetadata | None) -> Dict[str, Any]:
    """Serialize one ARK worker error for operational review."""
    return {
        "ark": record.ark,
        "naan": record.naan,
        "name": record.name,
        "state": record.state,
        "status": _error_status(record),
        "stage": _metadata_stage(metadata),
        "error_type": _classify_error_type(record, metadata),
        "authority_id": record.authority_id,
        "target": record.target,
        "retry_count": record.publish_retry_count,
        "permanent": bool(record.publish_permanently_failed),
        "error": record.publish_last_error,
        "last_attempt_at": _isoformat_or_none(record.publish_last_attempt_at),
        "created_at": _isoformat_or_none(record.created_at),
        "updated_at": _isoformat_or_none(record.updated_at),
        "metadata": {
            "level1_cid": metadata.level1_cid if metadata else None,
            "original_cid": metadata.original_cid if metadata else None,
            "original_schema": metadata.original_schema if metadata else None,
            "original_media_type": metadata.original_media_type if metadata else None,
        },
    }


def _error_base_query(db: Session):
    """Return the base query for active ARK rows that currently have worker errors."""
    return (
        db.query(ARKRecord, ARKMetadata)
        .outerjoin(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
        .filter(
            ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]),
            (ARKRecord.publish_permanently_failed == 1)
            | (
                (ARKRecord.publish_retry_count > 0)
                & (ARKRecord.publish_permanently_failed == 0)
            )
        )
    )


def _apply_error_sql_filters(
    query,
    *,
    list_type: str,
    stage: str,
    authority_id: str | None,
):
    """Apply DB-native filters shared by error listing and rescue."""
    if list_type == "permanent":
        query = query.filter(ARKRecord.publish_permanently_failed == 1)
    elif list_type == "retrying":
        query = query.filter(
            ARKRecord.publish_retry_count > 0,
            ARKRecord.publish_permanently_failed == 0,
        )

    if authority_id:
        query = query.filter(ARKRecord.authority_id == authority_id)

    if stage == "metadata":
        query = query.filter(
            (ARKMetadata.id.is_(None))
            | (ARKMetadata.level1_cid.is_(None))
            | (ARKMetadata.original_cid.is_(None))
        )
    elif stage == "chain":
        query = query.filter(
            ARKMetadata.level1_cid.is_not(None),
            ARKMetadata.original_cid.is_not(None),
        )

    return query


def _select_error_rows(
    db: Session,
    *,
    list_type: str = "all",
    stage: str = "all",
    authority_id: str | None = None,
    error_type: str = "all",
) -> list[tuple[ARKRecord, ARKMetadata | None]]:
    """Select error rows and apply derived error type filtering."""
    rows = (
        _apply_error_sql_filters(
            _error_base_query(db),
            list_type=list_type,
            stage=stage,
            authority_id=authority_id,
        )
        .order_by(ARKRecord.updated_at.desc(), ARKRecord.id.desc())
        .all()
    )

    if error_type != "all":
        rows = [
            (record, metadata)
            for record, metadata in rows
            if _classify_error_type(record, metadata) == error_type
        ]

    return rows


def _build_errors_summary(rows: list[tuple[ARKRecord, ARKMetadata | None]]) -> Dict[str, Any]:
    """Build a compact errors report from selected rows."""
    summary = {"total": 0, "permanent": 0, "retrying": 0}
    by_stage = {
        "metadata": {"total": 0, "permanent": 0, "retrying": 0},
        "chain": {"total": 0, "permanent": 0, "retrying": 0},
    }
    by_type = {error_type: 0 for error_type in ERROR_TYPES}
    by_authority: dict[str, int] = {}
    oldest_error_at = None

    for record, metadata in rows:
        status = _error_status(record)
        if status == "none":
            continue

        stage = _metadata_stage(metadata)
        error_type = _classify_error_type(record, metadata)
        summary["total"] += 1
        summary[status] += 1
        by_stage[stage]["total"] += 1
        by_stage[stage][status] += 1
        by_type[error_type] = by_type.get(error_type, 0) + 1
        by_authority[record.authority_id] = by_authority.get(record.authority_id, 0) + 1

        if record.publish_last_attempt_at and (
            oldest_error_at is None or record.publish_last_attempt_at < oldest_error_at
        ):
            oldest_error_at = record.publish_last_attempt_at

    return {
        "summary": summary,
        "by_stage": by_stage,
        "by_type": by_type,
        "by_authority": by_authority,
        "oldest_error_at": _isoformat_or_none(oldest_error_at),
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
    congestion_retry_seconds: int | None = None,
    storage_retry_seconds: int | None = None,
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
    paused_chain = bool(record and record.status in {"PAUSED_CHAIN_STALLED", "PAUSED_CHAIN_CONGESTED"})
    paused_storage = bool(record and record.status == "PAUSED_STORAGE_UNAVAILABLE")
    if paused_rpc:
        next_action = "pause_rpc"
        sleep_seconds_next = int(rpc_retry_seconds or sleep_seconds)
    elif paused_chain:
        next_action = "pause_chain"
        sleep_seconds_next = int(congestion_retry_seconds or rpc_retry_seconds or sleep_seconds)
    elif paused_storage:
        next_action = "pause_storage"
        sleep_seconds_next = int(storage_retry_seconds or sleep_seconds)
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
    elif paused_chain:
        pressure = "chain_congested"
    elif paused_storage:
        pressure = "storage_unavailable"
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
        "congestion_retry_seconds": congestion_retry_seconds,
        "storage_retry_seconds": storage_retry_seconds,
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
    congestion_retry_seconds: int | None = None,
    storage_retry_seconds: int | None = None,
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
                congestion_retry_seconds=congestion_retry_seconds,
                storage_retry_seconds=storage_retry_seconds,
                queue=queue,
                running=False,
                stale=False,
                now=now,
            ),
        }

    age_seconds = max((now - record.last_heartbeat_at).total_seconds(), 0.0)
    stale = age_seconds > stale_after
    active_states = {"STARTING", "RUNNING", "IDLE", "PAUSED_RPC_UNAVAILABLE"}
    active_states.add("PAUSED_STORAGE_UNAVAILABLE")
    active_states.update({"PAUSED_CHAIN_STALLED", "PAUSED_CHAIN_CONGESTED"})
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
            congestion_retry_seconds=congestion_retry_seconds,
            storage_retry_seconds=storage_retry_seconds,
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
    chain_capacity = _build_chain_capacity_summary(settings)
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
        storage_retry_seconds=settings.metadata_worker_storage_retry_seconds,
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
        congestion_retry_seconds=settings.chain_worker_congestion_retry_seconds,
        queue=queue_summary["queues"]["chain"],
    )
    chain_status["cadence"]["effective_page_size_recommended"] = chain_capacity[
        "recommended_page_size"
    ]

    metadata_state = _state_from_worker_status(metadata_status, queue_summary["queues"]["metadata"])
    chain_state = _state_from_worker_status(chain_status, queue_summary["queues"]["chain"])
    metadata_status["state"] = metadata_state
    metadata_status["activity"] = metadata_state if metadata_state in {"idle", "working", "backlogged", "delayed"} else None
    chain_status["state"] = chain_state
    chain_status["activity"] = chain_state if chain_state in {"idle", "working", "backlogged", "delayed"} else None

    rpc_status = check_rpc_health()
    storage_status = check_metadata_storage_health()
    error_totals = _build_error_totals(queue_summary["errors"])
    queue_totals = _build_queue_totals(queue_summary["queues"])
    worker_states = {metadata_state, chain_state}
    overall = _derive_overall_status(worker_states, error_totals, rpc_status, storage_status)

    response = {
        "overall": overall,
        "message": _build_status_message(
            {"metadata": _simple_worker_status(metadata_status, queue_summary["queues"]["metadata"]),
             "chain": _simple_worker_status(chain_status, queue_summary["queues"]["chain"])},
            error_totals,
            rpc_status,
            storage_status,
        ),
        "rpc": rpc_status,
        "storage": storage_status,
        "chain_capacity": chain_capacity,
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
    if status.get("status") == "PAUSED_CHAIN_STALLED":
        return "paused_chain_stalled"
    if status.get("status") == "PAUSED_CHAIN_CONGESTED":
        return "paused_chain_congested"
    if status.get("status") == "PAUSED_STORAGE_UNAVAILABLE":
        return "paused_storage_unavailable"
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
    storage: Dict[str, Any],
) -> str:
    """Derive overall health from workers, RPC, and blocked ARKs."""
    hard_failure_states = {"unknown", "stale", "error", "stopped"}
    degraded_states = {
        "paused_rpc_unavailable",
        "paused_storage_unavailable",
        "paused_chain_stalled",
        "paused_chain_congested",
        "backlogged",
    }
    if worker_states and worker_states.issubset(hard_failure_states | {"disabled"}):
        return "down"
    if (
        worker_states & hard_failure_states
        or worker_states & degraded_states
        or error_summary["permanent"] > 0
        or not rpc["available"]
        or not storage["available"]
    ):
        return "degraded"
    return "ok"


def _build_status_message(
    workers: Dict[str, Any],
    error_summary: Dict[str, int],
    rpc: Dict[str, Any],
    storage: Dict[str, Any],
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
        elif state == "paused_chain_stalled":
            parts.append(f"{labels[key]} is paused because block production is stalled")
        elif state == "paused_chain_congested":
            parts.append(f"{labels[key]} is paused because chain congestion is suspected")
        elif state == "paused_storage_unavailable":
            parts.append(f"{labels[key]} is paused because metadata storage is unavailable")
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
    if not storage["available"]:
        parts.append("metadata storage is unavailable")
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
    overall = _derive_overall_status(
        worker_states,
        error_summary,
        full_status["rpc"],
        full_status["storage"],
    )

    return {
        "overall": overall,
        "message": _build_status_message(workers, error_summary, full_status["rpc"], full_status["storage"]),
        "rpc": full_status["rpc"],
        "storage": full_status["storage"],
        "chain_capacity": full_status["chain_capacity"],
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


@router.get("/errors", response_model=Dict[str, Any])
async def get_worker_errors(
    list_type: str | None = Query(
        default=None,
        alias="list",
        pattern="^(permanent|retrying|all)$",
        description="When omitted, return a compact summary. Use permanent/retrying/all to list rows.",
    ),
    stage: str = Query(
        default="all",
        pattern="^(all|metadata|chain)$",
        description="Filter by inferred failed stage.",
    ),
    authority_id: str | None = Query(
        default=None,
        description="Filter errors by authority UUID.",
    ),
    error_type: str = Query(
        default="all",
        pattern=(
            "^(all|transaction_reverted|gas_limit|authority|metadata_storage|"
            "infrastructure|reconcile_conflict|max_retries|unknown)$"
        ),
        description="Filter by derived operational error type.",
    ),
    page: int = Query(
        default=1,
        ge=1,
        description="1-based page number.",
    ),
    page_size: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Items per page.",
    ),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Summarize or list ARK worker errors for operational review.

    By default this endpoint returns a compact report. Use `list=permanent`,
    `list=retrying`, or `list=all` to request paginated rows.
    """
    resolved_list_type = list_type or "all"
    rows = _select_error_rows(
        db,
        list_type=resolved_list_type,
        stage=stage,
        authority_id=authority_id,
        error_type=error_type,
    )

    filters = {
        "list": list_type,
        "stage": stage,
        "authority_id": authority_id,
        "error_type": error_type,
    }

    if list_type is None:
        return {
            "filters": filters,
            **_build_errors_summary(rows),
        }

    total = len(rows)
    total_pages = ceil(total / page_size) if total else 0
    offset = (page - 1) * page_size
    page_rows = rows[offset : offset + page_size]

    return {
        "filters": filters,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1 and total > 0,
        },
        "items": [
            _serialize_worker_error(record, metadata)
            for record, metadata in page_rows
        ],
    }


def _rescue_operation_for_row(record: ARKRecord, metadata: ARKMetadata) -> ARKPublishOperation:
    """Build the semantic core-lib operation used by rescue."""
    action = "create" if record.state == ARKState.DRAFT.value else "update"
    return ARKPublishOperation(
        ref=record.ark,
        action=action,
        naan=record.naan,
        name=record.name,
        url=record.target,
        cid=metadata.level1_cid,
    )


def _rescue_item(
    record: ARKRecord,
    metadata: ARKMetadata | None,
    *,
    status: str,
    gas_limit: int,
    error_type: str | None = None,
    gas_estimate: int | None = None,
    gas_used: int | None = None,
    error: str | None = None,
) -> Dict[str, Any]:
    """Serialize one rescue result item."""
    return {
        "ark": record.ark,
        "error_type": error_type or _classify_error_type(record, metadata),
        "status": status,
        "final_state": _state_label(record.state),
        "gas_limit": gas_limit,
        "gas_estimate": gas_estimate,
        "gas_used": gas_used,
        "error": error,
    }


@router.post("/errors/rescue", response_model=Dict[str, Any])
async def rescue_worker_errors(
    request: RescueErrorsRequest,
    identity: dict = Depends(require_authority_identity),
    corelib_client: DARKCoreClient = Depends(get_corelib_client),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Rescue permanent chain errors selected by the same filters as /errors."""
    enforce_authority_match(identity, request.authority_id)

    settings = get_settings()
    max_items = int(settings.permanent_rescue_max_items)
    limit = min(int(request.limit or max_items), max_items)
    rescue_gas_limit = int(corelib_client.config.default_gas_limit) * 2
    filters = {
        "authority_id": request.authority_id,
        "stage": request.stage,
        "error_type": request.error_type,
    }

    if request.stage == "metadata":
        return {
            "filters": filters,
            "selected": 0,
            "processed": 0,
            "limit": limit,
            "gas": {
                "current_limit": int(corelib_client.config.default_gas_limit),
                "rescue_limit": rescue_gas_limit,
            },
            "items": [],
        }

    rows = _select_error_rows(
        db,
        list_type="permanent",
        stage="chain",
        authority_id=request.authority_id,
        error_type=request.error_type,
    )
    selected = len(rows)
    rows = rows[:limit]
    repo = ARKRepository(db)
    chain_worker = ChainPublisherWorker(
        corelib_client,
        page_size=settings.chain_worker_page_size,
        max_retries=settings.chain_worker_max_retries,
        backoff_base=settings.chain_worker_retry_backoff_base,
    )

    items: list[Dict[str, Any]] = []
    stopped = False
    stop_reason = None

    for record, metadata in rows:
        if metadata is None or not metadata.level1_cid or not metadata.original_cid:
            continue

        original_error_type = _classify_error_type(record, metadata)
        operation = _rescue_operation_for_row(record, metadata)
        try:
            gas_estimate = corelib_client.estimate_ark_operation_gas(
                request.authority_id,
                operation,
            )
        except Exception as exc:
            error_msg = f"Gas estimate failed during rescue: {exc}"
            repo.mark_publish_failed(record.ark, error_msg, is_permanent=True)
            db.commit()
            db.refresh(record)
            item = _rescue_item(
                record,
                metadata,
                status="estimate_failed",
                gas_limit=rescue_gas_limit,
                error=error_msg,
            )
            items.append(item)
            if _is_infrastructure_error_message(str(exc)):
                stopped = True
                stop_reason = "estimate_unavailable"
                break
            continue

        if int(gas_estimate) > rescue_gas_limit:
            error_msg = (
                "Gas limit too low during rescue: "
                f"estimate {gas_estimate} exceeds rescue gas limit {rescue_gas_limit}"
            )
            repo.mark_publish_failed(record.ark, error_msg, is_permanent=True)
            db.commit()
            db.refresh(record)
            items.append(
                _rescue_item(
                    record,
                    metadata,
                    status="skipped_gas_x2_insufficient",
                    gas_limit=rescue_gas_limit,
                    gas_estimate=int(gas_estimate),
                    error=error_msg,
                )
            )
            continue

        result = corelib_client.publish_ark_operation(
            request.authority_id,
            operation,
            gas_limit=rescue_gas_limit,
            gas_estimate=int(gas_estimate),
        )

        if result.status == "confirmed":
            try:
                repo.update_to_published(record.ark)
                repo.reset_publish_tracking(record.ark)
                db.commit()
                db.refresh(record)
                items.append(
                    _rescue_item(
                        record,
                        metadata,
                        status="confirmed",
                        gas_limit=rescue_gas_limit,
                        error_type=original_error_type,
                        gas_estimate=int(gas_estimate),
                        gas_used=result.gas_used,
                    )
                )
            except ValueError as exc:
                db.rollback()
                db.refresh(record)
                items.append(
                    _rescue_item(
                        record,
                        metadata,
                        status="finalize_failed",
                        gas_limit=rescue_gas_limit,
                        gas_estimate=int(gas_estimate),
                        gas_used=result.gas_used,
                        error=str(exc),
                    )
                )
            continue

        if result.status == "reverted":
            error_msg = f"Rescue transaction reverted (permanent): {result.error or result.status}"
            chain_worker._handle_publish_error(
                repo=repo,
                db=db,
                ark_record=record,
                ark_id=record.ark,
                naan=record.naan,
                name=record.name,
                expected_cid=metadata.level1_cid,
                error_msg=error_msg,
                original_is_permanent=True,
                original_is_infrastructure=False,
            )
            latest = repo.get_by_ark(record.ark)
            items.append(
                _rescue_item(
                    latest or record,
                    metadata,
                    status="reverted",
                    gas_limit=rescue_gas_limit,
                    gas_estimate=int(gas_estimate),
                    gas_used=result.gas_used,
                    error=error_msg,
                )
            )
            continue

        error_msg = f"Rescue transaction {result.status} (infrastructure): {result.error or result.status}"
        repo.mark_publish_failed(record.ark, error_msg, is_permanent=True)
        db.commit()
        db.refresh(record)
        items.append(
            _rescue_item(
                record,
                metadata,
                status=result.status,
                gas_limit=rescue_gas_limit,
                gas_estimate=int(gas_estimate),
                gas_used=result.gas_used,
                error=error_msg,
            )
        )
        stopped = True
        stop_reason = result.status
        break

    return {
        "filters": filters,
        "selected": selected,
        "processed": len(items),
        "limit": limit,
        "stopped": stopped,
        "stop_reason": stop_reason,
        "gas": {
            "current_limit": int(corelib_client.config.default_gas_limit),
            "rescue_limit": rescue_gas_limit,
        },
        "items": items,
    }
