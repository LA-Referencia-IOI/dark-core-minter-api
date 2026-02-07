"""
Additional tests for ARK persistence and error handling.
"""

import json
import pytest
from unittest.mock import patch
from app.models.states import ARKState
from app.database.models import ARKRecord
from app.config import get_settings
from app.utils.noid import ALPHABET, compute_checkdigit, encode_counter


def _expected_name(naan: str, counter: int) -> str:
    """Build expected deterministic name using current runtime settings."""
    settings = get_settings()
    stem = f"{settings.minter_shoulder}{encode_counter(counter, settings.minter_noid_length)}"
    if settings.minter_noid_checkdigit:
        return f"{stem}{compute_checkdigit(f'{naan}/{stem}')}"
    return stem


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


def test_reserve_ark_uses_counter_sequence(client, mock_orchestrator):
    """Reserved ARKs should use deterministic sequence values per namespace."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    first = client.post(
        "/api/v1/arks",
        json={"authority_id": "test-uuid", "naan": "12345"},
    )
    second = client.post(
        "/api/v1/arks",
        json={"authority_id": "test-uuid", "naan": "12345"},
    )

    assert first.status_code == 201
    assert second.status_code == 201

    first_name = first.json()["ark"].split("/", 1)[1]
    second_name = second.json()["ark"].split("/", 1)[1]

    expected_first = _expected_name("12345", 0)
    expected_second = _expected_name("12345", 1)

    assert first_name == expected_first
    assert second_name == expected_second


def test_reserve_ark_skips_collided_counter_value(client, test_db, mock_orchestrator):
    """If a generated name already exists, reservation should advance to next counter."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    first_name = _expected_name("12345", 0)

    from app.repositories import ARKRepository

    ARKRepository(test_db).create_reserved(
        naan="12345",
        name=first_name,
        authority_id="someone-else",
    )
    test_db.commit()

    response = client.post(
        "/api/v1/arks",
        json={"authority_id": "test-uuid", "naan": "12345"},
    )

    assert response.status_code == 201
    name = response.json()["ark"].split("/", 1)[1]
    expected = _expected_name("12345", 1)
    assert name == expected


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


