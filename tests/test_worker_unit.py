"""
Unit tests for ARKPublisher using mocks only (no real DB).
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from dark_core_lib.exceptions import ARKNotFoundError, AuthorityError, AuthorityNotFoundError, AuthorizationError
from dark_core_lib.metadata import StorageError, StoredDocument
from dark_core_lib.models import ARKPublishResult

from app.models.states import ARKState
from app.workers.publisher import ARKPublisher, MetadataPersistenceWorker, _utc_now


class MockMetadataStorage:
    """In-memory mock metadata storage."""

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


class MockARKRecord:
    """Minimal ARK record shape consumed by ARKPublisher."""

    def __init__(
        self,
        *,
        ark_id: int,
        naan: str,
        name: str,
        state: ARKState,
        authority_id: str = "auth-uuid-123",
        target: str = "https://example.com",
        publish_retry_count: int = 0,
    ):
        self.id = ark_id
        self.naan = naan
        self.name = name
        self.state = state
        self.authority_id = authority_id
        self.target = target
        self.publish_retry_count = publish_retry_count

    @property
    def ark(self):
        return f"ark:{self.naan}/{self.name}"


class MockMetadataRecord:
    """Minimal metadata record shape consumed by ARKPublisher."""

    def __init__(
        self,
        *,
        level1_json: dict = None,
        original_content: str = "<raw>metadata</raw>",
        original_schema: str = "dublin_core",
        original_media_type: str = "application/xml",
        level1_cid: str = None,
        original_cid: str = None,
    ):
        self.level1_json = level1_json or {
            "title": "My Resource",
            "authors": ["Doe, Jane"],
            "year": 2024,
            "original_metadata": {
                "schema": original_schema,
                "media_type": original_media_type,
                "cid": None,
            },
        }
        self.original_content = original_content
        self.original_schema = original_schema
        self.original_media_type = original_media_type
        self.level1_cid = level1_cid
        self.original_cid = original_cid
        self.level1_replica_count = 0
        self.level2_replica_count = 0
        self.replication_checked_at = None
        self.replication_last_error = None


def _build_repo(ark_record, metadata_record):
    repo = Mock()
    repo.get_by_ark.return_value = ark_record
    repo.get_metadata_by_ark_id.return_value = metadata_record

    def _update_metadata_cids(
        ark_record_id,
        level1_cid=None,
        level2_cid=None,
        level1_replica_count=None,
        level2_replica_count=None,
        purge_local=False,
        reset_publish_tracking=False,
    ):
        if metadata_record is None:
            return
        if level1_cid is not None:
            metadata_record.level1_cid = level1_cid
        if level2_cid is not None:
            metadata_record.original_cid = level2_cid
        if level1_replica_count is not None:
            metadata_record.level1_replica_count = level1_replica_count
        if level2_replica_count is not None:
            metadata_record.level2_replica_count = level2_replica_count
        if purge_local:
            metadata_record.level1_json = None
            metadata_record.original_content = None
        if reset_publish_tracking:
            ark_record.publish_retry_count = 0

    repo.update_metadata_cids = Mock(side_effect=_update_metadata_cids)
    repo.update_to_published = Mock()
    repo.mark_publish_failed = Mock()
    repo.defer_publish_retry = Mock()
    repo.get_drafts_pending_publish.return_value = []
    repo.get_metadata_pending_persist.return_value = []
    repo.get_chain_pending_publish.return_value = []
    return repo


class TestARKPublisherUnit:
    """Unit tests for ARKPublisher."""

    def test_publish_single_ark_success(self):
        ark_record = MockARKRecord(ark_id=1, naan="12345", name="test", state=ARKState.DRAFT)
        metadata_record = MockMetadataRecord()
        mock_repo = _build_repo(ark_record, metadata_record)

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
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/test")

        assert success is True
        mock_repo.update_metadata_cids.assert_called_once()
        mock_repo.update_to_published.assert_called_once()
        mock_corelib.create_ark.assert_called_once()
        assert publisher.stats["total_succeeded"] == 1

    def test_publish_single_ark_update_calls_update_ark(self):
        ark_record = MockARKRecord(ark_id=2, naan="12345", name="update", state=ARKState.UPDATE)
        metadata_record = MockMetadataRecord(
            level1_cid="cid-level-1",
            original_cid="cid-level-2",
        )
        mock_repo = _build_repo(ark_record, metadata_record)

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
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/update")

        assert success is True
        mock_corelib.update_ark.assert_called_once()
        mock_corelib.create_ark.assert_not_called()

    def test_publish_single_ark_no_metadata_record(self):
        ark_record = MockARKRecord(ark_id=3, naan="12345", name="no-meta", state=ARKState.DRAFT)
        mock_repo = _build_repo(ark_record, metadata_record=None)

        publisher = ARKPublisher(
            corelib_client=Mock(),
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/no-meta")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is True

    def test_publish_single_ark_authority_error(self):
        ark_record = MockARKRecord(ark_id=4, naan="12345", name="auth-error", state=ARKState.DRAFT)
        metadata_record = MockMetadataRecord(
            level1_cid="cid-level-1",
            original_cid="cid-level-2",
        )
        mock_repo = _build_repo(ark_record, metadata_record)

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=AuthorityError("Unauthorized"))
        mock_corelib.get_ark = Mock(side_effect=ARKNotFoundError("not found"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/auth-error")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is False
        assert "Authority error (retriable)" in mock_repo.mark_publish_failed.call_args.args[1]
        assert publisher.stats["total_permanent_failures"] == 0

    def test_publish_single_ark_authority_error_max_retries(self):
        ark_record = MockARKRecord(
            ark_id=40,
            naan="12345",
            name="auth-max-retries",
            state=ARKState.DRAFT,
            publish_retry_count=4,
        )
        metadata_record = MockMetadataRecord(
            level1_cid="cid-level-1",
            original_cid="cid-level-2",
        )
        mock_repo = _build_repo(ark_record, metadata_record)

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=AuthorityError("RPC internal error"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/auth-max-retries")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is True
        assert "Authority error (max retries exceeded, permanent)" in mock_repo.mark_publish_failed.call_args.args[1]
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_authority_not_found_is_permanent(self):
        ark_record = MockARKRecord(ark_id=41, naan="12345", name="auth-not-found", state=ARKState.DRAFT)
        metadata_record = MockMetadataRecord(
            level1_cid="cid-level-1",
            original_cid="cid-level-2",
        )
        mock_repo = _build_repo(ark_record, metadata_record)

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
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/auth-not-found")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is True
        assert "Authority error (permanent)" in mock_repo.mark_publish_failed.call_args.args[1]
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_authorization_error_is_permanent(self):
        ark_record = MockARKRecord(ark_id=42, naan="12345", name="authorization-error", state=ARKState.DRAFT)
        metadata_record = MockMetadataRecord()
        mock_repo = _build_repo(ark_record, metadata_record)

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
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/authorization-error")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is True
        assert "Authority error (permanent)" in mock_repo.mark_publish_failed.call_args.args[1]
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_max_retries(self):
        ark_record = MockARKRecord(
            ark_id=5,
            naan="12345",
            name="max-retries",
            state=ARKState.DRAFT,
            publish_retry_count=4,
        )
        metadata_record = MockMetadataRecord(
            level1_cid="cid-level-1",
            original_cid="cid-level-2",
        )
        mock_repo = _build_repo(ark_record, metadata_record)

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
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/max-retries")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is True

    def test_run_publish_cycle_processes_batch(self):
        draft_1 = MockARKRecord(ark_id=10, naan="12345", name="batch-1", state=ARKState.DRAFT)
        draft_2 = MockARKRecord(ark_id=11, naan="12345", name="batch-2", state=ARKState.DRAFT)
        metadata_1 = MockMetadataRecord()
        metadata_2 = MockMetadataRecord()

        by_ark = {
            draft_1.ark: draft_1,
            draft_2.ark: draft_2,
        }
        by_id = {
            draft_1.id: metadata_1,
            draft_2.id: metadata_2,
        }

        mock_repo = Mock()
        mock_repo.get_metadata_pending_persist.return_value = [
            SimpleNamespace(ark=draft_1.ark),
            SimpleNamespace(ark=draft_2.ark),
        ]
        mock_repo.get_chain_pending_publish.return_value = [
            draft_1,
            draft_2,
        ]
        mock_repo.get_by_ark.side_effect = lambda ark: by_ark.get(ark)
        mock_repo.get_metadata_by_ark_id.side_effect = lambda ark_id: by_id.get(ark_id)

        def _update_metadata_cids(
            ark_record_id,
            level1_cid=None,
            level2_cid=None,
            level1_replica_count=None,
            level2_replica_count=None,
            purge_local=False,
            reset_publish_tracking=False,
        ):
            metadata = by_id.get(ark_record_id)
            if metadata is None:
                return
            if level1_cid is not None:
                metadata.level1_cid = level1_cid
            if level2_cid is not None:
                metadata.original_cid = level2_cid
            if level1_replica_count is not None:
                metadata.level1_replica_count = level1_replica_count
            if level2_replica_count is not None:
                metadata.level2_replica_count = level2_replica_count
            if purge_local:
                metadata.level1_json = None
                metadata.original_content = None

        mock_repo.update_metadata_cids = Mock(side_effect=_update_metadata_cids)
        mock_repo.update_to_published = Mock()
        mock_repo.mark_publish_failed = Mock()

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
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                publisher.run_publish_cycle()

        assert publisher.stats["total_processed"] == 2
        assert publisher.stats["total_succeeded"] == 2
        mock_corelib.publish_ark_operations.assert_called_once()

    def test_storage_health_check(self):
        storage = MockMetadataStorage()
        assert storage.health_check() is True

        storage.should_fail = True
        assert storage.health_check() is False

    def test_publisher_statistics(self):
        publisher = ARKPublisher(
            corelib_client=Mock(),
            metadata_storage=MockMetadataStorage(),
            page_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        assert publisher.stats["total_succeeded"] == 0
        assert publisher.stats["total_failed"] == 0
        assert publisher.stats["total_permanent_failures"] == 0


class TestMetadataReconciliationScheduling:
    def _run_cycle(self, records, last_reconciliation_at=None):
        worker = MetadataPersistenceWorker(MockMetadataStorage(), concurrency=1)
        worker.stats["last_reconciliation_at"] = last_reconciliation_at
        repo = Mock()
        repo.get_metadata_pending_persist.return_value = records
        db = Mock()
        session_factory = Mock(return_value=db)
        with patch("app.workers.publisher.SessionLocal", return_value=session_factory):
            with patch("app.workers.publisher.ARKRepository", return_value=repo):
                with patch.object(worker, "_process_ark_batch") as process:
                    with patch.object(worker, "_run_reconciliation_cycle") as reconcile:
                        worker.run_publish_cycle()
        return process, reconcile

    def test_reconciles_immediately_when_ingestion_is_idle(self):
        process, reconcile = self._run_cycle([], last_reconciliation_at=_utc_now())
        process.assert_called_once_with([])
        reconcile.assert_called_once_with()

    def test_reconciles_after_five_minutes_under_continuous_load(self):
        from datetime import timedelta

        process, reconcile = self._run_cycle(
            [SimpleNamespace(ark="ark:12345/pending")],
            last_reconciliation_at=_utc_now() - timedelta(seconds=301),
        )
        process.assert_called_once_with(["ark:12345/pending"])
        reconcile.assert_called_once_with()
