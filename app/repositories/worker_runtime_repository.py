"""
Repository for worker runtime heartbeat/status.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.database.models import WorkerRuntimeStatus


class WorkerRuntimeRepository:
    """DB operations for worker runtime status."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_name(self, worker_name: str) -> Optional[WorkerRuntimeStatus]:
        """Get worker runtime row by worker name."""
        return (
            self.db.query(WorkerRuntimeStatus)
            .filter(WorkerRuntimeStatus.worker_name == worker_name)
            .first()
        )

    def upsert_status(
        self,
        worker_name: str,
        instance_id: str,
        host: str,
        pid: int,
        status: str,
        heartbeat_at: datetime,
        started_at: datetime,
        last_cycle_at: Optional[datetime] = None,
        last_cycle_duration_seconds: Optional[float] = None,
        last_cycle_processed: Optional[int] = None,
        last_cycle_succeeded: Optional[int] = None,
        last_cycle_failed: Optional[int] = None,
        last_reconciliation_at: Optional[datetime] = None,
        last_reconciliation_checked: int = 0,
        last_reconciliation_repaired: int = 0,
        last_reconciliation_purged: int = 0,
        last_reconciliation_failed: int = 0,
        last_error: Optional[str] = None,
        total_processed: int = 0,
        total_succeeded: int = 0,
        total_failed: int = 0,
        total_permanent_failures: int = 0,
    ) -> WorkerRuntimeStatus:
        """
        Create or update worker runtime status row.

        Note:
            Does not commit. Caller controls transaction boundary.
        """
        record = self.get_by_name(worker_name)
        if record is None:
            record = WorkerRuntimeStatus(
                worker_name=worker_name,
                instance_id=instance_id,
                host=host,
                pid=pid,
                status=status,
                last_heartbeat_at=heartbeat_at,
                started_at=started_at,
                last_cycle_at=last_cycle_at,
                last_cycle_duration_seconds=last_cycle_duration_seconds,
                last_cycle_processed=last_cycle_processed,
                last_cycle_succeeded=last_cycle_succeeded,
                last_cycle_failed=last_cycle_failed,
                last_reconciliation_at=last_reconciliation_at,
                last_reconciliation_checked=last_reconciliation_checked,
                last_reconciliation_repaired=last_reconciliation_repaired,
                last_reconciliation_purged=last_reconciliation_purged,
                last_reconciliation_failed=last_reconciliation_failed,
                last_error=last_error,
                total_processed=total_processed,
                total_succeeded=total_succeeded,
                total_failed=total_failed,
                total_permanent_failures=total_permanent_failures,
            )
            self.db.add(record)
            return record

        record.instance_id = instance_id
        record.host = host
        record.pid = pid
        record.status = status
        record.last_heartbeat_at = heartbeat_at
        record.started_at = started_at
        record.last_cycle_at = last_cycle_at
        record.last_cycle_duration_seconds = last_cycle_duration_seconds
        record.last_cycle_processed = last_cycle_processed
        record.last_cycle_succeeded = last_cycle_succeeded
        record.last_cycle_failed = last_cycle_failed
        record.last_reconciliation_at = last_reconciliation_at
        record.last_reconciliation_checked = last_reconciliation_checked
        record.last_reconciliation_repaired = last_reconciliation_repaired
        record.last_reconciliation_purged = last_reconciliation_purged
        record.last_reconciliation_failed = last_reconciliation_failed
        record.last_error = last_error
        record.total_processed = total_processed
        record.total_succeeded = total_succeeded
        record.total_failed = total_failed
        record.total_permanent_failures = total_permanent_failures

        return record
