"""
Additional tests for ARK persistence and error handling.
"""

import json
import pytest
from unittest.mock import patch
from app.models.states import ARKState
from app.database.models import ARKRecord


def test_reserve_ark_persists_to_db(client, test_db, mock_orchestrator):
    """Test that reserving an ARK persists to database."""
    # Ensure authorization passes
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    
    assert response.status_code == 201
    data = response.json()
    
    # Parse the returned ARK to get naan and name
    from app.repositories.ark_repository import parse_ark
    naan, name = parse_ark(data["ark"])
    
    # Verify in database using naan and name
    db_ark = test_db.query(ARKRecord).filter_by(naan=naan, name=name).first()
    assert db_ark is not None
    assert db_ark.state == ARKState.RESERVED
    assert db_ark.authority_id == "test-uuid"
    assert db_ark.naan == "12345"


def test_reserve_ark_unauthorized_naan(client, mock_orchestrator):
    """Test that unauthorized NAAN is rejected."""
    # Simulate authorization failure
    mock_orchestrator.is_authorized_for_naan.return_value = False
    
    response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "99999",
        },
    )
    
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_update_ark_to_draft(client, test_db, mock_orchestrator):
    """Test updating ARK from RESERVED to DRAFT."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # 1. Reserve an ARK
    reserve_response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )
    assert reserve_response.status_code == 201
    ark = reserve_response.json()["ark"]
    
    # 2. Update to DRAFT with new metadata format
    metadata_content = json.dumps({"title": "Test Resource", "author": "Test Author"})
    update_response = client.put(
        f"/api/v1/arks/{ark}",
        json={
            "authority_id": "test-uuid",
            "target": "https://example.org/resource",
            "metadata": metadata_content,
            "metadata_format": "json",
        },
    )
    
    assert update_response.status_code == 200
    data = update_response.json()
    assert data["state"] == ARKState.DRAFT
    assert data["target"] == "https://example.org/resource"
    assert data.get("metadata_cid") is not None  # CID is now set at update time
    assert data.get("metadata_format") == "json"
    
    # 3. Verify in database
    from app.repositories.ark_repository import parse_ark
    naan, name = parse_ark(ark)
    db_ark = test_db.query(ARKRecord).filter_by(naan=naan, name=name).first()
    assert db_ark.state == ARKState.DRAFT
    assert db_ark.target == "https://example.org/resource"
    assert db_ark.metadata_format == "json"
    assert db_ark.metadata_cid is not None


def test_update_ark_wrong_state(client, test_db, mock_orchestrator):
    """Test that updating non-RESERVED ARK fails."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # Create a DRAFT ARK directly in DB
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name="test123",
        authority_id="test-uuid",
    )
    # Manually change to DRAFT
    db_ark.state = ARKState.DRAFT
    db_ark.target = "https://example.org"
    db_ark.metadata_format = "json"
    db_ark.metadata_cid = "abc123"
    test_db.commit()
    
    # Try to update again (should fail)
    response = client.put(
        "/api/v1/arks/ark:12345/test123",
        json={
            "authority_id": "test-uuid",
            "target": "https://new-url.org",
            "metadata": json.dumps({"new": "data"}),
            "metadata_format": "json",
        },
    )
    
    assert response.status_code == 400
    assert "RESERVED state" in response.json()["detail"]


def test_batch_partial_failure(client, test_db, mock_orchestrator):
    """Test batch reservation with some failures."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # Create a conflict by pre-creating an ARK
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    existing_ark = ark_repo.create_reserved(
        naan="12345",
        name="conflict",
        authority_id="test-uuid",
    )
    test_db.commit()
    
    response = client.post(
        "/api/v1/arks/batch",
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
            "items": [
                {"client_item_id": "item-1"},
                {"client_item_id": "item-2"},
                {"client_item_id": "item-3"},
            ],
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should have some successes
    assert len(data["results"]) > 0
    
    # Check that each result has valid data
    for result in data["results"]:
        assert result["state"] == ARKState.RESERVED
        assert result["ark"].startswith("ark:12345/")


def test_batch_partial_failure_preserves_previous_successes(client, test_db, mock_orchestrator):
    """A failed batch item must not rollback previously successful items."""
    mock_orchestrator.is_authorized_for_naan.return_value = True

    with patch(
        "app.api.arks.mint_ark_id",
        side_effect=[
            "ark:12345/fixed-a",  # success
            "ark:12345/fixed-a",  # duplicate -> fail
            "ark:12345/fixed-b",  # success
        ],
    ):
        response = client.post(
            "/api/v1/arks/batch",
            json={
                "authority_id": "test-uuid",
                "naan": "12345",
                "items": [
                    {"client_item_id": "item-1"},
                    {"client_item_id": "item-2"},
                    {"client_item_id": "item-3"},
                ],
            },
        )

    assert response.status_code == 200
    data = response.json()

    assert len(data["results"]) == 2
    assert len(data["errors"]) == 1

    returned_arks = {result["ark"] for result in data["results"]}
    assert returned_arks == {"ark:12345/fixed-a", "ark:12345/fixed-b"}

    persisted_arks = {row.ark for row in test_db.query(ARKRecord).all()}
    assert persisted_arks == returned_arks


def test_get_ark_from_db(client, test_db, mock_orchestrator):
    """Test getting ARK that only exists in DB (RESERVED/DRAFT)."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # Create ARK in DB
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name="dbonly",
        authority_id="test-uuid",
        alternate_identifiers=[{"schema": "doi", "value": "10.1234/test"}],
    )
    test_db.commit()
    
    # Get ARK
    response = client.get("/api/v1/arks/ark:12345/dbonly")
    
    assert response.status_code == 200
    data = response.json()
    assert data["ark"] == "ark:12345/dbonly"
    assert data["state"] == ARKState.RESERVED
    assert data["alternate_identifiers"][0]["schema"] == "doi"


def test_tombstone_ark(client, test_db, mock_orchestrator):
    """Test tombstoning an ARK."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # Create ARK
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name="todelete",
        authority_id="test-uuid",
    )
    test_db.commit()
    
    # Delete/Tombstone
    response = client.delete("/api/v1/arks/ark:12345/todelete")
    
    assert response.status_code == 200
    
    # Verify in DB
    test_db.refresh(db_ark)
    assert db_ark.state == ARKState.TOMBSTONE
    assert db_ark.tombstoned_at is not None


def test_health_check_with_db(client):
    """Test health check includes database status."""
    response = client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    assert "database" in data
    assert data["database"] == "healthy"
    assert "status" in data
