"""
Dependency injection for FastAPI.

Provides singleton instances of the dark-core-lib client.
"""

import logging
import time
from typing import Optional

from dark_core_lib import DARKCoreClient, CoreConfig, get_metadata_storage as build_metadata_storage
from dark_core_lib.exceptions import ConfigurationError, ConnectionError as CoreConnectionError
from dark_core_lib.metadata import MetadataStorage

from app.config import get_settings

logger = logging.getLogger(__name__)

# Singleton core client instance
_corelib_client: Optional[DARKCoreClient] = None


def get_corelib_client() -> DARKCoreClient:
    """
    Get the DARKCoreClient singleton instance.
    
    Returns:
        DARKCoreClient instance
        
    Raises:
        RuntimeError: If client not initialized
    """
    global _corelib_client
    if _corelib_client is None:
        return init_corelib_client(max_wait_seconds=0)
    return _corelib_client


def _build_corelib_config() -> CoreConfig:
    """Build dark-core-lib configuration from application settings."""
    settings = get_settings()
    settings.validate_blockchain_config()

    return CoreConfig(
        rpc_url=settings.dark_rpc_url,
        chain_id=settings.dark_chain_id,
        authority_contract_address=settings.dark_authority_address,
        dark_contract_address=settings.dark_contract_address,
        admin_private_key=settings.dark_admin_private_key,
        read_only=False,
        default_gas_limit=settings.dark_gas_limit,
    )


def init_corelib_client(
    *,
    max_wait_seconds: Optional[float] = None,
    retry_interval_seconds: Optional[float] = None,
    force_reconnect: bool = False,
) -> DARKCoreClient:
    """
    Initialize the DARKCoreClient singleton.
    
    Called during application lifespan startup.
    
    Returns:
        Initialized DARKCoreClient
        
    Raises:
        ConfigurationError: If configuration is invalid
    """
    global _corelib_client
    
    if _corelib_client is not None and not force_reconnect:
        return _corelib_client

    settings = get_settings()
    wait_seconds = (
        float(max_wait_seconds)
        if max_wait_seconds is not None
        else float(settings.dark_rpc_connect_retry_seconds)
    )
    retry_seconds = (
        float(retry_interval_seconds)
        if retry_interval_seconds is not None
        else float(settings.dark_rpc_connect_retry_interval_seconds)
    )
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    config = _build_corelib_config()

    logger.info("Initializing DARKCoreClient...")

    while True:
        try:
            _corelib_client = DARKCoreClient(config)
            logger.info("DARKCoreClient initialized successfully")
            return _corelib_client
        except CoreConnectionError:
            _corelib_client = None
            if time.monotonic() >= deadline:
                raise

            sleep_seconds = min(max(retry_seconds, 0.1), max(deadline - time.monotonic(), 0.1))
            logger.warning(
                "DARKCoreClient RPC connection failed; retrying in %.1fs",
                sleep_seconds,
            )
            time.sleep(sleep_seconds)


def shutdown_corelib_client() -> None:
    """
    Cleanup client on shutdown.
    """
    global _corelib_client
    if _corelib_client is not None:
        logger.info("Shutting down DARKCoreClient")
        _corelib_client = None

def get_db():
    """
    Database session dependency.
    
    Yields:
        SQLAlchemy Session
        
    Usage:
        @app.get("/items")
        def get_items(db: Session = Depends(get_db)):
            ...
    """
    from app.database.connection import SessionLocal
    
    session_factory = SessionLocal()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


# Singleton metadata storage instance
_metadata_storage: Optional[MetadataStorage] = None


def get_metadata_storage() -> MetadataStorage:
    """
    Get the MetadataStorage singleton instance.
    
    Returns:
        MetadataStorage instance
        
    Raises:
        RuntimeError: If storage not initialized
    """
    global _metadata_storage
    if _metadata_storage is None:
        raise RuntimeError(
            "Metadata storage not initialized. "
            "Call init_metadata_storage() during application startup."
        )
    return _metadata_storage


def init_metadata_storage() -> MetadataStorage:
    """
    Initialize the MetadataStorage singleton.
    
    Called during application lifespan startup.
    
    Returns:
        Initialized MetadataStorage
    """
    global _metadata_storage
    settings = get_settings()

    storage_type = settings.metadata_storage_type.lower()
    logger.info(f"Initializing metadata storage backend: {storage_type}")

    if storage_type == "filesystem":
        _metadata_storage = build_metadata_storage(
            storage_type="filesystem",
            storage_path=settings.metadata_storage_path,
        )
    elif storage_type == "store_api":
        _metadata_storage = build_metadata_storage(
            storage_type="store_api",
            store_api_url=settings.metadata_store_api_url,
            timeout_seconds=settings.metadata_store_api_timeout_seconds,
        )
    else:
        raise ValueError(f"Unsupported metadata storage type: {storage_type}")

    logger.info("Metadata storage initialized successfully")
    
    return _metadata_storage


def shutdown_metadata_storage() -> None:
    """Cleanup metadata storage on shutdown."""
    global _metadata_storage
    if _metadata_storage is not None:
        logger.info("Shutting down metadata storage")
        _metadata_storage = None
