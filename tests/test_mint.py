"""
Tests for mint endpoints.
"""

import pytest
from unittest.mock import MagicMock


def test_batch_mint_single_success(client, mock_orchestrator, mock_ark_info):
    """Test batch mint with a single successful item."""
    mock_orchestrator.ark_exists.return_value = False
    mock_orchestrator.create_ark.return_value = mock_ark_info
    
    response = client.post(
        "/api/v1/mint/batch",
        json={
            "items": [
                {
                    "authority_id": "test-uuid",
                    "naan": "12345",
                    "name": "test001",
                    "url": "https://example.org/doc/1",
                    "cid": "bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
                }
            ]
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert len(data["results"]) == 1
    assert data["results"][0]["status"] == "success"


def test_batch_mint_already_exists(client, mock_orchestrator, mock_ark_info):
    """Test batch mint when ARK already exists."""
    mock_orchestrator.ark_exists.return_value = True
    mock_orchestrator.get_ark.return_value = mock_ark_info
    
    response = client.post(
        "/api/v1/mint/batch",
        json={
            "items": [
                {
                    "authority_id": "test-uuid",
                    "naan": "12345",
                    "name": "test001",
                    "url": "https://example.org/doc/1",
                    "cid": "bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
                }
            ]
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["status"] == "already_exists"


def test_batch_mint_authorization_error(client, mock_orchestrator):
    """Test batch mint with authorization failure."""
    from dark_orchestrator.exceptions import AuthorityError
    
    mock_orchestrator.ark_exists.return_value = False
    mock_orchestrator.create_ark.side_effect = AuthorityError("Authority not authorized for NAAN")
    
    response = client.post(
        "/api/v1/mint/batch",
        json={
            "items": [
                {
                    "authority_id": "unauthorized-uuid",
                    "naan": "99999",
                    "name": "test001",
                    "url": "https://example.org/doc/1",
                    "cid": "bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
                }
            ]
        },
    )
    
    assert response.status_code == 200  # Batch returns 200 with per-item errors
    data = response.json()
    assert data["status"] == "error"
    assert data["results"][0]["status"] == "authorization_error"


def test_batch_mint_partial_success(client, mock_orchestrator, mock_ark_info):
    """Test batch mint with mixed results."""
    from dark_orchestrator.exceptions import AuthorityError
    
    # First call succeeds, second fails
    mock_orchestrator.ark_exists.return_value = False
    mock_orchestrator.create_ark.side_effect = [
        mock_ark_info,
        AuthorityError("Not authorized"),
    ]
    
    response = client.post(
        "/api/v1/mint/batch",
        json={
            "items": [
                {
                    "authority_id": "test-uuid",
                    "naan": "12345",
                    "name": "test001",
                    "url": "https://example.org/doc/1",
                    "cid": "bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
                },
                {
                    "authority_id": "bad-uuid",
                    "naan": "99999",
                    "name": "test002",
                    "url": "https://example.org/doc/2",
                    "cid": "bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
                },
            ]
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "partial_success"
    assert data["results"][0]["status"] == "success"
    assert data["results"][1]["status"] == "authorization_error"


def test_batch_mint_exceeds_limit(client, mock_orchestrator):
    """Test batch mint with too many items."""
    # Create 101 items (default limit is 100)
    items = [
        {
            "authority_id": "test-uuid",
            "naan": "12345",
            "name": f"test{i:03d}",
            "url": f"https://example.org/doc/{i}",
            "cid": "bafybeigdyrzt5sfp7udbbkc5dla2yv5ifyrkkwdxgper",
        }
        for i in range(101)
    ]
    
    response = client.post("/api/v1/mint/batch", json={"items": items})
    
    assert response.status_code == 400
    assert "exceeds limit" in response.json()["detail"]
