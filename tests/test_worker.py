"""
Tests for ARK publisher worker.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

from dark_orchestrator.exceptions import AuthorityError

from app.workers.publisher import ARKPublisher
from app.database.models import ARKRecord
from app.models.states import ARKState
from app.storage.exceptions import StorageError


class MockMetadataStorage:
    """Mock metadata storage for testing."""
    
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.stored = {}
    
    def store_metadata(self, metadata):
        if self.should_fail:
            raise StorageError("Mock storage error")
        cid = f"mock_cid_{len(self.stored)}"
        self.stored[cid] = metadata
        return cid
    
    def get_metadata(self, cid):
        return self.stored.get(cid)
    
    def health_check(self):
        return not self.should_fail


class MockARKRepository:
    """Mock ARK repository for unit testing."""
    
    def __init__(self):
        self.arks = {}
        self.call_count = 0
    
    def get_drafts_pending_publish(self, limit, max_retries, backoff_base):
        """Return DRAFT ARKs that should be published."""
        return list(self.arks.values())[:limit]
    
    def get_by_ark(self, ark):
        """Get ARK by identifier."""
        return self.arks.get(ark)
    
    def mark_publish_succeeded(self, ark, metadata_cid):
        """Mark ARK as published."""
        if ark in self.arks:
            self.arks[ark]['state'] = ARKState.PUBLISHED
            self.arks[ark]['metadata_cid'] = metadata_cid
    
    def mark_publish_failed(self, ark, error_message, is_permanent=False):
        """Mark ARK publication as failed."""
        if ark in self.arks:
            self.arks[ark]['publish_retry_count'] = self.arks[ark].get('publish_retry_count', 0) + 1
            self.arks[ark]['publish_last_error'] = error_message
            self.arks[ark]['publish_permanently_failed'] = 1 if is_permanent else 0


class TestARKPublisher:
    """Test suite for ARK publisher worker."""
    
    def test_publish_single_ark_success(self, db_session):
        """Test successful ARK publication."""
        # Create a DRAFT ARK
        ark_record = ARKRecord(
            ark="ark:/12345/test",
            naan="12345",
            name="test",
            state=ARKState.DRAFT,
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_json={"title": "Test ARK"},
        )
        db_session.add(ark_record)
        db_session.commit()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        mock_orchestrator.create_ark = Mock()
        
        # Mock storage
        mock_storage = MockMetadataStorage()
        
        # Create publisher
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal to return our test session
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session_local.return_value = db_session
            
            # Publish
            success = publisher.publish_single_ark("ark:/12345/test")
        
        # Verify success
        assert success is True
        
        # Verify orchestrator was called
        mock_orchestrator.create_ark.assert_called_once_with(
            uuid="auth-uuid-123",
            naan="12345",
            name="test",
            url="https://example.com",
            cid="mock_cid_0",
        )
        
        # Verify ARK state updated to PUBLISHED
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.PUBLISHED
        assert ark_record.metadata_cid == "mock_cid_0"
        assert publisher.stats["total_succeeded"] == 1
    
    def test_publish_single_ark_storage_failure(self, db_session):
        """Test ARK publication with storage failure."""
        # Create a DRAFT ARK
        ark_record = ARKRecord(
            ark="ark:/12345/test",
            naan="12345",
            name="test",
            state=ARKState.DRAFT,
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_json={"title": "Test ARK"},
        )
        db_session.add(ark_record)
        db_session.commit()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        
        # Mock storage that fails
        mock_storage = MockMetadataStorage(should_fail=True)
        
        # Create publisher
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session_local.return_value = db_session
            
            # Publish
            success = publisher.publish_single_ark("ark:/12345/test")
        
        # Verify failure
        assert success is False
        
        # Verify ARK still in DRAFT state with error tracking
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 1
        assert "Metadata storage failed" in ark_record.publish_last_error
        assert ark_record.publish_last_attempt_at is not None
        # total_failed is tracked by run_publish_cycle, not publish_single_ark.
        assert publisher.stats["total_failed"] == 0
    
    def test_publish_single_ark_authority_error(self, db_session):
        """Test ARK publication with authority error (permanent failure)."""
        # Create a DRAFT ARK
        ark_record = ARKRecord(
            ark="ark:/12345/test",
            naan="12345",
            name="test",
            state=ARKState.DRAFT,
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_json={"title": "Test ARK"},
        )
        db_session.add(ark_record)
        db_session.commit()
        
        # Mock orchestrator that raises AuthorityError
        mock_orchestrator = Mock()
        mock_orchestrator.create_ark = Mock(side_effect=AuthorityError("Unauthorized"))
        
        # Mock storage
        mock_storage = MockMetadataStorage()
        
        # Create publisher
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session_local.return_value = db_session
            
            # Publish
            success = publisher.publish_single_ark("ark:/12345/test")
        
        # Verify failure
        assert success is False
        
        # Verify ARK marked as permanently failed
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 1
        assert "Authority error (permanent)" in ark_record.publish_last_error
        assert ark_record.publish_permanently_failed == 1
        assert publisher.stats["total_permanent_failures"] == 1
    
    def test_publish_single_ark_max_retries_exceeded(self, db_session):
        """Test ARK publication with max retries exceeded."""
        # Create a DRAFT ARK with retries near max
        ark_record = ARKRecord(
            ark="ark:/12345/test",
            naan="12345",
            name="test",
            state=ARKState.DRAFT,
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_json={"title": "Test ARK"},
            publish_retry_count=4,  # One before max
        )
        db_session.add(ark_record)
        db_session.commit()
        
        # Mock orchestrator that raises generic error
        mock_orchestrator = Mock()
        mock_orchestrator.create_ark = Mock(side_effect=Exception("Network error"))
        
        # Mock storage
        mock_storage = MockMetadataStorage()
        
        # Create publisher with max_retries=5
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session_local.return_value = db_session
            
            # Publish
            success = publisher.publish_single_ark("ark:/12345/test")
        
        # Verify failure
        assert success is False
        
        # Verify ARK marked as permanently failed after reaching max retries
        db_session.refresh(ark_record)
        assert ark_record.state == ARKState.DRAFT
        assert ark_record.publish_retry_count == 5
        assert ark_record.publish_permanently_failed == 1
        assert publisher.stats["total_permanent_failures"] == 1
    
    def test_run_publish_cycle_processes_batch(self, db_session):
        """Test publish cycle processes multiple ARKs."""
        # Create multiple DRAFT ARKs
        for i in range(3):
            ark_record = ARKRecord(
                ark=f"ark:/12345/test{i}",
                naan="12345",
                name=f"test{i}",
                state=ARKState.DRAFT,
                authority_id="auth-uuid-123",
                target=f"https://example.com/{i}",
                metadata_json={"title": f"Test ARK {i}"},
            )
            db_session.add(ark_record)
        db_session.commit()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        mock_orchestrator.create_ark = Mock()
        
        # Mock storage
        mock_storage = MockMetadataStorage()
        
        # Create publisher
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session_local.return_value = db_session
            
            # Run cycle
            publisher.run_publish_cycle()
        
        # Verify all ARKs processed
        assert publisher.stats["total_processed"] == 3
        assert publisher.stats["total_succeeded"] == 3
        assert publisher.stats["last_run_at"] is not None
        assert publisher.stats["last_run_duration"] is not None
    
    def test_run_publish_cycle_respects_backoff(self, db_session):
        """Test publish cycle respects exponential backoff."""
        # Create ARK with recent failure
        recent_failure = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
        ark_record = ARKRecord(
            ark="ark:/12345/test",
            naan="12345",
            name="test",
            state=ARKState.DRAFT,
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_json={"title": "Test ARK"},
            publish_retry_count=2,
            publish_last_attempt_at=recent_failure,
        )
        db_session.add(ark_record)
        db_session.commit()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        
        # Mock storage
        mock_storage = MockMetadataStorage()
        
        # Create publisher with backoff_base=2.0
        # With retry_count=2, backoff should be 2^2 * 60 = 240 seconds
        # Since only 30 seconds have passed, it should not be processed
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session_local.return_value = db_session
            
            # Run cycle
            publisher.run_publish_cycle()
        
        # Verify ARK was NOT processed (due to backoff)
        assert publisher.stats["total_processed"] == 0
