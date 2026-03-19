"""Test stub exceptions for dark_core_lib."""


class DarkCoreError(Exception):
    """Base stub exception."""


class ConfigurationError(DarkCoreError):
    """Configuration-related error."""


class ConnectionError(DarkCoreError):
    """Connection-related error."""


class ReadOnlyModeError(DarkCoreError):
    """Read-only mode violation."""


class TransactionError(DarkCoreError):
    """Blockchain transaction-related error."""

    def __init__(self, message: str, tx_hash: str = None, gas_used: int = None):
        super().__init__(message)
        self.tx_hash = tx_hash
        self.gas_used = gas_used


class AuthorityError(DarkCoreError):
    """Authority/authorization-related error."""


class AuthorityNotFoundError(AuthorityError):
    """Authority not found."""


class AuthorityAlreadyExistsError(AuthorityError):
    """Authority already exists."""


class AuthorizationError(AuthorityError):
    """Authorization failed."""


class ARKError(DarkCoreError):
    """ARK operation-related error."""


class ARKNotFoundError(ARKError):
    """ARK not found."""


class ARKAlreadyExistsError(ARKError):
    """ARK already exists."""

