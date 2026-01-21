"""
Tests for lookup endpoints.
"""

import pytest


def test_lookup_url_not_found(client, mock_orchestrator):
    """Test URL lookup when no ARK exists."""
    response = client.post(
        "/api/v1/lookup/url",
        json={"url": "https://example.org/doc/new"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["exists"] is False
    assert data["dark_id"] is None


def test_lookup_url_invalid(client, mock_orchestrator):
    """Test lookup with missing URL field."""
    response = client.post(
        "/api/v1/lookup/url",
        json={},  # Missing required field
    )
    
    assert response.status_code == 422  # Validation error
