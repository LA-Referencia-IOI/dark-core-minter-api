"""
Unit tests for ARKPublisher using mocks only (no real DB).
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from dark_core_lib.exceptions import AuthorityError

from app.models.states import ARKState
from app.storage.exceptions import StorageError
from app.workers.publisher import ARKPublisher


class MockMetadataStorage:
    """In-memory mock metadata storage."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.stored = {}

    def store_metadata(self, content, format):
        if self.should_fail:
            raise StorageError("Mock storage error")
        cid = f"mock_cid_{len(self.stored)}"
        self.stored[cid] = (content, format)
        return cid

    def get_metadata(self, cid):
        return self.stored.get(cid, (None, None))

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
        level1_cid: str = None,
        original_cid: str = None,
    ):
        self.level1_json = level1_json or {
            "title": "My Resource",
            "authors": ["Doe, Jane"],
            "year": 2024,
            "original_metadata": {"schema": original_schema, "cid": None},
        }
        self.original_content = original_content
        self.original_schema = original_schema
        self.level1_cid = level1_cid
        self.original_cid = original_cid


def _build_repo(ark_record, metadata_record):
    repo = Mock()
    repo.get_by_ark.return_value = ark_record
    repo.get_metadata_by_ark_id.return_value = metadata_record
    repo.update_metadata_cids = Mock()
    repo.update_to_published = Mock()
    repo.mark_publish_failed = Mock()
    repo.get_drafts_pending_publish.return_value = []
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
            batch_size=10,
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
        metadata_record = MockMetadataRecord()
        mock_repo = _build_repo(ark_record, metadata_record)

        mock_corelib = Mock()
        mock_corelib.update_ark = Mock()
        mock_corelib.create_ark = Mock()

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
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
            batch_size=10,
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
        metadata_record = MockMetadataRecord()
        mock_repo = _build_repo(ark_record, metadata_record)

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=AuthorityError("Unauthorized"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                success = publisher.publish_single_ark("ark:12345/auth-error")

        assert success is False
        mock_repo.mark_publish_failed.assert_called_once()
        assert mock_repo.mark_publish_failed.call_args.kwargs["is_permanent"] is True
        assert publisher.stats["total_permanent_failures"] == 1

    def test_publish_single_ark_max_retries(self):
        ark_record = MockARKRecord(
            ark_id=5,
            naan="12345",
            name="max-retries",
            state=ARKState.DRAFT,
            publish_retry_count=4,
        )
        metadata_record = MockMetadataRecord()
        mock_repo = _build_repo(ark_record, metadata_record)

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock(side_effect=Exception("Network error"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
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
        mock_repo.get_drafts_pending_publish.return_value = [
            SimpleNamespace(ark=draft_1.ark),
            SimpleNamespace(ark=draft_2.ark),
        ]
        mock_repo.get_by_ark.side_effect = lambda ark: by_ark.get(ark)
        mock_repo.get_metadata_by_ark_id.side_effect = lambda ark_id: by_id.get(ark_id)
        mock_repo.update_metadata_cids = Mock()
        mock_repo.update_to_published = Mock()
        mock_repo.mark_publish_failed = Mock()

        mock_corelib = Mock()
        mock_corelib.create_ark = Mock()

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = Mock()
            with patch("app.workers.publisher.ARKRepository", return_value=mock_repo):
                publisher.run_publish_cycle()

        assert publisher.stats["total_processed"] == 2
        assert publisher.stats["total_succeeded"] == 2

    def test_storage_health_check(self):
        storage = MockMetadataStorage()
        assert storage.health_check() is True

        storage.should_fail = True
        assert storage.health_check() is False

    def test_publisher_statistics(self):
        publisher = ARKPublisher(
            corelib_client=Mock(),
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        assert publisher.stats["total_succeeded"] == 0
        assert publisher.stats["total_failed"] == 0
        assert publisher.stats["total_permanent_failures"] == 0
