"""
Async worker for publishing DRAFT ARKs to blockchain.

This worker processes ARKs in DRAFT state, stores their metadata,
and publishes them to the blockchain via dark-core-lib.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session
from dark_core_lib import DARKCoreClient
from dark_core_lib.exceptions import ARKError, AuthorityError

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
        corelib_client: DARKCoreClient,
        metadata_storage: MetadataStorage,
        batch_size: int = 10,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        """
        Initialize ARK publisher worker.
        
        Args:
            corelib_client: DARKCoreClient instance for blockchain ops
            metadata_storage: Metadata storage backend
            batch_size: Number of ARKs to process per cycle
            max_retries: Maximum retry attempts before permanent failure
            backoff_base: Base for exponential backoff calculation
        """
        self.corelib_client = corelib_client
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
            
            # Step 1: Handle Two-Level Metadata Storage
            # We need to ensure both L1 and L2 are stored in IPFS and CIDs are linked.
            
            # Fetch metadata record
            metadata_record = repo.get_metadata_by_ark_id(ark_record.id)
            if not metadata_record:
                error_msg = "No metadata record found in DB"
                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=True)
                db.commit()
                self.stats["total_permanent_failures"] += 1
                return False
            
            # Use existing CIDs if available (retry logic) or store new ones
            l1_cid = metadata_record.level1_cid
            l2_cid = metadata_record.original_cid
            
            try:
                # Store Level 2 (Original) if missing
                if not l2_cid:
                    l2_cid = self.metadata_storage.store_metadata(
                        content=metadata_record.original_content,
                        format=metadata_record.original_schema
                    )
                    logger.info(f"Stored Level 2 metadata for {ark_id}: {l2_cid}")

                # Store Level 1 (JSON) if missing
                if not l1_cid:
                    # Inject L2 CID into L1 JSON
                    l1_json = metadata_record.level1_json.copy()
                    l1_json["original_metadata"]["cid"] = l2_cid
                    
                    # Store L1
                    import json
                    l1_content = json.dumps(l1_json)
                    l1_cid = self.metadata_storage.store_metadata(
                        content=l1_content,
                        format="json"
                    )
                    logger.info(f"Stored Level 1 metadata for {ark_id}: {l1_cid}")
                
                # Update DB with CIDs
                repo.update_metadata_cids(ark_record.id, l1_cid, l2_cid)
                
            except StorageError as e:
                # Storage errors are retriable
                error_msg = f"Metadata storage failed: {e}"
                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=False)
                db.commit()
                self._add_recent_error(ark_id, error_msg)
                return False

            cid = l1_cid
            logger.info(f"Ready to publish {ark_id} with L1 CID: {cid}")
            
            # Step 2: Publish to blockchain
            try:
                # Extract components using helper
                from app.repositories.ark_repository import parse_ark
                naan, name = parse_ark(ark_id)
                
                if ark_record.state == ARKState.DRAFT:
                    self.corelib_client.create_ark(
                        uuid=ark_record.authority_id,
                        naan=naan,
                        name=name,
                        url=ark_record.target,
                        cid=cid,
                    )
                    operation = "create"
                else:
                    self.corelib_client.update_ark(
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
                repo.update_to_published(ark_id)
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
