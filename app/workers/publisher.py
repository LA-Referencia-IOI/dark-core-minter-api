"""Independent workers for metadata, replication and blockchain publication.

Each attempt takes a PostgreSQL advisory lock for its ARK. The lock remains
held during external I/O and the same session stores the result, so API writes
and workers cannot process the same ARK concurrently.
"""

from __future__ import annotations

import logging
import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dark_core_lib import ARKPublishOperation, ARKPublishResult, DARKCoreClient
from dark_core_lib.exceptions import (
    ARKNotFoundError,
    AuthorityNotFoundError,
    AuthorizationError,
    ConnectionError as DarkConnectionError,
)
from dark_core_lib.metadata import MetadataService, MetadataStorage

from app.database.ark_locks import acquire_ark_lock
from app.database.connection import SessionLocal
from app.database.models import ARKRecord
from app.models.processing import (
    ProcessingErrorCode,
    ProcessingStage,
    ProcessingStatus,
    ProcessingWaitReason,
)
from app.models.states import ARKState
from app.repositories.ark_repository import ARKRepository, _ready_for_processing, parse_ark

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_infrastructure_error(exc: Exception) -> bool:
    if isinstance(exc, (DarkConnectionError, TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    return any(
        word in text for word in ("connection", "timeout", "unavailable", "refused", "temporarily")
    )


def _has_cluster_assignment(status: Any) -> bool:
    """Return whether Cluster still owns a pin attempt for this CID.

    The explicit count is authoritative.  The status fallback keeps the
    worker safe with non-Store API implementations of MetadataStorage.
    """
    assigned = getattr(status, "assigned_replicas", None)
    if assigned is not None:
        return int(assigned) > 0
    return str(getattr(status, "status", "")).lower() in {"pinned", "pinning", "queued"}


class _Stats:
    def _init_stats(self) -> None:
        self.stats: dict[str, Any] = {
            "total_processed": 0,
            "total_succeeded": 0,
            "total_failed": 0,
            "total_deferred": 0,
            "total_permanent_failures": 0,
            "last_run_at": None,
            "last_run_duration": None,
            "last_run_processed": 0,
            "last_run_succeeded": 0,
            "last_run_failed": 0,
            "last_run_deferred": 0,
            "last_run_page_size": 0,
            "recent_errors": [],
            "last_reconciliation_at": None,
            "last_reconciliation_checked": 0,
            "last_reconciliation_advanced": 0,
            "last_reconciliation_waiting": 0,
            "last_reconciliation_repaired": 0,
            "last_reconciliation_purged": 0,
            "last_reconciliation_failed": 0,
            "last_reconciliation_mode": "first_pin",
            "last_maintenance_block_reason": None,
            "last_replication_promotions": 0,
        }

    def get_stats(self) -> dict[str, Any]:
        result = dict(self.stats)
        for key in ("last_run_at", "last_reconciliation_at"):
            if result[key]:
                result[key] = result[key].isoformat()
        return result

    def _start(self) -> tuple[datetime, tuple[int, int, int, int]]:
        return _now(), tuple(
            self.stats[f"total_{key}"] for key in ("processed", "succeeded", "failed", "deferred")
        )

    def _finish(self, started: datetime, before: tuple[int, int, int, int]) -> None:
        self.stats["last_run_at"] = _now()
        self.stats["last_run_duration"] = (_now() - started).total_seconds()
        for key, value in zip(("processed", "succeeded", "failed", "deferred"), before):
            self.stats[f"last_run_{key}"] = self.stats[f"total_{key}"] - value

    def _error(self, ark: str, message: str) -> None:
        self.stats["recent_errors"] = [
            {"ark": ark, "error": message, "timestamp": _now().isoformat()}
        ] + self.stats["recent_errors"][:9]


class MetadataPersistenceWorker(_Stats):
    """Persist L2 then L1 and hand the ARK to availability verification."""

    def __init__(
        self,
        metadata_storage: MetadataStorage,
        page_size: int = 100,
        concurrency: int = 4,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.service = MetadataService(metadata_storage)
        self.page_size = max(int(page_size), 1)
        self.concurrency = max(int(concurrency), 1)
        self.max_retries = max(int(max_retries), 1)
        self.backoff_base = float(backoff_base)
        self._init_stats()

    def _persist(self, ark: str) -> bool:
        with acquire_ark_lock(ark) as db:
            if db is None:
                return False
            repo = ARKRepository(db)
            try:
                record = repo.get_by_ark(ark)
                if (
                    record is None
                    or record.state not in (ARKState.DRAFT, ARKState.UPDATE)
                    or record.processing_stage != int(ProcessingStage.METADATA)
                    or not _ready_for_processing(record, _now())
                ):
                    return False
                metadata = repo.get_metadata_by_ark_id(record.id)
                if metadata is None:
                    raise ValueError("metadata record is missing")

                # Keep the lock connection but finish the read transaction before I/O.
                db.commit()
                level2_cid = metadata.original_cid
                if level2_cid is None:
                    if metadata.original_content is None or not metadata.original_media_type:
                        raise ValueError("Level 2 payload and media type are required")
                    level2_cid = self.service.store_level2(
                        metadata.original_content.encode("utf-8"),
                        metadata.original_media_type,
                        metadata.original_schema,
                    )
                level1_cid = metadata.level1_cid
                if level1_cid is None:
                    if metadata.level1_json is None:
                        raise ValueError("Level 1 metadata JSON is required")
                    _, level1_cid = self.service.store_level1_with_level2_reference(
                        metadata.level1_json, level2_cid
                    )

                current = db.query(ARKRecord).filter_by(id=record.id).with_for_update().one()
                current_metadata = repo.get_metadata_by_ark_id(current.id)
                if current_metadata is None:
                    raise ValueError("metadata record disappeared")
                current_metadata.level1_cid = level1_cid
                current_metadata.original_cid = level2_cid
                current_metadata.level1_replica_count = None
                current_metadata.level2_replica_count = None
                current_metadata.replication_checked_at = None
                current_metadata.replication_observation_count = 0
                current_metadata.replication_error_code = None
                current_metadata.replication_last_error = None
                current.processing_stage = int(ProcessingStage.AVAILABILITY)
                current.processing_status = int(ProcessingStatus.READY)
                current.processing_attempt_count = 0
                current.next_action_at = None
                current.processing_wait_reason = int(ProcessingWaitReason.NONE)
                current.processing_error_code = None
                current.processing_error_detail = None
                db.commit()
                return True
            except Exception as exc:
                db.rollback()
                message = f"metadata persistence failed: {exc}"
                if _is_infrastructure_error(exc):
                    repo.defer_processing_retry(
                        ark, message, int(ProcessingErrorCode.STORAGE_UNAVAILABLE)
                    )
                    self.stats["total_deferred"] += 1
                else:
                    repo.mark_processing_failed(
                        ark,
                        message,
                        int(ProcessingErrorCode.STORAGE_INVALID_RESPONSE),
                        is_permanent=True,
                    )
                    self.stats["total_permanent_failures"] += 1
                db.commit()
                self._error(ark, message)
                return False

    def persist_single_ark(self, ark_id: str) -> bool:
        return self._persist(ark_id)

    def run_publish_cycle(self, effective_concurrency: Optional[int] = None) -> None:
        started, before = self._start()
        db = SessionLocal()()
        try:
            arks = [
                record.ark
                for record in ARKRepository(db).get_metadata_pending_persist(
                    self.page_size, self.max_retries, self.backoff_base
                )
            ]
        finally:
            db.close()
        self.stats["last_run_page_size"] = self.page_size
        concurrency = max(1, min(int(effective_concurrency or self.concurrency), self.concurrency))
        self.stats["last_effective_concurrency"] = concurrency
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for future in as_completed([executor.submit(self._persist, ark) for ark in arks]):
                self.stats["total_processed"] += 1
                self.stats["total_succeeded" if future.result() else "total_failed"] += 1
        self._finish(started, before)


class ReplicationReconciliationWorker(_Stats):
    """Observe global pin counts, repair missing CIDs, then purge safely."""

    def __init__(
        self,
        metadata_storage: MetadataStorage,
        page_size: int = 50,
        concurrency: int = 2,
        pinning_recheck_seconds: int = 2,
        queued_recheck_seconds: int = 5,
        visibility_recheck_seconds: int = 3,
        max_recheck_seconds: int = 30,
        repair_grace_seconds: int = 120,
        repair_cooldown_seconds: int = 900,
        publish_after_replicas: int = 1,
        target_replicas: int = 2,
        status_batch_size: int = 200,
        idle_sleep_seconds: int = 2,
    ):
        self.storage = metadata_storage
        self.service = MetadataService(metadata_storage)
        self.page_size = max(int(page_size), 1)
        self.concurrency = max(int(concurrency), 1)
        self.pinning_recheck_seconds = max(int(pinning_recheck_seconds), 1)
        self.queued_recheck_seconds = max(int(queued_recheck_seconds), 1)
        self.visibility_recheck_seconds = max(int(visibility_recheck_seconds), 1)
        self.max_recheck_seconds = max(int(max_recheck_seconds), self.queued_recheck_seconds)
        self.repair_grace_seconds = max(int(repair_grace_seconds), 0)
        self.repair_cooldown_seconds = max(int(repair_cooldown_seconds), 0)
        self.publish_after = max(int(publish_after_replicas), 1)
        self.target_replicas = max(int(target_replicas), self.publish_after)
        self.status_batch_size = min(max(int(status_batch_size), 1), 200)
        self.idle_sleep_seconds = max(int(idle_sleep_seconds), 1)
        self._recent_statuses: dict[str, tuple[datetime, Any]] = {}
        self._init_stats()

    def _wait_schedule(
        self,
        ark: str,
        reason: ProcessingWaitReason,
        previous_reason: int | None,
        previous_replicas: int,
        replicas: int,
        observations: int,
    ) -> datetime:
        """Schedule normal Cluster propagation with bounded exponential backoff.

        The wait reason and confirmed count are persisted already, so no extra
        state/table is required. A state/count change starts a fresh backoff.
        """
        base = {
            ProcessingWaitReason.CLUSTER_PINNING: self.pinning_recheck_seconds,
            ProcessingWaitReason.INITIAL_VISIBILITY: self.visibility_recheck_seconds,
            ProcessingWaitReason.CLUSTER_QUEUED: self.queued_recheck_seconds,
        }.get(reason, self.max_recheck_seconds)
        changed = int(previous_reason or 0) != int(reason) or replicas > previous_replicas
        exponent = 0 if changed else min(max(observations - 1, 0), 8)
        seconds = min(base * (2 ** exponent), self.max_recheck_seconds)
        # Stable +/- 10% jitter spreads a simultaneous batch without making
        # behaviour nondeterministic or hard to diagnose.
        jitter = ((int(hashlib.sha256(ark.encode("utf-8")).hexdigest()[:8], 16) % 21) - 10) / 100
        return _now() + timedelta(seconds=max(1, seconds * (1 + jitter)))

    def _reconcile(self, ark: str, observed_statuses: Optional[dict[str, Any]] = None) -> dict[str, int]:
        result = {"checked": 1, "advanced": 0, "waiting": 0, "repaired": 0, "purged": 0, "failed": 0}
        with acquire_ark_lock(ark) as db:
            if db is None:
                return {**result, "checked": 0}
            repo = ARKRepository(db)
            try:
                record = repo.get_by_ark(ark)
                if (
                    record is None
                    or record.state not in (ARKState.DRAFT, ARKState.UPDATE, ARKState.PUBLISHED)
                    or record.processing_stage
                    not in (int(ProcessingStage.AVAILABILITY), int(ProcessingStage.REPLICATION))
                    or not _ready_for_processing(record, _now())
                ):
                    return {**result, "checked": 0}
                metadata = repo.get_metadata_by_ark_id(record.id)
                if metadata is None or not metadata.level1_cid or not metadata.original_cid:
                    raise ValueError("metadata CIDs are required for reconciliation")
                level1_cid, level2_cid = metadata.level1_cid, metadata.original_cid
                db.commit()

                now = _now()
                first_seen_at = metadata.created_at or metadata.updated_at or now
                last_repair_at = metadata.last_repair_at
                repair_allowed = (
                    (now - first_seen_at).total_seconds() >= self.repair_grace_seconds
                    and (last_repair_at is None or (now - last_repair_at).total_seconds() >= self.repair_cooldown_seconds)
                )
                statuses = observed_statuses
                if statuses is None:
                    statuses = self.storage.get_replication_statuses([level2_cid, level1_cid])
                level2_status = statuses.get(level2_cid)
                level1_status = statuses.get(level1_cid)
                if level2_status is None or level1_status is None:
                    raise ValueError("Store API batch status omitted a CID")
                if str(level1_status.status).lower() == "unknown" or str(level2_status.status).lower() == "unknown":
                    # Store API returns partial batch results. Requeue only
                    # this ARK instead of failing unrelated CIDs in the page.
                    raise TimeoutError("Store API did not complete this CID observation")
                if (
                    level2_status.total_replicas == 0
                    and level2_status.status in {"unpinned", "error"}
                    and (level2_status.status == "error" or not _has_cluster_assignment(level2_status))
                    and repair_allowed
                ):
                    if metadata.original_content is None or not metadata.original_media_type:
                        raise ValueError("Level 2 payload is unavailable for repair")
                    repaired = self.service.store_level2(
                        metadata.original_content.encode("utf-8"),
                        metadata.original_media_type,
                        metadata.original_schema,
                    )
                    if repaired != level2_cid:
                        raise ValueError("Level 2 repair returned a different CID")
                    result["repaired"] += 1
                    metadata.last_repair_at = now
                if (
                    level1_status.total_replicas == 0
                    and level1_status.status in {"unpinned", "error"}
                    and (level1_status.status == "error" or not _has_cluster_assignment(level1_status))
                    and repair_allowed
                ):
                    if metadata.level1_json is None:
                        raise ValueError("Level 1 payload is unavailable for repair")
                    _, repaired = self.service.store_level1_with_level2_reference(
                        metadata.level1_json, level2_cid
                    )
                    if repaired != level1_cid:
                        raise ValueError("Level 1 repair returned a different CID")
                    result["repaired"] += 1
                    metadata.last_repair_at = now

                current = db.query(ARKRecord).filter_by(id=record.id).with_for_update().one()
                current_metadata = repo.get_metadata_by_ark_id(current.id)
                if (
                    current_metadata is None
                    or current_metadata.level1_cid != level1_cid
                    or current_metadata.original_cid != level2_cid
                ):
                    return {**result, "checked": 0}
                old_l1 = int(current_metadata.level1_replica_count or 0)
                old_l2 = int(current_metadata.level2_replica_count or 0)
                previous_reason = current.processing_wait_reason
                current_metadata.level1_replica_count = max(int(level1_status.total_replicas), 0)
                current_metadata.level2_replica_count = max(int(level2_status.total_replicas), 0)
                current_metadata.replication_checked_at = now
                current_metadata.replication_observation_count = int(current_metadata.replication_observation_count or 0) + 1
                if result["repaired"]:
                    current_metadata.last_repair_at = now
                current_metadata.replication_error_code = None
                current_metadata.replication_last_error = None
                replicas = min(
                    current_metadata.level1_replica_count, current_metadata.level2_replica_count
                )
                if current.processing_stage == int(ProcessingStage.AVAILABILITY):
                    if replicas >= self.publish_after:
                        current.processing_stage = int(ProcessingStage.CHAIN)
                        current.processing_status = int(ProcessingStatus.READY)
                        current.next_action_at = None
                        current.processing_wait_reason = int(ProcessingWaitReason.NONE)
                        result["advanced"] += 1
                    else:
                        statuses_seen = {str(level1_status.status).lower(), str(level2_status.status).lower()}
                        reason = (
                            ProcessingWaitReason.CLUSTER_QUEUED
                            if "queued" in statuses_seen
                            else ProcessingWaitReason.CLUSTER_PINNING
                            if "pinning" in statuses_seen
                            else ProcessingWaitReason.INITIAL_VISIBILITY
                        )
                        # Cluster propagation is a normal scheduled action.
                        current.processing_status = int(ProcessingStatus.WAITING)
                        current.next_action_at = self._wait_schedule(
                            ark, reason, previous_reason, min(old_l1, old_l2), replicas,
                            int(current_metadata.replication_observation_count or 1),
                        )
                        current.processing_wait_reason = int(reason)
                        result["waiting"] += 1
                elif replicas >= self.target_replicas:
                    current_metadata.level1_json = None
                    current_metadata.original_content = None
                    current_metadata.payload_purged_at = _now()
                    current.processing_stage = int(ProcessingStage.COMPLETE)
                    current.processing_status = int(ProcessingStatus.DONE)
                    current.next_action_at = None
                    current.processing_wait_reason = int(ProcessingWaitReason.NONE)
                    result["purged"] = 1
                    result["advanced"] += 1
                else:
                    # The content is available, but the target replica count
                    # has not been reached yet.  Keep it in the normal
                    # replication queue and re-observe it later.
                    current.processing_status = int(ProcessingStatus.WAITING)
                    current.next_action_at = _now() + timedelta(seconds=self.max_recheck_seconds)
                    current.processing_wait_reason = int(ProcessingWaitReason.REPLICA_TARGET)
                    result["waiting"] += 1
                db.commit()
                return result
            except Exception as exc:
                db.rollback()
                # Reconciliation is deliberately endless; it never creates a terminal failure.
                repo.defer_processing_retry(
                    ark,
                    f"replication reconciliation failed: {exc}",
                    int(ProcessingErrorCode.REPLICATION_UNAVAILABLE),
                    delay_seconds=self.pinning_recheck_seconds,
                )
                db.commit()
                self._error(ark, f"replication reconciliation failed: {exc}")
                return {**result, "failed": 1}

    def run_publish_cycle(self) -> None:
        started, before = self._start()
        db = SessionLocal()()
        maintenance = False
        try:
            repo = ARKRepository(db)
            records = repo.get_reconciliation_candidates(self.page_size, int(ProcessingStage.AVAILABILITY))
            if not records and not repo.has_critical_reconciliation_backlog():
                maintenance = True
                records = repo.get_reconciliation_candidates(self.page_size, int(ProcessingStage.REPLICATION))
            self.stats["last_reconciliation_mode"] = "maintenance" if maintenance else "first_pin"
            self.stats["last_maintenance_block_reason"] = None if maintenance or records else "critical_backlog"
            ark_cids = {record.ark: (record.level1_cid, record.level2_cid) for record in records}
        finally:
            db.close()
        if maintenance and ark_cids:
            try:
                promotion_results = self.storage.ensure_replication(
                    [cid for pair in ark_cids.values() for cid in pair],
                    self.target_replicas,
                )
                self.stats["last_replication_promotions"] = sum(
                    value in {"promotion_requested", "already_durable"}
                    for value in promotion_results.values()
                )
            except Exception as exc:
                logger.warning("Durability promotion request failed: %s", exc)
        unique_cids = sorted({cid for pair in ark_cids.values() for cid in pair})
        observed: dict[str, Any] = {}
        observed_at = _now()
        # A CID can be shared by records selected in adjacent cycles. Avoid a
        # duplicate remote probe inside the two-second observation window.
        for cid in unique_cids:
            cached = self._recent_statuses.get(cid)
            if cached and (observed_at - cached[0]).total_seconds() < 2:
                observed[cid] = cached[1]
        pending_cids = [cid for cid in unique_cids if cid not in observed]
        batch_count = 0
        batch_started = _now()
        for offset in range(0, len(pending_cids), self.status_batch_size):
            batch = pending_cids[offset:offset + self.status_batch_size]
            try:
                fresh = self.storage.get_replication_statuses(batch)
                observed.update(fresh)
                self._recent_statuses.update({cid: (_now(), status) for cid, status in fresh.items()})
                batch_count += 1
            except Exception as exc:
                # Leave this batch absent so each affected ARK is deferred by
                # its normal retry path; unrelated CID batches can proceed.
                logger.warning("Replication status batch failed (%s CIDs): %s", len(batch), exc)
        self._recent_statuses = {
            cid: value for cid, value in self._recent_statuses.items()
            if (_now() - value[0]).total_seconds() < 2
        }
        arks = list(ark_cids)
        totals = {"checked": 0, "advanced": 0, "waiting": 0, "repaired": 0, "purged": 0, "failed": 0}
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            for future in as_completed([executor.submit(self._reconcile, ark, observed) for ark in arks]):
                for key, value in future.result().items():
                    totals[key] += value
        self.stats["total_processed"] += totals["checked"]
        self.stats["total_succeeded"] += totals["advanced"]
        self.stats["total_deferred"] += totals["waiting"]
        self.stats["total_failed"] += totals["failed"]
        self.stats.update(
            {
                "last_run_page_size": self.page_size,
                "last_reconciliation_at": _now(),
                "last_reconciliation_checked": totals["checked"],
                "last_reconciliation_advanced": totals["advanced"],
                "last_reconciliation_waiting": totals["waiting"],
                "last_reconciliation_repaired": totals["repaired"],
                "last_reconciliation_purged": totals["purged"],
                "last_reconciliation_failed": totals["failed"],
                "last_reconciliation_unique_cids": len(unique_cids),
                "last_reconciliation_batches": batch_count,
                "last_reconciliation_status_seconds": (_now() - batch_started).total_seconds(),
            }
        )
        logger.info(
            "Replication cycle mode=%s arks=%s unique_cids=%s batches=%s advanced=%s waiting=%s failed=%s",
            self.stats["last_reconciliation_mode"], len(arks), len(unique_cids), batch_count,
            totals["advanced"], totals["waiting"], totals["failed"],
        )
        self._finish(started, before)

    def next_wake_plan(self, runtime: dict[str, Any]) -> tuple[int, str] | None:
        """Return a bounded, due-aware sleep decision for the worker loop."""
        db = SessionLocal()()
        try:
            repository = ARKRepository(db)
            due = repository.get_reconciliation_candidates(1, int(ProcessingStage.AVAILABILITY))
            if due:
                return 0, "continue_due_work"
            next_due = repository.get_next_reconciliation_action_at()
        finally:
            db.close()
        if next_due is None:
            return self.idle_sleep_seconds, "idle_no_work"
        delay = max(int((next_due - _now()).total_seconds()), 0)
        return min(delay, self.idle_sleep_seconds), "sleeping_until_due"


class ChainPublisherWorker(_Stats):
    """Publish only availability-qualified metadata to the blockchain."""

    def __init__(
        self,
        corelib_client: DARKCoreClient,
        page_size: int = 20,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.corelib_client = corelib_client
        self.page_size = max(int(page_size), 1)
        self.max_retries = max(int(max_retries), 1)
        self.backoff_base = float(backoff_base)
        self._init_stats()

    def _chain_matches(self, naan: str, name: str, target: str, cid: str) -> bool:
        try:
            current = self.corelib_client.get_ark(naan, name)
        except ARKNotFoundError:
            return False
        return bool(current and current.url == target and current.cid == cid)

    def _record_failure(self, repo: ARKRepository, ark: str, exc: Exception) -> None:
        message = f"blockchain publish failed: {exc}"
        if isinstance(exc, (AuthorityNotFoundError, AuthorizationError)):
            code = (
                ProcessingErrorCode.AUTHORITY_NOT_FOUND
                if isinstance(exc, AuthorityNotFoundError)
                else ProcessingErrorCode.AUTHORIZATION_FAILED
            )
            repo.mark_processing_failed(ark, message, int(code), is_permanent=True)
            self.stats["total_permanent_failures"] += 1
        elif _is_infrastructure_error(exc):
            repo.defer_processing_retry(
                ark, message, int(ProcessingErrorCode.CHAIN_RPC_UNAVAILABLE)
            )
            self.stats["total_deferred"] += 1
        else:
            repo.mark_processing_failed(
                ark, message, int(ProcessingErrorCode.CHAIN_REVERTED), is_permanent=True
            )
            self.stats["total_permanent_failures"] += 1
        self._error(ark, message)

    def _finalize_pipeline_confirmation(
        self, repo: ARKRepository, item: dict[str, Any]
    ) -> bool:
        """Persist a confirmed (or reconciled) chain write if it is still current.

        The pipeline runs outside the database transaction.  Re-checking the
        internal stage and L1 CID while holding the ARK lock prevents an old
        result from publishing a record that was changed in the meantime.
        """
        current = repo.get_by_ark(item["ark"])
        metadata = (
            repo.get_metadata_by_ark_id(current.id) if current is not None else None
        )
        if (
            current is None
            or current.processing_stage != int(ProcessingStage.CHAIN)
            or metadata is None
            or metadata.level1_cid != item["cid"]
        ):
            return False
        expected_state = (
            current.state.value if isinstance(current.state, ARKState) else current.state
        )
        repo.update_to_published(item["ark"], expected_states=(expected_state,))
        return True

    def _handle_pipeline_result(
        self, repo: ARKRepository, item: dict[str, Any], result: ARKPublishResult
    ) -> bool:
        """Apply a pipeline result without mistaking an uncertain RPC result for a revert.

        A pipeline may intentionally skip later operations after a send or
        receipt uncertainty to preserve nonce order.  Such a result says
        nothing about the semantic state of its ARK: the previous submission
        can already be on chain.  First reconcile the exact on-chain value;
        otherwise defer it without consuming the retry budget.
        """
        if result.status == "confirmed":
            return self._finalize_pipeline_confirmation(repo, item)

        naan, name = parse_ark(item["ark"])
        try:
            if self._chain_matches(naan, name, item["target"], item["cid"]):
                logger.info("reconciled successful pipeline write for %s", item["ark"])
                return self._finalize_pipeline_confirmation(repo, item)
        except Exception as exc:
            # We cannot safely classify a result while the chain is unreadable.
            message = f"blockchain publication reconciliation failed: {exc}"
            repo.defer_processing_retry(
                item["ark"], message, int(ProcessingErrorCode.CHAIN_RPC_UNAVAILABLE)
            )
            self.stats["total_deferred"] += 1
            self._error(item["ark"], message)
            return False

        message = result.error or f"blockchain pipeline result was {result.status}"
        if result.status == "reverted":
            self._record_failure(repo, item["ark"], RuntimeError(message))
            return False

        # ``not_sent``, ``send_failed``, ``ambiguous`` and an unexpected
        # pipeline failure are operational outcomes.  They must remain
        # retryable: marking them CHAIN_REVERTED loses otherwise valid ARKs.
        deferred_message = f"blockchain pipeline {result.status}: {message}"
        repo.defer_processing_retry(
            item["ark"], deferred_message, int(ProcessingErrorCode.CHAIN_RPC_UNAVAILABLE)
        )
        self.stats["total_deferred"] += 1
        self._error(item["ark"], deferred_message)
        return False

    def publish_single_ark(self, ark: str) -> bool:
        with acquire_ark_lock(ark) as db:
            if db is None:
                return False
            repo = ARKRepository(db)
            try:
                record = repo.get_by_ark(ark)
                if (
                    record is None
                    or record.state not in (ARKState.DRAFT, ARKState.UPDATE)
                    or record.processing_stage != int(ProcessingStage.CHAIN)
                    or not _ready_for_processing(record, _now())
                ):
                    return False
                metadata = repo.get_metadata_by_ark_id(record.id)
                if metadata is None or not metadata.level1_cid or not metadata.original_cid:
                    raise ValueError("both metadata CIDs are required for chain publication")
                naan, name = parse_ark(ark)
                state, authority_id, target, level1_cid = (
                    record.state,
                    record.authority_id,
                    record.target,
                    metadata.level1_cid,
                )
                db.commit()
                try:
                    if state == ARKState.DRAFT:
                        self.corelib_client.create_ark(
                            uuid=authority_id,
                            naan=naan,
                            name=name,
                            url=target,
                            cid=level1_cid,
                            fetch_result=False,
                        )
                    else:
                        self.corelib_client.update_ark(
                            uuid=authority_id,
                            naan=naan,
                            name=name,
                            url=target,
                            cid=level1_cid,
                            fetch_result=False,
                        )
                except Exception as exc:
                    # The chain operation can be accepted while its HTTP/RPC reply is lost.
                    if not self._chain_matches(naan, name, target, level1_cid):
                        raise exc
                    logger.info("reconciled successful chain write for %s", ark)
                current = db.query(ARKRecord).filter_by(id=record.id).with_for_update().one()
                current_metadata = repo.get_metadata_by_ark_id(current.id)
                if (
                    current.state != state
                    or current.processing_stage != int(ProcessingStage.CHAIN)
                    or current_metadata is None
                    or current_metadata.level1_cid != level1_cid
                ):
                    return False
                expected_state = state.value if isinstance(state, ARKState) else state
                repo.update_to_published(ark, expected_states=(expected_state,))
                db.commit()
                return True
            except Exception as exc:
                db.rollback()
                self._record_failure(repo, ark, exc)
                db.commit()
                return False

    def run_publish_cycle(self, effective_page_size: Optional[int] = None) -> None:
        started, before = self._start()
        page_size = max(int(effective_page_size or self.page_size), 1)
        db = SessionLocal()()
        try:
            repo = ARKRepository(db)
            records = repo.get_chain_pending_publish(
                page_size, self.max_retries, self.backoff_base
            )
            items = []
            for record in records:
                metadata = repo.get_metadata_by_ark_id(record.id)
                if metadata is None or not metadata.level1_cid or not metadata.original_cid:
                    continue
                naan, name = parse_ark(record.ark)
                action = "create" if record.state == ARKState.DRAFT else "update"
                items.append(
                    {
                        "ark": record.ark,
                        "authority_id": record.authority_id,
                        "state": record.state,
                        "target": record.target,
                        "cid": metadata.level1_cid,
                        "action": action,
                        "operation": ARKPublishOperation(
                            ref=record.ark,
                            action=action,
                            naan=naan,
                            name=name,
                            url=record.target,
                            cid=metadata.level1_cid,
                        ),
                    }
                )
        finally:
            db.close()
        self.stats["last_run_page_size"] = page_size

        grouped = defaultdict(list)
        for item in items:
            grouped[item["authority_id"]].append(item)

        for authority_id, group in grouped.items():
            operations = [item["operation"] for item in group]
            try:
                results = self.corelib_client.publish_ark_operations(
                    uuid=authority_id,
                    operations=operations,
                    pipeline_size=page_size,
                )
                results_by_ref = {result.ref: result for result in results}
            except Exception as exc:
                logger.error("Pipeline publish failed for authority %s: %s", authority_id, exc)
                results_by_ref = {
                    item["ark"]: ARKPublishResult(
                        ref=item["ark"],
                        action=item["action"],
                        status="failed",
                        error=str(exc),
                    )
                    for item in group
                }

            for item in group:
                self.stats["total_processed"] += 1
                result = results_by_ref.get(item["ark"])
                if result is None:
                    result = ARKPublishResult(
                        ref=item["ark"],
                        action=item["action"],
                        status="ambiguous",
                        error="Missing pipeline result from core-lib",
                    )
                if result.status == "confirmed":
                    with acquire_ark_lock(item["ark"]) as result_db:
                        if result_db is None:
                            self.stats["total_failed"] += 1
                            continue
                        result_repo = ARKRepository(result_db)
                        if not self._finalize_pipeline_confirmation(result_repo, item):
                            self.stats["total_failed"] += 1
                            continue
                        result_db.commit()
                    self.stats["total_succeeded"] += 1
                else:
                    with acquire_ark_lock(item["ark"]) as result_db:
                        if result_db is not None:
                            result_repo = ARKRepository(result_db)
                            if self._handle_pipeline_result(result_repo, item, result):
                                result_db.commit()
                                self.stats["total_succeeded"] += 1
                                continue
                            result_db.commit()
                    self.stats["total_failed"] += 1
        self._finish(started, before)


class ARKPublisher(_Stats):
    """Single-process façade for local development; deployment runs three workers."""

    def __init__(
        self,
        corelib_client: DARKCoreClient,
        metadata_storage: MetadataStorage,
        page_size: int = 20,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.metadata_worker = MetadataPersistenceWorker(
            metadata_storage, page_size, 1, max_retries, backoff_base
        )
        self.replication_worker = ReplicationReconciliationWorker(
            metadata_storage=metadata_storage,
            page_size=page_size,
            concurrency=1,
            pinning_recheck_seconds=1,
            queued_recheck_seconds=1,
            visibility_recheck_seconds=1,
            max_recheck_seconds=1,
        )
        self.chain_worker = ChainPublisherWorker(
            corelib_client, page_size, max_retries, backoff_base
        )
        self._init_stats()

    def publish_single_ark(self, ark_id: str) -> bool:
        self.metadata_worker.persist_single_ark(ark_id)
        self.replication_worker._reconcile(ark_id)
        return self.chain_worker.publish_single_ark(ark_id)

    def run_publish_cycle(self) -> None:
        self.metadata_worker.run_publish_cycle()
        self.replication_worker.run_publish_cycle()
        self.chain_worker.run_publish_cycle()
