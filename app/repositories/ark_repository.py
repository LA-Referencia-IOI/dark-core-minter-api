"""
ARK repository for database operations.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from app.database.models import ARKRecord, ARKMetadata
from app.models.states import ARKState


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


def _is_ready_for_publish(draft: ARKRecord, now: datetime, backoff_base: float, max_retries: int) -> bool:
    """Return True when draft can be claimed for publish attempt."""
    if draft.publish_permanently_failed == 1:
        return False
    if draft.publish_retry_count >= max_retries:
        return False
    if draft.publish_last_attempt_at is None:
        return True

    backoff_seconds = (backoff_base ** draft.publish_retry_count) * 60
    next_attempt_time = draft.publish_last_attempt_at + timedelta(seconds=backoff_seconds)
    return now >= next_attempt_time


def _ready_for_publish_filter(now: datetime, backoff_base: float, max_retries: int):
    """Return a SQL filter matching the publish backoff readiness rule."""
    retry_clauses = []
    for retry_count in range(max(int(max_retries), 0)):
        backoff_seconds = (backoff_base ** retry_count) * 60
        retry_clauses.append(
            and_(
                ARKRecord.publish_retry_count == retry_count,
                or_(
                    ARKRecord.publish_last_attempt_at.is_(None),
                    ARKRecord.publish_last_attempt_at
                    <= now - timedelta(seconds=backoff_seconds),
                ),
            )
        )

    if not retry_clauses:
        return ARKRecord.id.is_(None)

    return and_(
        ARKRecord.publish_permanently_failed == 0,
        or_(*retry_clauses),
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
        return self.db.query(ARKRecord).filter(
            ARKRecord.naan == naan,
            ARKRecord.name == name
        ).first()
    
    def get_by_naan_name(self, naan: str, name: str) -> Optional[ARKRecord]:
        """
        Get ARK record by naan and name components.
        
        Args:
            naan: Name Assigning Authority Number
            name: ARK name/suffix
        
        Returns:
            ARKRecord if found, None otherwise
        """
        return self.db.query(ARKRecord).filter(
            ARKRecord.naan == naan,
            ARKRecord.name == name
        ).first()
    
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
        return self.db.query(ARKRecord).filter(
            ARKRecord.naan == naan,
            ARKRecord.name == name
        ).count() > 0
    
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
            ark_record.publish_retry_count = 0
            ark_record.publish_last_error = None
            ark_record.publish_last_attempt_at = None
            ark_record.publish_permanently_failed = 0
            ark_record.updated_at = _utc_now()
        
        return metadata

    def get_metadata_by_ark_id(self, ark_record_id: int) -> Optional[ARKMetadata]:
        return self.db.query(ARKMetadata).filter_by(ark_record_id=ark_record_id).first()

    def update_metadata_cids(
        self,
        ark_record_id: int,
        level1_cid: Optional[str] = None,
        level2_cid: Optional[str] = None,
        purge_local: bool = False,
        reset_publish_tracking: bool = False,
    ) -> None:
        """Update CIDs after worker processing."""
        metadata = self.get_metadata_by_ark_id(ark_record_id)
        if not metadata:
            raise ValueError(f"Metadata not found for ARK ID {ark_record_id}")
        
        if level1_cid is not None:
            metadata.level1_cid = level1_cid
        if level2_cid is not None:
            metadata.original_cid = level2_cid
        if purge_local:
            metadata.level1_json = None
            metadata.original_content = None
        metadata.updated_at = _utc_now()

        if reset_publish_tracking:
            ark_record = self.db.query(ARKRecord).filter_by(id=ark_record_id).first()
            if ark_record:
                ark_record.publish_retry_count = 0
                ark_record.publish_last_error = None
                ark_record.publish_last_attempt_at = None
                ark_record.publish_permanently_failed = 0
                ark_record.updated_at = _utc_now()

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
                ARKRecord.publish_permanently_failed == 0,
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
                ARKRecord.publish_permanently_failed == 0,
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
        """Apply backoff and claim ready records for a worker cycle."""
        now = _utc_now()
        query = query.filter(_ready_for_publish_filter(now, backoff_base, max_retries))

        dialect_name = self.db.get_bind().dialect.name
        if dialect_name == "postgresql":
            candidates = (
                query.order_by(ARKRecord.authority_id.asc(), ARKRecord.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
                .all()
            )
        else:
            candidates = query.order_by(ARKRecord.authority_id.asc(), ARKRecord.created_at.asc()).all()

        ready_records = []
        for record in candidates:
            if not _is_ready_for_publish(record, now, backoff_base, max_retries):
                continue

            record.publish_last_attempt_at = now
            record.updated_at = now
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
            raise ValueError(
                f"ARK must be in PUBLISHED state, currently {existing.state}"
            )

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
        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
                ARKRecord.state.in_(list(expected_states)),
            )
            .values(
                state=ARKState.PUBLISHED.value,
                publish_retry_count=0,
                publish_last_error=None,
                publish_last_attempt_at=None,
                publish_permanently_failed=0,
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
    
    def mark_publish_failed(
        self,
        ark: str,
        error_message: str,
        is_permanent: bool = False,
    ) -> ARKRecord:
        """
        Mark ARK publish attempt as failed.
        
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
            "publish_retry_count": ARKRecord.publish_retry_count + 1,
            "publish_last_error": error_message[:1000],  # Truncate long errors
            "publish_last_attempt_at": now,
            "updated_at": now,
        }
        if is_permanent:
            values["publish_permanently_failed"] = 1

        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
            )
            .values(**values)
        )

        if result.rowcount == 0:
            raise ValueError(f"ARK not found: {ark}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def defer_publish_retry(
        self,
        ark: str,
        error_message: str,
    ) -> ARKRecord:
        """
        Release a claimed publish attempt without consuming an ARK retry.

        Used for infrastructure failures where the worker cannot safely decide
        publication state, such as RPC loss during reconcile.
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
            )
            .values(
                publish_last_error=error_message[:1000],
                publish_last_attempt_at=None,
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            raise ValueError(f"ARK not found: {ark}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark
    
    def reset_publish_tracking(self, ark: str) -> ARKRecord:
        """
        Reset publish tracking fields (e.g., after manual fix).
        
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
        
        db_ark.publish_retry_count = 0
        db_ark.publish_last_error = None
        db_ark.publish_last_attempt_at = None
        db_ark.publish_permanently_failed = 0
        db_ark.updated_at = _utc_now()
        
        return db_ark
