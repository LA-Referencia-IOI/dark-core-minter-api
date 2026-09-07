"""Status endpoints expose compact runtime state and detailed replication data."""

from datetime import datetime, timezone

from app.api import worker as worker_api
from app.database.models import ARKMetadata, ARKRecord, WorkerRuntimeStatus
from app.models.processing import ProcessingStage, ProcessingStatus
from app.models.states import ARKState


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_lightweight_status_uses_only_heartbeats(client, monkeypatch):
    def expensive(*args, **kwargs):
        raise AssertionError("lightweight status must not query operational dependencies")

    monkeypatch.setattr(worker_api, "check_rpc_health", expensive)
    monkeypatch.setattr(worker_api, "check_metadata_storage_health", expensive)
    monkeypatch.setattr(worker_api, "_build_chain_capacity_summary", expensive)
    response = client.get("/api/v1/worker/status")
    assert response.status_code == 200
    assert response.json()["source"] == "db_heartbeat"
    assert set(response.json()["workers"]) == {"metadata", "replication", "chain", "recovery"}


def test_full_status_lists_replication_queue(client, test_db):
    now = _now()
    record = ARKRecord(
        naan="12345",
        name="2000000x",
        state=ARKState.PUBLISHED.value,
        authority_id="test-uuid",
        target="https://example.org/object",
        processing_stage=int(ProcessingStage.REPLICATION),
        processing_status=int(ProcessingStatus.PENDING),
    )
    test_db.add(record)
    test_db.flush()
    test_db.add(
        ARKMetadata(
            ark_record_id=record.id,
            level1_json={"title": "item"},
            level1_cid="bafy-l1",
            original_content="raw",
            original_schema="dc",
            original_media_type="text/plain",
            original_cid="bafy-l2",
            level1_replica_count=1,
            level2_replica_count=1,
        )
    )
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="replication-reconciler",
            instance_id="test",
            host="test",
            pid=1,
            status="RUNNING",
            started_at=now,
            last_heartbeat_at=now,
        )
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status?detail=full")
    assert response.status_code == 200
    data = response.json()
    assert data["replication"]["retained_payloads"] == 1
    assert data["workers"]["replication"]["running"] is True

    queue = client.get("/api/v1/worker/replication")
    assert queue.status_code == 200
    assert queue.json()["items"][0]["level1"]["replicas"] == 1


def test_error_summary_is_aggregate_and_error_pages_are_bounded(client):
    summary = client.get("/api/v1/worker/errors")
    assert summary.status_code == 200
    assert summary.json()["summary"]["total"] == 0

    page = client.get("/api/v1/worker/errors?list=all&page=1&page_size=10")
    assert page.status_code == 200
    assert page.json()["pagination"]["total"] == 0
    assert page.json()["items"] == []
