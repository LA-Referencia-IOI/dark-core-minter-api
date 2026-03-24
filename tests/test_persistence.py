"""
Additional tests for ARK persistence and error handling.
"""

from types import SimpleNamespace
import pytest
from unittest.mock import patch
from app.models.states import ARKState
from app.database.models import ARKRecord, ARKMetadata
from app.config import get_settings
from app.utils.noid import ALPHABET, compute_checkdigit, encode_counter


def _expected_name(naan: str, counter: int) -> str:
    """Build expected deterministic name using current runtime settings."""
    settings = get_settings()
    stem = f"{settings.minter_shoulder}{encode_counter(counter, settings.minter_noid_length)}"
    if settings.minter_noid_checkdigit:
        return f"{stem}{compute_checkdigit(f'{naan}/{stem}')}"
    return stem


def _build_update_payload(
    authority_id: str = "test-uuid",
    target: str = "https://example.org/resource",
    title: str = "Test Resource",
    year: int = 2024,
    metadata_schema: str = "dublin_core",
    original_metadata: str = "<raw>metadata</raw>",
    metadata_media_type: str = "application/xml",
) -> dict:
    """Build a valid two-level metadata update payload."""
    return {
        "authority_id": authority_id,
        "target": target,
        "minimal_metadata": {
            "title": title,
            "authors": ["Test Author"],
            "year": year,
        },
        "original_metadata": original_metadata,
        "metadata_schema": metadata_schema,
        "metadata_media_type": metadata_media_type,
    }


def test_reserve_ark_persists_to_db(client, test_db, mock_corelib):
    """Test that reserving an ARK persists to database."""
    # Ensure authorization passes
    mock_corelib.is_authorized_for_naan.return_value = True
    
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


def test_reserve_ark_uses_counter_sequence(client, mock_corelib):
    """Reserved ARKs should use deterministic sequence values per namespace."""
    mock_corelib.is_authorized_for_naan.return_value = True
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


def test_reserve_ark_skips_collided_counter_value(client, test_db, mock_corelib):
    """If a generated name already exists, reservation should advance to next counter."""
    mock_corelib.is_authorized_for_naan.return_value = True
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


def test_reserve_ark_unauthorized_naan(client, mock_corelib):
    """Test that unauthorized NAAN is rejected."""
    # Simulate authorization failure
    mock_corelib.is_authorized_for_naan.return_value = False
    
    response = client.post(
        "/api/v1/arks",
        json={
            "authority_id": "test-uuid",
            "naan": "99999",
        },
    )
    
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_reserve_rejects_mismatched_authority_header(client):
    """Mutating requests must authenticate as the same authority they declare."""
    response = client.post(
        "/api/v1/arks",
        headers={"X-Authority-Id": "other-uuid"},
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )

    assert response.status_code == 403
    assert "does not match" in response.json()["detail"].lower()


def test_reserve_requires_authority_header_when_mtls_disabled(client):
    """Local/dev mode must not allow anonymous mutating requests."""
    response = client.post(
        "/api/v1/arks",
        headers={"X-Authority-Id": " "},
        json={
            "authority_id": "test-uuid",
            "naan": "12345",
        },
    )

    assert response.status_code == 401
    assert "authority identity required" in response.json()["detail"].lower()


def test_update_ark_to_draft(client, test_db, mock_corelib):
    """Test updating ARK from RESERVED to DRAFT."""
    mock_corelib.is_authorized_for_naan.return_value = True
    
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
    
    # 2. Update to DRAFT with two-level metadata payload
    update_response = client.put(
        f"/api/v1/arks/{ark}",
        json=_build_update_payload(target="https://example.org/resource"),
    )
    
    assert update_response.status_code == 200
    data = update_response.json()
    assert data["state"] == ARKState.DRAFT
    assert data["target"] == "https://example.org/resource"
    assert data.get("metadata_cid") is None
    assert data.get("metadata_schema") == "dublin_core"
    
    # 3. Verify in database (ARK + two-level metadata)
    from app.repositories.ark_repository import parse_ark
    naan, name = parse_ark(ark)
    db_ark = test_db.query(ARKRecord).filter_by(naan=naan, name=name).first()
    assert db_ark.state == ARKState.DRAFT
    assert db_ark.target == "https://example.org/resource"

    db_meta = test_db.query(ARKMetadata).filter_by(ark_record_id=db_ark.id).first()
    assert db_meta is not None
    assert db_meta.original_schema == "dublin_core"
    assert db_meta.level1_json["title"] == "Test Resource"
    assert db_meta.level1_json["original_metadata"]["media_type"] == "application/xml"
    assert db_meta.level1_json["original_metadata"]["cid"] is None
    assert db_meta.level1_cid is None
    assert db_meta.original_cid is None


