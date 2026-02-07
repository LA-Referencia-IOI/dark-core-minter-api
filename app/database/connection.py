"""
Database connection and session management.
"""

import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

# Global engine and session factory
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the SQLAlchemy engine."""
    global _engine
    
    if _engine is None:
        settings = get_settings()
        settings.validate_database_config()

        # Create PostgreSQL engine with connection pooling.
        _engine = create_engine(
            settings.database_url,
            echo=settings.database_echo,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
        )
        
        logger.info(f"Created database engine for: {settings.database_url}")
    
    return _engine


def get_session_local():
    """Get or create the SessionLocal factory."""
    global _SessionLocal
    
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    return _SessionLocal


# Export SessionLocal for dependency injection
SessionLocal = get_session_local


def init_db() -> None:
    """
    Initialize the database.
    
    1. Runs Alembic migrations (fails fast if migration fails)
    2. Falls back to creating tables if Alembic not configured
    
    Raises:
        Exception: If migrations fail or database initialization fails
    """
    logger.info("Initializing database...")
    
    # Try to run Alembic migrations
    try:
        # Check if alembic.ini exists
        alembic_ini = Path("alembic.ini")
        if alembic_ini.exists():
            logger.info("Running Alembic migrations...")
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Alembic migrations completed successfully")
            logger.debug(result.stdout)
        else:
            logger.warning("alembic.ini not found, falling back to create_all()")
            # Fallback: create tables directly
            from app.database.models import Base
            engine = get_engine()
            Base.metadata.create_all(bind=engine)
            logger.info("Database tables created via create_all()")
    
    except subprocess.CalledProcessError as e:
        logger.error(f"Alembic migration failed: {e.stderr}")
        raise Exception(f"Database migration failed: {e.stderr}") from e
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def close_db() -> None:
    """Close database connections and dispose of the engine."""
    global _engine
    
    if _engine is not None:
        logger.info("Closing database connections...")
        _engine.dispose()
        _engine = None
        logger.info("Database connections closed")
