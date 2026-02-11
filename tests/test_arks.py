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
        },
    )
    
    assert response.status_code == 201
    data = response.json()
    assert data["state"] == ARKState.RESERVED
    # Default shoulder in config is empty string unless mocked
    assert data["ark"].startswith("ark:12345/")
    assert data.get("target") is None
    assert data.get("minimal_metadata") is None


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
    assert data["results"][0].get("target") is None
    assert data["results"][1].get("minimal_metadata") is None


def test_update_metadata_publish(client, mock_orchestrator):
    """Test updating metadata transitions ARK to DRAFT."""
    reserve_response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    assert reserve_response.status_code == 201
    ark = reserve_response.json()["ark"]

    response = client.put(
        f"/api/v1/arks/{ark}",
        json={
            "authority_id": "test-uuid",
            "target": "http://new.target.com",
            # New two-level metadata structure
            "minimal_metadata": {
                "title": "My Research",
                "authors": ["Doe, John"],
                "year": 2023,
                "publisher": "Test Publisher",
                "alternate_identifiers": [
                    {"schema": "doi", "value": "10.1234/new"}
                ],
            },
            "original_metadata": "<raw>metadata</raw>",
            "metadata_schema": "dublin_core",
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == ARKState.DRAFT
    assert data["target"] == "http://new.target.com"
    # metadata_cid is now null until worker runs
    assert data.get("metadata_cid") is None
    assert data["metadata_schema"] == "dublin_core"
    assert "metadata_format" not in data
    assert data["minimal_metadata"]["alternate_identifiers"][0]["value"] == "10.1234/new"
    
    # Publication to chain is handled by the background worker, not this endpoint.
    mock_orchestrator.create_ark.assert_not_called()


def test_update_rejects_legacy_level1_metadata_field(client):
    """Legacy level1_metadata field should be rejected; use minimal_metadata."""
    reserve_response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    assert reserve_response.status_code == 201
    ark = reserve_response.json()["ark"]

    response = client.put(
        f"/api/v1/arks/{ark}",
        json={
            "authority_id": "test-uuid",
            "target": "http://legacy.target.com",
            "level1_metadata": {
                "title": "Legacy payload",
                "authors": ["Doe, Legacy"],
                "year": 2026,
            },
            "original_metadata": "<raw>legacy</raw>",
            "metadata_schema": "dublin_core",
        },
    )

    assert response.status_code == 422


def test_reserve_rejects_top_level_alternate_identifiers(client):
    """Reserve must reject deprecated top-level alternate identifiers."""
    response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
            "alternate_identifiers": [{"schema": "doi", "value": "10.1234/test"}],
        },
    )

    assert response.status_code == 422


def test_update_rejects_top_level_alternate_identifiers(client):
    """Update must reject deprecated top-level alternate identifiers."""
    reserve_response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    assert reserve_response.status_code == 201
    ark = reserve_response.json()["ark"]

    response = client.put(
        f"/api/v1/arks/{ark}",
        json={
            "authority_id": "test-uuid",
            "target": "http://new.target.com",
            "minimal_metadata": {
                "title": "My Research",
                "authors": ["Doe, John"],
                "year": 2023,
            },
            "original_metadata": "<raw>metadata</raw>",
            "metadata_schema": "dublin_core",
            "alternate_identifiers": [{"schema": "doi", "value": "10.1234/new"}],
        },
    )

    assert response.status_code == 422


def test_delete_tombstone(client, mock_orchestrator):
    """Test delete (tombstone)."""
    reserve_response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    assert reserve_response.status_code == 201
    ark = reserve_response.json()["ark"]
    
    response = client.delete(f"/api/v1/arks/{ark}")
    
    assert response.status_code == 200  # Returns null/void


def test_update_metadata_xml_format(client, mock_orchestrator):
    """Test updating metadata with XML format."""
    reserve_response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    assert reserve_response.status_code == 201
    ark = reserve_response.json()["ark"]

    xml_metadata = '<?xml version="1.0"?><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"><dc:title>Test</dc:title></oai_dc:dc>'
    
    response = client.put(
        f"/api/v1/arks/{ark}",
        json={
            "authority_id": "test-uuid",
            "target": "http://example.com/resource",
            "minimal_metadata": {
                "title": "Test",
                "authors": ["Smith, Jane"],
                "year": 2024
            },
            "original_metadata": xml_metadata,
            "metadata_schema": "oai_dc",
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == ARKState.DRAFT
    assert data["metadata_schema"] == "oai_dc"
    assert "metadata_format" not in data
    assert isinstance(data.get("minimal_metadata"), dict)
    # CIDs are null initially
    assert data.get("metadata_cid") is None
