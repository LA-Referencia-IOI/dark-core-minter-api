"""
Tests for worker status API backed by DB heartbeat.
"""

from datetime import datetime, timedelta, timezone

from app.database.models import ARKMetadata, ARKRecord, WorkerRuntimeStatus
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
    metadata_complete: bool = False,
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
    db.flush()
    db.add(
        ARKMetadata(
            ark_record_id=ark.id,
            level1_json=None if metadata_complete else {"title": name},
            level1_cid=f"l1-{name}" if metadata_complete else None,
            original_content=None if metadata_complete else f"<raw>{name}</raw>",
            original_schema="dublin_core",
            original_media_type="application/xml",
            original_cid=f"l2-{name}" if metadata_complete else None,
        )
    )
    return ark


def test_worker_status_simple_unknown_when_no_heartbeat(client):
    """Default API response should be compact and readable with no heartbeat."""
    response = client.get("/api/v1/worker/status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "down"
    assert data["rpc"]["available"] is True
    assert data["workers"]["metadata"]["state"] == "unknown"
    assert data["workers"]["metadata"]["alive"] is False
    assert data["workers"]["metadata"]["queue"] == {"pending": 0, "ready": 0, "delayed": 0}
    assert data["workers"]["chain"]["state"] == "unknown"
    assert data["workers"]["chain"]["alive"] is False
    assert data["workers"]["chain"]["queue"] == {"pending": 0, "ready": 0, "delayed": 0}
    assert data["errors"] == {"retrying": 0, "permanent": 0}
    assert data["arks"] == {
        "reserved": 0,
        "draft": 0,
        "update": 0,
        "published": 0,
        "tombstone": 0,
    }
    assert "config" not in data
    assert "cadence" not in data
    assert "running" not in data
    assert "Metadata worker is unknown" in data["message"]


def test_worker_status_full_unknown_when_no_heartbeat(client):
    """Full API response should include detailed per-worker debug data."""
    response = client.get("/api/v1/worker/status?detail=full")

    assert response.status_code == 200
    data = response.json()
    assert data["workers"]["metadata"]["enabled"] is True
    assert data["workers"]["metadata"]["running"] is False
    assert data["workers"]["metadata"]["status"] == "UNKNOWN"
    assert data["workers"]["metadata"]["source"] == "db_heartbeat"
    assert data["workers"]["chain"]["enabled"] is True
    assert data["workers"]["chain"]["running"] is False
    assert data["workers"]["chain"]["status"] == "UNKNOWN"
    assert data["queues"]["metadata"]["pending_total"] == 0
    assert data["queues"]["chain"]["pending_total"] == 0
    assert data["errors"]["chain"] == {
        "retrying": 0,
        "permanent": 0,
        "oldest_error_at": None,
    }
    assert data["errors"]["metadata"] == {
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
    assert data["config"]["metadata"]["worker_page_size"] == 100
    assert data["config"]["metadata"]["worker_sleep_seconds"] == 2
    assert data["config"]["chain"]["worker_page_size"] == 20
    assert data["config"]["chain"]["worker_sleep_seconds"] == 5
    assert data["workers"]["chain"]["cadence"]["pressure"] == "unknown"
    assert data["workers"]["chain"]["cadence"]["page_size"] == 20
    assert data["workers"]["chain"]["cadence"]["sleep_seconds"] == 5
    assert data["workers"]["chain"]["cadence"]["last_cycle_full_page"] is False
    assert data["workers"]["chain"]["cadence"]["next_action"] == "sleep"
    assert data["workers"]["chain"]["cadence"]["sleep_seconds_next"] == 5
    assert data["rpc"]["available"] is True
    assert data["config"]["chain"]["worker_rpc_retry_seconds"] == 10
    assert data["config"]["rpc"]["health_timeout_seconds"] == 2.0
    assert "running" not in data
    assert "queue" not in data
    assert "cadence" not in data


def test_worker_status_simple_summarizes_live_workers_and_queues(client, test_db):
    """Default status should expose only operational fields for live workers."""
    now = _utc_now()
    for worker_name in ("metadata-publisher", "chain-publisher"):
        test_db.add(
            WorkerRuntimeStatus(
                worker_name=worker_name,
                instance_id=f"{worker_name}-instance",
                host="worker-host",
                pid=12345,
                status="RUNNING",
                last_heartbeat_at=now,
                started_at=now - timedelta(minutes=2),
                last_cycle_at=now - timedelta(seconds=5),
                last_cycle_duration_seconds=2.5,
                last_cycle_processed=1,
                last_cycle_succeeded=1,
                last_cycle_failed=0,
                total_processed=1,
                total_succeeded=1,
                total_failed=0,
                total_permanent_failures=0,
            )
        )
    _add_ark(test_db, name="metadata-ready", state=ARKState.DRAFT)
    _add_ark(test_db, name="chain-ready", state=ARKState.DRAFT, metadata_complete=True)
    test_db.commit()

    response = client.get("/api/v1/worker/status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "ok"
    assert data["workers"]["metadata"]["state"] == "working"
    assert data["workers"]["metadata"]["alive"] is True
    assert data["workers"]["metadata"]["last_cycle"] == {
        "processed": 1,
        "succeeded": 1,
        "failed": 0,
        "duration_seconds": 2.5,
        "page_size": 100,
        "full_page": False,
        "next_action": "sleep",
    }
    assert data["workers"]["metadata"]["queue"] == {"pending": 1, "ready": 1, "delayed": 0}
    assert data["workers"]["chain"]["state"] == "working"
    assert data["workers"]["chain"]["alive"] is True
    assert data["workers"]["chain"]["queue"] == {"pending": 1, "ready": 1, "delayed": 0}
    assert data["errors"] == {"retrying": 0, "permanent": 0}
    assert data["arks"]["draft"] == 2
    assert "config" not in data
    assert "queues" not in data


def test_worker_status_reports_chain_worker_paused_for_rpc(client, test_db):
    """Fresh PAUSED_RPC_UNAVAILABLE heartbeat should read as alive but degraded."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="chain-publisher",
            instance_id="instance-paused",
            host="worker-host",
            pid=12345,
            status="PAUSED_RPC_UNAVAILABLE",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_error="RPC unavailable: connection refused",
            total_processed=0,
            total_succeeded=0,
            total_failed=0,
            total_permanent_failures=0,
        )
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "degraded"
    assert data["workers"]["chain"]["state"] == "paused_rpc_unavailable"
    assert data["workers"]["chain"]["alive"] is True
    assert "paused because RPC is unavailable" in data["message"]


def test_worker_status_message_mentions_permanent_errors(client, test_db):
    """Idle workers with permanent ARK errors should explain degraded status."""
    now = _utc_now()
    for worker_name in ("metadata-publisher", "chain-publisher"):
        test_db.add(
            WorkerRuntimeStatus(
                worker_name=worker_name,
                instance_id=f"{worker_name}-instance",
                host="worker-host",
                pid=12345,
                status="RUNNING",
                last_heartbeat_at=now,
                started_at=now - timedelta(minutes=2),
                last_cycle_at=now - timedelta(seconds=5),
                last_cycle_duration_seconds=0.01,
                last_cycle_processed=0,
                last_cycle_succeeded=0,
                last_cycle_failed=0,
                total_processed=0,
                total_succeeded=0,
                total_failed=0,
                total_permanent_failures=0,
            )
        )
    _add_ark(
        test_db,
        name="blocked-permanent",
        state=ARKState.DRAFT,
        publish_retry_count=5,
        publish_last_attempt_at=now - timedelta(minutes=5),
        publish_last_error="permanent failure",
        publish_permanently_failed=1,
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status")

    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "degraded"
    assert data["workers"]["metadata"]["state"] == "idle"
    assert data["workers"]["metadata"]["activity"] == "idle"
    assert data["workers"]["metadata"]["status"] == "running"
    assert data["errors"] == {"retrying": 0, "permanent": 1}
    assert "1 permanent ARK errors require review" in data["message"]


def test_worker_status_running_from_fresh_heartbeat(client, test_db):
    """API should report running for a fresh RUNNING heartbeat."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="chain-publisher",
            instance_id="instance-1",
            host="worker-host",
            pid=12345,
            status="RUNNING",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_cycle_at=now - timedelta(seconds=30),
            last_cycle_duration_seconds=8.5,
            last_cycle_processed=10,
            last_cycle_succeeded=9,
            last_cycle_failed=1,
            total_processed=10,
            total_succeeded=9,
            total_failed=1,
            total_permanent_failures=0,
        )
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status?detail=full")
    assert response.status_code == 200

    data = response.json()
    chain = data["workers"]["chain"]
    assert chain["enabled"] is True
    assert chain["running"] is True
    assert chain["stale"] is False
    assert chain["status"] == "RUNNING"
    assert chain["worker_name"] == "chain-publisher"
    assert chain["stats"]["total_processed"] == 10
    assert chain["cadence"]["page_size"] == 20
    assert chain["cadence"]["sleep_seconds"] == 5
    assert chain["cadence"]["rpc_retry_seconds"] == 10
    assert chain["cadence"]["last_cycle_duration_seconds"] == 8.5
    assert chain["cadence"]["last_cycle_processed"] == 10
    assert chain["cadence"]["last_cycle_succeeded"] == 9
    assert chain["cadence"]["last_cycle_failed"] == 1
    assert chain["cadence"]["cycle_utilization_ratio"] == 1.7
    assert chain["cadence"]["last_cycle_full_page"] is False
    assert chain["cadence"]["next_action"] == "sleep"
    assert chain["cadence"]["sleep_seconds_next"] == 5
    assert chain["cadence"]["pressure"] == "idle"
    assert data["queues"]["chain"]["pending_total"] == 0
    assert data["errors"]["chain"]["permanent"] == 0


def test_worker_status_stale_when_heartbeat_is_old(client, test_db):
    """API should mark worker as stale when heartbeat is too old."""
    old = _utc_now() - timedelta(minutes=10)
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="chain-publisher",
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

    response = client.get("/api/v1/worker/status?detail=full")
    assert response.status_code == 200

    data = response.json()
    chain = data["workers"]["chain"]
    assert chain["enabled"] is True
    assert chain["running"] is False
    assert chain["stale"] is True
    assert chain["status"] == "RUNNING"
    assert chain["cadence"]["pressure"] == "stale"


def test_worker_status_reports_full_page_continue_action(client, test_db):
    """API should show when the worker will continue after a full page."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="chain-publisher",
            instance_id="instance-overlap",
            host="worker-host",
            pid=12345,
            status="RUNNING",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_cycle_at=now - timedelta(seconds=5),
            last_cycle_duration_seconds=35.25,
            last_cycle_processed=100,
            last_cycle_succeeded=100,
            last_cycle_failed=0,
            total_processed=100,
            total_succeeded=100,
            total_failed=0,
            total_permanent_failures=0,
        )
    )
    test_db.commit()

    response = client.get("/api/v1/worker/status?detail=full")
    assert response.status_code == 200

    data = response.json()
    cadence = data["workers"]["chain"]["cadence"]
    assert cadence["last_cycle_duration_seconds"] == 35.25
    assert cadence["cycle_utilization_ratio"] == 7.05
    assert cadence["last_cycle_full_page"] is True
    assert cadence["next_action"] == "continue"
    assert cadence["sleep_seconds_next"] == 0
    assert cadence["pressure"] == "backlogged"


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

    response = client.get("/api/v1/worker/status?detail=full")
    assert response.status_code == 200

    data = response.json()
    assert data["queues"]["metadata"]["pending_total"] == 4
    assert data["queues"]["metadata"]["ready_now"] == 3
    assert data["queues"]["metadata"]["delayed_by_backoff"] == 1
    assert data["queues"]["metadata"]["oldest_pending_at"] == old_pending.isoformat()

    assert data["errors"]["metadata"]["retrying"] == 2
    assert data["errors"]["metadata"]["permanent"] == 1
    assert data["errors"]["metadata"]["oldest_error_at"] == old_error.isoformat()

    assert data["by_state"] == {
        "reserved": 1,
        "draft": 3,
        "update": 2,
        "published": 1,
        "tombstone": 1,
    }
