"""
Abstract base class for metadata storage backends.

Supports multiple metadata formats (JSON, XML, etc.) stored as raw content.
"""

from abc import ABC, abstractmethod
from typing import Tuple


class MetadataStorage(ABC):
    """Abstract interface for ARK metadata persistence."""
    
    @abstractmethod
    def store_metadata(self, content: str, format: str) -> str:
        """
        Store raw metadata content and return a content identifier (CID).
        
        Args:
            content: Raw metadata content (JSON string, XML string, etc.)
            format: Format identifier ("json", "xml", etc.)
                
        Returns:
            Content identifier (CID) as string.
            For filesystem: MD5 hash of content
            For IPFS: IPFS CID
            
        Raises:
            StorageError: If storage operation fails
        """
        pass
    
    @abstractmethod
    def get_metadata(self, cid: str) -> Tuple[str, str]:
        """
        Retrieve metadata by content identifier.
        
        Args:
            cid: Content identifier
            
        Returns:
            Tuple of (raw_content, format)
            
        Raises:
            MetadataNotFoundError: If CID not found
            StorageError: If retrieval fails
        """
        pass
    
    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if storage backend is healthy and accessible.
        
        Returns:
            True if healthy, False otherwise
        """
        pass
