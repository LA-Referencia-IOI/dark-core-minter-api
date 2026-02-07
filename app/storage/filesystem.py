"""
Filesystem-based metadata storage implementation.

Stores metadata as JSON files using MD5 hash as content identifier.
Suitable for development and testing.
"""

import json
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any

from .base import MetadataStorage
from .exceptions import StorageError, MetadataNotFoundError


logger = logging.getLogger(__name__)


class FileSystemMetadataStorage(MetadataStorage):
    """
    Filesystem implementation of metadata storage.
    
    Files are stored as {md5_hash}.json in the configured directory.
    Uses MD5 hash of JSON content as the "CID" for content-addressable storage.
    """
    
    def __init__(self, storage_path: str):
        """
        Initialize filesystem storage.
        
        Args:
            storage_path: Directory path for storing metadata files
        """
        self.storage_path = Path(storage_path)
        self._ensure_storage_directory()
    
    def _ensure_storage_directory(self) -> None:
        """Create storage directory if it doesn't exist."""
        try:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Metadata storage directory ready: {self.storage_path}")
        except Exception as e:
            raise StorageError(f"Failed to create storage directory: {e}")
    
    def _calculate_md5(self, content: str) -> str:
        """Calculate MD5 hash of content."""
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def _get_file_path(self, cid: str) -> Path:
        """Get file path for a given CID."""
        # Sanitize CID to prevent directory traversal
        safe_cid = Path(cid).name
        if safe_cid != cid:
            raise ValueError(f"Invalid CID format: {cid}")
        return self.storage_path / f"{safe_cid}.json"
    
    def store_metadata(self, metadata: Dict[str, Any]) -> str:
        """
        Store metadata as JSON file and return MD5 hash as CID.
        
        The content is serialized deterministically (sorted keys) to ensure
        identical metadata produces the same CID.
        
        Args:
            metadata: Metadata dictionary to store
            
        Returns:
            MD5 hash of the JSON content (as CID)
            
        Raises:
            StorageError: If write operation fails
        """
        try:
            # Serialize with sorted keys for deterministic output
            json_content = json.dumps(metadata, sort_keys=True, indent=2)
            
            # Calculate CID (MD5 hash)
            cid = self._calculate_md5(json_content)
            
            # Write atomically using temp file + rename
            file_path = self._get_file_path(cid)
            temp_path = file_path.with_suffix('.tmp')
            
            temp_path.write_text(json_content, encoding='utf-8')
            temp_path.replace(file_path)
            
            logger.info(f"Stored metadata with CID: {cid}")
            return cid
            
        except Exception as e:
            logger.error(f"Failed to store metadata: {e}")
            raise StorageError(f"Metadata storage failed: {e}")
    
    def get_metadata(self, cid: str) -> Dict[str, Any]:
        """
        Retrieve metadata by CID.
        
        Args:
            cid: Content identifier (MD5 hash)
            
        Returns:
            Metadata dictionary
            
        Raises:
            MetadataNotFoundError: If CID not found
            StorageError: If read operation fails
        """
        try:
            file_path = self._get_file_path(cid)
            
            if not file_path.exists():
                raise MetadataNotFoundError(f"Metadata not found for CID: {cid}")
            
            content = file_path.read_text(encoding='utf-8')
            metadata = json.loads(content)
            
            logger.debug(f"Retrieved metadata for CID: {cid}")
            return metadata
            
        except MetadataNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to retrieve metadata for CID {cid}: {e}")
            raise StorageError(f"Metadata retrieval failed: {e}")
    
    def health_check(self) -> bool:
        """
        Check if storage directory is accessible.
        
        Returns:
            True if directory exists and is writable
        """
        try:
            # Check directory exists
            if not self.storage_path.exists():
                logger.error(f"Storage directory does not exist: {self.storage_path}")
                return False
            
            # Check directory is writable
            test_file = self.storage_path / ".health_check"
            test_file.touch()
            test_file.unlink()
            
            return True
            
        except Exception as e:
            logger.error(f"Storage health check failed: {e}")
            return False
