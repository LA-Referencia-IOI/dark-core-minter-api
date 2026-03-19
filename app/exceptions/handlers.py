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
    AuthorityError,
    ARKError,
    TransactionError,
)

from app.models.responses import ErrorResponse

logger = logging.getLogger(__name__)


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
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    error="AUTHORITY_NOT_FOUND",
                    message=message,
                    retryable=False,
                ).model_dump(),
            )
        
        if "not authorized" in message.lower() or "not allowed" in message.lower():
            return JSONResponse(
                status_code=403,
                content=ErrorResponse(
                    error="AUTHORIZATION_FAILED",
                    message=message,
                    retryable=False,
                ).model_dump(),
            )
        
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="AUTHORITY_ERROR",
                message=message,
                retryable=False,
            ).model_dump(),
        )
    
    @app.exception_handler(ARKError)
    async def ark_error_handler(request: Request, exc: ARKError):
        """Handle ARKError exceptions."""
        message = str(exc)
        
        if "not found" in message.lower():
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    error="ARK_NOT_FOUND",
                    message=message,
                    retryable=False,
                ).model_dump(),
            )
        
        if "already exists" in message.lower():
            return JSONResponse(
                status_code=409,
                content=ErrorResponse(
                    error="ALREADY_EXISTS",
                    message=message,
                    retryable=False,
                ).model_dump(),
            )
        
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="ARK_ERROR",
                message=message,
                retryable=False,
            ).model_dump(),
        )
    
    @app.exception_handler(TransactionError)
    async def transaction_error_handler(request: Request, exc: TransactionError):
        """
        Handle TransactionError exceptions.
        
        These are typically retryable blockchain errors.
        """
        logger.error(f"Transaction error: {exc}")
        
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error="BLOCKCHAIN_ERROR",
                message=str(exc),
                retryable=True,
                details={
                    "tx_hash": getattr(exc, "tx_hash", None),
                    "gas_used": getattr(exc, "gas_used", None),
                },
            ).model_dump(),
        )
    
    @app.exception_handler(ConfigurationError)
    async def configuration_error_handler(request: Request, exc: ConfigurationError):
        """Handle ConfigurationError exceptions."""
        logger.error(f"Configuration error: {exc}")
        
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="CONFIGURATION_ERROR",
                message="Internal configuration error",
                retryable=False,
            ).model_dump(),
        )
    
    @app.exception_handler(DarkCoreError)
    async def dark_error_handler(request: Request, exc: DarkCoreError):
        """Handle generic DarkCoreError exceptions."""
        logger.error(f"dARK core error: {exc}")
        
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="INTERNAL_ERROR",
                message=str(exc),
                retryable=False,
            ).model_dump(),
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """Handle unexpected exceptions."""
        logger.exception(f"Unexpected error: {exc}")
        
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="INTERNAL_ERROR",
                message="An unexpected error occurred",
                retryable=False,
            ).model_dump(),
        )
