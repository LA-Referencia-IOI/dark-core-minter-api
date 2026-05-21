"""Test stub package for dark_core_lib."""

from .config import CoreConfig
from .metadata import (
    FileSystemMetadataStorage,
    Level1Metadata,
    MetadataNotFoundError,
    MetadataService,
    MetadataStorage,
    OriginalMetadataRef,
    StorageError,
    StoreApiMetadataStorage,
    StoredDocument,
    get_metadata_storage,
)
from .models import ARKInfo, ARKPublishOperation, ARKPublishResult, AuthorityInfo, TxReceiptInfo
from .exceptions import (
    DarkCoreError,
    ConfigurationError,
    ConnectionError,
    ReadOnlyModeError,
    TransactionError,
    AuthorityError,
    AuthorityNotFoundError,
    AuthorityAlreadyExistsError,
    AuthorizationError,
    ARKError,
    ARKNotFoundError,
    ARKAlreadyExistsError,
)


class DARKCoreClient:
    """Lightweight stub client used only for tests."""

    def __init__(self, config: CoreConfig):
        self.config = config
