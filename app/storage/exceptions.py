"""
Storage-specific exceptions.
"""


class StorageError(Exception):
    """Base exception for storage operations."""
    pass


class MetadataNotFoundError(StorageError):
    """Raised when metadata CID is not found in storage."""
    pass
