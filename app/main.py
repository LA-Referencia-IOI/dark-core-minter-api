"""
dARK Core API - Main Application Entry Point

FastAPI application with lifespan management, middleware, and routing.
"""

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dependencies import init_corelib_client, shutdown_corelib_client, get_corelib_client, get_db
from app.api.router import api_router
from app.exceptions.handlers import register_exception_handlers
from app.utils.rpc_health import check_rpc_health
from app.utils.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.
    
    Initializes resources on startup and cleans up on shutdown.
    """
    # Startup
    logger.info("Starting dARK Core API...")
    settings = get_settings()
    
    # Validate mTLS config if enabled
    if settings.mtls_enabled:
        settings.validate_mtls_config()
        logger.info("mTLS is enabled")
    else:
        logger.warning("mTLS is disabled - for development only!")
    
    # Initialize database
    try:
        from app.database import init_db
        logger.info("Initializing database...")
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
    
    # Blockchain client is initialized lazily by endpoints that need it. This
    # keeps DB-only endpoints available when RPC is temporarily unavailable.
    
    # Initialize metadata storage
    try:
        from app.dependencies import init_metadata_storage
        metadata_storage = init_metadata_storage()
        
        # Store globally for health check.
        app.state.metadata_storage = metadata_storage
    except Exception as e:
        logger.error(f"Failed to initialize metadata storage: {e}")
        raise
    
    yield
    
    # Shutdown
    logger.info("Shutting down dARK Core API...")

    shutdown_corelib_client()
    
    from app.dependencies import shutdown_metadata_storage
    shutdown_metadata_storage()

    from app.database import close_db
    close_db()


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    
    Returns:
        Configured FastAPI application
    """
    settings = get_settings()
    
    app = FastAPI(
        title="dARK Core API",
        description=(
            "REST API service for dARK minting and lifecycle operations. "
            "Provides blockchain operations for Minter nodes."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    
    # CORS middleware (configurable for production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Register exception handlers
    register_exception_handlers(app)
    
    # Mount API router
    app.include_router(api_router, prefix="/api/v1")
    
    # Health endpoint (no auth required)
    @app.get("/health/live", tags=["Health"])
    async def liveness_check():
        """Cheap process liveness check; does not touch DB, RPC or storage."""
        return {"status": "alive"}

    @app.get("/health", tags=["Health"])
    async def health_check(db: Session = Depends(get_db)):
        """Health check endpoint for load balancers."""
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
        
        status = "healthy"
        response = {}
        
        # Check blockchain RPC without requiring the full core-lib client.
        rpc = check_rpc_health()
        response["blockchain_connected"] = bool(rpc["available"])
        response["current_block"] = rpc["block_number"]
        response["rpc"] = rpc
        if not rpc["available"]:
            response["blockchain_error"] = rpc["last_error"]
            status = "degraded"
        
        # Check database
        try:
            db.execute(text("SELECT 1")).fetchone()
            response["database"] = "healthy"
        except SQLAlchemyError as e:
            response["database"] = "unhealthy"
            response["database_error"] = str(e)
            status = "unhealthy"
        except Exception as e:
            response["database"] = "unhealthy"
            response["database_error"] = str(e)
            status = "unhealthy"
        
        # Check metadata storage
        try:
            storage_healthy = app.state.metadata_storage.health_check()
            response["metadata_storage"] = "healthy" if storage_healthy else "unhealthy"
            if not storage_healthy:
                status = "degraded"
        except Exception as e:
            response["metadata_storage"] = "unhealthy"
            response["metadata_storage_error"] = str(e)
            status = "degraded"
        
        response["status"] = status
        
        # Return 503 if unhealthy
        if status == "unhealthy":
            import json
            from fastapi import Response
            return Response(
                content=json.dumps(response),
                status_code=503,
                media_type="application/json"
            )
        
        return response
    
    return app


# Application instance
app = create_app()


def run_server():
    """Run the server (used by CLI)."""
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.minter_api_host,
        port=settings.minter_api_port,
        reload=False,
        workers=settings.minter_api_workers,
        access_log=settings.minter_uvicorn_access_log,
    )
