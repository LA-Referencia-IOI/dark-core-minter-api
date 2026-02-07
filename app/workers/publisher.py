"""
Async worker for publishing DRAFT ARKs to blockchain.

This worker processes ARKs in DRAFT state, stores their metadata,
and publishes them to the blockchain via the orchestrator.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session
from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.exceptions import ARKError, AuthorityError

from app.database.connection import SessionLocal
from app.repositories.ark_repository import ARKRepository
from app.models.states import ARKState
from app.storage.base import MetadataStorage
from app.storage.exceptions import StorageError


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ARKPublisher:
    """
    Background worker for publishing DRAFT ARKs to blockchain.
    
    Processes ARKs in FIFO order (oldest first) with independent
    transactions per ARK to ensure batch failures don't affect
    successful publications.
    """
    
    def __init__(
        self,
        orchestrator: DARKOrchestrator,
        metadata_storage: MetadataStorage,
        batch_size: int = 10,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        """
        Initialize ARK publisher worker.
        
        Args:
            orchestrator: DarkOrchestrator instance for blockchain ops
            metadata_storage: Metadata storage backend
            batch_size: Number of ARKs to process per cycle
            max_retries: Maximum retry attempts before permanent failure
            backoff_base: Base for exponential backoff calculation
        """
        self.orchestrator = orchestrator
        self.metadata_storage = metadata_storage
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        
        # Statistics
        self.stats = {
            "total_processed": 0,
            "total_succeeded": 0,
            "total_failed": 0,
            "total_permanent_failures": 0,
            "last_run_at": None,
            "last_run_duration": None,
            "recent_errors": [],  # Keep last 10 errors
        }
    
    def publish_single_ark(self, ark_id: str) -> bool:
        """
        Publish a single ARK with independent transaction.
        
        This method creates its own DB session to ensure that
        one ARK failure doesn't rollback other successful publications.
        
        Args:
            ark_id: ARK identifier to publish
            
        Returns:
            True if successful, False otherwise
        """
        # Create independent session for this ARK
        session_factory = SessionLocal()
        db: Session = session_factory()
        
        try:
            repo = ARKRepository(db)
            ark_record = repo.get_by_ark(ark_id)
            
            if not ark_record:
                logger.warning(f"ARK not found during publish: {ark_id}")
                return False
            
            if ark_record.state not in (ARKState.DRAFT, ARKState.UPDATE):
                logger.warning(f"ARK {ark_id} not in publishable state: {ark_record.state}")
                return False
            
            # Metadata is already stored; CID should be in DB
            cid = ark_record.metadata_cid
            if not cid:
                error_msg = "No metadata CID found - metadata must be stored first"
                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=True)
                db.commit()
                self.stats["total_permanent_failures"] += 1
                return False
            
            logger.info(f"Using existing metadata CID for {ark_id}: {cid}")
            
            # Step 2: Publish to blockchain
            try:
                # Extract components using helper
                from app.repositories.ark_repository import parse_ark
                naan, name = parse_ark(ark_id)
                
                if ark_record.state == ARKState.DRAFT:
                    self.orchestrator.create_ark(
                        uuid=ark_record.authority_id,
                        naan=naan,
                        name=name,
                        url=ark_record.target,
                        cid=cid,
                    )
                    operation = "create"
                else:
                    self.orchestrator.update_ark(
                        uuid=ark_record.authority_id,
                        naan=naan,
                        name=name,
                        url=ark_record.target,
                        cid=cid,
                    )
                    operation = "update"
                
                logger.info(f"ARK {operation} published to blockchain: {ark_id}")
                
            except AuthorityError as e:
                # Authority errors are permanent (unauthorized, etc.)
                error_msg = f"Authority error (permanent): {e}"
                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=True)
                db.commit()
                self.stats["total_permanent_failures"] += 1
                self._add_recent_error(ark_id, error_msg)
                return False

            except ARKError as e:
                # Contract-level ARK errors are usually semantic (already exists/not found).
                error_msg = f"ARK error (permanent): {e}"
                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=True)
                db.commit()
                self.stats["total_permanent_failures"] += 1
                self._add_recent_error(ark_id, error_msg)
                return False
                
            except Exception as e:
                # Other errors are retriable (network, gas, etc.)
                error_msg = f"Blockchain publish failed: {e}"
                logger.error(f"{error_msg} for ARK {ark_id}")
                
                # Check if max retries exceeded
                is_permanent = ark_record.publish_retry_count >= self.max_retries - 1
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=is_permanent)
                db.commit()
                
                if is_permanent:
                    self.stats["total_permanent_failures"] += 1
                    logger.error(f"ARK {ark_id} marked as permanently failed after {self.max_retries} attempts")
                
                self._add_recent_error(ark_id, error_msg)
                return False
            
            # Step 3: Update DB to PUBLISHED
            try:
                repo.update_to_published(ark_id, cid)
                db.commit()
            except ValueError as e:
                db.rollback()
                latest = repo.get_by_ark(ark_id)
                if latest and latest.state == ARKState.PUBLISHED:
                    logger.info(f"ARK already published by concurrent worker: {ark_id}")
                    self.stats["total_succeeded"] += 1
                    return True

                logger.warning(f"CAS conflict finalizing publish for {ark_id}: {e}")
                self._add_recent_error(ark_id, f"CAS conflict: {e}")
                return False
            
            logger.info(f"✓ Successfully published ARK: {ark_id}")
            self.stats["total_succeeded"] += 1
            return True
            
        except Exception as e:
            logger.error(f"Unexpected error publishing {ark_id}: {e}")
            db.rollback()
            self._add_recent_error(ark_id, f"Unexpected error: {e}")
            return False
            
        finally:
            db.close()
    
    def run_publish_cycle(self) -> None:
        """
        Run a single publish cycle.
        
        Fetches a batch of DRAFT ARKs and processes each with
        independent transactions. Designed to be called by scheduler.
        """
        start_time = _utc_now()
        
        try:
            # Create session for batch query
            session_factory = SessionLocal()
            db: Session = session_factory()
            
            try:
                repo = ARKRepository(db)
                
                # Get batch of ARKs ready for publish
                drafts = repo.get_drafts_pending_publish(
                    limit=self.batch_size,
                    max_retries=self.max_retries,
                    backoff_base=self.backoff_base,
                )

                # Persist claim updates (publish_last_attempt_at) and release row locks.
                db.commit()
                
                if not drafts:
                    logger.debug("No DRAFT ARKs pending publish")
                    return
                
                logger.info(f"Processing {len(drafts)} DRAFT ARKs")
                
                # Extract ARK IDs for processing
                ark_ids = [draft.ark for draft in drafts]
                
            finally:
                db.close()
            
            # Process each ARK with independent transaction
            for ark_id in ark_ids:
                self.stats["total_processed"] += 1
                success = self.publish_single_ark(ark_id)
                
                if not success:
                    self.stats["total_failed"] += 1
            
        except Exception as e:
            logger.error(f"Error in publish cycle: {e}")
            self._add_recent_error("BATCH", f"Cycle error: {e}")
            
        finally:
            # Update stats
            duration = (_utc_now() - start_time).total_seconds()
            self.stats["last_run_at"] = start_time
            self.stats["last_run_duration"] = duration
            
            logger.info(
                f"Publish cycle complete. Duration: {duration:.2f}s, "
                f"Processed: {self.stats['total_processed']}, "
                f"Success: {self.stats['total_succeeded']}, "
                f"Failed: {self.stats['total_failed']}"
            )
    
    def _add_recent_error(self, ark_id: str, error_msg: str) -> None:
        """Add error to recent errors list (keep last 10)."""
        self.stats["recent_errors"].insert(0, {
            "ark": ark_id,
            "error": error_msg,
            "timestamp": _utc_now().isoformat(),
        })
        # Keep only last 10
        self.stats["recent_errors"] = self.stats["recent_errors"][:10]
    
    def get_stats(self) -> dict:
        """Get worker statistics."""
        return {
            **self.stats,
            "last_run_at": self.stats["last_run_at"].isoformat() if self.stats["last_run_at"] else None,
        }
