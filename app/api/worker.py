"""Status, permanent errors and explicit administrative retries for three workers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from math import ceil
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.ark_locks import acquire_ark_lock
from app.database.models import ARKMetadata, ARKRecord
from app.dependencies import get_db
from app.middleware.auth import require_mtls
from app.models.processing import ProcessingErrorCode, ProcessingStage, ProcessingStatus, ProcessingWaitReason, code_label
from app.models.states import ARKState
from app.repositories import WorkerRuntimeRepository
from app.repositories.ark_repository import ARKRepository
from app.utils.rpc_health import check_rpc_health
from app.utils.storage_health import check_metadata_storage_health

router = APIRouter(tags=["Worker"])
logger = logging.getLogger(__name__)

WORKER_STAGES = {
    "metadata": (ProcessingStage.METADATA,),
    "replication": (ProcessingStage.AVAILABILITY, ProcessingStage.REPLICATION),
    "chain": (ProcessingStage.CHAIN,),
}


class RetryRequest(BaseModel):
    arks: list[str] = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _label(value: int | None, enum) -> str:
    return code_label(value, enum) or "unknown"


def _config(settings) -> dict[str, dict[str, Any]]:
    return {
        "metadata": {"kind": "metadata", "name": settings.metadata_worker_runtime_name, "enabled": settings.metadata_worker_enabled, "sleep": settings.metadata_worker_sleep_seconds, "page_size": settings.metadata_worker_page_size},
        "replication": {"kind": "replication", "name": settings.replication_worker_runtime_name, "enabled": settings.replication_worker_enabled, "sleep": settings.replication_worker_sleep_seconds, "page_size": settings.replication_worker_page_size},
        "chain": {"kind": "chain", "name": settings.chain_worker_runtime_name, "enabled": settings.chain_worker_enabled, "sleep": settings.chain_worker_sleep_seconds, "page_size": settings.chain_worker_page_size},
    }


def _process(record, cfg: dict[str, Any], now: datetime, stale_after: int) -> dict[str, Any]:
    if not cfg["enabled"]:
        return {"enabled": False, "alive": False, "process_state": "DISABLED"}
    if record is None:
        return {"enabled": True, "alive": False, "process_state": "DOWN"}
    age = (now - record.last_heartbeat_at).total_seconds() if record.last_heartbeat_at else None
    if age is None or age > stale_after or record.status in {"ERROR", "STOPPED"}:
        return {"enabled": True, "alive": False, "process_state": "DOWN", "heartbeat_age_seconds": age, "last_error": record.last_error}
    if record.status.startswith("PAUSED_"):
        return {"enabled": True, "alive": True, "process_state": "PAUSED", "pause_reason": record.status.lower().removeprefix("paused_"), "heartbeat_age_seconds": age, "last_error": record.last_error}
    wake_at = record.next_wake_at
    sleeping = record.status == "SLEEPING" and bool(wake_at and wake_at > now)
    # A heartbeat written exactly at cycle completion represents the normal
    # post-cycle wait even if an older/synthetic runtime row still says RUNNING.
    if (
        not sleeping
        and record.status == "RUNNING"
        and record.last_cycle_at is not None
        and record.last_heartbeat_at is not None
        and record.last_cycle_at == record.last_heartbeat_at
    ):
        sleeping = True
    last_cycle = {
        "observed": record.last_cycle_processed or 0,
        "advanced": record.last_cycle_succeeded or 0,
        "waiting": 0,
        "repaired": 0,
        "failed": record.last_cycle_failed or 0,
        "duration_seconds": record.last_cycle_duration_seconds,
    }
    if cfg.get("kind") == "replication":
        last_cycle.update({
            "observed": record.last_reconciliation_checked or 0,
            "advanced": record.last_reconciliation_advanced or 0,
            "waiting": record.last_reconciliation_waiting or 0,
            "repaired": record.last_reconciliation_repaired or 0,
            "failed": record.last_reconciliation_failed or 0,
        })
    no_progress_cycles = int(getattr(record, "consecutive_no_progress_cycles", 0) or 0)
    return {
        "enabled": True, "alive": True,
        "process_state": "SLEEPING" if sleeping else record.status if record.status in {"RUNNING", "STARTING"} else "RUNNING",
        "activity": "sleeping_until_due" if sleeping else "working",
        "wake_at": _iso(wake_at) if sleeping else None,
        "wake_in_seconds": max(0, round((wake_at - now).total_seconds(), 1)) if sleeping and wake_at else 0,
        "heartbeat_age_seconds": age,
        "last_cycle_at": _iso(record.last_cycle_at),
        "last_cycle": last_cycle,
        "no_progress_cycles": no_progress_cycles,
        "stalled_suspected": no_progress_cycles >= 3,
        "last_error": record.last_error,
        "totals": {
            "processed": int(record.total_processed or 0),
            "advanced": int(record.total_succeeded or 0),
            "failed": int(record.total_failed or 0),
        },
    }


def _workload(db: Session) -> dict[str, Any]:
    """Bounded grouped SQL; process state is intentionally not inferred here."""
    now = _now()
    result = {
        name: {
            "ready_now": 0,
            "waiting": 0,
            "oldest_wait_seconds": 0,
            "next_action_at": None,
            "waiting_reasons": {},
            "failed": 0,
            "by_stage": {},
        }
        for name in WORKER_STAGES
    }
    owner = {int(stage): name for name, stages in WORKER_STAGES.items() for stage in stages}
    due = case((ARKRecord.next_action_at <= now, 1), else_=0)
    rows = db.query(ARKRecord.processing_stage, ARKRecord.processing_status, ARKRecord.processing_wait_reason, due, func.count(), func.min(ARKRecord.next_action_at), func.min(ARKRecord.created_at)).filter(
        ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value, ARKState.PUBLISHED.value])
    ).group_by(ARKRecord.processing_stage, ARKRecord.processing_status, ARKRecord.processing_wait_reason, due).all()
    for stage, status, reason, is_due, count, earliest, oldest_created in rows:
        name = owner.get(int(stage))
        if not name:
            continue
        item = result[name]
        stage_label = _label(stage, ProcessingStage)
        stage_totals = item["by_stage"].setdefault(stage_label, {"ready_now": 0, "waiting": 0, "failed": 0})
        if int(status) == int(ProcessingStatus.READY) or (
            int(status) == int(ProcessingStatus.WAITING) and bool(is_due)
        ):
            item["ready_now"] += int(count)
            stage_totals["ready_now"] += int(count)
        elif int(status) == int(ProcessingStatus.WAITING):
            item["waiting"] += int(count)
            stage_totals["waiting"] += int(count)
            label = _label(reason, ProcessingWaitReason)
            item["waiting_reasons"][label] = item["waiting_reasons"].get(label, 0) + int(count)
            if earliest and (item["next_action_at"] is None or earliest < item["next_action_at"]):
                item["next_action_at"] = earliest
            if oldest_created:
                item["oldest_wait_seconds"] = max(item["oldest_wait_seconds"], round((now - oldest_created).total_seconds(), 1))
        elif int(status) == int(ProcessingStatus.FAILED):
            item["failed"] += int(count)
            stage_totals["failed"] += int(count)
    for item in result.values():
        # Keep the legacy alias while exposing the unambiguous name used by
        # the dashboard and new clients.
        item["ready"] = item["ready_now"]
        item["next_action_at"] = _iso(item["next_action_at"])
    critical_metadata = db.query(func.count()).select_from(ARKRecord).filter(
        ARKRecord.processing_stage == int(ProcessingStage.METADATA),
        ARKRecord.processing_status.in_([
            int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)
        ]),
    ).scalar() or 0
    critical_availability = db.query(func.count()).select_from(ARKRecord).join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id).filter(
        ARKRecord.processing_stage == int(ProcessingStage.AVAILABILITY),
        ARKRecord.processing_status.in_([
            int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)
        ]),
        ARKMetadata.level1_cid.isnot(None),
        ARKMetadata.original_cid.isnot(None),
    ).scalar() or 0
    published_replication = db.query(func.count()).select_from(ARKRecord).filter(
        ARKRecord.state == ARKState.PUBLISHED.value,
        ARKRecord.processing_stage == int(ProcessingStage.REPLICATION),
        ARKRecord.processing_status.in_([
            int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)
        ]),
    ).scalar() or 0
    result["replication"]["maintenance_ready"] = int(published_replication)
    result["replication"]["maintenance_blocked_by"] = (
        "metadata_backlog" if critical_metadata else
        "first_pin_backlog" if critical_availability else None
    )
    availability_total = result["replication"]["by_stage"].get("availability", {})
    availability_pressure = int(availability_total.get("ready_now", 0) + availability_total.get("waiting", 0))
    result["replication"]["availability_pressure"] = (
        "saturated" if availability_pressure > 2000 or result["replication"]["oldest_wait_seconds"] > 300
        else "busy" if availability_pressure >= 500 else "normal"
    )
    return result


def _errors_summary(db: Session) -> dict[str, Any]:
    rows = db.query(ARKRecord.processing_stage, ARKRecord.processing_error_code, func.count()).filter(
        ARKRecord.processing_status == int(ProcessingStatus.FAILED)
    ).group_by(ARKRecord.processing_stage, ARKRecord.processing_error_code).all()
    by_stage, by_code = {}, {}
    for stage, code, count in rows:
        stage_label, code_label_value = _label(stage, ProcessingStage), _label(code, ProcessingErrorCode)
        by_stage[stage_label] = by_stage.get(stage_label, 0) + int(count)
        by_code[code_label_value] = by_code.get(code_label_value, 0) + int(count)
    return {"total": sum(by_stage.values()), "by_stage": by_stage, "by_code": by_code}


def _backlog_age_metrics(db: Session) -> dict[str, Any]:
    """One bounded aggregate for the expensive diagnostic mode only."""
    stages = [int(ProcessingStage.METADATA), int(ProcessingStage.AVAILABILITY), int(ProcessingStage.CHAIN)]
    base = db.query(
        func.count(ARKRecord.id),
        func.min(ARKRecord.created_at),
    ).filter(
        ARKRecord.processing_stage.in_(stages),
        ARKRecord.processing_status.in_([int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)]),
    )
    count, oldest = base.one()
    result = {"count": int(count or 0), "oldest_seconds": round((_now() - oldest).total_seconds(), 1) if oldest else 0}
    # Percentiles are PostgreSQL-specific. The deployment requires PostgreSQL;
    # leave them explicit-but-unavailable for lightweight SQLite unit tests.
    if db.bind and db.bind.dialect.name == "postgresql" and count:
        p50, p95 = db.execute(text("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at))),
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)))
            FROM ark_records
            WHERE processing_stage IN (1, 2, 4)
              AND processing_status IN (1, 2)
        """)).one()
        result.update({"p50_seconds": round(float(p50 or 0), 1), "p95_seconds": round(float(p95 or 0), 1)})
    return result


