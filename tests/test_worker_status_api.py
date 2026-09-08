"""The status API reports process state separately from normal waits."""

from datetime import datetime, timedelta, timezone

from app.api import worker as worker_api
from app.database.models import ARKRecord, WorkerRuntimeStatus
from app.models.processing import ProcessingStage, ProcessingStatus, ProcessingWaitReason
from app.models.states import ARKState


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_simple_status_reads_heartbeats_only(client, monkeypatch):
    def external_probe(*args, **kwargs):
        raise AssertionError("simple status must not call external services")

    monkeypatch.setattr(worker_api, "check_rpc_health", external_probe)
    monkeypatch.setattr(worker_api, "check_metadata_storage_health", external_probe)

    response = client.get("/api/v1/worker/status")
    assert response.status_code == 200
    assert response.json()["source"] == "db_heartbeat"
    assert set(response.json()["workers"]) == {"metadata", "replication", "chain"}


def test_full_status_separates_process_and_workload(client, test_db):
    now = _now()
    ready = ARKRecord(
        naan="12345", name="200ready", state=ARKState.PUBLISHED.value,
        authority_id="test-uuid", target="https://example.org/ready",
        processing_stage=int(ProcessingStage.REPLICATION),
        processing_status=int(ProcessingStatus.READY),
    )
    waiting = ARKRecord(
        naan="12345", name="200wait", state=ARKState.PUBLISHED.value,
        authority_id="test-uuid", target="https://example.org/wait",
        processing_stage=int(ProcessingStage.REPLICATION),
        processing_status=int(ProcessingStatus.WAITING),
        processing_wait_reason=int(ProcessingWaitReason.REPLICA_TARGET),
        next_action_at=now + timedelta(seconds=30),
    )
    runtime = WorkerRuntimeStatus(
        worker_name="replication-reconciler", instance_id="test", host="test", pid=1,
        status="RUNNING", started_at=now, last_heartbeat_at=now,
        last_cycle_at=now,
    )
    test_db.add_all([ready, waiting, runtime])
    test_db.commit()

    response = client.get("/api/v1/worker/status?detail=full")
    assert response.status_code == 200
    data = response.json()
    assert data["workload"]["replication"]["ready"] == 1
    assert data["workload"]["replication"]["waiting"] == 1
    assert data["workload"]["replication"]["waiting_reasons"] == {"replica_target": 1}
    assert data["workers"]["replication"]["process_state"] == "SLEEPING"


def test_errors_list_only_permanent_failures(client, test_db):
    failed = ARKRecord(
        naan="12345", name="200failed", state=ARKState.DRAFT.value,
        authority_id="test-uuid", target="https://example.org/failed",
        processing_stage=int(ProcessingStage.CHAIN),
        processing_status=int(ProcessingStatus.FAILED),
    )
    test_db.add(failed)
    test_db.commit()

    response = client.get("/api/v1/worker/errors")
    assert response.status_code == 200
    assert response.json()["summary"]["total"] == 1
    assert response.json()["items"][0]["ark"] == failed.ark
