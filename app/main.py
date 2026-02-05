"""
dARK Core API - Main Application Entry Point

FastAPI application with lifespan management, middleware, and routing.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.dependencies import init_orchestrator, shutdown_orchestrator
from app.api.router import api_router
from app.exceptions.handlers import register_exception_handlers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
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
    
    # Initialize orchestrator connection to blockchain
    try:
        orchestrator = init_orchestrator()
        logger.info(f"Connected to blockchain at block {orchestrator.get_block_number()}")
    except Exception as e:
        logger.error(f"Failed to initialize orchestrator: {e}")
        raise
    
    yield
    
    # Shutdown
    logger.info("Shutting down dARK Core API...")
    shutdown_orchestrator()


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
            "REST API service for the dARK Core Orchestrator. "
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
    @app.get("/health", tags=["Health"])
    async def health_check():
        """Health check endpoint for load balancers."""
        from app.dependencies import get_orchestrator
        try:
            orchestrator = get_orchestrator()
            connected = orchestrator.is_connected()
            block = orchestrator.get_block_number() if connected else None
            return {
                "status": "healthy" if connected else "degraded",
                "blockchain_connected": connected,
                "current_block": block,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
            }
    
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
    )
