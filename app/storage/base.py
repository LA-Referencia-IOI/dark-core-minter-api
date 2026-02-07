"""
Abstract base class for metadata storage backends.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any


class MetadataStorage(ABC):
    """Abstract interface for ARK metadata persistence."""
    
    @abstractmethod
    def store_metadata(self, metadata: Dict[str, Any]) -> str:
        """
        Store ARK metadata and return a content identifier (CID).
        
        Args:
            metadata: ARK metadata dictionary containing:
                - target: URL target
                - alternate_identifiers: List of alternate IDs (optional)
                - Any additional metadata fields
                
        Returns:
            Content identifier (CID) as string.
            For filesystem: MD5 hash of JSON content
            For IPFS: IPFS CID
            
        Raises:
            StorageError: If storage operation fails
        """
        pass
    
    @abstractmethod
    def get_metadata(self, cid: str) -> Dict[str, Any]:
        """
        Retrieve metadata by content identifier.
        
        Args:
            cid: Content identifier
            
        Returns:
            Metadata dictionary
            
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
