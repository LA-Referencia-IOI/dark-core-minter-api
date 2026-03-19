"""Test stub package for dark_core_lib."""

from .config import CoreConfig
from .models import ARKInfo, AuthorityInfo, TxReceiptInfo
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

