"""
Dependency injection for FastAPI.

Provides singleton instances of the DARKOrchestrator.
"""

import logging
from typing import Optional

from dark_orchestrator import DARKOrchestrator
from dark_orchestrator.config import DARKConfig
from dark_orchestrator.exceptions import ConfigurationError

from app.config import get_settings

logger = logging.getLogger(__name__)

# Singleton orchestrator instance
_orchestrator: Optional[DARKOrchestrator] = None


def get_orchestrator() -> DARKOrchestrator:
    """
    Get the DARKOrchestrator singleton instance.
    
    Returns:
        DARKOrchestrator instance
        
    Raises:
        RuntimeError: If orchestrator not initialized
    """
    global _orchestrator
    if _orchestrator is None:
        raise RuntimeError(
            "Orchestrator not initialized. "
            "Call init_orchestrator() during application startup."
        )
    return _orchestrator


def init_orchestrator() -> DARKOrchestrator:
    """
    Initialize the DARKOrchestrator singleton.
    
    Called during application lifespan startup.
    
    Returns:
        Initialized DARKOrchestrator
        
    Raises:
        ConfigurationError: If configuration is invalid
    """
    global _orchestrator
    
    settings = get_settings()
    settings.validate_blockchain_config()
    
    logger.info("Initializing DARKOrchestrator...")
    
    # Create config for orchestrator
    config = DARKConfig(
        rpc_url=settings.dark_rpc_url,
        chain_id=settings.dark_chain_id,
        authority_address=settings.dark_authority_address,
        dark_address=settings.dark_contract_address,
        admin_private_key=settings.dark_admin_private_key,
    )
    
    _orchestrator = DARKOrchestrator(config)
    logger.info("DARKOrchestrator initialized successfully")
    
    return _orchestrator


def shutdown_orchestrator() -> None:
    """
    Cleanup orchestrator on shutdown.
    """
    global _orchestrator
    if _orchestrator is not None:
        logger.info("Shutting down DARKOrchestrator")
        _orchestrator = None


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
_metadata_storage: Optional["MetadataStorage"] = None


def get_metadata_storage() -> "MetadataStorage":
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


def init_metadata_storage() -> "MetadataStorage":
    """
    Initialize the MetadataStorage singleton.
    
    Called during application lifespan startup.
    
    Returns:
        Initialized MetadataStorage
    """
    global _metadata_storage
    from app.storage import get_metadata_storage as build_metadata_storage
    
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


# Type hint for MetadataStorage
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.storage.base import MetadataStorage
