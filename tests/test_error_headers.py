"""Tests for stable dARK error headers."""

from dark_core_lib.exceptions import (
    AuthorizationError,
    ConnectionError as CoreConnectionError,
    TransactionError,
)
from app.config import get_settings
from app.utils.noid import compute_checkdigit, encode_counter


def _valid_ark() -> str:
    settings = get_settings()
    stem = f"{settings.minter_shoulder}{encode_counter(0, settings.minter_noid_length)}"
    name = f"{stem}{compute_checkdigit(f'12345/{stem}')}"
    return f"ark:12345/{name}"


def test_authority_error_handler_sets_dark_headers(client, mock_corelib):
    """Authority errors should expose stable non-retryable headers."""
    mock_corelib.get_authority_by_uuid.side_effect = AuthorizationError("not authorized")

    response = client.get("/api/v1/authority/test-uuid")

    assert response.status_code == 403
    assert response.json()["error"] == "AUTHORIZATION_FAILED"
    assert response.json()["retryable"] is False
    assert response.headers["X-DARK-Error-Code"] == "AUTHORIZATION_FAILED"
    assert response.headers["X-DARK-Retryable"] == "false"


def test_core_connection_error_handler_sets_dark_headers(client, mock_corelib):
    """Connection errors should expose stable retryable headers."""
    mock_corelib.ark_exists.side_effect = CoreConnectionError("Cannot connect to RPC")

    response = client.get(f"/api/v1/arks/{_valid_ark()}")

    assert response.status_code == 503
    assert response.json()["error"] == "BLOCKCHAIN_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert response.headers["X-DARK-Error-Code"] == "BLOCKCHAIN_UNAVAILABLE"
    assert response.headers["X-DARK-Retryable"] == "true"


def test_transaction_error_handler_sets_dark_headers(client, mock_corelib):
    """Transaction errors should expose stable retryable headers and details."""
    mock_corelib.ark_exists.return_value = True
    mock_corelib.get_ark.side_effect = TransactionError(
        "Receipt timeout",
        tx_hash="0xabc",
        gas_used=42,
    )

    response = client.get(f"/api/v1/arks/{_valid_ark()}")

    assert response.status_code == 503
    assert response.json()["error"] == "BLOCKCHAIN_ERROR"
    assert response.json()["retryable"] is True
    assert response.json()["details"]["tx_hash"] == "0xabc"
    assert response.json()["details"]["gas_used"] == 42
    assert response.headers["X-DARK-Error-Code"] == "BLOCKCHAIN_ERROR"
    assert response.headers["X-DARK-Retryable"] == "true"
