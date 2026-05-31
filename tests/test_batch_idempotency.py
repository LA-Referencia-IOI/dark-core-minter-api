"""
Tests for idempotent batch ARK reservation by client item ID.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database.models import ARKRecord, NoidCounter
from app.models.states import ARKState
from app.repositories import ARKRepository


def _reserve_batch(client, authority_id="test-uuid", naan="12345", *client_item_ids):
    return client.post(
        "/api/v1/arks/batch",
        headers={"X-Authority-Id": authority_id},
        json={
            "authority_id": authority_id,
            "naan": naan,
            "items": [{"client_item_id": item_id} for item_id in client_item_ids],
        },
    )


def test_batch_reserve_repeated_client_item_returns_same_ark(client, test_db):
    first = _reserve_batch(client, "test-uuid", "12345", "oai:repo:123")
    second = _reserve_batch(client, "test-uuid", "12345", "oai:repo:123")

    assert first.status_code == 200
    assert second.status_code == 200

    first_ark = first.json()["results"][0]["ark"]
    second_ark = second.json()["results"][0]["ark"]
    assert second_ark == first_ark

    persisted = test_db.query(ARKRecord).filter(ARKRecord.client_item_id == "oai:repo:123").all()
    assert len(persisted) == 1

    counter = test_db.query(NoidCounter).one()
    assert counter.next_value == 1


def test_batch_reserve_duplicate_client_item_inside_same_batch_returns_same_ark(client, test_db):
    response = _reserve_batch(client, "test-uuid", "12345", "oai:repo:duplicate", "oai:repo:duplicate")

    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["ark"] == data["results"][1]["ark"]

    persisted = test_db.query(ARKRecord).filter(ARKRecord.client_item_id == "oai:repo:duplicate").all()
    assert len(persisted) == 1


@pytest.mark.parametrize(
    "state",
    [ARKState.RESERVED, ARKState.DRAFT, ARKState.UPDATE, ARKState.PUBLISHED],
)
def test_batch_reserve_returns_existing_active_ark_for_any_active_state(client, test_db, state):
    ark_repo = ARKRepository(test_db)
    existing = ark_repo.create_reserved(
        naan="12345",
        name=f"existing-{state.value.lower()}",
        authority_id="test-uuid",
        client_item_id=f"oai:repo:{state.value.lower()}",
    )
    test_db.flush()
    existing.state = state.value
    test_db.commit()

    response = _reserve_batch(client, "test-uuid", "12345", existing.client_item_id)

    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["ark"] == existing.ark
    assert data["results"][0]["state"] == state.value
    assert test_db.query(ARKRecord).filter(ARKRecord.client_item_id == existing.client_item_id).count() == 1


def test_batch_reserve_tombstoned_client_item_does_not_block_new_reservation(client, test_db):
    ark_repo = ARKRepository(test_db)
    tombstoned = ark_repo.create_reserved(
        naan="12345",
        name="old-tombstone",
        authority_id="test-uuid",
        client_item_id="oai:repo:tombstone",
    )
    test_db.flush()
    tombstoned.state = ARKState.TOMBSTONE.value
    test_db.commit()

    response = _reserve_batch(client, "test-uuid", "12345", "oai:repo:tombstone")

    assert response.status_code == 200
    new_ark = response.json()["results"][0]["ark"]
    assert new_ark != tombstoned.ark

    rows = test_db.query(ARKRecord).filter(ARKRecord.client_item_id == "oai:repo:tombstone").all()
    assert len(rows) == 2
    assert {row.state for row in rows} == {ARKState.TOMBSTONE.value, ARKState.RESERVED.value}


def test_batch_reserve_same_client_item_different_authority_or_naan_do_not_collide(client, test_db):
    first = _reserve_batch(client, "test-uuid", "12345", "oai:repo:same")
    second = _reserve_batch(client, "other-uuid", "12345", "oai:repo:same")
    third = _reserve_batch(client, "test-uuid", "67890", "oai:repo:same")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200

    arks = {
        first.json()["results"][0]["ark"],
        second.json()["results"][0]["ark"],
        third.json()["results"][0]["ark"],
    }
    assert len(arks) == 3
    assert test_db.query(ARKRecord).filter(ARKRecord.client_item_id == "oai:repo:same").count() == 3


def test_batch_reserve_recovers_when_concurrent_request_wins_after_prefetch(client, test_db):
    ark_repo = ARKRepository(test_db)
    winner = ark_repo.create_reserved(
        naan="12345",
        name="winner",
        authority_id="test-uuid",
        client_item_id="oai:repo:race",
    )
    test_db.commit()

    with patch.object(ARKRepository, "get_active_by_client_item_ids", return_value={}):
        response = _reserve_batch(client, "test-uuid", "12345", "oai:repo:race")

    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["ark"] == winner.ark
    assert test_db.query(ARKRecord).filter(ARKRecord.client_item_id == "oai:repo:race").count() == 1


def test_active_client_item_unique_constraint_blocks_duplicate_active_rows(test_db_engine):
    SessionLocal = sessionmaker(bind=test_db_engine)
    first_db = SessionLocal()
    second_db = SessionLocal()
    try:
        ARKRepository(first_db).create_reserved(
            naan="12345",
            name="first",
            authority_id="test-uuid",
            client_item_id="oai:repo:concurrent",
        )
        first_db.commit()

        ARKRepository(second_db).create_reserved(
            naan="12345",
            name="second",
            authority_id="test-uuid",
            client_item_id="oai:repo:concurrent",
        )
        with pytest.raises(IntegrityError):
            second_db.commit()
    finally:
        first_db.close()
        second_db.close()
