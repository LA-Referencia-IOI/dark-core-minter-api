"""
Tests for worker status API backed by DB heartbeat.
"""

from datetime import datetime, timedelta, timezone

from app.database.models import ARKRecord, WorkerRuntimeStatus
from app.models.states import ARKState


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_ark(
    db,
    *,
    name: str,
    state: ARKState,
    created_at=None,
    publish_retry_count: int = 0,
    publish_last_attempt_at=None,
    publish_last_error=None,
    publish_permanently_failed: int = 0,
):
    """Create an ARK row with worker-tracking fields for status tests."""
    ark = ARKRecord(
        naan="12345",
        name=name,
        state=state.value,
        authority_id="auth-uuid-123",
        target=f"https://example.org/{name}",
        created_at=created_at or _utc_now(),
        updated_at=created_at or _utc_now(),
        publish_retry_count=publish_retry_count,
        publish_last_attempt_at=publish_last_attempt_at,
        publish_last_error=publish_last_error,
        publish_permanently_failed=publish_permanently_failed,
    )
    db.add(ark)
    return ark


def test_worker_status_unknown_when_no_heartbeat(client):
    """API should return UNKNOWN when no worker heartbeat exists."""
    response = client.get("/api/v1/worker/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["running"] is False
    assert data["status"] == "UNKNOWN"
    assert data["source"] == "db_heartbeat"
    assert data["queue"] == {
        "pending_total": 0,
        "ready_now": 0,
        "delayed_by_backoff": 0,
        "oldest_pending_at": None,
    }
    assert data["errors"] == {
        "retrying": 0,
        "permanent": 0,
        "oldest_error_at": None,
    }
    assert data["by_state"] == {
        "reserved": 0,
        "draft": 0,
        "update": 0,
        "published": 0,
        "tombstone": 0,
    }
    assert data["config"]["worker_interval_seconds"] == 30
    assert data["config"]["worker_batch_size"] == 100


def test_worker_status_running_from_fresh_heartbeat(client, test_db):
    """API should report running for a fresh RUNNING heartbeat."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="ark-publisher",
            instance_id="instance-1",
            host="worker-host",
            pid=12345,
            status="RUNNING",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_cycle_at=now - timedelta(seconds=30),
            total_processed=10,
            total_succeeded=9,
            total_failed=1,
            total_permanent_failures=0,
        )
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status")
    assert response.status_code == 200

    data = response.json()
    assert data["enabled"] is True
    assert data["running"] is True
    assert data["stale"] is False
    assert data["status"] == "RUNNING"
    assert data["worker_name"] == "ark-publisher"
    assert data["stats"]["total_processed"] == 10
    assert data["queue"]["pending_total"] == 0
    assert data["errors"]["permanent"] == 0


def test_worker_status_stale_when_heartbeat_is_old(client, test_db):
    """API should mark worker as stale when heartbeat is too old."""
    old = _utc_now() - timedelta(minutes=10)
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="ark-publisher",
            instance_id="instance-2",
            host="worker-host",
            pid=99999,
            status="RUNNING",
            last_heartbeat_at=old,
            started_at=old - timedelta(minutes=5),
            total_processed=5,
            total_succeeded=5,
            total_failed=0,
            total_permanent_failures=0,
        )
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status")
    assert response.status_code == 200

    data = response.json()
    assert data["enabled"] is True
    assert data["running"] is False
    assert data["stale"] is True
    assert data["status"] == "RUNNING"


def test_worker_status_includes_queue_state_and_error_summary(client, test_db):
    """API should include queue counters, state totals, and error counters."""
    now = _utc_now()
    old_pending = now - timedelta(hours=2)
    old_error = now - timedelta(hours=1)
    recent_failure = now - timedelta(seconds=30)
    ready_failure = now - timedelta(minutes=10)

    _add_ark(test_db, name="reserved", state=ARKState.RESERVED)
    _add_ark(test_db, name="published", state=ARKState.PUBLISHED)
    _add_ark(test_db, name="tombstone", state=ARKState.TOMBSTONE)
    _add_ark(test_db, name="draft-ready", state=ARKState.DRAFT, created_at=old_pending)
    _add_ark(test_db, name="update-ready", state=ARKState.UPDATE, created_at=now - timedelta(minutes=30))
    _add_ark(
        test_db,
        name="draft-delayed",
        state=ARKState.DRAFT,
        publish_retry_count=1,
        publish_last_attempt_at=recent_failure,
        publish_last_error="temporary failure",
    )
    _add_ark(
        test_db,
        name="update-retry-ready",
        state=ARKState.UPDATE,
        publish_retry_count=1,
        publish_last_attempt_at=ready_failure,
        publish_last_error="old temporary failure",
    )
    _add_ark(
        test_db,
        name="permanent",
        state=ARKState.DRAFT,
        publish_retry_count=5,
        publish_last_attempt_at=old_error,
        publish_last_error="permanent failure",
        publish_permanently_failed=1,
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status")
    assert response.status_code == 200

    data = response.json()
    assert data["queue"]["pending_total"] == 4
    assert data["queue"]["ready_now"] == 3
    assert data["queue"]["delayed_by_backoff"] == 1
    assert data["queue"]["oldest_pending_at"] == old_pending.isoformat()

    assert data["errors"]["retrying"] == 2
    assert data["errors"]["permanent"] == 1
    assert data["errors"]["oldest_error_at"] == old_error.isoformat()

    assert data["by_state"] == {
        "reserved": 1,
        "draft": 3,
        "update": 2,
        "published": 1,
        "tombstone": 1,
    }
