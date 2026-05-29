"""
Exception handlers for dARK Core API.

Maps dark-core-lib exceptions to HTTP responses following the spec.
"""

import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dark_core_lib.exceptions import (
    DarkCoreError,
    ConfigurationError,
    ConnectionError as CoreConnectionError,
    AuthorityError,
    ARKError,
    TransactionError,
)

from app.models.responses import ErrorResponse
from app.utils.error_headers import (
    ALREADY_EXISTS,
    ARK_ERROR,
    ARK_NOT_FOUND,
    AUTHORITY_ERROR,
    AUTHORITY_NOT_FOUND,
    AUTHORIZATION_FAILED,
    BLOCKCHAIN_ERROR,
    BLOCKCHAIN_UNAVAILABLE,
    CONFIGURATION_ERROR,
    INTERNAL_ERROR,
    dark_error_headers,
)

logger = logging.getLogger(__name__)


def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    retryable: bool,
    details: dict | None = None,
) -> JSONResponse:
    """Build a standard error body plus stable dARK error headers."""
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error=error_code,
            message=message,
            retryable=retryable,
            details=details,
        ).model_dump(),
        headers=dark_error_headers(error_code, retryable),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI app."""
    
    @app.exception_handler(AuthorityError)
    async def authority_error_handler(request: Request, exc: AuthorityError):
        """
        Handle AuthorityError exceptions.
        
        Maps to:
        - 403 for authorization failures
        - 404 for not found
        """
        message = str(exc)
        
        if "not found" in message.lower():
            return _error_response(
                status_code=404,
                error_code=AUTHORITY_NOT_FOUND,
                message=message,
                retryable=False,
            )
        
        if "not authorized" in message.lower() or "not allowed" in message.lower():
            return _error_response(
                status_code=403,
                error_code=AUTHORIZATION_FAILED,
                message=message,
                retryable=False,
            )
        
        return _error_response(
            status_code=400,
            error_code=AUTHORITY_ERROR,
            message=message,
            retryable=False,
        )
    
    @app.exception_handler(ARKError)
    async def ark_error_handler(request: Request, exc: ARKError):
        """Handle ARKError exceptions."""
        message = str(exc)
        
        if "not found" in message.lower():
            return _error_response(
                status_code=404,
                error_code=ARK_NOT_FOUND,
                message=message,
                retryable=False,
            )
        
        if "already exists" in message.lower():
            return _error_response(
                status_code=409,
                error_code=ALREADY_EXISTS,
                message=message,
                retryable=False,
            )
        
        return _error_response(
            status_code=400,
            error_code=ARK_ERROR,
            message=message,
            retryable=False,
        )
    
    @app.exception_handler(TransactionError)
    async def transaction_error_handler(request: Request, exc: TransactionError):
        """
        Handle TransactionError exceptions.
        
        These are typically retryable blockchain errors.
        """
        logger.error(f"Transaction error: {exc}")
        
        return _error_response(
            status_code=503,
            error_code=BLOCKCHAIN_ERROR,
            message=str(exc),
            retryable=True,
            details={
                "tx_hash": getattr(exc, "tx_hash", None),
                "gas_used": getattr(exc, "gas_used", None),
            },
        )
    
    @app.exception_handler(ConfigurationError)
    async def configuration_error_handler(request: Request, exc: ConfigurationError):
        """Handle ConfigurationError exceptions."""
        logger.error(f"Configuration error: {exc}")
        
        return _error_response(
            status_code=500,
            error_code=CONFIGURATION_ERROR,
            message="Internal configuration error",
            retryable=False,
        )

    @app.exception_handler(CoreConnectionError)
    async def connection_error_handler(request: Request, exc: CoreConnectionError):
        """Handle unavailable blockchain RPC/connection failures."""
        logger.error(f"Blockchain connection error: {exc}")

        return _error_response(
            status_code=503,
            error_code=BLOCKCHAIN_UNAVAILABLE,
            message=str(exc),
            retryable=True,
        )
    
    @app.exception_handler(DarkCoreError)
    async def dark_error_handler(request: Request, exc: DarkCoreError):
        """Handle generic DarkCoreError exceptions."""
        logger.error(f"dARK core error: {exc}")
        
        return _error_response(
            status_code=500,
            error_code=INTERNAL_ERROR,
            message=str(exc),
            retryable=False,
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """Handle unexpected exceptions."""
        logger.exception(f"Unexpected error: {exc}")
        
        return _error_response(
            status_code=500,
            error_code=INTERNAL_ERROR,
            message="An unexpected error occurred",
            retryable=False,
        )
