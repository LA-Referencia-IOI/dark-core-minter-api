"""
Unit tests for ARK publisher worker - no database dependencies.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest

from dark_orchestrator.exceptions import AuthorityError

from app.workers.publisher import ARKPublisher
from app.models.states import ARKState
from app.storage.exceptions import StorageError


class MockMetadataStorage:
    """Mock metadata storage for testing."""
    
    def __init__(self, should_fail=False):
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
    """Mock ARK record for testing."""
    
    def __init__(self, naan, name, authority_id, target, metadata_cid=None, metadata_format=None):
        self.naan = naan
        self.name = name
        self.authority_id = authority_id
        self.target = target
        self.metadata_cid = metadata_cid  # Now required for DRAFT state
        self.metadata_format = metadata_format or "json"
        self.state = ARKState.DRAFT
        self.publish_retry_count = 0
        self.publish_last_error = None
        self.publish_last_attempt_at = None
        self.publish_permanently_failed = 0
    
    @property
    def ark(self):
        """Compute full ARK identifier from naan and name."""
        return f"ark:{self.naan}/{self.name}"


class TestARKPublisherUnit:
    """Unit tests for ARK publisher worker."""
    
    def test_publish_single_ark_success(self):
        """Test successful ARK publication."""
        # Create mock ARK with pre-stored metadata CID
        ark_record = MockARKRecord(
            naan="12345",
            name="test",
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_cid="pre_stored_cid",
            metadata_format="json",
        )
        
        # Mock repository
        mock_repo = Mock()
        mock_repo.get_by_ark.return_value = ark_record
        mock_repo.mark_publish_succeeded = Mock()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        mock_orchestrator.create_ark = Mock()
        
        # Mock storage (not used since metadata_cid already exists)
        mock_storage = MockMetadataStorage()
        
        # Create publisher
        publisher = ARKPublisher(
            orchestrator=mock_orchestrator,
            metadata_storage=mock_storage,
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Mock SessionLocal and repository factory
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session = Mock()
            mock_session_local.return_value = mock_session
            
            with patch('app.workers.publisher.ARKRepository') as mock_repo_class:
                mock_repo_class.return_value = mock_repo
                
                # Publish
                success = publisher.publish_single_ark("ark:12345/test")
        
        # Verify success
        assert success is True
        
        # Verify orchestrator was called
        assert mock_orchestrator.create_ark.called
        
        # Verify stats updated
        assert publisher.stats["total_succeeded"] == 1
    
    def test_publish_single_ark_no_metadata_cid(self):
        """Test ARK publication fails gracefully when metadata_cid is missing."""
        # Create mock ARK without metadata_cid
        ark_record = MockARKRecord(
            naan="12345",
            name="test",
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_cid=None,  # Missing
            metadata_format="json",
        )
        
        # Mock repository
        mock_repo = Mock()
        mock_repo.get_by_ark.return_value = ark_record
        mock_repo.mark_publish_failed = Mock()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        
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
        
        # Mock SessionLocal and repository
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session = Mock()
            mock_session_local.return_value = mock_session
            
            with patch('app.workers.publisher.ARKRepository') as mock_repo_class:
                mock_repo_class.return_value = mock_repo
                
                # Publish
                success = publisher.publish_single_ark("ark:12345/test")
        
        # Verify failure
        assert success is False
    
    def test_publish_single_ark_authority_error(self):
        """Test ARK publication with authority error (permanent failure)."""
        # Create mock ARK
        ark_record = MockARKRecord(
            naan="12345",
            name="test",
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_cid="pre_stored_cid",
            metadata_format="json",
        )
        
        # Mock repository
        mock_repo = Mock()
        mock_repo.get_by_ark.return_value = ark_record
        mock_repo.mark_publish_failed = Mock()
        
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
        
        # Mock SessionLocal and repository
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session = Mock()
            mock_session_local.return_value = mock_session
            
            with patch('app.workers.publisher.ARKRepository') as mock_repo_class:
                mock_repo_class.return_value = mock_repo
                
                # Publish
                success = publisher.publish_single_ark("ark:12345/test")
        
        # Verify failure
        assert success is False
        
        # Verify mark_publish_failed was called with permanent=True
        call_args = mock_repo.mark_publish_failed.call_args
        assert call_args is not None
        assert call_args[1].get('is_permanent') is True
    
    def test_publish_single_ark_max_retries(self):
        """Test ARK publication after max retries exceeded."""
        # Create mock ARK with max retries already exceeded
        ark_record = MockARKRecord(
            naan="12345",
            name="test",
            authority_id="auth-uuid-123",
            target="https://example.com",
            metadata_cid="pre_stored_cid",
            metadata_format="json",
        )
        ark_record.publish_retry_count = 5
        ark_record.publish_last_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # Mock repository
        mock_repo = Mock()
        mock_repo.get_by_ark.return_value = ark_record
        mock_repo.mark_publish_failed = Mock()
        
        # Mock orchestrator
        mock_orchestrator = Mock()
        mock_orchestrator.create_ark = Mock(side_effect=Exception("Network error"))
        
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
        
        # Mock SessionLocal and repository
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session = Mock()
            mock_session_local.return_value = mock_session
            
            with patch('app.workers.publisher.ARKRepository') as mock_repo_class:
                mock_repo_class.return_value = mock_repo
                
                # Publish - should fail due to max retries
                success = publisher.publish_single_ark("ark:12345/test")
        
        # Verify failure
        assert success is False
    
    def test_run_publish_cycle_processes_batch(self):
        """Test that run_publish_cycle processes a batch of ARKs."""
        # Create mock ARKs with pre-stored metadata CIDs
        ark1 = MockARKRecord(
            naan="12345",
            name="test1",
            authority_id="auth-uuid-1",
            target="https://example.com/1",
            metadata_cid="cid_1",
            metadata_format="json",
        )
        ark2 = MockARKRecord(
            naan="12345",
            name="test2",
            authority_id="auth-uuid-2",
            target="https://example.com/2",
            metadata_cid="cid_2",
            metadata_format="json",
        )
        
        # Mock repository
        mock_repo = Mock()
        mock_repo.get_drafts_pending_publish.return_value = [ark1, ark2]
        mock_repo.get_by_ark.side_effect = lambda ark: ark1 if ark == ark1.ark else ark2
        mock_repo.mark_publish_succeeded = Mock()
        
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
        
        # Mock SessionLocal and repository
        with patch('app.workers.publisher.SessionLocal') as mock_session_local:
            mock_session = Mock()
            mock_session_local.return_value = mock_session
            
            with patch('app.workers.publisher.ARKRepository') as mock_repo_class:
                mock_repo_class.return_value = mock_repo
                
                # Run cycle
                publisher.run_publish_cycle()
        
        # Verify batch was processed
        assert mock_repo.get_drafts_pending_publish.called
        assert publisher.stats["total_processed"] == 2
    
    def test_storage_health_check(self):
        """Test metadata storage health check."""
        storage = MockMetadataStorage()
        
        # Should be healthy
        assert storage.health_check() is True
        
        # Fail storage
        storage.should_fail = True
        assert storage.health_check() is False
    
    def test_publisher_statistics(self):
        """Test publisher statistics tracking."""
        # Create publisher
        publisher = ARKPublisher(
            orchestrator=Mock(),
            metadata_storage=MockMetadataStorage(),
            batch_size=10,
            max_retries=5,
            backoff_base=2.0,
        )
        
        # Verify initial stats
        assert publisher.stats["total_succeeded"] == 0
        assert publisher.stats["total_failed"] == 0
        assert publisher.stats["total_permanent_failures"] == 0
