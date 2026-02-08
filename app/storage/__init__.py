"""
Metadata storage abstraction for ARK metadata persistence.

Supports multiple storage backends: filesystem and dark-store-api.
"""

from .base import MetadataStorage
from .exceptions import StorageError, MetadataNotFoundError
from .filesystem import FileSystemMetadataStorage
from .store_api import StoreApiMetadataStorage


def get_metadata_storage(storage_type: str = "filesystem", **kwargs) -> MetadataStorage:
    """
    Factory function to create metadata storage instance.
    
    Args:
        storage_type: Type of storage ("filesystem" or "store_api")
        **kwargs: Storage-specific configuration
        
    Returns:
        MetadataStorage instance
        
    Raises:
        ValueError: If storage_type is not supported
    """
    storage_type_normalized = storage_type.lower()

    if storage_type_normalized == "filesystem":
        storage_path = kwargs.get("storage_path", "./metadata_storage")
        return FileSystemMetadataStorage(storage_path)
    elif storage_type_normalized == "store_api":
        store_api_url = kwargs.get("store_api_url", "http://localhost:8002")
        timeout_seconds = kwargs.get("timeout_seconds", 10.0)
        return StoreApiMetadataStorage(store_api_url, timeout_seconds=timeout_seconds)
    else:
        raise ValueError(f"Unsupported storage type: {storage_type}")


__all__ = [
    "MetadataStorage",
    "StorageError",
    "MetadataNotFoundError",
    "FileSystemMetadataStorage",
    "StoreApiMetadataStorage",
    "get_metadata_storage",
]
