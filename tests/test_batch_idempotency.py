"""
Tests for idempotent batch ARK reservation by client item ID.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from requests import Response
from requests.exceptions import HTTPError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database.models import ARKRecord, NoidCounter
from app.models.states import ARKState
from app.repositories import ARKRepository


def _reserve_batch(client, authority_id="test-uuid", naan="12345", *client_item_ids):
    return client.post(
        "/api/v1/arks/batch",
        headers={"X-DARK-Authority-Id": authority_id},
        json={
            "authority_id": authority_id,
            "naan": naan,
            "items": [{"client_item_id": item_id} for item_id in client_item_ids],
        },
    )


def test_batch_reserve_logs_authority_lookup_failure(client, mock_corelib, test_db, caplog):
    def fail_lookup(_authority_id):
        try:
            raise TimeoutError("private RPC detail")
        except TimeoutError as exc:
            raise RuntimeError("private authority detail") from exc

    mock_corelib.get_authority_by_uuid.side_effect = fail_lookup
    response = _reserve_batch(client, "test-uuid", "12345", "oai:repo:timeout")

    assert response.status_code == 503
    assert response.json() == {"detail": "AUTHORITY_STATUS_UNAVAILABLE"}
    assert mock_corelib.get_authority_by_uuid.call_count == 2
    assert response.headers["X-DARK-Error-Code"] == "AUTHORITY_STATUS_UNAVAILABLE"
    assert response.headers["X-DARK-Retryable"] == "true"
    assert f"request_id={response.headers['X-Request-ID']}" in caplog.text
    assert "exception_type=RuntimeError cause_type=TimeoutError elapsed_ms=" in caplog.text
    assert "private RPC detail" not in caplog.text
    assert "private authority detail" not in caplog.text
    assert test_db.query(ARKRecord).count() == 0
    assert test_db.query(NoidCounter).count() == 0


def test_timeout_before_reservation_allows_full_batch_and_next_batch(client, mock_corelib, test_db):
    active = SimpleNamespace(active=True)
    mock_corelib.get_authority_by_uuid.side_effect = [TimeoutError(), active, active, active]
    ids = [f"oai:test:{i}" for i in range(100)]
    first = _reserve_batch(client, "test-uuid", "12345", *ids)
    assert first.status_code == 200
    assert len(first.json()["results"]) == 100
    # Simulate a lost response after commit: recovering with stable item IDs
    # returns the same ARKs and consumes no additional NOIDs.
    recovered = _reserve_batch(client, "test-uuid", "12345", *ids)
    assert recovered.json()["results"] == first.json()["results"]
    following = _reserve_batch(client, "test-uuid", "12345", "oai:test:100")
    assert following.status_code == 200
    assert test_db.query(ARKRecord).count() == test_db.query(NoidCounter).one().next_value == 101


def test_authority_revoked_during_retry_never_reserves(client, mock_corelib, test_db):
    mock_corelib.get_authority_by_uuid.side_effect = [TimeoutError(), SimpleNamespace(active=False)]
    response = _reserve_batch(client, "test-uuid", "12345", "revoked")
    assert response.status_code == 403
    assert response.json()["detail"] == "AUTHORITY_INACTIVE"
    assert test_db.query(ARKRecord).count() == test_db.query(NoidCounter).count() == 0


@pytest.mark.parametrize(("status", "attempts", "retryable"), [(503, 2, "true"), (401, 1, "false")])
def test_upstream_rpc_http_error(client, mock_corelib, status, attempts, retryable):
    upstream = Response()
    upstream.status_code = status
    mock_corelib.get_authority_by_uuid.side_effect = HTTPError(response=upstream)
    response = _reserve_batch(client, "test-uuid", "12345", "item")
    assert response.headers["X-DARK-Retryable"] == retryable
    assert mock_corelib.get_authority_by_uuid.call_count == attempts


def test_unexpected_authority_failure_does_not_request_automatic_retry(client, mock_corelib):
    mock_corelib.get_authority_by_uuid.side_effect = ValueError("decode failed")
    response = _reserve_batch(client, "test-uuid", "12345", "item")
    assert response.status_code == 503
    assert response.headers["X-DARK-Retryable"] == "false"
    assert mock_corelib.get_authority_by_uuid.call_count == 1


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
