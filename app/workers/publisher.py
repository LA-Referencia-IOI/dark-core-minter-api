"""
Workers for the asynchronous ARK publication pipeline.

MetadataPersistenceWorker persists local metadata payloads to the configured
metadata backend. ChainPublisherWorker publishes ARKs with persisted metadata
to the blockchain.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from dark_core_lib import ARKPublishOperation, ARKPublishResult, DARKCoreClient
from dark_core_lib.exceptions import (
    ARKError,
    ARKNotFoundError,
    AuthorityError,
    AuthorityNotFoundError,
    AuthorizationError,
    ConnectionError as CoreConnectionError,
    TransactionError,
)
from dark_core_lib.metadata import MetadataService, MetadataStorage, StorageError

from app.database.connection import SessionLocal
from app.models.states import ARKState
from app.repositories.ark_repository import ARKRepository, parse_ark


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _WorkerStatsMixin:
    """Shared in-process counters exposed through DB heartbeat."""

    def _init_stats(self) -> None:
        self.stats = {
            "total_processed": 0,
            "total_succeeded": 0,
            "total_failed": 0,
            "total_permanent_failures": 0,
            "last_run_at": None,
            "last_run_duration": None,
            "last_run_processed": None,
            "last_run_succeeded": None,
            "last_run_failed": None,
            "recent_errors": [],
        }

    def _add_recent_error(self, ark_id: str, error_msg: str) -> None:
        """Add error to recent errors list (keep last 10)."""
        self.stats["recent_errors"].insert(0, {
            "ark": ark_id,
            "error": error_msg,
            "timestamp": _utc_now().isoformat(),
        })
        self.stats["recent_errors"] = self.stats["recent_errors"][:10]

    def get_stats(self) -> dict:
        """Get worker statistics."""
        return {
            **self.stats,
            "last_run_at": self.stats["last_run_at"].isoformat() if self.stats["last_run_at"] else None,
        }

    def _snapshot_cycle_totals(self) -> dict:
        """Return cumulative counters before a cycle starts."""
        return {
            "processed": int(self.stats["total_processed"] or 0),
            "succeeded": int(self.stats["total_succeeded"] or 0),
            "failed": int(self.stats["total_failed"] or 0),
        }

    def _record_cycle_metrics(self, start_time: datetime, before: dict) -> float:
        """Record per-cycle duration and counter deltas."""
        duration = (_utc_now() - start_time).total_seconds()
        self.stats["last_run_at"] = start_time
        self.stats["last_run_duration"] = duration
        self.stats["last_run_processed"] = int(self.stats["total_processed"] or 0) - before["processed"]
        self.stats["last_run_succeeded"] = int(self.stats["total_succeeded"] or 0) - before["succeeded"]
        self.stats["last_run_failed"] = int(self.stats["total_failed"] or 0) - before["failed"]
        return duration


class MetadataPersistenceWorker(_WorkerStatsMixin):
    """Worker that persists local ARK metadata to IPFS/store API."""

    def __init__(
        self,
        metadata_storage: MetadataStorage,
        page_size: int = 100,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.metadata_storage = metadata_storage
        self.metadata_service = MetadataService(metadata_storage)
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._init_stats()

    def persist_single_ark(self, ark_id: str) -> bool:
        """Persist one ARK metadata payload and purge local JSON/content."""
        session_factory = SessionLocal()
        db: Session = session_factory()

        try:
            repo = ARKRepository(db)
            ark_record = repo.get_by_ark(ark_id)

            if not ark_record:
                logger.warning(f"ARK not found during metadata persistence: {ark_id}")
                return False

            if ark_record.state not in (ARKState.DRAFT, ARKState.UPDATE):
                logger.warning(f"ARK {ark_id} not in metadata-persistable state: {ark_record.state}")
                return False

            metadata_record = repo.get_metadata_by_ark_id(ark_record.id)
            if not metadata_record:
                error_msg = "No metadata record found in DB"
                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=True)
                db.commit()
                self.stats["total_permanent_failures"] += 1
                self._add_recent_error(ark_id, error_msg)
                return False

            l1_cid = metadata_record.level1_cid
            l2_cid = metadata_record.original_cid
            had_l2_cid = bool(l2_cid)

            try:
                if not l2_cid:
                    original_media_type = getattr(metadata_record, "original_media_type", None)
                    if not original_media_type:
                        raise StorageError("Original metadata media type is required")
                    if metadata_record.original_content is None:
                        raise StorageError("Original metadata content is required before persistence")

                    l2_cid = self.metadata_service.store_level2(
                        content=metadata_record.original_content.encode("utf-8"),
                        content_type=original_media_type,
                        schema=metadata_record.original_schema,
                    )
                    logger.info(f"Stored Level 2 metadata for {ark_id}: {l2_cid}")

                if not l1_cid:
                    if metadata_record.level1_json is None:
                        raise StorageError("Level 1 metadata JSON is required before persistence")

                    _, l1_cid = self.metadata_service.store_level1_with_level2_reference(
                        metadata_record.level1_json,
                        l2_cid,
                    )
                    logger.info(f"Stored Level 1 metadata for {ark_id}: {l1_cid}")

                repo.update_metadata_cids(
                    ark_record.id,
                    level1_cid=l1_cid,
                    level2_cid=l2_cid,
                    purge_local=True,
                    reset_publish_tracking=True,
                )
                db.commit()

            except StorageError as e:
                if l2_cid and not had_l2_cid:
                    repo.update_metadata_cids(ark_record.id, level2_cid=l2_cid)
                is_permanent = (
                    "is required" in str(e)
                    or ark_record.publish_retry_count >= self.max_retries - 1
                )
                if is_permanent:
                    error_msg = f"Metadata storage failed (permanent): {e}"
                else:
                    error_msg = f"Metadata storage failed: {e}"

                logger.error(f"{error_msg} for ARK {ark_id}")
                repo.mark_publish_failed(ark_id, error_msg, is_permanent=is_permanent)
                db.commit()

                if is_permanent:
                    self.stats["total_permanent_failures"] += 1
                self._add_recent_error(ark_id, error_msg)
                return False

            logger.info(f"Successfully persisted metadata for ARK: {ark_id}")
            self.stats["total_succeeded"] += 1
            return True

        except Exception as e:
            logger.error(f"Unexpected error persisting metadata for {ark_id}: {e}")
            db.rollback()
            self._add_recent_error(ark_id, f"Unexpected error: {e}")
            return False

        finally:
            db.close()

    def run_publish_cycle(self) -> None:
        """Run one metadata persistence cycle."""
        start_time = _utc_now()
        before = self._snapshot_cycle_totals()

        try:
            session_factory = SessionLocal()
            db: Session = session_factory()

            try:
                repo = ARKRepository(db)
                records = repo.get_metadata_pending_persist(
                    limit=self.page_size,
                    max_retries=self.max_retries,
                    backoff_base=self.backoff_base,
                )
                db.commit()

                if not records:
                    logger.debug("No ARKs pending metadata persistence")
                    return

                ark_ids = [record.ark for record in records]
                logger.info(f"Persisting metadata for {len(ark_ids)} ARKs")

            finally:
                db.close()

            for ark_id in ark_ids:
                self.stats["total_processed"] += 1
                if not self.persist_single_ark(ark_id):
                    self.stats["total_failed"] += 1

        except Exception as e:
            logger.error(f"Error in metadata persistence cycle: {e}")
            self._add_recent_error("BATCH", f"Cycle error: {e}")

        finally:
            duration = self._record_cycle_metrics(start_time, before)
            logger.info(
                f"Metadata cycle complete. Duration: {duration:.2f}s, "
                f"Processed: {self.stats['total_processed']}, "
                f"Success: {self.stats['total_succeeded']}, "
                f"Failed: {self.stats['total_failed']}"
            )


class ChainPublisherWorker(_WorkerStatsMixin):
    """Worker that publishes ARKs with persisted metadata to blockchain."""

    def __init__(
        self,
        corelib_client: DARKCoreClient,
        page_size: int = 20,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.corelib_client = corelib_client
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._init_stats()

    def _finalize_publish(self, repo: ARKRepository, db: Session, ark_id: str) -> bool:
        """Mark an ARK as published after a confirmed or reconciled chain write."""
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

        logger.info(f"Successfully published ARK: {ark_id}")
        self.stats["total_succeeded"] += 1
        return True

    def _reconcile_chain_state(
        self,
        *,
        ark_id: str,
        naan: str,
        name: str,
        expected_url: str,
        expected_cid: str,
    ) -> str:
        """
        Read on-chain state after a failed write.

        Returns one of: matched, missing, mismatch, failed.
        """
        try:
            chain_ark = self.corelib_client.get_ark(naan, name)
        except ARKNotFoundError:
            logger.info(f"Reconcile found no on-chain ARK for {ark_id}")
            return "missing"
        except Exception as exc:
            logger.warning(f"Reconcile read failed for {ark_id}: {exc}")
            return "failed"

        if chain_ark.cid == expected_cid and chain_ark.url == expected_url:
            logger.info(f"Reconcile confirmed expected on-chain state for {ark_id}")
            return "matched"

        logger.warning(
            "Reconcile found divergent on-chain state for %s "
            "(expected url=%r cid=%r, got url=%r cid=%r)",
            ark_id,
            expected_url,
            expected_cid,
            chain_ark.url,
            chain_ark.cid,
        )
        return "mismatch"

    def _defer_infrastructure_retry(
        self,
        *,
        repo: ARKRepository,
        db: Session,
        ark_id: str,
        error_msg: str,
    ) -> bool:
        """Release a chain attempt without consuming retry budget."""
        logger.warning(f"{error_msg} for ARK {ark_id}; deferring without consuming retry")
        repo.defer_publish_retry(ark_id, error_msg)
        db.commit()
        self._add_recent_error(ark_id, error_msg)
        return False

    def _handle_publish_error(
        self,
        *,
        repo: ARKRepository,
        db: Session,
        ark_record,
        ark_id: str,
        naan: str,
        name: str,
        expected_cid: str,
        error_msg: str,
        original_is_permanent: bool,
        original_is_infrastructure: bool = False,
    ) -> bool:
        """Reconcile after a chain write error, then record retry/permanent status."""
        reconcile_result = self._reconcile_chain_state(
            ark_id=ark_id,
            naan=naan,
            name=name,
            expected_url=ark_record.target,
            expected_cid=expected_cid,
        )

        if reconcile_result == "matched":
            logger.info(f"Treating failed publish as successful after reconcile: {ark_id}")
            return self._finalize_publish(repo, db, ark_id)

        if reconcile_result == "failed" and original_is_infrastructure:
            return self._defer_infrastructure_retry(
                repo=repo,
                db=db,
                ark_id=ark_id,
                error_msg=f"{error_msg}; reconcile unavailable",
            )

        is_permanent = original_is_permanent
        if reconcile_result == "mismatch" and ark_record.state == ARKState.DRAFT:
            is_permanent = True
            error_msg = f"{error_msg}; reconcile conflict: on-chain ARK differs from expected create"
        elif reconcile_result in ("missing", "mismatch"):
            error_msg = f"{error_msg}; reconcile result: {reconcile_result}"
        else:
            error_msg = f"{error_msg}; reconcile unavailable"

        logger.error(f"{error_msg} for ARK {ark_id}")
        repo.mark_publish_failed(ark_id, error_msg, is_permanent=is_permanent)
        db.commit()

        if is_permanent:
            self.stats["total_permanent_failures"] += 1
        self._add_recent_error(ark_id, error_msg)
        return False

    def _is_infrastructure_exception(self, exc: Exception) -> bool:
        """Return True for RPC/transport errors that should not consume ARK retries."""
        if isinstance(exc, CoreConnectionError):
            return True
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True

        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "rpc",
                "timeout",
                "timed out",
                "connection",
                "connect",
                "refused",
                "unavailable",
                "transport",
            )
        )

    def _classify_publish_exception(self, exc: Exception, ark_record) -> tuple[str, bool, bool]:
        """Return error message, permanence, and infrastructure flag."""
        if isinstance(exc, (AuthorityNotFoundError, AuthorizationError)):
            return f"Authority error (permanent): {exc}", True, False

        if isinstance(exc, AuthorityError):
            is_permanent = ark_record.publish_retry_count >= self.max_retries - 1
            if is_permanent:
                return f"Authority error (max retries exceeded, permanent): {exc}", True, False
            return f"Authority error (retriable): {exc}", False, False

        if isinstance(exc, ARKError):
            return f"ARK error (permanent): {exc}", True, False

        if isinstance(exc, TransactionError):
            is_reverted = getattr(exc, "status", None) == 0
            is_infrastructure = (not is_reverted) and self._is_infrastructure_exception(exc)
            is_permanent = (not is_infrastructure) and (
                is_reverted or ark_record.publish_retry_count >= self.max_retries - 1
            )
            if is_permanent:
                return f"Transaction error (permanent): {exc}", True, False
            return f"Transaction error (retriable): {exc}", False, is_infrastructure

        is_infrastructure = self._is_infrastructure_exception(exc)
        is_permanent = (not is_infrastructure) and ark_record.publish_retry_count >= self.max_retries - 1
        return f"Blockchain publish failed: {exc}", is_permanent, is_infrastructure

    def _classify_pipeline_result(
        self,
        result: ARKPublishResult,
        ark_record,
    ) -> tuple[str, bool, bool]:
        """Return error message, permanence, and infrastructure flag."""
        detail = result.error or result.status
        if result.status == "reverted":
            return f"Pipeline transaction reverted (permanent): {detail}", True, False

        if result.status in {"ambiguous", "send_failed", "not_sent"}:
            return f"Pipeline transaction {result.status} (infrastructure): {detail}", False, True

        is_permanent = ark_record.publish_retry_count >= self.max_retries - 1
        if is_permanent:
            return f"Pipeline transaction {result.status} (max retries exceeded, permanent): {detail}", True, False
        return f"Pipeline transaction {result.status} (retriable): {detail}", False, False

    def _apply_pipeline_result(
        self,
        item: dict,
        result: ARKPublishResult,
    ) -> bool:
        """Apply one semantic pipeline result to the local ARK row."""
        session_factory = SessionLocal()
        db: Session = session_factory()

        try:
            repo = ARKRepository(db)
            ark_record = repo.get_by_ark(item["ark_id"])
            if not ark_record:
                logger.warning(f"ARK not found while applying pipeline result: {item['ark_id']}")
                return False

            if result.status == "confirmed":
                logger.info(f"Pipeline confirmed ARK {item['action']}: {item['ark_id']}")
                return self._finalize_publish(repo, db, item["ark_id"])

            error_msg, is_permanent, is_infrastructure = self._classify_pipeline_result(result, ark_record)
            return self._handle_publish_error(
                repo=repo,
                db=db,
                ark_record=ark_record,
                ark_id=item["ark_id"],
                naan=item["naan"],
                name=item["name"],
                expected_cid=item["cid"],
                error_msg=error_msg,
                original_is_permanent=is_permanent,
                original_is_infrastructure=is_infrastructure,
            )

        except Exception as exc:
            logger.error(f"Unexpected error applying pipeline result for {item['ark_id']}: {exc}")
            db.rollback()
            self._add_recent_error(item["ark_id"], f"Unexpected pipeline apply error: {exc}")
            return False

        finally:
            db.close()

    def _apply_publish_exception(self, item: dict, exc: Exception) -> bool:
        """Apply an exception raised for a whole authority pipeline call."""
        session_factory = SessionLocal()
        db: Session = session_factory()

        try:
            repo = ARKRepository(db)
            ark_record = repo.get_by_ark(item["ark_id"])
            if not ark_record:
                logger.warning(f"ARK not found while applying pipeline exception: {item['ark_id']}")
                return False

            error_msg, is_permanent, is_infrastructure = self._classify_publish_exception(exc, ark_record)
            return self._handle_publish_error(
                repo=repo,
                db=db,
                ark_record=ark_record,
                ark_id=item["ark_id"],
                naan=item["naan"],
                name=item["name"],
                expected_cid=item["cid"],
                error_msg=error_msg,
                original_is_permanent=is_permanent,
                original_is_infrastructure=is_infrastructure,
            )

        except Exception as apply_exc:
            logger.error(f"Unexpected error applying pipeline exception for {item['ark_id']}: {apply_exc}")
            db.rollback()
            self._add_recent_error(item["ark_id"], f"Unexpected pipeline exception apply error: {apply_exc}")
            return False

        finally:
            db.close()

    def publish_single_ark(self, ark_id: str) -> bool:
        """Publish a single ARK to blockchain."""
        session_factory = SessionLocal()
        db: Session = session_factory()

        try:
            repo = ARKRepository(db)
            ark_record = repo.get_by_ark(ark_id)

            if not ark_record:
                logger.warning(f"ARK not found during blockchain publish: {ark_id}")
                return False

            if ark_record.state not in (ARKState.DRAFT, ARKState.UPDATE):
                logger.warning(f"ARK {ark_id} not in publishable state: {ark_record.state}")
                return False

            metadata_record = repo.get_metadata_by_ark_id(ark_record.id)
            if not metadata_record or not metadata_record.level1_cid or not metadata_record.original_cid:
                logger.info(f"ARK {ark_id} is not ready for blockchain publish")
                return False

            cid = metadata_record.level1_cid
            logger.info(f"Ready to publish {ark_id} with L1 CID: {cid}")
            naan, name = parse_ark(ark_id)

            try:
                if ark_record.state == ARKState.DRAFT:
                    self.corelib_client.create_ark(
                        uuid=ark_record.authority_id,
                        naan=naan,
                        name=name,
                        url=ark_record.target,
                        cid=cid,
                        fetch_result=False,
                    )
                    operation = "create"
                else:
                    self.corelib_client.update_ark(
                        uuid=ark_record.authority_id,
                        naan=naan,
                        name=name,
                        url=ark_record.target,
                        cid=cid,
                        fetch_result=False,
                    )
                    operation = "update"

                logger.info(f"ARK {operation} published to blockchain: {ark_id}")

            except Exception as e:
                error_msg, is_permanent, is_infrastructure = self._classify_publish_exception(e, ark_record)
                return self._handle_publish_error(
                    repo=repo,
                    db=db,
                    ark_record=ark_record,
                    ark_id=ark_id,
                    naan=naan,
                    name=name,
                    expected_cid=cid,
                    error_msg=error_msg,
                    original_is_permanent=is_permanent,
                    original_is_infrastructure=is_infrastructure,
                )

            return self._finalize_publish(repo, db, ark_id)

        except Exception as e:
            logger.error(f"Unexpected error publishing {ark_id}: {e}")
            db.rollback()
            self._add_recent_error(ark_id, f"Unexpected error: {e}")
            return False

        finally:
            db.close()

    def _build_pipeline_item(self, repo: ARKRepository, record) -> Optional[dict]:
        """Build one semantic pipeline item from a claimed ARK record."""
        metadata_record = repo.get_metadata_by_ark_id(record.id)
        if not metadata_record or not metadata_record.level1_cid or not metadata_record.original_cid:
            logger.info(f"ARK {record.ark} is not ready for blockchain publish")
            return None

        naan, name = parse_ark(record.ark)
        action = "create" if record.state == ARKState.DRAFT else "update"
        operation = ARKPublishOperation(
            ref=record.ark,
            action=action,
            naan=naan,
            name=name,
            url=record.target,
            cid=metadata_record.level1_cid,
        )
        return {
            "ark_id": record.ark,
            "authority_id": record.authority_id,
            "state": record.state,
            "target": record.target,
            "naan": naan,
            "name": name,
            "cid": metadata_record.level1_cid,
            "action": action,
            "operation": operation,
        }

    def _publish_authority_group(self, authority_id: str, items: list[dict]) -> None:
        """Publish one authority group through the core-lib nonce pipeline."""
        operations = [item["operation"] for item in items]
        logger.info(
            "Publishing %s ARKs for authority %s with page_size=%s",
            len(operations),
            authority_id,
            self.page_size,
        )

        try:
            results = self.corelib_client.publish_ark_operations(
                uuid=authority_id,
                operations=operations,
                pipeline_size=self.page_size,
            )
        except Exception as exc:
            logger.error(f"Pipeline publish failed for authority {authority_id}: {exc}")
            for item in items:
                self.stats["total_processed"] += 1
                if not self._apply_publish_exception(item, exc):
                    self.stats["total_failed"] += 1
            return

        results_by_ref = {result.ref: result for result in results}
        for item in items:
            self.stats["total_processed"] += 1
            result = results_by_ref.get(item["ark_id"])
            if result is None:
                result = ARKPublishResult(
                    ref=item["ark_id"],
                    action=item["action"],
                    status="ambiguous",
                    error="Missing pipeline result from core-lib",
                )

            if not self._apply_pipeline_result(item, result):
                self.stats["total_failed"] += 1

    def run_publish_cycle(self) -> None:
        """Run one blockchain publication cycle."""
        start_time = _utc_now()
        before = self._snapshot_cycle_totals()

        try:
            session_factory = SessionLocal()
            db: Session = session_factory()

            try:
                repo = ARKRepository(db)
                records = repo.get_chain_pending_publish(
                    limit=self.page_size,
                    max_retries=self.max_retries,
                    backoff_base=self.backoff_base,
                )
                db.commit()

                if not records:
                    logger.debug("No ARKs pending blockchain publish")
                    return

                items = []
                for record in records:
                    item = self._build_pipeline_item(repo, record)
                    if item is not None:
                        items.append(item)
                logger.info(f"Publishing {len(items)} ARKs to blockchain")

            finally:
                db.close()

            grouped_items = defaultdict(list)
            for item in items:
                grouped_items[item["authority_id"]].append(item)

            for authority_id, group_items in grouped_items.items():
                self._publish_authority_group(authority_id, group_items)

        except Exception as e:
            logger.error(f"Error in blockchain publish cycle: {e}")
            self._add_recent_error("BATCH", f"Cycle error: {e}")

        finally:
            duration = self._record_cycle_metrics(start_time, before)
            logger.info(
                f"Blockchain cycle complete. Duration: {duration:.2f}s, "
                f"Processed: {self.stats['total_processed']}, "
                f"Success: {self.stats['total_succeeded']}, "
                f"Failed: {self.stats['total_failed']}"
            )


class ARKPublisher(_WorkerStatsMixin):
    """
    Backward-compatible facade that runs metadata persistence then chain publish.

    New deployments should run MetadataPersistenceWorker and ChainPublisherWorker
    as separate processes.
    """

    def __init__(
        self,
        corelib_client: DARKCoreClient,
        metadata_storage: MetadataStorage,
        page_size: int = 20,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.metadata_worker = MetadataPersistenceWorker(
            metadata_storage=metadata_storage,
            page_size=page_size,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )
        self.chain_worker = ChainPublisherWorker(
            corelib_client=corelib_client,
            page_size=page_size,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )
        self._init_stats()

    def publish_single_ark(self, ark_id: str) -> bool:
        """Persist metadata and then publish one ARK."""
        metadata_ready = False
        session_factory = SessionLocal()
        db: Session = session_factory()
        try:
            repo = ARKRepository(db)
            ark_record = repo.get_by_ark(ark_id)
            if ark_record:
                metadata_record = repo.get_metadata_by_ark_id(ark_record.id)
                metadata_ready = bool(
                    metadata_record
                    and metadata_record.level1_cid
                    and metadata_record.original_cid
                )
        finally:
            db.close()

        if not metadata_ready:
            metadata_ok = self.metadata_worker.persist_single_ark(ark_id)
            if not metadata_ok:
                self._sync_stats()
                return False
        chain_ok = self.chain_worker.publish_single_ark(ark_id)
        self._sync_stats()
        return chain_ok

    def run_publish_cycle(self) -> None:
        """Run both stages in sequence for legacy single-worker execution."""
        self.metadata_worker.run_publish_cycle()
        self.chain_worker.run_publish_cycle()
        self._sync_stats()

    def _sync_stats(self) -> None:
        """Merge child worker counters into facade counters."""
        self.stats["total_processed"] = (
            self.chain_worker.stats["total_processed"]
            or self.metadata_worker.stats["total_processed"]
        )
        self.stats["total_succeeded"] = self.chain_worker.stats["total_succeeded"]
        self.stats["total_failed"] = (
            self.metadata_worker.stats["total_failed"]
            + self.chain_worker.stats["total_failed"]
        )
        self.stats["total_permanent_failures"] = (
            self.metadata_worker.stats["total_permanent_failures"]
            + self.chain_worker.stats["total_permanent_failures"]
        )
        self.stats["last_run_at"] = self.chain_worker.stats["last_run_at"] or self.metadata_worker.stats["last_run_at"]
        self.stats["last_run_duration"] = (
            (self.metadata_worker.stats["last_run_duration"] or 0)
            + (self.chain_worker.stats["last_run_duration"] or 0)
        )
        self.stats["last_run_processed"] = (
            (self.metadata_worker.stats["last_run_processed"] or 0)
            + (self.chain_worker.stats["last_run_processed"] or 0)
        )
        self.stats["last_run_succeeded"] = (
            (self.metadata_worker.stats["last_run_succeeded"] or 0)
            + (self.chain_worker.stats["last_run_succeeded"] or 0)
        )
        self.stats["last_run_failed"] = (
            (self.metadata_worker.stats["last_run_failed"] or 0)
            + (self.chain_worker.stats["last_run_failed"] or 0)
        )
        self.stats["recent_errors"] = (
            self.metadata_worker.stats["recent_errors"]
            + self.chain_worker.stats["recent_errors"]
        )[:10]
