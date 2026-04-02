"""
Pytest configuration and fixtures.
"""

import os
import sys
import tempfile
import shutil
import atexit
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

# Use lightweight stubs for dark_core_lib during tests to avoid
# importing heavy web3 dependency graphs at collection time.
_STUBS_DIR = Path(__file__).parent / "stubs"
if _STUBS_DIR.exists():
    sys.path.insert(0, str(_STUBS_DIR))

# Load component env if it exists (prefer deployed integration config).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_INTEGRATION_ENV = _PROJECT_ROOT / ".env.integration"
_DEFAULT_ENV = _PROJECT_ROOT / ".env"
if _INTEGRATION_ENV.exists():
    load_dotenv(_INTEGRATION_ENV)
elif _DEFAULT_ENV.exists():
    load_dotenv(_DEFAULT_ENV)

# Set dummy env vars ONLY if not already set (fallback for CI/clean envs)
if not os.getenv("DARK_RPC_URL"):
    os.environ["DARK_RPC_URL"] = "http://localhost:8545"
if not os.getenv("DARK_CHAIN_ID"):
    os.environ["DARK_CHAIN_ID"] = "1337"
if not os.getenv("DARK_AUTHORITY_ADDRESS"):
    os.environ["DARK_AUTHORITY_ADDRESS"] = "0x0000000000000000000000000000000000000001"
if not os.getenv("DARK_CONTRACT_ADDRESS"):
    os.environ["DARK_CONTRACT_ADDRESS"] = "0x0000000000000000000000000000000000000002"
if not os.getenv("DARK_ADMIN_PRIVATE_KEY"):
    os.environ["DARK_ADMIN_PRIVATE_KEY"] = "0x0000000000000000000000000000000000000000000000000000000000000003"

# Default to a per-run local SQLite file for fast/offline test execution.
# You can still force PostgreSQL by setting TEST_DATABASE_URL explicitly.
_default_sqlite_dir = Path(tempfile.mkdtemp(prefix="dark_core_minter_tests_")).resolve()
atexit.register(lambda: shutil.rmtree(_default_sqlite_dir, ignore_errors=True))
_default_sqlite_path = _default_sqlite_dir / "test.sqlite"
_test_db_url = os.getenv("TEST_DATABASE_URL", f"sqlite:///{_default_sqlite_path}")
os.environ["DATABASE_URL"] = _test_db_url

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
import app.dependencies as dependencies_module
from app.dependencies import get_corelib_client, init_corelib_client, get_db
from app.database.models import Base


# Mock corelib client for tests
@pytest.fixture
def mock_corelib():
    """Create a mock DARKCoreClient."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.get_block_number.return_value = 12345
    mock.is_authorized_for_naan.return_value = True  # Always authorized in tests
    mock.ark_exists.return_value = False
    return mock


@pytest.fixture
def test_db_engine():
    """Create test database engine for tests."""
    engine_kwargs = {"pool_pre_ping": True}
    if _test_db_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(_test_db_url, **engine_kwargs)
    # Isolated schema per test function.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def test_db(test_db_engine):
    """Create a test database session."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_db_engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db_session(test_db):
    """Backward-compatible fixture used by worker tests."""

    class _CallableSession:
        def __init__(self, session):
            self._session = session

        def __call__(self):
            # Worker code expects SessionLocal()() and then calls close() in finally.
            # Return the wrapper itself so we can keep close() as a no-op and preserve
            # the underlying session for test assertions after the worker call.
            return self

        def close(self):
            """No-op close for worker tests using shared fixture session."""
            return None

        def __getattr__(self, item):
            return getattr(self._session, item)

    return _CallableSession(test_db)


@pytest.fixture
def override_get_db(test_db):
    """Override get_db dependency for tests."""
    def _get_test_db():
        try:
            yield test_db
        finally:
            pass
    return _get_test_db


@pytest.fixture
def client(mock_corelib, override_get_db):
    """
    Create a test client with mocked dependencies.
    """
    mock_metadata_storage = MagicMock()
    mock_metadata_storage.health_check.return_value = True

    # Override dependencies
    app.dependency_overrides[get_corelib_client] = lambda: mock_corelib
    app.dependency_overrides[get_db] = override_get_db
    dependencies_module._corelib_client = mock_corelib
    dependencies_module._metadata_storage = mock_metadata_storage
    
    # Mock the lifespan initialization to prevent blockchain connection
    with patch("app.main.init_corelib_client", return_value=mock_corelib):
        with patch("app.database.init_db"):  # Skip DB migrations in tests
            with patch("app.dependencies.init_metadata_storage", return_value=mock_metadata_storage):
                with TestClient(app) as test_client:
                    test_client.headers.update({"X-Authority-Id": "test-uuid"})
                    yield test_client
    
    # Clear overrides after test
    app.dependency_overrides.clear()
    dependencies_module._corelib_client = None
    dependencies_module._metadata_storage = None


@pytest.fixture
def mock_ark_info():
    """Create mock ARK info for tests."""
    from datetime import datetime
    from dark_core_lib.models import ARKInfo
    
    return ARKInfo(
        naan="12345",
        name="test001",
        url="https://example.org/doc/1",
        cid="bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
        owner="0x1234567890123456789012345678901234567890",
        created_at=datetime(2026, 1, 21, 12, 0, 0),
        updated_at=datetime(2026, 1, 21, 12, 0, 0),
    )


@pytest.fixture
def mock_authority_info():
    """Create mock authority info for tests."""
    from dark_core_lib.models import AuthorityInfo
    
    return AuthorityInfo(
        uuid="test-authority-uuid",
        wallet_address="0x1234567890123456789012345678901234567890",
        naans=["12345", "67890"],
        active=True,
    )
