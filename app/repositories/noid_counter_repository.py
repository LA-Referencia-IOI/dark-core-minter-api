"""
Repository for deterministic NOID counter allocation.
"""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import CompileError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database.models import NoidCounter


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NoidCounterRepository:
    """DB operations for per-namespace NOID counters."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def build_namespace_key(naan: str, shoulder: str) -> str:
        """Build deterministic namespace key for counter partitioning."""
        normalized_shoulder = shoulder or ""
        return f"{naan}:{normalized_shoulder}"

    def _ensure_counter_row(self, namespace_key: str) -> None:
        """Create counter row lazily when namespace is first used."""
        bind = self.db.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ""

        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = (
                pg_insert(NoidCounter)
                .values(namespace_key=namespace_key, next_value=0, updated_at=_utc_now())
                .on_conflict_do_nothing(index_elements=["namespace_key"])
            )
            self.db.execute(stmt)
            return

        # Generic fallback: ignore duplicate row race using savepoint.
        with self.db.begin_nested():
            try:
                self.db.add(NoidCounter(namespace_key=namespace_key, next_value=0))
                self.db.flush()
            except IntegrityError:
                pass

    def allocate_next(self, namespace_key: str) -> int:
        """
        Atomically allocate the next counter value for namespace.

        Returns:
            The allocated integer value (starting at 0).
        """
        self._ensure_counter_row(namespace_key)

        try:
            # Preferred path: single atomic UPDATE ... RETURNING for concurrent writers.
            stmt = (
                update(NoidCounter)
                .where(NoidCounter.namespace_key == namespace_key)
                .values(next_value=NoidCounter.next_value + 1, updated_at=_utc_now())
                .returning(NoidCounter.next_value)
            )
            new_next_value = self.db.execute(stmt).scalar_one()
            return int(new_next_value) - 1
        except (CompileError, OperationalError):
            # Fallback for dialects that don't support RETURNING.
            row = (
                self.db.query(NoidCounter)
                .filter(NoidCounter.namespace_key == namespace_key)
                .with_for_update()
                .one()
            )
            current = int(row.next_value)
            row.next_value = current + 1
            row.updated_at = _utc_now()
            self.db.flush()
            return current