def test_update_ark_in_draft_overwrites_payload(client, test_db, mock_corelib):
    """Updating an ARK already in DRAFT should overwrite pending payload."""
    mock_corelib.is_authorized_for_naan.return_value = True
    
    # Create a DRAFT ARK directly in DB
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    name = _expected_name("12345", 42)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    test_db.flush()
    # Manually change to DRAFT with existing two-level metadata
    db_ark.state = ARKState.DRAFT
    db_ark.target = "https://example.org"
    old_meta = ark_repo.create_or_update_metadata(
        ark_record_id=db_ark.id,
        level1_json={
            "title": "Old title",
            "authors": ["Old Author"],
            "year": 2021,
            "original_metadata": {
                "schema": "dublin_core",
                "media_type": "application/xml",
                "cid": None,
            },
        },
        original_content="<old>raw</old>",
        original_schema="dublin_core",
    )
    old_meta.level1_cid = "old-l1-cid"
    old_meta.original_cid = "old-l2-cid"
    test_db.commit()
    
    # Update again (should overwrite and remain DRAFT)
    response = client.put(
        f"/api/v1/arks/ark:12345/{name}",
        json=_build_update_payload(
            target="https://new-url.org",
            title="New title",
            year=2026,
            original_metadata="<new>raw</new>",
        ),
    )
    
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == ARKState.DRAFT
    assert body["target"] == "https://new-url.org"
    assert body.get("metadata_schema") == "dublin_core"
    assert body.get("level1_cid") is None
    assert body.get("level2_cid") is None

    test_db.refresh(db_ark)
    db_meta = test_db.query(ARKMetadata).filter_by(ark_record_id=db_ark.id).first()
    assert db_meta.level1_json["title"] == "New title"
    assert db_meta.original_content == "<new>raw</new>"
    assert db_meta.level1_cid is None
    assert db_meta.original_cid is None


