"""
Tests for metadata storage implementations.

Tests the new multi-format storage API that accepts raw content and format.
"""

import os
import pytest
import tempfile
import shutil
import json
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
    
    # ==========================================================================
    # JSON Format Tests
    # ==========================================================================
    
    def test_store_json_metadata_returns_cid(self, storage):
        """Test that store_metadata returns a valid CID for JSON content."""
        content = '{"title": "Test Document", "author": "Test Author"}'
        cid = storage.store_metadata(content, "json")
        
        assert cid is not None
        assert len(cid) == 32  # MD5 hash length
        assert cid.isalnum()  # Hexadecimal string
    
    def test_store_json_metadata_creates_file(self, storage, temp_storage_path):
        """Test that store_metadata creates a JSON file."""
        content = '{"key": "value"}'
        cid = storage.store_metadata(content, "json")
        
        file_path = Path(temp_storage_path) / f"{cid}.json"
        assert file_path.exists()
    
    def test_store_json_metadata_deterministic(self, storage):
        """Test that same content produces same CID."""
        content = '{"a": 1, "b": 2, "c": 3}'
        
        cid1 = storage.store_metadata(content, "json")
        cid2 = storage.store_metadata(content, "json")
        
        assert cid1 == cid2
    
    def test_get_json_metadata_retrieves_stored(self, storage):
        """Test that get_metadata retrieves previously stored JSON content."""
        original = '{"title": "Test", "count": 42, "nested": {"key": "value"}}'
        cid = storage.store_metadata(original, "json")
        
        content, fmt = storage.get_metadata(cid)
        
        assert content == original
        assert fmt == "json"
    
    # ==========================================================================
    # XML Format Tests
    # ==========================================================================
    
    def test_store_xml_metadata_returns_cid(self, storage):
        """Test that store_metadata returns a valid CID for XML content."""
        content = '<?xml version="1.0"?><record><title>Test</title></record>'
        cid = storage.store_metadata(content, "xml")
        
        assert cid is not None
        assert len(cid) == 32  # MD5 hash length
        assert cid.isalnum()
    
    def test_store_xml_metadata_creates_file(self, storage, temp_storage_path):
        """Test that store_metadata creates an XML file."""
        content = '<?xml version="1.0"?><record><key>value</key></record>'
        cid = storage.store_metadata(content, "xml")
        
        file_path = Path(temp_storage_path) / f"{cid}.xml"
        assert file_path.exists()
    
    def test_get_xml_metadata_retrieves_stored(self, storage):
        """Test that get_metadata retrieves previously stored XML content."""
        original = '<?xml version="1.0"?><record><title>Test Doc</title></record>'
        cid = storage.store_metadata(original, "xml")
        
        content, fmt = storage.get_metadata(cid)
        
        assert content == original
        assert fmt == "xml"
    
    # ==========================================================================
    # CID Independence Tests
    # ==========================================================================
    
    def test_cid_depends_on_content_not_format(self, storage):
        """Test that CID is calculated from content, regardless of format."""
        content = "same content for both formats"
        
        cid_json = storage.store_metadata(content, "json")
        cid_xml = storage.store_metadata(content, "xml")
        
        # CIDs should be identical since content is the same
        assert cid_json == cid_xml
    
    # ==========================================================================
    # Error Cases
    # ==========================================================================
    
    def test_get_metadata_not_found(self, storage):
        """Test that get_metadata raises error for non-existent CID."""
        with pytest.raises(MetadataNotFoundError):
            storage.get_metadata("nonexistent_cid_12345")
    
    def test_get_metadata_invalid_cid_format(self, storage):
        """Test that get_metadata rejects path traversal attempts."""
        with pytest.raises(StorageError, match="Invalid CID format"):
            storage.get_metadata("../../../etc/passwd")
    
    # ==========================================================================
    # Health Check Tests
    # ==========================================================================
    
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
    
    # ==========================================================================
    # Content Edge Cases
    # ==========================================================================
    
    def test_store_metadata_with_special_characters(self, storage):
        """Test storing content with special characters."""
        content = json.dumps({
            "title": "Test with émojis 🎉",
            "description": "Contains \"quotes\" and 'apostrophes'",
            "unicode": "日本語テスト",
        })
        
        cid = storage.store_metadata(content, "json")
        retrieved_content, fmt = storage.get_metadata(cid)
        
        assert retrieved_content == content
        assert fmt == "json"
    
    def test_store_metadata_large_content(self, storage):
        """Test storing large metadata."""
        content = json.dumps({
            "large_field": "x" * 100000,  # 100KB of data
            "array": list(range(1000)),
        })
        
        cid = storage.store_metadata(content, "json")
        retrieved_content, fmt = storage.get_metadata(cid)
        
        assert retrieved_content == content
    
    def test_concurrent_stores(self, storage):
        """Test concurrent store operations don't corrupt data."""
        import threading
        
        results = {}
        errors = []
        
        def store_and_retrieve(idx):
            try:
                content = json.dumps({"index": idx, "data": f"item_{idx}"})
                cid = storage.store_metadata(content, "json")
                retrieved_content, fmt = storage.get_metadata(cid)
                results[idx] = (content == retrieved_content and fmt == "json")
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=store_and_retrieve, args=(i,)) for i in range(10)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert all(results.values()), "Some metadata was corrupted"
