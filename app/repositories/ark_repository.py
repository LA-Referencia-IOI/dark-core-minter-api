"""
ARK repository for database operations.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from app.database.models import ARKRecord, ARKMetadata, ProcessingErrorCodeModel
from app.models.processing import ProcessingStage, ProcessingStatus, ProcessingWaitReason
from app.models.states import ARKState


@dataclass(frozen=True)
class ReconciliationCandidate:
    """Joined, bounded input for a single replication observation."""

    ark: str
    record_id: int
    level1_cid: str
    level2_cid: str


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ark(ark: str) -> Tuple[str, str]:
    """
    Parse full ARK identifier into naan and name components.

    Args:
        ark: Full ARK identifier (e.g., "ark:12345/abc123")

    Returns:
        Tuple of (naan, name)

    Raises:
        ValueError: If ARK format is invalid
    """
    if not ark.startswith("ark:"):
        raise ValueError(f"Invalid ARK format: {ark}")

    parts = ark[4:].split("/", 1)  # Remove "ark:" prefix and split
    if len(parts) != 2:
        raise ValueError(f"Invalid ARK format: {ark}")

    return parts[0], parts[1]


def _ready_for_processing(record: ARKRecord, now: datetime) -> bool:
    """Return whether a record is ready for an advisory-lock protected attempt."""
    status = int(record.processing_status)
    if status == int(ProcessingStatus.READY):
        return True
    if status == int(ProcessingStatus.WAITING):
        return record.next_action_at is not None and record.next_action_at <= now
    return False


def _ready_for_processing_filter(now: datetime):
    """SQL predicate matching ready work and due normal waits."""
    return or_(
        and_(
            ARKRecord.processing_status == int(ProcessingStatus.READY),
        ),
        and_(
            ARKRecord.processing_status == int(ProcessingStatus.WAITING),
            ARKRecord.next_action_at <= now,
        ),
    )


class ARKRepository:
    """Repository for ARK database operations."""

    def __init__(self, db: Session):
        """
        Initialize repository with database session.

        Args:
            db: SQLAlchemy session
        """
        self.db = db

    def create_reserved(
        self,
        naan: str,
        name: str,
        authority_id: str,
        client_item_id: Optional[str] = None,
    ) -> ARKRecord:
        """
        Create a new ARK record in RESERVED state.

        Args:
            naan: Name Assigning Authority Number
            name: ARK name/suffix
            authority_id: Authority UUID that owns this ARK
            client_item_id: Optional client tracking ID

        Returns:
            Created ARKRecord (ark property computes full identifier)

        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        db_ark = ARKRecord(
            naan=naan,
            name=name,
            state=ARKState.RESERVED.value,
            authority_id=authority_id,
            client_item_id=client_item_id,
        )
        self.db.add(db_ark)
        return db_ark

    def get_active_by_client_item_id(
        self,
        authority_id: str,
        naan: str,
        client_item_id: Optional[str],
    ) -> Optional[ARKRecord]:
        """
        Return the active ARK for a client item idempotency key, if any.

        Tombstoned records intentionally do not block new reservations for the
        same client item ID.
        """
        if client_item_id is None:
            return None

        return (
            self.db.query(ARKRecord)
            .filter(
                ARKRecord.authority_id == authority_id,
                ARKRecord.naan == naan,
                ARKRecord.client_item_id == client_item_id,
                ARKRecord.state != ARKState.TOMBSTONE.value,
            )
            .order_by(ARKRecord.id.asc())
            .first()
        )

    def get_active_by_client_item_ids(
        self,
        authority_id: str,
        naan: str,
        client_item_ids: List[str],
    ) -> Dict[str, ARKRecord]:
        """
        Return active ARKs keyed by client item ID for a batch request.
        """
        unique_ids = list(
            dict.fromkeys(item_id for item_id in client_item_ids if item_id is not None)
        )
        if not unique_ids:
            return {}

        rows = (
            self.db.query(ARKRecord)
            .filter(
                ARKRecord.authority_id == authority_id,
                ARKRecord.naan == naan,
                ARKRecord.client_item_id.in_(unique_ids),
                ARKRecord.state != ARKState.TOMBSTONE.value,
            )
            .order_by(ARKRecord.id.asc())
            .all()
        )

        result: Dict[str, ARKRecord] = {}
        for row in rows:
            if row.client_item_id is not None and row.client_item_id not in result:
                result[row.client_item_id] = row
        return result

    def create_published_import(
        self,
        naan: str,
        name: str,
        authority_id: str,
        target: Optional[str] = None,
    ) -> ARKRecord:
        """
        Create a local record for an ARK that already exists on-chain.

        Initial local state is PUBLISHED.
        """
        db_ark = ARKRecord(
            naan=naan,
            name=name,
            state=ARKState.PUBLISHED.value,
            authority_id=authority_id,
            target=target,
        )
        self.db.add(db_ark)
        self.db.flush()  # We need the ID for metadata creation

        # If metadata is provided, create the ARKMetadata record
        # Note: In import scenarios, we might only have CIDs or partial data
        # For now, we only create ARKMetadata if we have content, which isn't passed here yet.
        # Future TODO: Allow importing full metadata content.

        return db_ark

    def get_by_ark(self, ark: str) -> Optional[ARKRecord]:
        """
        Get ARK record by full ARK identifier.

        Args:
            ark: Full ARK identifier (e.g., "ark:12345/abc123")

        Returns:
            ARKRecord if found, None otherwise
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError:
            return None
        return (
            self.db.query(ARKRecord).filter(ARKRecord.naan == naan, ARKRecord.name == name).first()
        )

    def get_by_naan_name(self, naan: str, name: str) -> Optional[ARKRecord]:
        """
        Get ARK record by naan and name components.

        Args:
            naan: Name Assigning Authority Number
            name: ARK name/suffix

        Returns:
            ARKRecord if found, None otherwise
        """
        return (
            self.db.query(ARKRecord).filter(ARKRecord.naan == naan, ARKRecord.name == name).first()
        )

    def exists(self, ark: str) -> bool:
        """
        Check if ARK exists in database.

        Args:
            ark: Full ARK identifier

        Returns:
            True if exists, False otherwise
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError:
            return False
        return (
            self.db.query(ARKRecord).filter(ARKRecord.naan == naan, ARKRecord.name == name).count()
            > 0
        )

    def update_to_draft(
        self,
        ark: str,
        target: str,
    ) -> ARKRecord:
        """
        Update ARK to DRAFT state.

        Args:
            ark: Full ARK identifier
            target: Target URL

        Returns:
            Updated ARKRecord

        Raises:
            ValueError: If ARK not in RESERVED state or missing required fields

        Note:
            Does NOT commit. Caller must commit the transaction.
            Metadata content should be stored in ARKMetadata separately.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        # Validate required fields
        if not target:
            raise ValueError("Target URL is required")

        now = _utc_now()
        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
                ARKRecord.state == ARKState.RESERVED.value,
            )
            .values(
                state=ARKState.DRAFT.value,
                target=target,
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            existing = self.get_by_naan_name(naan, name)
            if not existing:
                raise ValueError(f"ARK not found: {ark}")
            raise ValueError(f"ARK must be in RESERVED state, currently {existing.state}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def create_or_update_metadata(
        self,
        ark_record_id: int,
        level1_json: dict,
        original_content: str,
        original_schema: str,
        original_media_type: Optional[str] = None,
    ) -> ARKMetadata:
        """
        Create or update the two-level metadata record for an ARK.
        """
        metadata = self.db.query(ARKMetadata).filter_by(ark_record_id=ark_record_id).first()

        if metadata:
            metadata.level1_json = level1_json
            metadata.original_content = original_content
            metadata.original_schema = original_schema
            metadata.original_media_type = original_media_type
            # CIDs are reset on update because content changed (worker will re-publish)
            metadata.level1_cid = None
            metadata.original_cid = None
            metadata.level1_replica_count = None
            metadata.level2_replica_count = None
            metadata.replication_checked_at = None
            metadata.replication_error_code = None
            metadata.replication_last_error = None
            metadata.payload_purged_at = None
            metadata.updated_at = _utc_now()
        else:
            metadata = ARKMetadata(
                ark_record_id=ark_record_id,
                level1_json=level1_json,
                original_content=original_content,
                original_schema=original_schema,
                original_media_type=original_media_type,
            )
            self.db.add(metadata)

        ark_record = self.db.query(ARKRecord).filter_by(id=ark_record_id).first()
        if ark_record:
            ark_record.processing_stage = int(ProcessingStage.METADATA)
            ark_record.processing_status = int(ProcessingStatus.READY)
            ark_record.processing_attempt_count = 0
            ark_record.next_action_at = None
            ark_record.processing_wait_reason = int(ProcessingWaitReason.NONE)
            ark_record.processing_error_code = None
            ark_record.processing_error_detail = None
            ark_record.updated_at = _utc_now()

        return metadata

    def get_metadata_by_ark_id(self, ark_record_id: int) -> Optional[ARKMetadata]:
        return self.db.query(ARKMetadata).filter_by(ark_record_id=ark_record_id).first()

    def update_metadata_cids(
        self,
        ark_record_id: int,
        level1_cid: Optional[str] = None,
        level2_cid: Optional[str] = None,
        level1_replica_count: Optional[int] = None,
        level2_replica_count: Optional[int] = None,
        purge_local: bool = False,
        reset_processing: bool = False,
    ) -> None:
        """Update CIDs after worker processing."""
        metadata = self.get_metadata_by_ark_id(ark_record_id)
        if not metadata:
            raise ValueError(f"Metadata not found for ARK ID {ark_record_id}")

        if level1_cid is not None:
            metadata.level1_cid = level1_cid
        if level2_cid is not None:
            metadata.original_cid = level2_cid
        if level1_replica_count is not None:
            metadata.level1_replica_count = max(int(level1_replica_count), 0)
        if level2_replica_count is not None:
            metadata.level2_replica_count = max(int(level2_replica_count), 0)
        if purge_local:
            metadata.level1_json = None
            metadata.original_content = None
        metadata.updated_at = _utc_now()

        if reset_processing:
            ark_record = self.db.query(ARKRecord).filter_by(id=ark_record_id).first()
            if ark_record:
                ark_record.processing_attempt_count = 0
                ark_record.next_action_at = None
                ark_record.processing_wait_reason = int(ProcessingWaitReason.NONE)
                ark_record.processing_error_code = None
                ark_record.processing_error_detail = None
                ark_record.updated_at = _utc_now()

    def get_metadata_pending_reconciliation(
        self,
        limit: int = 50,
        recheck_seconds: int = 300,
        stage: int | None = None,
    ) -> List[ARKRecord]:
        """Return a work-conserving page, prioritizing first-pin work."""
        now = _utc_now()
        del recheck_seconds
        normal_queue = _ready_for_processing_filter(now)

        def query_for(stage: int, quota: int) -> List[ARKRecord]:
            query = (
            self.db.query(ARKRecord)
            .join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
            .filter(
                ARKMetadata.level1_cid.isnot(None),
                ARKMetadata.original_cid.isnot(None),
                normal_queue,
                ARKRecord.processing_stage == stage,
            )
            .order_by(
                ARKRecord.next_action_at.asc().nullsfirst(),
                ARKMetadata.replication_checked_at.asc().nullsfirst(),
                ARKRecord.id.asc(),
            )
            )
            return self._claim_ready_records(query, quota, max_retries=0, backoff_base=1.0)

        if stage is not None:
            return query_for(int(stage), limit)
        availability = query_for(int(ProcessingStage.AVAILABILITY), limit)
        if len(availability) >= limit:
            return availability[:limit]
        durability = query_for(int(ProcessingStage.REPLICATION), limit - len(availability))
        selected = availability + durability
        return selected[:limit]

    def get_reconciliation_candidates(
        self, limit: int, stage: int
    ) -> List[ReconciliationCandidate]:
        """Return CIDs with the selector in one SQL query, not N+1 lookups."""
        now = _utc_now()
        rows = (
            self.db.query(ARKRecord, ARKMetadata)
            .join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
            .filter(
                ARKMetadata.level1_cid.isnot(None),
                ARKMetadata.original_cid.isnot(None),
                _ready_for_processing_filter(now),
                ARKRecord.processing_stage == int(stage),
            )
            .order_by(
                ARKRecord.next_action_at.asc().nullsfirst(),
                ARKMetadata.replication_checked_at.asc().nullsfirst(),
                ARKRecord.id.asc(),
            )
            .limit(limit)
            .all()
        )
        return [
            ReconciliationCandidate(record.ark, record.id, metadata.level1_cid, metadata.original_cid)
            for record, metadata in rows
        ]

    def has_critical_reconciliation_backlog(self) -> bool:
        """Whether metadata persistence or first-pin work still exists."""
        metadata = self.db.query(ARKRecord.id).filter(
            ARKRecord.processing_stage == int(ProcessingStage.METADATA),
            ARKRecord.processing_status.in_([
                int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)
            ]),
        ).limit(1).first()
        if metadata is not None:
            return True
        availability = (
            self.db.query(ARKRecord.id)
            .join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
            .filter(
                ARKRecord.processing_stage == int(ProcessingStage.AVAILABILITY),
                ARKRecord.processing_status.in_([
                    int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)
                ]),
                ARKMetadata.level1_cid.isnot(None),
                ARKMetadata.original_cid.isnot(None),
            )
            .limit(1)
            .first()
        )
        return availability is not None

    def availability_backlog_size(self) -> int:
        """Cheap count used only by the metadata worker pressure controller."""
        return int(self.db.query(ARKRecord.id).filter(
            ARKRecord.processing_stage == int(ProcessingStage.AVAILABILITY),
            ARKRecord.processing_status.in_([int(ProcessingStatus.READY), int(ProcessingStatus.WAITING)]),
        ).count())

    def get_next_reconciliation_action_at(self):
        """Return the next scheduled availability/durability observation."""
        return (
            self.db.query(ARKRecord.next_action_at)
            .join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
            .filter(
                ARKRecord.processing_stage.in_(
                    [int(ProcessingStage.AVAILABILITY), int(ProcessingStage.REPLICATION)]
                ),
                ARKRecord.next_action_at.isnot(None),
                ARKMetadata.level1_cid.isnot(None),
                ARKMetadata.original_cid.isnot(None),
            )
            .order_by(ARKRecord.next_action_at.asc())
            .limit(1)
            .scalar()
        )

    def get_metadata_pending_persist(
        self,
        limit: int = 10,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ) -> List[ARKRecord]:
        """Get DRAFT/UPDATE ARKs whose metadata still needs persisted CIDs."""
        query = (
            self.db.query(ARKRecord)
            .join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
            .filter(
                ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]),
                ARKRecord.processing_stage == int(ProcessingStage.METADATA),
                or_(ARKMetadata.level1_cid.is_(None), ARKMetadata.original_cid.is_(None)),
            )
        )

        return self._claim_ready_records(query, limit, max_retries, backoff_base)

    def get_chain_pending_publish(
        self,
        limit: int = 10,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ) -> List[ARKRecord]:
        """Get DRAFT/UPDATE ARKs with persisted metadata ready for blockchain."""
        query = (
            self.db.query(ARKRecord)
            .join(ARKMetadata, ARKMetadata.ark_record_id == ARKRecord.id)
            .filter(
                ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]),
                ARKRecord.processing_stage == int(ProcessingStage.CHAIN),
                ARKMetadata.level1_cid.isnot(None),
                ARKMetadata.original_cid.isnot(None),
            )
        )

        return self._claim_ready_records(query, limit, max_retries, backoff_base)

    def _claim_ready_records(
        self,
        query,
        limit: int,
        max_retries: int,
        backoff_base: float,
    ) -> List[ARKRecord]:
        """Return ready records; workers acquire a PostgreSQL advisory lock per ARK."""
        now = _utc_now()
        del max_retries, backoff_base
        query = query.filter(_ready_for_processing_filter(now))

        candidates = query.limit(limit).all()

        ready_records = []
        for record in candidates:
            if not _ready_for_processing(record, now):
                continue

            ready_records.append(record)
            if len(ready_records) >= limit:
                break

        return ready_records

    def update_draft_content(
        self,
        ark: str,
        target: str,
    ) -> ARKRecord:
        """
        Legacy guard for the old DRAFT overwrite path.

        DRAFT payloads are worker-owned once staged, so callers must not mutate
        them in place. Keep the method as an explicit rejection point for any
        older internal code that still reaches for it.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        db_ark = self.get_by_naan_name(naan, name)
        if not db_ark:
            raise ValueError(f"ARK not found: {ark}")
        if db_ark.state != ARKState.DRAFT:
            raise ValueError(f"ARK must be in DRAFT state, currently {db_ark.state}")
        raise ValueError("ARK already has pending creation")

    def update_to_update(
        self,
        ark: str,
        target: str,
    ) -> ARKRecord:
        """
        Transition ARK to UPDATE state with pending metadata changes.

        Allowed source state: PUBLISHED.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        if not target:
            raise ValueError("Target URL is required")

        now = _utc_now()
        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
                ARKRecord.state == ARKState.PUBLISHED.value,
            )
            .values(
                state=ARKState.UPDATE.value,
                target=target,
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            existing = self.get_by_naan_name(naan, name)
            if not existing:
                raise ValueError(f"ARK not found: {ark}")
            raise ValueError(f"ARK must be in PUBLISHED state, currently {existing.state}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def get_drafts_pending_publish(
        self,
        limit: int = 10,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ) -> List[ARKRecord]:
        """
        Get ARKs in DRAFT state pending publication with backoff filtering.

        Args:
            limit: Maximum number of records to return
            max_retries: Maximum retry count (beyond this, filter out)
            backoff_base: Base for exponential backoff calculation

        Returns:
            List of ARKRecord in DRAFT state ready to publish
        """
        return self.get_chain_pending_publish(
            limit=limit,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )

    def update_to_published(
        self,
        ark: str,
        expected_states: Optional[Tuple[str, ...]] = None,
    ) -> ARKRecord:
        """
        Update ARK to PUBLISHED state.

        Args:
            ark: Full ARK identifier

        Returns:
            Updated ARKRecord

        Raises:
            ValueError: If ARK not found

        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        if expected_states is None:
            expected_states = (ARKState.DRAFT.value, ARKState.UPDATE.value)

        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        now = _utc_now()
        conditions = [
            ARKRecord.naan == naan,
            ARKRecord.name == name,
            ARKRecord.state.in_(list(expected_states)),
        ]
        result = self.db.execute(
            update(ARKRecord)
            .where(*conditions)
            .values(
                state=ARKState.PUBLISHED.value,
                processing_stage=int(ProcessingStage.REPLICATION),
                processing_status=int(ProcessingStatus.READY),
                processing_attempt_count=0,
                next_action_at=None,
                processing_wait_reason=int(ProcessingWaitReason.NONE),
                processing_error_code=None,
                processing_error_detail=None,
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            existing = self.get_by_naan_name(naan, name)
            if not existing:
                raise ValueError(f"ARK not found: {ark}")
            expected_str = ", ".join(expected_states)
            raise ValueError(f"ARK must be in one of [{expected_str}], currently {existing.state}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def update_to_tombstone(self, ark: str) -> ARKRecord:
        """
        Update ARK to TOMBSTONE state (soft delete).

        Args:
            ark: Full ARK identifier

        Returns:
            Updated ARKRecord

        Raises:
            ValueError: If ARK not found

        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        now = _utc_now()
        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
                ARKRecord.state != ARKState.TOMBSTONE.value,
            )
            .values(
                state=ARKState.TOMBSTONE.value,
                tombstoned_at=now,
                processing_status=int(ProcessingStatus.CANCELLED),
                next_action_at=None,
                processing_wait_reason=int(ProcessingWaitReason.NONE),
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            existing = self.get_by_naan_name(naan, name)
            if not existing:
                raise ValueError(f"ARK not found: {ark}")
            return existing

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def mark_processing_failed(
        self,
        ark: str,
        error_message: str,
        error_code: int,
        is_permanent: bool = False,
    ) -> ARKRecord:
        """
        Record failure for the current internal workflow stage.

        Args:
            ark: Full ARK identifier
            error_message: Error message to store
            is_permanent: If True, mark as permanently failed

        Returns:
            Updated ARKRecord

        Raises:
            ValueError: If ARK not found

        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        now = _utc_now()
        values = {
            "processing_attempt_count": ARKRecord.processing_attempt_count + 1,
            "processing_error_code": int(error_code),
            "processing_error_detail": error_message[:1000],
            "updated_at": now,
        }
        policy = self.db.query(ProcessingErrorCodeModel).filter_by(id=int(error_code)).first()
        # The persisted error catalogue, not individual call sites, defines
        is_permanent = not bool(policy.retryable) if policy is not None else is_permanent
        if is_permanent:
            values["processing_status"] = int(ProcessingStatus.FAILED)
            values["next_action_at"] = None
            values["processing_wait_reason"] = int(ProcessingWaitReason.NONE)
        else:
            values["processing_status"] = int(ProcessingStatus.WAITING)
            values["next_action_at"] = now + timedelta(minutes=1)
            values["processing_wait_reason"] = int(ProcessingWaitReason.STORAGE_BACKOFF)

        conditions = [ARKRecord.naan == naan, ARKRecord.name == name]
        result = self.db.execute(update(ARKRecord).where(*conditions).values(**values))

        if result.rowcount == 0:
            raise ValueError(f"ARK not found: {ark}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def defer_processing_retry(
        self,
        ark: str,
        error_message: str,
        error_code: int,
        delay_seconds: int = 60,
    ) -> ARKRecord:
        """
        Defer a processing attempt without consuming its retry budget.

        Used for infrastructure failures where the worker cannot safely decide
        publication state, such as RPC loss during reconcile.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        now = _utc_now()
        conditions = [ARKRecord.naan == naan, ARKRecord.name == name]
        result = self.db.execute(
            update(ARKRecord)
            .where(*conditions)
            .values(
                processing_status=int(ProcessingStatus.WAITING),
                next_action_at=now + timedelta(seconds=max(int(delay_seconds), 0)),
                processing_wait_reason=int(ProcessingWaitReason.STORAGE_BACKOFF),
                processing_error_code=int(error_code),
                processing_error_detail=error_message[:1000],
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            raise ValueError(f"ARK not found: {ark}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def reset_processing(self, ark: str) -> ARKRecord:
        """
        Reset the current internal stage after manual remediation.

        Args:
            ark: Full ARK identifier

        Returns:
            Updated ARKRecord

        Raises:
            ValueError: If ARK not found

        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        db_ark = self.get_by_ark(ark)
        if not db_ark:
            raise ValueError(f"ARK not found: {ark}")

        db_ark.processing_status = int(ProcessingStatus.READY)
        db_ark.processing_attempt_count = 0
        db_ark.next_action_at = None
        db_ark.processing_wait_reason = int(ProcessingWaitReason.NONE)
        db_ark.processing_error_code = None
        db_ark.processing_error_detail = None
        db_ark.updated_at = _utc_now()

        return db_ark
