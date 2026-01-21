"""
Tests for resolve endpoints.
"""

import pytest
from datetime import datetime


def test_ark_exists_true(client, mock_orchestrator):
    """Test ARK exists check when ARK exists."""
    mock_orchestrator.ark_exists.return_value = True
    
    response = client.get("/api/v1/ark/12345/test001/exists")
    
    assert response.status_code == 200
    data = response.json()
    assert data["exists"] is True
    assert data["ark_id"] == "ark:/12345/test001"


def test_ark_exists_false(client, mock_orchestrator):
    """Test ARK exists check when ARK does not exist."""
    mock_orchestrator.ark_exists.return_value = False
    
    response = client.get("/api/v1/ark/12345/nonexistent/exists")
    
    assert response.status_code == 200
    data = response.json()
    assert data["exists"] is False


def test_resolve_ark_success(client, mock_orchestrator, mock_ark_info):
    """Test successful ARK resolution."""
    mock_orchestrator.get_ark.return_value = mock_ark_info
    
    response = client.get("/api/v1/resolve/12345/test001")
    
    assert response.status_code == 200
    data = response.json()
    assert data["ark_id"] == "ark:/12345/test001"
    assert data["url"] == "https://example.org/doc/1"
    assert "cid" in data


def test_get_ark_info_success(client, mock_orchestrator, mock_ark_info):
    """Test getting full ARK info."""
    mock_orchestrator.get_ark.return_value = mock_ark_info
    
    response = client.get("/api/v1/ark/12345/test001")
    
    assert response.status_code == 200
    data = response.json()
    assert data["naan"] == "12345"
    assert data["name"] == "test001"
    assert data["owner"] == mock_ark_info.owner
    assert "created_at" in data
    assert "updated_at" in data


def test_resolve_ark_not_found(client, mock_orchestrator):
    """Test resolving non-existent ARK."""
    from dark_orchestrator.exceptions import ARKError
    mock_orchestrator.get_ark.side_effect = ARKError("ARK not found")
    
    response = client.get("/api/v1/resolve/12345/nonexistent")
    
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "ARK_NOT_FOUND"
