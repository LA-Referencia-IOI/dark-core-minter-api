"""Test stub exceptions for dark_orchestrator."""


class DARKError(Exception):
    """Base stub exception."""


class ConfigurationError(DARKError):
    """Configuration-related error."""


class AuthorityError(DARKError):
    """Authority/authorization-related error."""


class ARKError(DARKError):
    """ARK operation-related error."""


class TransactionError(DARKError):
    """Blockchain transaction-related error."""

