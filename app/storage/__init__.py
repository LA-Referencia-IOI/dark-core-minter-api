"""
Metadata storage abstraction for ARK metadata persistence.

Supports multiple storage backends: filesystem (development), IPFS (future).
"""

from .base import MetadataStorage
from .exceptions import StorageError, MetadataNotFoundError
from .filesystem import FileSystemMetadataStorage


def get_metadata_storage(storage_type: str = "filesystem", **kwargs) -> MetadataStorage:
    """
    Factory function to create metadata storage instance.
    
    Args:
        storage_type: Type of storage ("filesystem" or "ipfs")
        **kwargs: Storage-specific configuration
        
    Returns:
        MetadataStorage instance
        
    Raises:
        ValueError: If storage_type is not supported
    """
    if storage_type == "filesystem":
        storage_path = kwargs.get("storage_path", "./metadata_storage")
        return FileSystemMetadataStorage(storage_path)
    elif storage_type == "ipfs":
        # Future implementation
        raise NotImplementedError("IPFS storage not yet implemented")
    else:
        raise ValueError(f"Unsupported storage type: {storage_type}")


__all__ = [
    "MetadataStorage",
    "StorageError",
    "MetadataNotFoundError",
    "FileSystemMetadataStorage",
    "get_metadata_storage",
]
