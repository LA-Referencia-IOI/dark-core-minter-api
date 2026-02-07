"""
dARK Core API - Main Application Entry Point

FastAPI application with lifespan management, middleware, and routing.
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.dependencies import init_orchestrator, shutdown_orchestrator, get_db
from app.api.router import api_router
from app.exceptions.handlers import register_exception_handlers

# Configure logging
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("api.log"),
    ]
)
logger = logging.getLogger(__name__)

# Global scheduler and worker instances
_scheduler: Optional[BackgroundScheduler] = None
_ark_publisher = None


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
    
    # Initialize orchestrator connection to blockchain
    try:
        orchestrator = init_orchestrator()
        logger.info(f"Connected to blockchain at block {orchestrator.get_block_number()}")
    except Exception as e:
        logger.error(f"Failed to initialize orchestrator: {e}")
        raise
    
    # Initialize metadata storage
    try:
        from app.storage import get_metadata_storage
        metadata_storage = get_metadata_storage(
            storage_type=settings.metadata_storage_type,
            storage_path=settings.metadata_storage_path,
        )
        logger.info(f"Metadata storage initialized: {settings.metadata_storage_type}")
        
        # Store globally for health check and worker access
        app.state.metadata_storage = metadata_storage
    except Exception as e:
        logger.error(f"Failed to initialize metadata storage: {e}")
        raise
    
    # Initialize async worker if enabled
    global _scheduler, _ark_publisher
    if settings.worker_enabled:
        try:
            from app.workers.publisher import ARKPublisher
            
            _ark_publisher = ARKPublisher(
                orchestrator=orchestrator,
                metadata_storage=metadata_storage,
                batch_size=settings.worker_batch_size,
                max_retries=settings.worker_max_retries,
                backoff_base=settings.worker_retry_backoff_base,
            )
            
            # Create background scheduler
            _scheduler = BackgroundScheduler()
            _scheduler.add_job(
                func=_ark_publisher.run_publish_cycle,
                trigger=IntervalTrigger(seconds=settings.worker_interval_seconds),
                id="ark_publisher",
                name="ARK Publisher Worker",
                max_instances=1,  # Only one instance running at a time
                replace_existing=True,
            )
            _scheduler.start()
            
            # Store globally for monitoring endpoint
            app.state.ark_publisher = _ark_publisher
            
            logger.info(f"ARK publisher worker started (interval: {settings.worker_interval_seconds}s)")
        except Exception as e:
            logger.error(f"Failed to initialize worker: {e}")
            raise
    else:
        logger.info("ARK publisher worker disabled")
    
    yield
    
    # Shutdown
    logger.info("Shutting down dARK Core API...")
    
    # Stop worker scheduler (access global from module level)
    if _scheduler:
        logger.info("Stopping ARK publisher worker...")
        _scheduler.shutdown(wait=True)
        logger.info("ARK publisher worker stopped")
    
    shutdown_orchestrator()
    
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
    async def health_check(db: Session = Depends(get_db)):
        """Health check endpoint for load balancers."""
        from app.dependencies import get_orchestrator
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
        
        status = "healthy"
        response = {}
        
        # Check blockchain
        try:
            orchestrator = get_orchestrator()
            connected = orchestrator.is_connected()
            block = orchestrator.get_block_number() if connected else None
            response["blockchain_connected"] = connected
            response["current_block"] = block
            if not connected:
                status = "degraded"
        except Exception as e:
            response["blockchain_connected"] = False
            response["blockchain_error"] = str(e)
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
        
        # Check worker status
        if hasattr(app.state, "ark_publisher"):
            try:
                worker_stats = app.state.ark_publisher.get_stats()
                response["worker"] = {
                    "enabled": True,
                    "last_run": worker_stats.get("last_run_at"),
                }
            except Exception as e:
                response["worker"] = {"enabled": True, "error": str(e)}
        else:
            response["worker"] = {"enabled": False}
        
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
    )
