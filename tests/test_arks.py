"""
Tests for ARK lifecycle endpoints.
"""

import pytest
from unittest.mock import MagicMock
from app.models.states import ARKState


def test_reserve_ark(client, mock_orchestrator):
    """Test reserving a single ARK."""
    # Mock checks
    mock_orchestrator.ark_exists.return_value = False
    
    response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
            "alternate_identifiers": [
                {"schema": "doi", "value": "10.1234/test"}
            ]
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == ARKState.RESERVED
    # Default shoulder in config is empty string unless mocked
    assert data["ark"].startswith("ark:/12345/")
    assert data["target"] is None
    assert data["alternate_identifiers"][0]["schema"] == "doi"
    assert data["alternate_identifiers"][0]["value"] == "10.1234/test"


def test_batch_reserve(client):
    """Test batch reservation."""
    response = client.post(
        "/api/v1/arks/batch",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
            "items": [
                {
                    "target": "http://example.com/1",
                    "alternate_identifiers": [{"schema": "oai", "value": "oai:1"}],
                    "client_item_id": "req-001"
                },
                {
                    "target": "http://example.com/2",
                    "client_item_id": "req-002"
                }
            ]
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["client_item_id"] == "req-001"
    assert data["results"][1]["client_item_id"] == "req-002"
    assert data["results"][0]["alternate_identifiers"][0]["value"] == "oai:1"
    assert data["results"][0]["target"] == "http://example.com/1"
    assert data["results"][1]["alternate_identifiers"] is None


def test_update_metadata_publish(client, mock_orchestrator):
    """Test updating metadata triggers publication."""
    # Case: ARK reserved (exists=False), then Publish (create_ark)
    mock_orchestrator.ark_exists.return_value = False
    
    # Mock create_ark to return something or just pass
    mock_orchestrator.create_ark.return_value = MagicMock(ark_id="ark:/12345/res1")
    
    response = client.put(
        "/api/v1/arks/ark:/12345/res1",
        json={
            "authority_id": "test-uuid",
            "target": "http://new.target.com",
            "metadata": {"title": "My Research"},
            "alternate_identifiers": [
                 {"schema": "doi", "value": "10.1234/new"}
            ]
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == ARKState.PUBLISHED
    assert data["target"] == "http://new.target.com"
    assert data["alternate_identifiers"][0]["value"] == "10.1234/new"
    
    # Verify create_ark was called
    mock_orchestrator.create_ark.assert_called_once()
    args = mock_orchestrator.create_ark.call_args
    assert args.kwargs["url"] == "http://new.target.com"


def test_delete_tombstone(client, mock_orchestrator):
    """Test delete (tombstone)."""
    mock_orchestrator.ark_exists.return_value = True
    
    response = client.delete("/api/v1/arks/ark:/12345/todelete")
    
    assert response.status_code == 200  # Returns null/void
