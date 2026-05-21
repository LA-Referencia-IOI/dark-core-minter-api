"""
Database connection and session management.
"""

import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
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
    Initialize database connectivity without mutating schema.

    Schema migrations are intentionally run on demand through run_migrations()
    or the Docker entrypoint "migrate" command.

    Raises:
        Exception: If the database cannot be reached.
    """
    logger.info("Initializing database...")

    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("Database connection verified successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def run_migrations() -> None:
    """
    Run Alembic migrations on demand.

    This is deliberately separate from API and worker startup so multiprocess
    servers do not run concurrent migrations.
    """
    alembic_ini = Path("alembic.ini")
    if not alembic_ini.exists():
        raise FileNotFoundError("alembic.ini not found; cannot run migrations")

    logger.info("Running Alembic migrations...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Alembic migrations completed successfully")
        logger.debug(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"Alembic migration failed: {e.stderr}")
        raise Exception(f"Database migration failed: {e.stderr}") from e


def close_db() -> None:
    """Close database connections and dispose of the engine."""
    global _engine
    
    if _engine is not None:
        logger.info("Closing database connections...")
        _engine.dispose()
        _engine = None
        logger.info("Database connections closed")
