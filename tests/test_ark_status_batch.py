"""Contract tests for the bounded ARK reconciliation status batch endpoint."""

from types import SimpleNamespace

from fastapi import HTTPException

from app.config import get_settings
from app.main import app
from app.middleware.auth import require_mtls
from app.models.states import ARKState
from app.repositories import ARKRepository
from app.utils.noid import ALPHABET, compute_checkdigit, encode_counter


ENDPOINT = "/api/v1/arks/status/batch"


def _ark(counter: int) -> str:
    settings = get_settings()
    stem = f"{settings.minter_shoulder}{encode_counter(counter, settings.minter_noid_length)}"
    name = f"{stem}{compute_checkdigit(f'12345/{stem}')}" if settings.minter_noid_checkdigit else stem
    return f"ark:12345/{name}"


def _create(test_db, counter: int, state: ARKState, target: str | None = None):
    ark = _ark(counter)
    _, name = ark.split("/", 1)
    record = ARKRepository(test_db).create_reserved("12345", name, "test-uuid")
    record.state = state
    record.target = target
    test_db.commit()
    return ark


def test_status_batch_mixed_pending_and_published_states(client, test_db, mock_corelib):
    reserved = _create(test_db, 1, ARKState.RESERVED)
    draft = _create(test_db, 2, ARKState.DRAFT, "https://draft.example")
    published = _create(test_db, 3, ARKState.PUBLISHED, "https://stale.example")
    mock_corelib.get_ark.return_value = SimpleNamespace(
        url="https://chain.example", cid="chain-cid"
    )

    response = client.post(ENDPOINT, json={"arks": [reserved, draft, published]})

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "v1"
    assert [item["ark"] for item in body["results"]] == [reserved, draft, published]
    assert [item["status"]["state"] for item in body["results"]] == ["R", "D", "P"]
    assert all("error" not in item for item in body["results"])
    assert body["results"][1]["status"]["target"] == "https://draft.example"
    assert body["results"][2]["status"]["target"] == "https://chain.example"
    assert body["results"][2]["status"]["metadata_cid"] == "chain-cid"
    mock_corelib.get_ark.assert_called_once()


def test_status_batch_returns_per_item_not_found_invalid_and_chain_errors(client, mock_corelib):
    missing = _ark(10)
    valid_chain_failure = _ark(11)
    settings = get_settings()
    stem = f"{settings.minter_shoulder}{encode_counter(12, settings.minter_noid_length)}"
    invalid = f"ark:12345/{stem}{next(ch for ch in ALPHABET if ch != compute_checkdigit(f'12345/{stem}'))}"

    # The chain failure ARK exists, then its detail lookup fails.
    mock_corelib.ark_exists.side_effect = lambda naan, name: name == valid_chain_failure.split("/", 1)[1]
    mock_corelib.get_ark.side_effect = RuntimeError("RPC unavailable")

    response = client.post(ENDPOINT, json={"arks": [missing, invalid, valid_chain_failure]})

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["error"]["code"] == "not_found"
    assert results[1]["error"]["code"] == "invalid_ark"
    assert results[2]["error"]["code"] == "blockchain_error"
    assert results[2]["error"]["retryable"] is True
    assert all("status" not in item for item in results)


def test_status_batch_limits_request_to_100_and_keeps_order(client, test_db):
    ark = _create(test_db, 20, ARKState.RESERVED)

    response = client.post(ENDPOINT, json={"arks": [ark] * 100})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 100

    response = client.post(ENDPOINT, json={"arks": [ark] * 101})
    assert response.status_code == 422


def test_status_batch_requires_mtls_authentication(client):
    async def reject_mtls():
        raise HTTPException(status_code=401, detail="Client certificate required")

    app.dependency_overrides[require_mtls] = reject_mtls
    try:
        response = client.post(ENDPOINT, json={"arks": [_ark(30)]})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(require_mtls, None)


def test_status_batch_continues_after_isolated_blockchain_failure(client, test_db, mock_corelib):
    pending = _create(test_db, 40, ARKState.UPDATE, "https://pending.example")
    failed_chain = _ark(41)
    mock_corelib.ark_exists.return_value = True
    mock_corelib.get_ark.side_effect = RuntimeError("one lookup failed")

    response = client.post(ENDPOINT, json={"arks": [failed_chain, pending]})

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["error"]["code"] == "blockchain_error"
    assert results[1]["status"]["state"] == "U"
    assert results[1]["status"]["target"] == "https://pending.example"
