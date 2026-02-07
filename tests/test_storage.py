"""
Tests for metadata storage implementations.
"""

import os
import pytest
import tempfile
import shutil
from pathlib import Path

from app.storage.filesystem import FileSystemMetadataStorage
from app.storage.exceptions import StorageError, MetadataNotFoundError


class TestFileSystemMetadataStorage:
    """Tests for FileSystemMetadataStorage."""
    
    @pytest.fixture
    def temp_storage_path(self):
        """Create a temporary directory for storage tests."""
        temp_dir = tempfile.mkdtemp(prefix="test_storage_")
        yield temp_dir
        # Cleanup after tests
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
    
    @pytest.fixture
    def storage(self, temp_storage_path):
        """Create a FileSystemMetadataStorage instance."""
        return FileSystemMetadataStorage(storage_path=temp_storage_path)
    
    def test_init_creates_directory(self, temp_storage_path):
        """Test that initialization creates the storage directory."""
        new_path = os.path.join(temp_storage_path, "nested", "storage")
        storage = FileSystemMetadataStorage(storage_path=new_path)
        assert os.path.exists(new_path)
    
    def test_store_metadata_returns_cid(self, storage):
        """Test that store_metadata returns a valid CID."""
        metadata = {"title": "Test Document", "author": "Test Author"}
        cid = storage.store_metadata(metadata)
        
        assert cid is not None
        assert len(cid) == 32  # MD5 hash length
        assert cid.isalnum()  # Hexadecimal string
    
    def test_store_metadata_creates_file(self, storage, temp_storage_path):
        """Test that store_metadata creates a JSON file."""
        metadata = {"key": "value"}
        cid = storage.store_metadata(metadata)
        
        file_path = Path(temp_storage_path) / f"{cid}.json"
        assert file_path.exists()
    
    def test_store_metadata_deterministic(self, storage):
        """Test that same metadata produces same CID."""
        metadata = {"a": 1, "b": 2, "c": 3}
        
        cid1 = storage.store_metadata(metadata)
        cid2 = storage.store_metadata(metadata)
        
        assert cid1 == cid2
    
    def test_store_metadata_key_order_independent(self, storage):
        """Test that key order doesn't affect CID (sorted keys)."""
        metadata1 = {"z": 1, "a": 2}
        metadata2 = {"a": 2, "z": 1}
        
        cid1 = storage.store_metadata(metadata1)
        cid2 = storage.store_metadata(metadata2)
        
        assert cid1 == cid2
    
    def test_get_metadata_retrieves_stored(self, storage):
        """Test that get_metadata retrieves previously stored metadata."""
        original = {"title": "Test", "count": 42, "nested": {"key": "value"}}
        cid = storage.store_metadata(original)
        
        retrieved = storage.get_metadata(cid)
        
        assert retrieved == original
    
    def test_get_metadata_not_found(self, storage):
        """Test that get_metadata raises error for non-existent CID."""
        with pytest.raises(MetadataNotFoundError):
            storage.get_metadata("nonexistent_cid_12345")
    
    def test_get_metadata_invalid_cid_format(self, storage):
        """Test that get_metadata rejects path traversal attempts."""
        with pytest.raises(StorageError, match="Invalid CID format"):
            storage.get_metadata("../../../etc/passwd")
    
    def test_health_check_healthy(self, storage):
        """Test that health_check returns True for valid storage."""
        assert storage.health_check() is True
    
    def test_health_check_missing_directory(self, temp_storage_path):
        """Test that health_check returns False if directory is deleted."""
        storage = FileSystemMetadataStorage(storage_path=temp_storage_path)
        
        # Remove the directory
        shutil.rmtree(temp_storage_path)
        
        assert storage.health_check() is False
    
    def test_health_check_not_writable(self, storage, temp_storage_path):
        """Test that health_check returns False if directory is not writable."""
        # Make directory read-only
        os.chmod(temp_storage_path, 0o444)
        
        try:
            result = storage.health_check()
            # Should return False on permission error
            assert result is False
        finally:
            # Restore permissions for cleanup
            os.chmod(temp_storage_path, 0o755)
    
    def test_store_metadata_with_special_characters(self, storage):
        """Test storing metadata with special characters."""
        metadata = {
            "title": "Test with émojis 🎉",
            "description": "Contains \"quotes\" and 'apostrophes'",
            "unicode": "日本語テスト",
        }
        
        cid = storage.store_metadata(metadata)
        retrieved = storage.get_metadata(cid)
        
        assert retrieved == metadata
    
    def test_store_metadata_large_content(self, storage):
        """Test storing large metadata."""
        metadata = {
            "large_field": "x" * 100000,  # 100KB of data
            "array": list(range(1000)),
        }
        
        cid = storage.store_metadata(metadata)
        retrieved = storage.get_metadata(cid)
        
        assert retrieved == metadata
    
    def test_concurrent_stores(self, storage):
        """Test concurrent store operations don't corrupt data."""
        import threading
        
        results = {}
        errors = []
        
        def store_and_retrieve(idx):
            try:
                metadata = {"index": idx, "data": f"item_{idx}"}
                cid = storage.store_metadata(metadata)
                retrieved = storage.get_metadata(cid)
                results[idx] = (metadata == retrieved)
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=store_and_retrieve, args=(i,)) for i in range(10)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert all(results.values()), "Some metadata was corrupted"
