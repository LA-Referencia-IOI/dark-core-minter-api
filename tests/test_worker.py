"""
Integration-style tests for ARK publisher worker with a real test DB session.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from dark_core_lib.exceptions import AuthorityError

from app.database.models import ARKMetadata, ARKRecord
from app.models.states import ARKState
from app.storage.exceptions import StorageError
from app.workers.publisher import ARKPublisher


class MockMetadataStorage:
    """Mock metadata storage backend for worker tests."""

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


def _level1_payload(title: str = "Test Title", year: int = 2024, schema: str = "dublin_core") -> dict:
    return {
        "title": title,
        "authors": ["Test Author"],
        "year": year,
        "original_metadata": {"schema": schema, "cid": None},
    }


def _create_ark_with_metadata(
    db_session,
    *,
    name: str,
    state: ARKState = ARKState.DRAFT,
    target: str = "https://example.com",
    publish_retry_count: int = 0,
    publish_last_attempt_at=None,
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
            batch_size=10,
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
        )

        stored_l1_content, stored_l1_format = mock_storage.get_metadata(metadata_record.level1_cid)
        parsed_l1 = json.loads(stored_l1_content)
        assert stored_l1_format == "json"
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
            batch_size=10,
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
            batch_size=10,
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
        mock_corelib.create_ark = Mock(side_effect=AuthorityError("Unauthorized"))

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            success = publisher.publish_single_ark("ark:12345/test-auth-error")

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
        )

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
        mock_corelib.create_ark = Mock()
        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
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

        publisher = ARKPublisher(
            corelib_client=mock_corelib,
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )

        with patch("app.workers.publisher.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session
            publisher.run_publish_cycle()

        assert publisher.stats["total_processed"] == 0
        mock_corelib.create_ark.assert_not_called()