def _simple_status(db: Session) -> dict[str, Any]:
    settings, now = get_settings(), _now()
    config = _config(settings)
    runtime = WorkerRuntimeRepository(db).get_by_names(tuple(value["name"] for value in config.values()))
    workers = {name: _process(runtime.get(value["name"]), value, now, settings.worker_heartbeat_stale_after_seconds) for name, value in config.items()}
    states = {value["process_state"] for value in workers.values() if value["enabled"]}
    stalled = any(worker.get("stalled_suspected") for worker in workers.values())
    overall = "down" if states and states <= {"DOWN"} else "degraded" if states & {"DOWN", "PAUSED"} or stalled else "ok"
    return {"overall": overall, "source": "db_heartbeat", "workers": workers}


@router.get("/status", response_model=dict[str, Any])
def get_worker_status(detail: str = Query("simple", pattern="^(simple|workload|infrastructure|full)$"), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return cheap heartbeats, bounded SQL workload, infrastructure, or both."""
    settings = get_settings()
    response = _simple_status(db)
    if detail in {"workload", "full"}:
        response["workload"] = _workload(db)
        response["errors"] = _errors_summary(db)
        response["backlog_age"] = _backlog_age_metrics(db)
        response["configuration"] = {
            "replication": {
                "page_size": settings.replication_worker_page_size,
                "status_batch_size": settings.replication_status_batch_size,
                "promotion_batch_size": settings.replication_promotion_batch_size,
                "maintenance_cycle_seconds": settings.replication_maintenance_cycle_seconds,
                "idle_sleep_seconds": settings.replication_idle_sleep_seconds,
                "first_pin_rechecks_seconds": [
                    settings.replication_first_pin_recheck_seconds,
                    settings.replication_first_pin_second_recheck_seconds,
                    settings.replication_first_pin_max_recheck_seconds,
                ],
                "durability_rechecks_seconds": [
                    settings.replication_durability_recheck_seconds,
                    settings.replication_durability_second_recheck_seconds,
                    settings.replication_durability_max_recheck_seconds,
                ],
                "publish_after_replicas": settings.replication_publish_after_replicas,
                "target_replicas": settings.replication_target_replicas,
            }
        }
    if detail in {"infrastructure", "full"}:
        response["infrastructure"] = {"rpc": check_rpc_health(), "storage": check_metadata_storage_health()}
    return response


@router.get("/errors", response_model=dict[str, Any])
def get_worker_errors(
    stage: str = Query("all", pattern="^(all|metadata|availability|chain|replication)$"),
    authority_id: str | None = None,
    error_code: str = Query("all", pattern="^(all|[a-z0-9_]+)$"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Only permanent failures are errors; normal waits are not listed here."""
    query = db.query(ARKRecord, ARKMetadata).outerjoin(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id).filter(ARKRecord.processing_status == int(ProcessingStatus.FAILED))
    if stage != "all": query = query.filter(ARKRecord.processing_stage == int(ProcessingStage[stage.upper()]))
    if authority_id: query = query.filter(ARKRecord.authority_id == authority_id)
    if error_code != "all":
        query = query.filter(ARKRecord.processing_error_code == int(ProcessingErrorCode[error_code.upper()])) if error_code.upper() in ProcessingErrorCode.__members__ else query.filter(ARKRecord.id == -1)
    total = query.count()
    rows = query.order_by(ARKRecord.updated_at.desc(), ARKRecord.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"summary": _errors_summary(db), "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": ceil(total / page_size) if total else 0}, "items": [{"ark": r.ark, "public_state": r.state, "stage": _label(r.processing_stage, ProcessingStage), "error_code": _label(r.processing_error_code, ProcessingErrorCode), "detail": r.processing_error_detail, "attempts": r.processing_attempt_count, "updated_at": _iso(r.updated_at), "level1_cid": m.level1_cid if m else None, "level2_cid": m.original_cid if m else None} for r, m in rows]}


