"""
ARK repository for database operations.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database.models import ARKRecord
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
        alternate_identifiers: Optional[list] = None,
        client_item_id: Optional[str] = None,
    ) -> ARKRecord:
        """
        Create a new ARK record in RESERVED state.
        
        Args:
            naan: Name Assigning Authority Number
            name: ARK name/suffix
            authority_id: Authority UUID that owns this ARK
            alternate_identifiers: Optional list of alternate identifiers
            client_item_id: Optional client tracking ID
        
        Returns:
            Created ARKRecord (ark property computes full identifier)
        
        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        # Serialize alternate identifiers if they are Pydantic models
        if alternate_identifiers:
            serialized_ids = []
            for item in alternate_identifiers:
                if hasattr(item, "model_dump"):
                    serialized_ids.append(item.model_dump())
                elif hasattr(item, "dict"):
                    serialized_ids.append(item.dict())
                else:
                    serialized_ids.append(item)
            alternate_identifiers = serialized_ids

        db_ark = ARKRecord(
            naan=naan,
            name=name,
            state=ARKState.RESERVED.value,
            authority_id=authority_id,
            alternate_identifiers=alternate_identifiers,
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
        metadata_cid: Optional[str] = None,
        metadata_format: Optional[str] = None,
        alternate_identifiers: Optional[list] = None,
    ) -> ARKRecord:
        """
        Create a local record for an ARK that already exists on-chain.

        Initial local state is PUBLISHED.
        """
        if alternate_identifiers:
            serialized_ids = []
            for item in alternate_identifiers:
                if hasattr(item, "model_dump"):
                    serialized_ids.append(item.model_dump())
                elif hasattr(item, "dict"):
                    serialized_ids.append(item.dict())
                else:
                    serialized_ids.append(item)
            alternate_identifiers = serialized_ids

        db_ark = ARKRecord(
            naan=naan,
            name=name,
            state=ARKState.PUBLISHED.value,
            authority_id=authority_id,
            target=target,
            metadata_cid=metadata_cid,
            metadata_format=metadata_format,
            alternate_identifiers=alternate_identifiers,
        )
        self.db.add(db_ark)
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
        metadata_cid: str,
        metadata_format: str,
        alternate_identifiers: Optional[list] = None,
    ) -> ARKRecord:
        """
        Update ARK to DRAFT state with stored metadata CID.
        
        Args:
            ark: Full ARK identifier
            target: Target URL
            metadata_cid: CID of stored metadata content
            metadata_format: Format of metadata ("json", "xml", etc.)
            alternate_identifiers: Optional alternate identifiers
        
        Returns:
            Updated ARKRecord
        
        Raises:
            ValueError: If ARK not in RESERVED state or missing required fields
        
        Note:
            Does NOT commit. Caller must commit the transaction.
            Metadata content is stored externally via MetadataStorage before calling this.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        # Validate required fields
        if not target:
            raise ValueError("Target URL is required")
        if not metadata_cid:
            raise ValueError("Metadata CID is required")
        if not metadata_format:
            raise ValueError("Metadata format is required")
        
        # Serialize alternate identifiers if they are Pydantic models
        if alternate_identifiers:
            serialized_ids = []
            for item in alternate_identifiers:
                if hasattr(item, "model_dump"):
                    serialized_ids.append(item.model_dump())
                elif hasattr(item, "dict"):
                    serialized_ids.append(item.dict())
                else:
                    serialized_ids.append(item)
            alternate_identifiers = serialized_ids

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
                metadata_cid=metadata_cid,
                metadata_format=metadata_format,
                alternate_identifiers=alternate_identifiers,
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

    def update_draft_content(
        self,
        ark: str,
        target: str,
        metadata_cid: str,
        metadata_format: str,
        alternate_identifiers: Optional[list] = None,
    ) -> ARKRecord:
        """
        Update an ARK already in DRAFT without changing state.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        if not target:
            raise ValueError("Target URL is required")
        if not metadata_cid:
            raise ValueError("Metadata CID is required")
        if not metadata_format:
            raise ValueError("Metadata format is required")

        if alternate_identifiers:
            serialized_ids = []
            for item in alternate_identifiers:
                if hasattr(item, "model_dump"):
                    serialized_ids.append(item.model_dump())
                elif hasattr(item, "dict"):
                    serialized_ids.append(item.dict())
                else:
                    serialized_ids.append(item)
            alternate_identifiers = serialized_ids

        now = _utc_now()
        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
                ARKRecord.state == ARKState.DRAFT.value,
            )
            .values(
                target=target,
                metadata_cid=metadata_cid,
                metadata_format=metadata_format,
                alternate_identifiers=alternate_identifiers,
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            existing = self.get_by_naan_name(naan, name)
            if not existing:
                raise ValueError(f"ARK not found: {ark}")
            raise ValueError(f"ARK must be in DRAFT state, currently {existing.state}")

        db_ark = self.get_by_naan_name(naan, name)
        if db_ark is None:
            raise ValueError(f"ARK not found: {ark}")
        return db_ark

    def update_to_update(
        self,
        ark: str,
        target: str,
        metadata_cid: str,
        metadata_format: str,
        alternate_identifiers: Optional[list] = None,
    ) -> ARKRecord:
        """
        Transition ARK to UPDATE state with pending metadata changes.

        Allowed source states: PUBLISHED and UPDATE.
        """
        try:
            naan, name = parse_ark(ark)
        except ValueError as exc:
            raise ValueError(f"ARK not found: {ark}") from exc

        if not target:
            raise ValueError("Target URL is required")
        if not metadata_cid:
            raise ValueError("Metadata CID is required")
        if not metadata_format:
            raise ValueError("Metadata format is required")

        if alternate_identifiers:
            serialized_ids = []
            for item in alternate_identifiers:
                if hasattr(item, "model_dump"):
                    serialized_ids.append(item.model_dump())
                elif hasattr(item, "dict"):
                    serialized_ids.append(item.dict())
                else:
                    serialized_ids.append(item)
            alternate_identifiers = serialized_ids

        now = _utc_now()
        result = self.db.execute(
            update(ARKRecord)
            .where(
                ARKRecord.naan == naan,
                ARKRecord.name == name,
                ARKRecord.state.in_([ARKState.PUBLISHED.value, ARKState.UPDATE.value]),
            )
            .values(
                state=ARKState.UPDATE.value,
                target=target,
                metadata_cid=metadata_cid,
                metadata_format=metadata_format,
                alternate_identifiers=alternate_identifiers,
                updated_at=now,
            )
        )

        if result.rowcount == 0:
            existing = self.get_by_naan_name(naan, name)
            if not existing:
                raise ValueError(f"ARK not found: {ark}")
            raise ValueError(
                f"ARK must be in PUBLISHED or UPDATE state, currently {existing.state}"
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
        query = self.db.query(ARKRecord).filter(
            ARKRecord.state.in_([ARKState.DRAFT.value, ARKState.UPDATE.value]),
            ARKRecord.publish_permanently_failed == 0,  # Not marked as failed
        )

        # Apply backoff filtering and claim rows by updating publish_last_attempt_at.
        now = _utc_now()

        dialect_name = self.db.get_bind().dialect.name
        if dialect_name == "postgresql":
            # Lock a window of rows and skip rows already locked by other workers.
            # We over-fetch to keep enough candidates after Python backoff filtering.
            fetch_limit = max(limit * 5, limit)
            candidates = (
                query.order_by(ARKRecord.created_at.asc())
                .limit(fetch_limit)
                .with_for_update(skip_locked=True)
                .all()
            )
        else:
            # Best effort fallback for non-PostgreSQL dialects.
            candidates = query.order_by(ARKRecord.created_at.asc()).all()

        ready_drafts = []
        for draft in candidates:
            if not _is_ready_for_publish(draft, now, backoff_base, max_retries):
                continue

            # Claim for this cycle so concurrent workers skip the same draft.
            draft.publish_last_attempt_at = now
            draft.updated_at = now
            ready_drafts.append(draft)
            if len(ready_drafts) >= limit:
                break

        return ready_drafts
    
    def update_to_published(
        self,
        ark: str,
        metadata_cid: str,
        expected_states: Optional[Tuple[str, ...]] = None,
    ) -> ARKRecord:
        """
        Update ARK to PUBLISHED state.
        
        Args:
            ark: Full ARK identifier
            metadata_cid: IPFS CID of metadata
        
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
                metadata_cid=metadata_cid,
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
