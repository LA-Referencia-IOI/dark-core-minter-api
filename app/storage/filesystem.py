"""
Filesystem-based metadata storage implementation.

Stores metadata files using MD5 hash as content identifier.
Supports multiple formats (JSON, XML, etc.) via sidecar metadata files.
Suitable for development and testing.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Tuple

from .base import MetadataStorage
from .exceptions import StorageError, MetadataNotFoundError


logger = logging.getLogger(__name__)

# Supported format extensions
FORMAT_EXTENSIONS = {
    "json": ".json",
    "xml": ".xml",
}


class FileSystemMetadataStorage(MetadataStorage):
    """
    Filesystem implementation of metadata storage.
    
    Files are stored as {md5_hash}.{ext} in the configured directory.
    Format is tracked via a sidecar .meta file.
    Uses MD5 hash of content as the "CID" for content-addressable storage.
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
    
    def _sanitize_cid(self, cid: str) -> str:
        """Sanitize CID to prevent directory traversal."""
        safe_cid = Path(cid).name
        if safe_cid != cid:
            raise StorageError(f"Invalid CID format: {cid}")
        return safe_cid
    
    def _get_content_path(self, cid: str, format: str) -> Path:
        """Get content file path for a given CID and format."""
        safe_cid = self._sanitize_cid(cid)
        ext = FORMAT_EXTENSIONS.get(format, f".{format}")
        return self.storage_path / f"{safe_cid}{ext}"
    
    def _get_meta_path(self, cid: str) -> Path:
        """Get metadata sidecar file path."""
        safe_cid = self._sanitize_cid(cid)
        return self.storage_path / f"{safe_cid}.meta"
    
    def store_metadata(self, content: str, format: str) -> str:
        """
        Store raw metadata content and return MD5 hash as CID.
        
        Args:
            content: Raw metadata content (JSON string, XML string, etc.)
            format: Format identifier ("json", "xml", etc.)
            
        Returns:
            MD5 hash of the content (as CID)
            
        Raises:
            StorageError: If write operation fails
        """
        try:
            # Calculate CID (MD5 hash of content)
            cid = self._calculate_md5(content)
            
            # Get file paths
            content_path = self._get_content_path(cid, format)
            meta_path = self._get_meta_path(cid)
            
            # Write content atomically using temp file + rename
            temp_content = content_path.with_suffix('.tmp')
            temp_content.write_text(content, encoding='utf-8')
            temp_content.replace(content_path)
            
            # Write format metadata
            meta_data = {"format": format}
            temp_meta = meta_path.with_suffix('.tmp')
            temp_meta.write_text(json.dumps(meta_data), encoding='utf-8')
            temp_meta.replace(meta_path)
            
            logger.info(f"Stored metadata with CID: {cid} (format: {format})")
            return cid
            
        except Exception as e:
            logger.error(f"Failed to store metadata: {e}")
            raise StorageError(f"Metadata storage failed: {e}")
    
    def get_metadata(self, cid: str) -> Tuple[str, str]:
        """
        Retrieve metadata by CID.
        
        Args:
            cid: Content identifier (MD5 hash)
            
        Returns:
            Tuple of (raw_content, format)
            
        Raises:
            MetadataNotFoundError: If CID not found
            StorageError: If read operation fails
        """
        try:
            safe_cid = self._sanitize_cid(cid)
            meta_path = self._get_meta_path(cid)
            
            # Read format from sidecar
            if not meta_path.exists():
                raise MetadataNotFoundError(f"Metadata not found for CID: {cid}")
            
            meta_data = json.loads(meta_path.read_text(encoding='utf-8'))
            format = meta_data.get("format", "json")
            
            # Get content file path
            content_path = self._get_content_path(cid, format)
            
            if not content_path.exists():
                raise MetadataNotFoundError(f"Content file not found for CID: {cid}")
            
            content = content_path.read_text(encoding='utf-8')
            
            logger.debug(f"Retrieved metadata for CID: {cid} (format: {format})")
            return content, format
            
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
