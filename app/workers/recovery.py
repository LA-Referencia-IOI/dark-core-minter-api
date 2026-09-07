"""Low-priority recovery scheduler for recoverable ARK processing failures."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_

from app.config import get_settings
from app.database.ark_locks import acquire_ark_lock
from app.database.connection import SessionLocal
from app.database.models import ARKMetadata, ARKRecord, ProcessingErrorCodeModel
from app.models.processing import ProcessingErrorCode, ProcessingStage, ProcessingStatus
from app.models.states import ARKState
from app.repositories import WorkerRuntimeRepository
from app.repositories.ark_repository import ARKRepository
from app.workers.publisher import _Stats


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def derive_recovery_stage(record: ARKRecord, metadata: ARKMetadata | None) -> tuple[ProcessingStage | None, str]:
    """Return the safe local restart stage, without contacting external systems."""
    if record.state == ARKState.RESERVED.value:
        return None, "reserved"
    if record.state == ARKState.TOMBSTONE.value:
        return None, "tombstoned"
    if metadata is None:
        return None, "metadata_missing"
    has_l1, has_l2 = bool(metadata.level1_cid), bool(metadata.original_cid)
    if record.state in (ARKState.DRAFT.value, ARKState.UPDATE.value):
        if has_l1 and has_l2:
            return ProcessingStage.AVAILABILITY, "verify_availability"
        can_make_l2 = has_l2 or bool(metadata.original_content and metadata.original_media_type)
        can_make_l1 = has_l1 or bool(metadata.level1_json)
        if can_make_l1 and can_make_l2:
            return ProcessingStage.METADATA, "persist_metadata"
        return None, "payload_missing"
    if record.state == ARKState.PUBLISHED.value:
        if has_l1 and has_l2:
            return ProcessingStage.REPLICATION, "verify_replication"
        return None, "published_cids_missing"
    return None, "unsupported_state"


class RecoveryWorker(_Stats):
    """Return recoverable ARKs to normal queues only while the system is idle."""

    def __init__(self, page_size: int = 50):
        self.page_size = max(int(page_size), 1)
        self._init_stats()

    def _normal_workers_alive_and_idle(self, db) -> bool:
        settings = get_settings()
        names = (
            settings.metadata_worker_runtime_name,
            settings.replication_worker_runtime_name,
            settings.chain_worker_runtime_name,
        )
        stale_before = _now().timestamp() - settings.worker_heartbeat_stale_after_seconds
        runtime = WorkerRuntimeRepository(db).get_by_names(names)
        if len(runtime) != len(names):
            return False
        for name in names:
            heartbeat = runtime[name].last_heartbeat_at
            if heartbeat is None or heartbeat.timestamp() < stale_before or runtime[name].status != "RUNNING":
                return False
        normal_pending = db.query(func.count(ARKRecord.id)).filter(
            ARKRecord.processing_status == int(ProcessingStatus.PENDING),
            ARKRecord.processing_stage.in_(
                [
                    int(ProcessingStage.METADATA),
                    int(ProcessingStage.AVAILABILITY),
                    int(ProcessingStage.CHAIN),
                    int(ProcessingStage.REPLICATION),
                ]
            ),
        ).scalar()
        return int(normal_pending or 0) == 0

    def _candidates(self, db) -> list[str]:
        now = _now()
        retryable_failed = and_(
            ARKRecord.processing_status == int(ProcessingStatus.FAILED),
            ProcessingErrorCodeModel.retryable.is_(True),
        )
        due_recoverable = and_(
            ARKRecord.processing_status == int(ProcessingStatus.RECOVERABLE),
            or_(
                ARKRecord.processing_next_attempt_at.is_(None),
                ARKRecord.processing_next_attempt_at <= now,
            ),
        )
        return [
            row.ark
            for row in (
                db.query(ARKRecord)
                .outerjoin(
                    ProcessingErrorCodeModel,
                    ProcessingErrorCodeModel.id == ARKRecord.processing_error_code,
                )
                .filter(or_(due_recoverable, retryable_failed))
                .order_by(ARKRecord.processing_next_attempt_at.asc().nullsfirst(), ARKRecord.id.asc())
                .limit(self.page_size)
                .all()
            )
        ]

    def _recover(self, ark: str) -> bool:
        with acquire_ark_lock(ark) as db:
            if db is None:
                return False
            repo = ARKRepository(db)
            record = repo.get_by_ark(ark)
            if record is None:
                return False
            now = _now()
            policy = (
                db.query(ProcessingErrorCodeModel)
                .filter_by(id=record.processing_error_code)
                .first()
                if record.processing_error_code is not None
                else None
            )
            eligible = (
                record.processing_status == int(ProcessingStatus.RECOVERABLE)
                and (
                    record.processing_next_attempt_at is None
                    or record.processing_next_attempt_at <= now
                )
            ) or (
                record.processing_status == int(ProcessingStatus.FAILED)
                and policy is not None
                and bool(policy.retryable)
            )
            if not eligible:
                return False
            metadata = repo.get_metadata_by_ark_id(record.id)
            stage, reason = derive_recovery_stage(record, metadata)
            if stage is None:
                if reason in {"reserved", "tombstoned"}:
                    return False
                repo.mark_processing_failed(
                    ark,
                    f"recovery not possible: {reason}",
                    int(ProcessingErrorCode.RECOVERY_NOT_POSSIBLE),
                    is_permanent=True,
                )
                db.commit()
                self._error(ark, reason)
                self.stats["total_permanent_failures"] += 1
                return False
            record.processing_stage = int(stage)
            record.processing_status = int(ProcessingStatus.PENDING)
            record.processing_next_attempt_at = None
            record.updated_at = _now()
            db.commit()
            return True

    def run_publish_cycle(self) -> None:
        started, before = self._start()
        session_factory = SessionLocal()
        db = session_factory()
        try:
            if not self._normal_workers_alive_and_idle(db):
                self.stats["last_run_page_size"] = self.page_size
                self._finish(started, before)
                return
            arks = self._candidates(db)
        finally:
            db.close()
        self.stats["last_run_page_size"] = self.page_size
        for ark in arks:
            self.stats["total_processed"] += 1
            if self._recover(ark):
                self.stats["total_succeeded"] += 1
            else:
                self.stats["total_failed"] += 1
        self._finish(started, before)
