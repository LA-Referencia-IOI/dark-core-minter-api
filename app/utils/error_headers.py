"""
Stable dARK error headers for clients that need retry semantics.
"""

from fastapi import HTTPException

DARK_ERROR_CODE_HEADER = "X-DARK-Error-Code"
DARK_RETRYABLE_HEADER = "X-DARK-Retryable"

AUTHORITY_ERROR = "AUTHORITY_ERROR"
AUTHORITY_IDENTITY_REQUIRED = "AUTHORITY_IDENTITY_REQUIRED"
AUTHORITY_NOT_FOUND = "AUTHORITY_NOT_FOUND"
AUTHORITY_MISMATCH = "AUTHORITY_MISMATCH"
AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
AUTHORIZATION_CHECK_UNAVAILABLE = "AUTHORIZATION_CHECK_UNAVAILABLE"
ARK_ERROR = "ARK_ERROR"
ARK_NOT_FOUND = "ARK_NOT_FOUND"
ALREADY_EXISTS = "ALREADY_EXISTS"
BLOCKCHAIN_ERROR = "BLOCKCHAIN_ERROR"
BLOCKCHAIN_UNAVAILABLE = "BLOCKCHAIN_UNAVAILABLE"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"


def dark_error_headers(error_code: str, retryable: bool) -> dict[str, str]:
    """Build stable error headers for API clients."""
    return {
        DARK_ERROR_CODE_HEADER: error_code,
        DARK_RETRYABLE_HEADER: "true" if retryable else "false",
    }


def dark_http_exception(
    status_code: int,
    detail: str,
    error_code: str,
    retryable: bool,
) -> HTTPException:
    """Build an HTTPException with dARK error headers."""
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=dark_error_headers(error_code, retryable),
    )