def test_update_ark_in_draft_overwrites_payload(client, test_db, mock_orchestrator):
    """Updating an ARK already in DRAFT should overwrite pending payload."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # Create a DRAFT ARK directly in DB
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    name = _expected_name("12345", 42)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    # Manually change to DRAFT
    db_ark.state = ARKState.DRAFT
    db_ark.target = "https://example.org"
    db_ark.metadata_format = "json"
    db_ark.metadata_cid = "abc123"
    test_db.commit()
    
    # Update again (should overwrite and remain DRAFT)
    response = client.put(
        f"/api/v1/arks/ark:12345/{name}",
        json={
            "authority_id": "test-uuid",
            "target": "https://new-url.org",
            "metadata": json.dumps({"new": "data"}),
            "metadata_format": "json",
        },
    )
    
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == ARKState.DRAFT
    assert body["target"] == "https://new-url.org"
    assert body["metadata_format"] == "json"
    assert body["metadata_cid"] is not None


def test_update_ark_published_transitions_to_update(client, test_db, mock_orchestrator):
    """Updating a local PUBLISHED ARK should transition it to UPDATE."""
    from app.repositories import ARKRepository

    repo = ARKRepository(test_db)
    name = _expected_name("12345", 43)
    db_ark = repo.create_published_import(
        naan="12345",
        name=name,
        authority_id="test-uuid",
        target="https://published.example/original",
        metadata_cid="cid-original",
        metadata_format="json",
    )
    test_db.commit()

    response = client.put(
        f"/api/v1/arks/ark:12345/{name}",
        json={
            "authority_id": "test-uuid",
            "target": "https://published.example/new",
            "metadata": json.dumps({"title": "Updated Title"}),
            "metadata_format": "json",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == ARKState.UPDATE
    assert data["target"] == "https://published.example/new"
    assert data["metadata_cid"] is not None

    test_db.refresh(db_ark)
    assert db_ark.state == ARKState.UPDATE


def test_update_ark_imports_blockchain_record_when_missing(client, test_db, mock_orchestrator):
    """If ARK is missing in DB but exists on-chain, API imports and queues UPDATE."""
    from types import SimpleNamespace

    imported_name = _expected_name("12345", 700)
    mock_orchestrator.ark_exists.return_value = True
    mock_orchestrator.get_ark.return_value = SimpleNamespace(
        naan="12345",
        name=imported_name,
        url="https://chain.example/original",
        cid="cid-chain",
        owner="0xabc",
    )

    response = client.put(
        f"/api/v1/arks/ark:12345/{imported_name}",
        json={
            "authority_id": "test-uuid",
            "target": "https://chain.example/new",
            "metadata": json.dumps({"title": "Imported then updated"}),
            "metadata_format": "json",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ark"] == f"ark:12345/{imported_name}"
    assert data["state"] == ARKState.UPDATE
    assert data["target"] == "https://chain.example/new"
    assert data["metadata_cid"] is not None

    db_ark = test_db.query(ARKRecord).filter_by(naan="12345", name=imported_name).first()
    assert db_ark is not None
    assert db_ark.state == ARKState.UPDATE
    assert db_ark.authority_id == "test-uuid"


def test_update_to_published_requires_draft_state(test_db):
    """Repository CAS should reject publish transition when state is not DRAFT."""
    from app.repositories import ARKRepository

    repo = ARKRepository(test_db)
    name = _expected_name("12345", 52)
    repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    test_db.commit()

    with pytest.raises(ValueError, match=r"one of \[D, U\]"):
        repo.update_to_published(f"ark:12345/{name}", "cid-test")


def test_tombstone_transition_is_idempotent(test_db):
    """Tombstone transition should be safe to call multiple times."""
    from app.repositories import ARKRepository

    repo = ARKRepository(test_db)
    name = _expected_name("12345", 53)
    repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    test_db.commit()

    first = repo.update_to_tombstone(f"ark:12345/{name}")
    test_db.commit()
    second = repo.update_to_tombstone(f"ark:12345/{name}")
    test_db.commit()

    assert first.state == ARKState.TOMBSTONE
    assert second.state == ARKState.TOMBSTONE


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
    name = _expected_name("12345", 77)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
        alternate_identifiers=[{"schema": "doi", "value": "10.1234/test"}],
    )
    test_db.commit()
    
    # Get ARK
    response = client.get(f"/api/v1/arks/ark:12345/{name}")
    
    assert response.status_code == 200
    data = response.json()
    assert data["ark"] == f"ark:12345/{name}"
    assert data["state"] == ARKState.RESERVED
    assert data["alternate_identifiers"][0]["schema"] == "doi"


def test_tombstone_ark(client, test_db, mock_orchestrator):
    """Test tombstoning an ARK."""
    mock_orchestrator.is_authorized_for_naan.return_value = True
    
    # Create ARK
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    name = _expected_name("12345", 88)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    test_db.commit()
    
    # Delete/Tombstone
    response = client.delete(f"/api/v1/arks/ark:12345/{name}")
    
    assert response.status_code == 200
    
    # Verify in DB
    test_db.refresh(db_ark)
    assert db_ark.state == ARKState.TOMBSTONE
    assert db_ark.tombstoned_at is not None


def test_get_ark_rejects_invalid_checkdigit_when_enabled(client):
    """GET should reject ARKs with invalid checkdigit when strict mode is enabled."""
    settings = get_settings()
    if not settings.minter_noid_checkdigit:
        pytest.skip("checkdigit validation disabled")

    stem = f"{settings.minter_shoulder}{encode_counter(0, settings.minter_noid_length)}"
    valid = compute_checkdigit(f"12345/{stem}")
    invalid = next(ch for ch in ALPHABET if ch != valid)
    response = client.get(f"/api/v1/arks/ark:12345/{stem}{invalid}")

    assert response.status_code == 400
    assert "checkdigit" in response.json()["detail"].lower()


def test_health_check_with_db(client):
    """Test health check includes database status."""
    response = client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    assert "database" in data
    assert data["database"] == "healthy"
    assert "status" in data
