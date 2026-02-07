"""
Tests for worker status API backed by DB heartbeat.
"""

from datetime import datetime, timedelta, timezone

from app.database.models import WorkerRuntimeStatus


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_worker_status_unknown_when_no_heartbeat(client):
    """API should return UNKNOWN when no worker heartbeat exists."""
    response = client.get("/api/v1/worker/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["running"] is False
    assert data["status"] == "UNKNOWN"
    assert data["source"] == "db_heartbeat"


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

