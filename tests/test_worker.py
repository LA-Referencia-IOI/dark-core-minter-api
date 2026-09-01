"""
Integration-style tests for ARK publisher worker with a real test DB session.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from dark_core_lib.exceptions import (
    ARKNotFoundError,
    AuthorityError,
    AuthorityNotFoundError,
    AuthorizationError,
    TransactionError,
)
from dark_core_lib.metadata import ReplicationStatus, StorageError, StoredDocument
from dark_core_lib.models import ARKInfo, ARKPublishResult

from app.database.models import ARKMetadata, ARKRecord
from app.models.states import ARKState
from app.repositories.ark_repository import ARKRepository
from app.workers.publisher import (
    ARKPublisher,
    ChainPublisherWorker,
    MetadataPersistenceWorker,
    ReplicationReconciliationWorker,
)


class MockMetadataStorage:
    """Mock metadata storage backend for worker tests."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.stored = {}

    def store_document(self, content, content_type, schema=None):
        if self.should_fail:
            raise StorageError("Mock storage error")
        cid = f"mock_cid_{len(self.stored)}"
        self.stored[cid] = StoredDocument(content=content, content_type=content_type, schema=schema)
        return cid

    def get_document(self, cid):
        return self.stored[cid]

    def health_check(self):
        return not self.should_fail

    def get_replication_status(self, cid):
        self.get_document(cid)
        return ReplicationStatus(
            cid=cid,
            status="pinned",
            total_replicas=1,
            local_replicas=1,
            remote_replicas=0,
            sites={"local": 1},
            purge_target_met=True,
        )


def _level1_payload(title: str = "Test Title", year: int = 2024, schema: str = "dublin_core") -> dict:
    return {
        "title": title,
        "authors": ["Test Author"],
        "year": year,
        "original_metadata": {
            "schema": schema,
            "media_type": "application/xml",
            "cid": None,
        },
    }


def _chain_ark_info(*, name: str, url: str, cid: str) -> ARKInfo:
    now = datetime.now(timezone.utc)
    return ARKInfo(
        naan="12345",
        name=name,
        url=url,
        cid=cid,
        owner="0x" + "a" * 40,
        created_at=now,
        updated_at=now,
    )


def _create_ark_with_metadata(
    db_session,
    *,
    name: str,
    state: ARKState = ARKState.DRAFT,
    target: str = "https://example.com",
    publish_retry_count: int = 0,
    publish_last_attempt_at=None,
    metadata_complete: bool = False,
):
    ark_record = ARKRecord(
        naan="12345",
        name=name,
        state=state,
        authority_id="auth-uuid-123",
        target=target,
        publish_retry_count=publish_retry_count,
        publish_last_attempt_at=publish_last_attempt_at,
    )
    db_session.add(ark_record)
    db_session.flush()

    metadata_record = ARKMetadata(
        ark_record_id=ark_record.id,
        level1_json=_level1_payload(title=f"Title {name}"),
        original_content=f"<raw>{name}</raw>",
        original_schema="dublin_core",
        original_media_type="application/xml",
        level1_cid=f"cid-l1-{name}" if metadata_complete else None,
        original_cid=f"cid-l2-{name}" if metadata_complete else None,
    )
    db_session.add(metadata_record)
    db_session.commit()
    return ark_record, metadata_record