@router.post("/retry", response_model=dict[str, Any])
def retry_failed_arks(request: RetryRequest, cert_info: dict = Depends(require_mtls)) -> dict[str, Any]:
    """Explicitly return a permanent failure to READY in its current stage."""
    identity, outcomes = (cert_info or {}).get("dn", "local-development"), []
    for ark in dict.fromkeys(request.arks):
        with acquire_ark_lock(ark) as db:
            if db is None:
                outcomes.append({"ark": ark, "status": "locked"}); continue
            record = ARKRepository(db).get_by_ark(ark)
            if record is None:
                outcomes.append({"ark": ark, "status": "not_found"}); continue
            if record.state in {ARKState.RESERVED.value, ARKState.TOMBSTONE.value}:
                outcomes.append({"ark": ark, "status": "not_retryable_state"}); continue
            if record.processing_status != int(ProcessingStatus.FAILED):
                outcomes.append({"ark": ark, "status": "not_failed"}); continue
            record.processing_status, record.next_action_at = int(ProcessingStatus.READY), None
            record.processing_wait_reason, record.processing_attempt_count = int(ProcessingWaitReason.NONE), 0
            record.updated_at = _now(); db.commit()
            logger.info("Administrative retry approved by %s for %s: %s", identity, ark, request.reason)
            outcomes.append({"ark": ark, "status": "accepted"})
    return {"requested": len(request.arks), "reason": request.reason, "items": outcomes}