def test_update_ark_published_transitions_to_update(client, test_db, mock_corelib):
    """Updating a local PUBLISHED ARK should transition it to UPDATE."""
    from app.repositories import ARKRepository

    repo = ARKRepository(test_db)
    name = _expected_name("12345", 43)
    db_ark = repo.create_published_import(
        naan="12345",
        name=name,
        authority_id="test-uuid",
        target="https://published.example/original",
    )
    test_db.commit()

    response = client.put(
        f"/api/v1/arks/ark:12345/{name}",
        json=_build_update_payload(
            target="https://published.example/new",
            title="Updated Title",
            year=2025,
            original_metadata="<updated>raw</updated>",
        ),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == ARKState.UPDATE
    assert data["target"] == "https://published.example/new"
    assert data.get("metadata_cid") is None
    assert data["metadata_schema"] == "dublin_core"
    assert data.get("level1_cid") is None
    assert data.get("level2_cid") is None

    test_db.refresh(db_ark)
    assert db_ark.state == ARKState.UPDATE
    db_meta = test_db.query(ARKMetadata).filter_by(ark_record_id=db_ark.id).first()
    assert db_meta is not None
    assert db_meta.level1_json["title"] == "Updated Title"
    assert db_meta.level1_cid is None
    assert db_meta.original_cid is None


def test_update_ark_imports_blockchain_record_when_missing(client, test_db, mock_corelib):
    """If ARK is missing in DB but exists on-chain, API imports and queues UPDATE."""
    imported_name = _expected_name("12345", 700)
    mock_corelib.ark_exists.return_value = True
    mock_corelib.get_ark.return_value = SimpleNamespace(
        naan="12345",
        name=imported_name,
        url="https://chain.example/original",
        cid="cid-chain",
        owner="0xabc",
    )
    mock_corelib.get_authority_by_uuid.return_value = SimpleNamespace(
        uuid="test-uuid",
        wallet_address="0xabc",
        naans=["12345"],
        active=True,
    )

    response = client.put(
        f"/api/v1/arks/ark:12345/{imported_name}",
        json=_build_update_payload(
            target="https://chain.example/new",
            title="Imported then updated",
            year=2022,
            original_metadata="<imported>raw</imported>",
        ),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ark"] == f"ark:12345/{imported_name}"
    assert data["state"] == ARKState.UPDATE
    assert data["target"] == "https://chain.example/new"
    assert data["metadata_cid"] == "cid-chain"
    assert data["metadata_schema"] == "dublin_core"

    db_ark = test_db.query(ARKRecord).filter_by(naan="12345", name=imported_name).first()
    assert db_ark is not None
    assert db_ark.state == ARKState.UPDATE
    assert db_ark.authority_id == "test-uuid"
    db_meta = test_db.query(ARKMetadata).filter_by(ark_record_id=db_ark.id).first()
    assert db_meta is not None
    assert db_meta.level1_json["title"] == "Imported then updated"


def test_update_import_rejects_non_owner(client, mock_corelib):
    """Import-on-update must verify the requested authority owns the on-chain ARK."""
    imported_name = _expected_name("12345", 701)
    mock_corelib.ark_exists.return_value = True
    mock_corelib.get_ark.return_value = SimpleNamespace(
        naan="12345",
        name=imported_name,
        url="https://chain.example/original",
        cid="cid-chain",
        owner="0xabc",
    )
    mock_corelib.get_authority_by_uuid.return_value = SimpleNamespace(
        uuid="test-uuid",
        wallet_address="0xdef",
        naans=["12345"],
        active=True,
    )

    response = client.put(
        f"/api/v1/arks/ark:12345/{imported_name}",
        json=_build_update_payload(
            target="https://chain.example/new",
            title="Unauthorized import",
            year=2022,
            original_metadata="<imported>raw</imported>",
        ),
    )

    assert response.status_code == 403
    assert "does not own on-chain ark" in response.json()["detail"].lower()


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
        repo.update_to_published(f"ark:12345/{name}")


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


def test_batch_partial_failure(client, test_db, mock_corelib):
    """Test batch reservation with some failures."""
    mock_corelib.is_authorized_for_naan.return_value = True
    
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


def test_batch_partial_failure_preserves_previous_successes(client, test_db, mock_corelib):
    """A failed batch item must not rollback previously successful items."""
    mock_corelib.is_authorized_for_naan.return_value = True

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


def test_get_ark_from_db(client, test_db, mock_corelib):
    """Test getting ARK that only exists in DB (RESERVED/DRAFT)."""
    mock_corelib.is_authorized_for_naan.return_value = True
    
    # Create ARK in DB with level1 alternate identifiers
    from app.repositories import ARKRepository
    ark_repo = ARKRepository(test_db)
    name = _expected_name("12345", 77)
    db_ark = ark_repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    test_db.flush()
    db_ark.state = ARKState.DRAFT
    ark_repo.create_or_update_metadata(
        ark_record_id=db_ark.id,
        level1_json={
            "title": "Resource with DOI",
            "authors": ["Test Author"],
            "year": 2024,
            "alternate_identifiers": [{"schema": "doi", "value": "10.1234/test"}],
            "original_metadata": {
                "schema": "dublin_core",
                "media_type": "application/xml",
                "cid": None,
            },
        },
        original_content="<raw>metadata</raw>",
        original_schema="dublin_core",
    )
    test_db.commit()
    
    # Get ARK
    response = client.get(f"/api/v1/arks/ark:12345/{name}")
    
    assert response.status_code == 200
    data = response.json()
    assert data["ark"] == f"ark:12345/{name}"
    assert data["state"] == ARKState.DRAFT
    assert "metadata_format" not in data
    assert data["minimal_metadata"]["alternate_identifiers"][0]["schema"] == "doi"


def test_tombstone_ark(client, test_db, mock_corelib):
    """Test tombstoning an ARK."""
    mock_corelib.is_authorized_for_naan.return_value = True
    
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


def test_tombstone_rejects_non_owner(client, test_db):
    """DELETE must enforce ARK ownership."""
    from app.repositories import ARKRepository

    ark_repo = ARKRepository(test_db)
    name = _expected_name("12345", 89)
    ark_repo.create_reserved(
        naan="12345",
        name=name,
        authority_id="test-uuid",
    )
    test_db.commit()

    response = client.delete(
        f"/api/v1/arks/ark:12345/{name}",
        headers={"X-Authority-Id": "other-uuid"},
    )

    assert response.status_code == 403
    assert "does not match" in response.json()["detail"].lower()


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