class TestARKPublisher:
    """Test suite for ARKPublisher."""

    def test_publish_single_ark_success(self, db_session):
        ark_record, metadata_record = _create_ark_with_metadata(db_session, name="test")

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock()
        mock_storage = MockMetadataStorage()

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=mock_storage,
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test")

        assert success is True
        db_session.refresh(ark_record)
        db_session.refresh(metadata_record)

        assert ark_record.state == ARKState.PUBLISHED
        assert metadata_record.level1_cid is not None
        assert metadata_record.original_cid is not None

        mock_corelib.create_ark.assert_called_once_with(
            uuid="auth-uuid-123",
            naan="12345",
            name="test",
            url="https://example.com",
            cid=metadata_record.level1_cid,
            fetch_result=False,
        )

        stored_l1_document = mock_storage.get_document(metadata_record.level1_cid)
        parsed_l1 = json.loads(stored_l1_document.content)
        assert stored_l1_document.content_type == "application/json"
        assert parsed_l1["original_metadata"]["cid"] == metadata_record.original_cid
        assert publisher.stats["total_succeeded"] == 1

    def test_publish_single_ark_update_success(self, db_session):
        ark_record, metadata_record = _create_ark_with_metadata(
            db_session,
            name="test-update",
            state=ARKState.UPDATE,
            target="https://example.com/updated",
        )

        mock_corelib = Mock()
        mock_corelib.update_ark = Mock()
        mock_corelib.create_ark = Mock()
        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-update")

        assert success is True
        db_session.refresh(ark_record)
        db_session.refresh(metadata_record)

        assert ark_record.state == ARKState.PUBLISHED
        mock_corelib.update_ark.assert_called_once_with(
            uuid="auth-uuid-123",
            naan="12345",
            name="test-update",
            url="https://example.com/updated",
            cid=metadata_record.level1_cid,
            fetch_result=False,
        )
        mock_corelib.create_ark.assert_not_called()

    def test_publish_single_ark_missing_metadata_record(self, db_session):
        ark_record = ARKRecord(
            naan="12345",
            name="test-no-meta",
            state=ARKState.DRAFT,
            authority_id="auth-uuid-123",
            target="https://example.com",
        )
        db_session.add(ark_record)
        db_session.commit()

        publisher = ARKPublisher(
            corelib_client=Mock(),
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-no-meta")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 1
        assert "No metadata record found in DB" in ark_record.publish_last_error
        assert ark_record.publish_permanently_failed == 1

    def test_publish_single_ark_authority_error(self, db_session):
        ark_record, _ = _create_ark_with_metadata(db_session, name="test-auth-error")

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(
            side_effect=AuthorityError("Failed to get authority: {'code': -32603, 'message': 'Internal error'}")
        )
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-auth-error")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 0
        assert "Authority error (retriable)" in ark_record.publish_last_error
        assert publisher.stats["total_permanent_failures"] == 0

    def test_publish_single_ark_authority_error_max_retries_exceeded(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="test-auth-max-retries",
            publish_retry_count=4,
            metadata_complete=True,
        )

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(
            side_effect=AuthorityError("Failed to get authority key: {'code': -32603, 'message': 'Internal error'}")
        )

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-auth-max-retries")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 5
        assert ark_record.publish_permanently_failed == 1
        assert "Authority error (max retries exceeded, permanent)" in ark_record.publish_last_error
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_authority_not_found_is_permanent(self, db_session):
        ark_record, _ = _create_ark_with_metadata(db_session, name="test-auth-not-found")

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=AuthorityNotFoundError("Authority not found"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-auth-not-found")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1
        assert "Authority error (permanent)" in ark_record.publish_last_error
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_authorization_error_is_permanent(self, db_session):
        ark_record, _ = _create_ark_with_metadata(db_session, name="test-authorization-error")

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=AuthorizationError("NAAN not authorized"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-authorization-error")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1
        assert "Authority error (permanent)" in ark_record.publish_last_error
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_max_retries_exceeded(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="test-max-retries",
            publish_retry_count=4,
            metadata_complete=True,
        )

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=Exception("Network error"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-max-retries")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 5
        assert ark_record.publish_permanently_failed == 1
        assert publisher.stats["total_permanent_failures"] == 1

    def test_run_publish_cycle_processes_batch(self, db_session):
        ark_records = []
        for i in range(3):
            ark_record, _ = _create_ark_with_metadata(db_session, name=f"cycle-{i}")
            ark_records.append(ark_record)

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations = Mock(
            side_effect=lambda uuid, operations, pipeline_size: [
                ARKPublishResult(ref=operation.ref, action=operation.action, status="confirmed")
                for operation in operations
            ]
        )
        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            publisher.run_publish_cycle()

        assert publisher.stats["total_processed"] == 3
        assert publisher.stats["total_succeeded"] == 3
        for ark_record in ark_records:
            db_session.refresh(ark_record)
            assert ark_record.state == ARKState.PUBLISHED

    def test_run_publish_cycle_respects_backoff(self, db_session):
        recent_failure = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
        _create_ark_with_metadata(
            db_session,
            name="backoff",
            publish_retry_count=2,
            publish_last_attempt_at=recent_failure,
        )

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock()
        mock_corelib.publish_ark_operations = Mock()

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            publisher.run_publish_cycle()

        assert publisher.stats["total_processed"] == 0
        mock_corelib.create_ark.assert_not_called()
        mock_corelib.publish_ark_operations.assert_not_called()


class TestSplitWorkers:
    """Tests for the split metadata and chain worker pipeline."""

    def test_metadata_worker_persists_cids_retains_payloads_and_resets_tracking(self, db_session):
        ark_record, metadata_record = _create_ark_with_metadata(
            db_session,
            name="split-metadata",
            publish_retry_count=2,
            publish_last_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        ark_record.publish_last_error = "previous metadata error"
        db_session.commit()

        worker = MetadataPersistenceWorker(
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.persist_single_ark("ark:12345/split-metadata")

        assert success is True
        db_session.refresh(ark_record)
        db_session.refresh(metadata_record)

        assert metadata_record.level1_cid is not None
        assert metadata_record.original_cid is not None
        assert metadata_record.level1_json is not None
        assert metadata_record.original_content is not None
        assert metadata_record.level1_replica_count == 1
        assert metadata_record.level2_replica_count == 1
        assert metadata_record.replication_checked_at is not None
        assert ark_record.publish_retry_count == 0
        assert ark_record.publish_last_error is None
        assert ark_record.publish_last_attempt_at is None
        assert ark_record.publish_permanently_failed == 0

    @pytest.mark.parametrize("concurrency", [1, 2, 4])
    def test_metadata_worker_keeps_level2_before_level1(self, db_session, concurrency):
        _create_ark_with_metadata(db_session, name=f"ordered-{concurrency}")
        storage = MockMetadataStorage()
        worker = MetadataPersistenceWorker(storage, concurrency=concurrency)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            assert worker.persist_single_ark(f"ark:12345/ordered-{concurrency}")

        assert [document.content_type for document in storage.stored.values()] == [
            "application/xml",
            "application/json",
        ]

    def test_metadata_worker_storage_infrastructure_failure_does_not_consume_retry(self, db_session):
        ark_record, _ = _create_ark_with_metadata(db_session, name="metadata-storage-infra")

        failing_storage = MockMetadataStorage()
        failing_storage.store_document = Mock(
            side_effect=StorageError(
                "Store API store failed (500): Storage failed: "
                "Cluster pin registration failed: not enough peers to allocate CID"
            )
        )
        worker = MetadataPersistenceWorker(
            metadata_storage=failing_storage,
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.persist_single_ark("ark:12345/metadata-storage-infra")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 0
        assert ark_record.publish_last_attempt_at is None
        assert ark_record.publish_permanently_failed == 0
        assert "Metadata storage unavailable (infrastructure)" in ark_record.publish_last_error

    def test_reconciliation_selection_respects_recheck_and_page_size(self, db_session):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _, never_checked = _create_ark_with_metadata(
            db_session, name="recheck-never", metadata_complete=True
        )
        _, old_check = _create_ark_with_metadata(
            db_session, name="recheck-old", metadata_complete=True
        )
        _, recent_check = _create_ark_with_metadata(
            db_session, name="recheck-recent", metadata_complete=True
        )
        old_check.replication_checked_at = now - timedelta(seconds=301)
        recent_check.replication_checked_at = now
        db_session.commit()

        records = ARKRepository(db_session).get_metadata_pending_reconciliation(
            limit=2,
            recheck_seconds=300,
        )

        assert [record.ark for record in records] == [
            "ark:12345/recheck-never",
            "ark:12345/recheck-old",
        ]

    def test_reconciliation_requires_purge_target_for_both_levels(self, db_session):
        _, metadata_record = _create_ark_with_metadata(
            db_session,
            name="retain-until-both",
            metadata_complete=True,
        )
        storage = Mock()
        storage.get_replication_status.side_effect = [
            ReplicationStatus("cid-l2-retain-until-both", "pinned", 2, 2, 0, {"site-a": 2}, False),
            ReplicationStatus("cid-l1-retain-until-both", "pinned", 3, 2, 1, {"site-a": 2, "site-b": 1}, True),
        ]
        worker = ReplicationReconciliationWorker(storage, concurrency=1)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            result = worker._reconcile_single_ark("ark:12345/retain-until-both")

        db_session.refresh(metadata_record)
        assert result == {"checked": 1, "repaired": 0, "purged": 0, "failed": 0}
        assert metadata_record.level1_replica_count == 3
        assert metadata_record.level2_replica_count == 2
        assert metadata_record.level1_json is not None
        assert metadata_record.original_content is not None

    def test_reconciliation_purges_only_after_both_targets_are_met(self, db_session):
        _, metadata_record = _create_ark_with_metadata(
            db_session,
            name="purge-both",
            metadata_complete=True,
        )
        storage = Mock()
        storage.get_replication_status.side_effect = [
            ReplicationStatus("cid-l2-purge-both", "pinned", 3, 2, 1, {"site-a": 2, "site-b": 1}, True),
            ReplicationStatus("cid-l1-purge-both", "pinned", 3, 2, 1, {"site-a": 2, "site-b": 1}, True),
        ]
        worker = ReplicationReconciliationWorker(storage, concurrency=1)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            result = worker._reconcile_single_ark("ark:12345/purge-both")

        db_session.refresh(metadata_record)
        assert result["purged"] == 1
        assert result["failed"] == 0
        assert metadata_record.level1_json is None
        assert metadata_record.original_content is None

    def test_reconciliation_rejects_repair_with_different_cid(self, db_session):
        _, metadata_record = _create_ark_with_metadata(
            db_session,
            name="repair-mismatch",
            metadata_complete=True,
        )
        storage = MockMetadataStorage()
        storage.get_replication_status = Mock(return_value=ReplicationStatus(
            "cid-l2-repair-mismatch", "unpinned", 0, 0, 0, {}, False
        ))
        storage.store_document = Mock(return_value="different-cid")
        worker = ReplicationReconciliationWorker(storage, concurrency=1)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            result = worker._reconcile_single_ark("ark:12345/repair-mismatch")

        db_session.refresh(metadata_record)
        assert result["failed"] == 1
        assert result["purged"] == 0
        assert "CID mismatch" in metadata_record.replication_last_error
        assert metadata_record.level1_json is not None
        assert metadata_record.original_content is not None

    def test_reconciliation_rebuilds_zero_copy_level2_with_same_cid(self, db_session):
        _, metadata_record = _create_ark_with_metadata(
            db_session,
            name="repair-same-cid",
            metadata_complete=True,
        )
        storage = Mock()
        storage.store_document.return_value = "cid-l2-repair-same-cid"
        storage.get_replication_status.side_effect = [
            ReplicationStatus("cid-l2-repair-same-cid", "unpinned", 0, 0, 0, {}, False),
            ReplicationStatus("cid-l2-repair-same-cid", "pinned", 1, 1, 0, {"site-a": 1}, False),
            ReplicationStatus("cid-l1-repair-same-cid", "pinned", 1, 1, 0, {"site-a": 1}, False),
        ]
        worker = ReplicationReconciliationWorker(storage, concurrency=1)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            result = worker._reconcile_single_ark("ark:12345/repair-same-cid")

        db_session.refresh(metadata_record)
        assert result == {"checked": 1, "repaired": 1, "purged": 0, "failed": 0}
        assert metadata_record.level2_replica_count == 1
        assert metadata_record.replication_last_error is None

    def test_reconciliation_rebuilds_zero_copy_level1_with_same_cid(self, db_session):
        _, metadata_record = _create_ark_with_metadata(
            db_session,
            name="repair-level1",
            metadata_complete=True,
        )
        storage = Mock()
        storage.store_document.return_value = "cid-l1-repair-level1"
        storage.get_replication_status.side_effect = [
            ReplicationStatus("cid-l2-repair-level1", "pinned", 1, 1, 0, {"site-a": 1}, False),
            ReplicationStatus("cid-l1-repair-level1", "unpinned", 0, 0, 0, {}, False),
            ReplicationStatus("cid-l1-repair-level1", "pinned", 1, 1, 0, {"site-a": 1}, False),
        ]
        worker = ReplicationReconciliationWorker(storage, concurrency=1)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            result = worker._reconcile_single_ark("ark:12345/repair-level1")

        db_session.refresh(metadata_record)
        assert result == {"checked": 1, "repaired": 1, "purged": 0, "failed": 0}
        assert metadata_record.level1_replica_count == 1
        assert metadata_record.replication_last_error is None

    def test_reconciliation_refuses_update_when_cids_change_during_check(self, db_session):
        _, metadata_record = _create_ark_with_metadata(
            db_session,
            name="cid-race",
            metadata_complete=True,
        )
        statuses = [
            ReplicationStatus("cid-l2-cid-race", "pinned", 2, 2, 0, {"site-a": 2}, True),
            ReplicationStatus("cid-l1-cid-race", "pinned", 2, 2, 0, {"site-a": 2}, True),
        ]

        def replication_status(_cid):
            status = statuses.pop(0)
            if not statuses:
                metadata_record.level1_cid = "cid-l1-replaced"
                db_session.commit()
            return status

        storage = Mock()
        storage.get_replication_status.side_effect = replication_status
        worker = ReplicationReconciliationWorker(storage, concurrency=1)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            result = worker._reconcile_single_ark("ark:12345/cid-race")

        db_session.refresh(metadata_record)
        assert result["failed"] == 1
        assert result["purged"] == 0
        assert metadata_record.level1_json is not None
        assert metadata_record.original_content is not None

    def test_chain_worker_publishes_with_purged_metadata(self, db_session):
        ark_record, metadata_record = _create_ark_with_metadata(
            db_session,
            name="split-chain",
            state=ARKState.DRAFT,
        )
        metadata_record.level1_cid = "cid-level-1"
        metadata_record.original_cid = "cid-level-2"
        metadata_record.level1_json = None
        metadata_record.original_content = None
        db_session.commit()

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock()
        mock_corelib.get_ark = Mock(side_effect=AssertionError("get_ark should not run on success"))
        worker = ChainPublisherWorker(
            corelib_client=mock_corelib,
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/split-chain")

        assert success is True
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.PUBLISHED
        mock_corelib.create_ark.assert_called_once_with(
            uuid="auth-uuid-123",
            naan="12345",
            name="split-chain",
            url="https://example.com",
            cid="cid-level-1",
            fetch_result=False,
        )

    def test_chain_worker_groups_by_authority_for_pipeline(self, db_session):
        ark_a, _ = _create_ark_with_metadata(db_session, name="pipeline-a", metadata_complete=True)
        ark_b, _ = _create_ark_with_metadata(
            db_session,
            name="pipeline-b",
            state=ARKState.UPDATE,
            metadata_complete=True,
        )
        ark_c, _ = _create_ark_with_metadata(db_session, name="pipeline-c", metadata_complete=True)
        ark_c.authority_id = "auth-uuid-999"
        db_session.commit()

        mock_corelib = Mock()

        def _publish_operations(uuid, operations, pipeline_size):
            return [
                ARKPublishResult(ref=operation.ref, action=operation.action, status="confirmed")
                for operation in operations
            ]

        mock_corelib.publish_ark_operations = Mock(side_effect=_publish_operations)
        worker = ChainPublisherWorker(
            mock_corelib,
            page_size=7,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle()

        assert mock_corelib.publish_ark_operations.call_count == 2
        first_call = mock_corelib.publish_ark_operations.call_args_list[0]
        second_call = mock_corelib.publish_ark_operations.call_args_list[1]
        assert first_call.kwargs["uuid"] == "auth-uuid-123"
        assert [operation.ref for operation in first_call.kwargs["operations"]] == [
            ark_a.ark,
            ark_b.ark,
        ]
        assert [operation.action for operation in first_call.kwargs["operations"]] == [
            "create",
            "update",
        ]
        assert first_call.kwargs["pipeline_size"] == 7
        assert second_call.kwargs["uuid"] == "auth-uuid-999"
        assert [operation.ref for operation in second_call.kwargs["operations"]] == [ark_c.ark]
        assert worker.stats["total_processed"] == 3
        assert worker.stats["total_succeeded"] == 3

    def test_chain_worker_page_size_is_claim_limit(self, db_session):
        for i in range(3):
            _create_ark_with_metadata(db_session, name=f"page-limit-{i}", metadata_complete=True)

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations.return_value = [
            ARKPublishResult(ref="ark:12345/page-limit-0", action="create", status="confirmed"),
            ARKPublishResult(ref="ark:12345/page-limit-1", action="create", status="confirmed"),
        ]
        worker = ChainPublisherWorker(mock_corelib, page_size=2, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle()

        call = mock_corelib.publish_ark_operations.call_args
        assert len(call.kwargs["operations"]) == 2
        assert call.kwargs["pipeline_size"] == 2
        assert worker.stats["total_processed"] == 2

    def test_chain_worker_effective_page_size_overrides_nominal_page(self, db_session):
        for i in range(3):
            _create_ark_with_metadata(db_session, name=f"effective-page-{i}", metadata_complete=True)

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations.return_value = [
            ARKPublishResult(ref="ark:12345/effective-page-0", action="create", status="confirmed")
        ]
        worker = ChainPublisherWorker(mock_corelib, page_size=20, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle(effective_page_size=1)

        call = mock_corelib.publish_ark_operations.call_args
        assert len(call.kwargs["operations"]) == 1
        assert call.kwargs["pipeline_size"] == 1
        assert worker.stats["last_run_page_size"] == 1
        assert worker.stats["total_processed"] == 1

    def test_chain_claim_skips_delayed_records_before_limit(self, db_session, monkeypatch):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old_created_at = now - timedelta(hours=1)

        for i in range(10):
            ark_record, _ = _create_ark_with_metadata(
                db_session,
                name=f"delayed-{i}",
                publish_retry_count=1,
                publish_last_attempt_at=now,
                metadata_complete=True,
            )
            ark_record.created_at = old_created_at + timedelta(seconds=i)

        for i in range(2):
            ark_record, _ = _create_ark_with_metadata(
                db_session,
                name=f"ready-after-delay-{i}",
                metadata_complete=True,
            )
            ark_record.created_at = old_created_at + timedelta(minutes=10, seconds=i)

        db_session.commit()
        monkeypatch.setattr(db_session.get_bind().dialect, "name", "postgresql")

        records = ARKRepository(db_session).get_chain_pending_publish(
            limit=2,
            max_retries=5,
            backoff_base=2.0,
        )

        assert [record.name for record in records] == [
            "ready-after-delay-0",
            "ready-after-delay-1",
        ]

    def test_chain_worker_pipeline_ambiguous_result_defers_without_retry(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="pipeline-ambiguous",
            metadata_complete=True,
        )

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations.return_value = [
            ARKPublishResult(
                ref="ark:12345/pipeline-ambiguous",
                action="create",
                status="ambiguous",
                error="receipt timeout",
            )
        ]
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle()

        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 0
        assert ark_record.publish_last_attempt_at is None
        assert ark_record.publish_permanently_failed == 0
        assert "reconcile result: missing" in ark_record.publish_last_error

    def test_chain_worker_full_infrastructure_page_records_deferred_count(self, db_session):
        for i in range(2):
            _create_ark_with_metadata(db_session, name=f"infra-page-{i}", metadata_complete=True)

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations.return_value = [
            ARKPublishResult(
                ref=f"ark:12345/infra-page-{i}",
                action="create",
                status="ambiguous",
                error="Failed waiting for receipt: timeout",
            )
            for i in range(2)
        ]
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))
        worker = ChainPublisherWorker(mock_corelib, page_size=2, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle()

        assert worker.stats["last_run_processed"] == 2
        assert worker.stats["last_run_failed"] == 2
        assert worker.stats["last_run_deferred"] == 2

    def test_chain_worker_pipeline_infrastructure_failure_defers_without_retry(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="pipeline-rpc-down",
            metadata_complete=True,
        )

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations.return_value = [
            ARKPublishResult(
                ref="ark:12345/pipeline-rpc-down",
                action="create",
                status="ambiguous",
                error="RPC timeout waiting for receipt",
            )
        ]
        mock_corelib.get_ark = Mock(side_effect=Exception("RPC unavailable"))
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle()

        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 0
        assert ark_record.publish_last_attempt_at is None
        assert ark_record.publish_permanently_failed == 0
        assert "reconcile unavailable" in ark_record.publish_last_error

    def test_chain_worker_pipeline_reverted_result_reconciles_permanent(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="pipeline-reverted",
            metadata_complete=True,
        )

        mock_corelib = Mock()
        mock_corelib.publish_ark_operations.return_value = [
            ARKPublishResult(
                ref="ark:12345/pipeline-reverted",
                action="create",
                status="reverted",
                error="revert",
            )
        ]
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            worker.run_publish_cycle()

        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1

    def test_chain_worker_reconcile_match_marks_published(self, db_session):
        ark_record, metadata_record = _create_ark_with_metadata(
            db_session,
            name="reconcile-match",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=Exception("RPC timeout"))
        mock_corelib.get_ark = Mock(
            return_value=_chain_ark_info(
                name="reconcile-match",
                url="https://example.com",
                cid=metadata_record.level1_cid,
            )
        )
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/reconcile-match")

        assert success is True
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.PUBLISHED
        assert ark_record.publish_retry_count == 0
        mock_corelib.get_ark.assert_called_once_with("12345", "reconcile-match")

    def test_chain_worker_create_mismatch_is_permanent_conflict(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="create-mismatch",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=Exception("RPC timeout"))
        mock_corelib.get_ark = Mock(
            return_value=_chain_ark_info(
                name="create-mismatch",
                url="https://other.example.com",
                cid="different-cid",
            )
        )
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/create-mismatch")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1
        assert "reconcile conflict" in ark_record.publish_last_error

    def test_chain_worker_create_not_found_after_infrastructure_error_defers(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="create-missing",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=Exception("RPC timeout"))
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/create-missing")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 0
        assert ark_record.publish_last_attempt_at is None
        assert ark_record.publish_permanently_failed == 0
        assert "reconcile result: missing" in ark_record.publish_last_error

    def test_chain_worker_create_not_found_after_revert_is_permanent(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="create-reverted",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(
            side_effect=TransactionError("Transaction reverted", tx_hash="0xabc", status=0)
        )
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/create-reverted")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1

    def test_chain_worker_update_mismatch_after_ambiguous_error_is_retryable(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="update-mismatch-retry",
            state=ARKState.UPDATE,
            target="https://example.com/new",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.update_ark = Mock(side_effect=Exception("RPC timeout"))
        mock_corelib.get_ark = Mock(
            return_value=_chain_ark_info(
                name="update-mismatch-retry",
                url="https://example.com/old",
                cid="old-cid",
            )
        )
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/update-mismatch-retry")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.UPDATE
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 0

    def test_chain_worker_update_mismatch_after_revert_is_permanent(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="update-mismatch-permanent",
            state=ARKState.UPDATE,
            target="https://example.com/new",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.update_ark = Mock(
            side_effect=TransactionError("Transaction reverted", tx_hash="0xdef", status=0)
        )
        mock_corelib.get_ark = Mock(
            return_value=_chain_ark_info(
                name="update-mismatch-permanent",
                url="https://example.com/old",
                cid="old-cid",
            )
        )
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/update-mismatch-permanent")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.UPDATE
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1

    def test_chain_worker_reconcile_read_failure_uses_original_classification(self, db_session):
        ark_record, _ = _create_ark_with_metadata(
            db_session,
            name="reconcile-read-failure",
            metadata_complete=True,
        )
        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=AuthorityNotFoundError("Authority not found"))
        mock_corelib.get_ark = Mock(side_effect=Exception("read unavailable"))
        worker = ChainPublisherWorker(mock_corelib, page_size=10, max_retries=5, backoff_base=2.0)

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = worker.publish_single_ark("ark:12345/reconcile-read-failure")

        assert success is False
        db_session.refresh(ark_record)
        assert ark_record.publish_retry_count == 1
        assert ark_record.publish_permanently_failed == 1
        assert "reconcile unavailable" in ark_record.publish_last_error
