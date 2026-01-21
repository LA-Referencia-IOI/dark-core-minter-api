"""
Pytest configuration and fixtures.
"""

import os
import pytest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

# Load .env if it exists (prioritize real config for integration tests if desired)
load_dotenv()

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

from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import get_orchestrator, init_orchestrator


# Mock orchestrator for tests
@pytest.fixture
def mock_orchestrator():
    """Create a mock DARKOrchestrator."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.get_block_number.return_value = 12345
    return mock


@pytest.fixture
def client(mock_orchestrator):
    """
    Create a test client with mocked dependencies.
    """
    # Override the orchestrator dependency
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    
    # Mock the lifespan initialization to prevent blockchain connection
    with patch("app.main.init_orchestrator", return_value=mock_orchestrator):
        with TestClient(app) as test_client:
            yield test_client
    
    # Clear overrides after test
    app.dependency_overrides.clear()


@pytest.fixture
def mock_ark_info():
    """Create mock ARK info for tests."""
    from datetime import datetime
    from dark_orchestrator.ark import ARKInfo
    
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
    from dark_orchestrator.authority import AuthorityInfo
    
    return AuthorityInfo(
        uuid="test-authority-uuid",
        wallet_address="0x1234567890123456789012345678901234567890",
        naans=["12345", "67890"],
        active=True,
    )
