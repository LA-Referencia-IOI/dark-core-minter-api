"""
ARK repository for database operations.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.database.models import ARKRecord
from app.models.states import ARKState


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        ark: str,
        naan: str,
        name: str,
        authority_id: str,
        alternate_identifiers: Optional[list] = None,
        client_item_id: Optional[str] = None,
    ) -> ARKRecord:
        """
        Create a new ARK record in RESERVED state.
        
        Args:
            ark: Full ARK identifier
            naan: Name Assigning Authority Number
            name: ARK name/suffix
            authority_id: Authority UUID that owns this ARK
            alternate_identifiers: Optional list of alternate identifiers
            client_item_id: Optional client tracking ID
        
        Returns:
            Created ARKRecord
        
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
            ark=ark,
            naan=naan,
            name=name,
            state=ARKState.RESERVED.value,
            authority_id=authority_id,
            alternate_identifiers=alternate_identifiers,
            client_item_id=client_item_id,
        )
        self.db.add(db_ark)
        return db_ark
    
    def get_by_ark(self, ark: str) -> Optional[ARKRecord]:
        """
        Get ARK record by full ARK identifier.
        
        Args:
            ark: Full ARK identifier
        
        Returns:
            ARKRecord if found, None otherwise
        """
        return self.db.query(ARKRecord).filter(ARKRecord.ark == ark).first()
    
    def exists(self, ark: str) -> bool:
        """
        Check if ARK exists in database.
        
        Args:
            ark: Full ARK identifier
        
        Returns:
            True if exists, False otherwise
        """
        return self.db.query(ARKRecord).filter(ARKRecord.ark == ark).count() > 0
    
    def update_to_draft(
        self,
        ark: str,
        target: str,
        metadata: dict,
        alternate_identifiers: Optional[list] = None,
    ) -> ARKRecord:
        """
        Update ARK to DRAFT state with metadata.
        
        Args:
            ark: Full ARK identifier
            target: Target URL
            metadata: Metadata payload
            alternate_identifiers: Optional alternate identifiers
        
        Returns:
            Updated ARKRecord
        
        Raises:
            ValueError: If ARK not in RESERVED state or missing required fields
        
        Note:
            Does NOT commit. Caller must commit the transaction.
        """
        db_ark = self.get_by_ark(ark)
        if not db_ark:
            raise ValueError(f"ARK not found: {ark}")
        
        if db_ark.state != ARKState.RESERVED:
            raise ValueError(f"ARK must be in RESERVED state, currently {db_ark.state}")
        
        # Validate required fields
        if not target:
            raise ValueError("Target URL is required")
        if not metadata:
            raise ValueError("Metadata is required")
        
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

        # Update record
        db_ark.state = ARKState.DRAFT.value
        db_ark.target = target
        db_ark.metadata_json = metadata
        db_ark.alternate_identifiers = alternate_identifiers
        db_ark.updated_at = _utc_now()
        
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
            ARKRecord.state == ARKState.DRAFT.value,
            ARKRecord.publish_permanently_failed == 0,  # Not marked as failed
        )
        
        # Apply backoff filtering: only include if enough time has passed since last attempt
        now = _utc_now()
        
        # Get all drafts and filter in Python (easier than complex SQL for backoff)
        all_drafts = query.order_by(ARKRecord.created_at.asc()).all()
        
        ready_drafts = []
        for draft in all_drafts:
            # First attempt or no previous attempt
            if draft.publish_last_attempt_at is None:
                ready_drafts.append(draft)
                if len(ready_drafts) >= limit:
                    break
                continue
            
            # Calculate backoff delay
            retry_count = draft.publish_retry_count
            backoff_seconds = (backoff_base ** retry_count) * 60
            next_attempt_time = draft.publish_last_attempt_at + timedelta(seconds=backoff_seconds)
            
            # Ready if enough time has passed
            if now >= next_attempt_time:
                ready_drafts.append(draft)
                if len(ready_drafts) >= limit:
                    break
        
        return ready_drafts
    
    def update_to_published(
        self,
        ark: str,
        metadata_cid: str,
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
        db_ark = self.get_by_ark(ark)
        if not db_ark:
            raise ValueError(f"ARK not found: {ark}")
        
        db_ark.state = ARKState.PUBLISHED.value
        db_ark.metadata_cid = metadata_cid
        db_ark.updated_at = _utc_now()
        
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
        db_ark = self.get_by_ark(ark)
        if not db_ark:
            raise ValueError(f"ARK not found: {ark}")
        
        db_ark.state = ARKState.TOMBSTONE.value
        db_ark.tombstoned_at = _utc_now()
        db_ark.updated_at = _utc_now()
        
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
        db_ark = self.get_by_ark(ark)
        if not db_ark:
            raise ValueError(f"ARK not found: {ark}")
        
        db_ark.publish_retry_count += 1
        db_ark.publish_last_error = error_message[:1000]  # Truncate long errors
        db_ark.publish_last_attempt_at = _utc_now()
        
        if is_permanent:
            db_ark.publish_permanently_failed = 1
        
        db_ark.updated_at = _utc_now()
        
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
