"""
Tests for worker status API backed by DB heartbeat.
"""

from datetime import datetime, timedelta, timezone

from dark_core_lib.models import ARKPublishResult

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
    authority_id: str = "auth-uuid-123",
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
        authority_id=authority_id,
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
    assert data["storage"]["available"] is True
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
    assert data["replication"] == {
        "pending": 0,
        "complete": 0,
        "degraded": 0,
        "error": 0,
        "retained_payloads": 0,
        "oldest_pending_at": None,
        "last_cycle": {
            "at": None,
            "checked": 0,
            "repaired": 0,
            "purged": 0,
            "failed": 0,
        },
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
    assert data["config"]["metadata"]["worker_concurrency"] == 4
    assert data["config"]["chain"]["worker_page_size"] == 20
    assert data["config"]["chain"]["worker_sleep_seconds"] == 5
    assert data["workers"]["chain"]["cadence"]["pressure"] == "unknown"
    assert data["workers"]["chain"]["cadence"]["page_size"] == 20
    assert data["workers"]["chain"]["cadence"]["effective_page_size_recommended"] == 20
    assert data["workers"]["chain"]["cadence"]["sleep_seconds"] == 5
    assert data["workers"]["chain"]["cadence"]["last_cycle_full_page"] is False
    assert data["workers"]["chain"]["cadence"]["next_action"] == "sleep"
    assert data["workers"]["chain"]["cadence"]["sleep_seconds_next"] == 5
    assert data["rpc"]["available"] is True
    assert data["storage"]["available"] is True
    assert data["config"]["chain"]["worker_rpc_retry_seconds"] == 10
    assert data["config"]["chain"]["worker_adaptive_page_enabled"] is True
    assert data["chain_capacity"]["state"] == "healthy"
    assert data["config"]["metadata"]["worker_storage_retry_seconds"] == 10
    assert data["config"]["rpc"]["health_timeout_seconds"] == 2.0
    assert "running" not in data
    assert "queue" not in data
    assert "cadence" not in data


def test_replication_endpoint_lists_retained_payload_and_counts(client, test_db):
    ark = _add_ark(test_db, name="replication-pending", state=ARKState.DRAFT)
    test_db.flush()
    metadata = test_db.query(ARKMetadata).filter_by(ark_record_id=ark.id).one()
    metadata.level1_cid = "bafy-l1"
    metadata.original_cid = "bafy-l2"
    metadata.level1_replica_count = 2
    metadata.level2_replica_count = 1
    metadata.replication_checked_at = _utc_now()
    test_db.commit()

    response = client.get("/api/v1/worker/replication?state=pending")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["items"][0]["ark"] == "ark:12345/replication-pending"
    assert data["items"][0]["payload_retained"] is True
    assert data["items"][0]["level1"] == {"cid": "bafy-l1", "replicas": 2}
    assert data["items"][0]["level2"] == {"cid": "bafy-l2", "replicas": 1}


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
    assert data["chain_capacity"]["state"] == "healthy"
    assert data["chain_capacity"]["recommended_page_size"] == 20
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


def test_worker_status_reports_chain_worker_paused_for_stalled_chain(client, test_db):
    """Fresh PAUSED_CHAIN_STALLED heartbeat should read as alive but degraded."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="chain-publisher",
            instance_id="instance-paused-chain",
            host="worker-host",
            pid=12345,
            status="PAUSED_CHAIN_STALLED",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_error="Chain stalled: block 123 has not advanced",
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
    assert data["workers"]["chain"]["state"] == "paused_chain_stalled"
    assert data["workers"]["chain"]["alive"] is True
    assert "paused because block production is stalled" in data["message"]


def test_worker_status_reports_chain_worker_paused_for_congestion(client, test_db):
    """Fresh PAUSED_CHAIN_CONGESTED heartbeat should read as alive but degraded."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="chain-publisher",
            instance_id="instance-paused-congestion",
            host="worker-host",
            pid=12345,
            status="PAUSED_CHAIN_CONGESTED",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_error="Chain congestion suspected",
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
    assert data["workers"]["chain"]["state"] == "paused_chain_congested"
    assert data["workers"]["chain"]["alive"] is True
    assert "paused because chain congestion is suspected" in data["message"]


def test_worker_status_reports_metadata_worker_paused_for_storage(client, test_db):
    """Fresh PAUSED_STORAGE_UNAVAILABLE heartbeat should read as alive but degraded."""
    now = _utc_now()
    test_db.add(
        WorkerRuntimeStatus(
            worker_name="metadata-publisher",
            instance_id="instance-paused-storage",
            host="worker-host",
            pid=12345,
            status="PAUSED_STORAGE_UNAVAILABLE",
            last_heartbeat_at=now,
            started_at=now - timedelta(minutes=2),
            last_error="Metadata storage unavailable: not enough peers",
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
    assert data["workers"]["metadata"]["state"] == "paused_storage_unavailable"
    assert data["workers"]["metadata"]["alive"] is True
    assert "paused because metadata storage is unavailable" in data["message"]


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


def test_worker_errors_endpoint_summarizes_by_default(client, test_db):
    """Error report should default to a compact summary."""
    now = _utc_now()
    _add_ark(
        test_db,
        name="metadata-permanent",
        state=ARKState.DRAFT,
        publish_retry_count=5,
        publish_last_attempt_at=now - timedelta(minutes=10),
        publish_last_error="Metadata storage failed (permanent): broken payload",
        publish_permanently_failed=1,
        metadata_complete=False,
    )
    _add_ark(
        test_db,
        name="chain-permanent",
        state=ARKState.UPDATE,
        publish_retry_count=5,
        publish_last_attempt_at=now - timedelta(minutes=5),
        publish_last_error="Pipeline transaction reverted (permanent): Transaction reverted",
        publish_permanently_failed=1,
        metadata_complete=True,
    )
    _add_ark(
        test_db,
        name="retryable",
        state=ARKState.DRAFT,
        publish_retry_count=1,
        publish_last_attempt_at=now,
        publish_last_error="Pipeline transaction ambiguous (infrastructure): timeout",
        publish_permanently_failed=0,
        metadata_complete=True,
    )
    _add_ark(
        test_db,
        name="published-historical-error",
        state=ARKState.PUBLISHED,
        publish_retry_count=1,
        publish_last_attempt_at=now,
        publish_last_error="Authority error (retriable): old failure",
        publish_permanently_failed=0,
        metadata_complete=True,
    )
    test_db.commit()

    response = client.get("/api/v1/worker/errors")

    assert response.status_code == 200
    data = response.json()
    assert data["filters"] == {
        "list": None,
        "stage": "all",
        "authority_id": None,
        "error_type": "all",
    }
    assert data["summary"] == {"total": 3, "permanent": 2, "retrying": 1}
    assert data["by_stage"]["metadata"] == {"total": 1, "permanent": 1, "retrying": 0}
    assert data["by_stage"]["chain"] == {"total": 2, "permanent": 1, "retrying": 1}
    assert data["by_type"]["metadata_storage"] == 1
    assert data["by_type"]["transaction_reverted"] == 1
    assert data["by_type"]["infrastructure"] == 1
    assert data["by_authority"] == {"auth-uuid-123": 3}
    assert "items" not in data


def test_worker_errors_classifies_http_max_retries_as_infrastructure(client, test_db):
    """HTTP client max retries should not be confused with worker max retries."""
    _add_ark(
        test_db,
        name="rpc-send-failed",
        state=ARKState.DRAFT,
        publish_retry_count=1,
        publish_last_attempt_at=_utc_now(),
        publish_last_error=(
            "Pipeline transaction send_failed (infrastructure): "
            "Failed to build/sign transaction: HTTPConnectionPool(host='rpc01', port=8545): "
            "Max retries exceeded with url: /"
        ),
        publish_permanently_failed=0,
        metadata_complete=True,
    )
    test_db.commit()

    response = client.get("/api/v1/worker/errors?list=retrying&stage=chain")

    assert response.status_code == 200
    data = response.json()
    assert data["pagination"]["total"] == 1
    assert data["items"][0]["error_type"] == "infrastructure"


def test_worker_errors_endpoint_lists_and_filters(client, test_db):
    """Error report should list filtered rows with pagination."""
    now = _utc_now()
    metadata_error = _add_ark(
        test_db,
        name="metadata-permanent",
        state=ARKState.DRAFT,
        publish_retry_count=5,
        publish_last_attempt_at=now - timedelta(minutes=10),
        publish_last_error="Metadata storage failed (permanent): broken payload",
        publish_permanently_failed=1,
        metadata_complete=False,
    )
    chain_error = _add_ark(
        test_db,
        name="chain-permanent",
        state=ARKState.UPDATE,
        publish_retry_count=5,
        publish_last_attempt_at=now - timedelta(minutes=5),
        publish_last_error="Pipeline transaction reverted (permanent): Transaction reverted",
        publish_permanently_failed=1,
        metadata_complete=True,
    )
    chain_error.updated_at = now
    metadata_error.updated_at = now - timedelta(minutes=1)
    _add_ark(
        test_db,
        name="retryable",
        state=ARKState.DRAFT,
        publish_retry_count=1,
        publish_last_attempt_at=now,
        publish_last_error="temporary",
        publish_permanently_failed=0,
        metadata_complete=True,
    )
    test_db.commit()

    response = client.get(
        "/api/v1/worker/errors"
        "?list=permanent&stage=chain&error_type=transaction_reverted&page=1&page_size=1"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["filters"] == {
        "list": "permanent",
        "stage": "chain",
        "authority_id": None,
        "error_type": "transaction_reverted",
    }
    assert data["pagination"] == {
        "page": 1,
        "page_size": 1,
        "total": 1,
        "total_pages": 1,
        "has_next": False,
        "has_previous": False,
    }
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["ark"] == "ark:12345/chain-permanent"
    assert item["stage"] == "chain"
    assert item["status"] == "permanent"
    assert item["error_type"] == "transaction_reverted"
    assert item["state"] == ARKState.UPDATE.value
    assert item["retry_count"] == 5
    assert item["permanent"] is True
    assert item["error"] == "Pipeline transaction reverted (permanent): Transaction reverted"
    assert item["metadata"]["level1_cid"] == "l1-chain-permanent"
    assert item["metadata"]["original_cid"] == "l2-chain-permanent"


def test_worker_errors_endpoint_filters_authority(client, test_db):
    """Error report should filter by authority id."""
    now = _utc_now()
    _add_ark(
        test_db,
        name="auth-one-permanent",
        state=ARKState.DRAFT,
        authority_id="auth-one",
        publish_retry_count=5,
        publish_last_attempt_at=now,
        publish_last_error="Pipeline transaction reverted (permanent): Transaction reverted",
        publish_permanently_failed=1,
        metadata_complete=True,
    )
    _add_ark(
        test_db,
        name="auth-two-permanent",
        state=ARKState.DRAFT,
        authority_id="auth-two",
        publish_retry_count=5,
        publish_last_attempt_at=now,
        publish_last_error="Authority error (permanent): not authorized",
        publish_permanently_failed=1,
        metadata_complete=True,
    )
    test_db.commit()

    response = client.get("/api/v1/worker/errors?list=permanent&authority_id=auth-one")

    assert response.status_code == 200
    data = response.json()
    assert data["pagination"]["total"] == 1
    assert data["items"][0]["ark"] == "ark:12345/auth-one-permanent"
    assert data["items"][0]["authority_id"] == "auth-one"


def test_worker_errors_rescue_confirms_filtered_authority_error(client, test_db, mock_corelib):
    """Rescue should publish selected permanent chain errors for the authenticated authority."""
    now = _utc_now()
    ark = _add_ark(
        test_db,
        name="rescue-me",
        state=ARKState.DRAFT,
        authority_id="test-uuid",
        publish_retry_count=5,
        publish_last_attempt_at=now,
        publish_last_error="Pipeline transaction reverted (permanent): Transaction reverted",
        publish_permanently_failed=1,
        metadata_complete=True,
    )
    test_db.commit()
    mock_corelib.config.default_gas_limit = 550000
    mock_corelib.estimate_ark_operation_gas.return_value = 508318
    mock_corelib.publish_ark_operation.return_value = ARKPublishResult(
        ref=ark.ark,
        action="create",
        status="confirmed",
        gas_limit=1100000,
        gas_used=620000,
        gas_estimate=508318,
    )

    response = client.post(
        "/api/v1/worker/errors/rescue",
        json={
            "authority_id": "test-uuid",
            "stage": "chain",
            "error_type": "transaction_reverted",
            "limit": 20,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["selected"] == 1
    assert data["processed"] == 1
    assert data["gas"] == {"current_limit": 550000, "rescue_limit": 1100000}
    assert data["items"][0]["ark"] == ark.ark
    assert data["items"][0]["status"] == "confirmed"
    assert data["items"][0]["error_type"] == "transaction_reverted"
    assert data["items"][0]["final_state"] == "published"
    mock_corelib.publish_ark_operation.assert_called_once()

    test_db.refresh(ark)
    assert ark.state == ARKState.PUBLISHED.value
    assert ark.publish_retry_count == 0
    assert ark.publish_last_error is None
    assert ark.publish_permanently_failed == 0


def test_worker_errors_rescue_rejects_authority_mismatch(client, test_db):
    """Rescue should use the same authority identity mechanism as mutating endpoints."""
    response = client.post(
        "/api/v1/worker/errors/rescue",
        json={"authority_id": "other-authority"},
    )

    assert response.status_code == 403


def test_worker_errors_rescue_skips_when_gas_x2_is_insufficient(client, test_db, mock_corelib):
    """Rescue should not send a tx when the estimate exceeds doubled gas."""
    now = _utc_now()
    ark = _add_ark(
        test_db,
        name="too-large",
        state=ARKState.DRAFT,
        authority_id="test-uuid",
        publish_retry_count=5,
        publish_last_attempt_at=now,
        publish_last_error="Pipeline transaction reverted (permanent): Transaction reverted",
        publish_permanently_failed=1,
        metadata_complete=True,
    )
    test_db.commit()
    mock_corelib.config.default_gas_limit = 550000
    mock_corelib.estimate_ark_operation_gas.return_value = 1200000

    response = client.post(
        "/api/v1/worker/errors/rescue",
        json={"authority_id": "test-uuid", "stage": "chain", "limit": 20},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["selected"] == 1
    assert data["processed"] == 1
    assert data["items"][0]["status"] == "skipped_gas_x2_insufficient"
    assert data["items"][0]["error_type"] == "gas_limit"
    mock_corelib.publish_ark_operation.assert_not_called()

    test_db.refresh(ark)
    assert ark.state == ARKState.DRAFT.value
    assert ark.publish_permanently_failed == 1
    assert "Gas limit too low" in ark.publish_last_error


def test_worker_errors_rescue_does_not_select_metadata_stage(client, test_db, mock_corelib):
    """Rescue should not operate on metadata-stage errors."""
    _add_ark(
        test_db,
        name="metadata-permanent",
        state=ARKState.DRAFT,
        authority_id="test-uuid",
        publish_retry_count=5,
        publish_last_attempt_at=_utc_now(),
        publish_last_error="Metadata storage failed (permanent): broken payload",
        publish_permanently_failed=1,
        metadata_complete=False,
    )
    test_db.commit()
    mock_corelib.config.default_gas_limit = 550000

    response = client.post(
        "/api/v1/worker/errors/rescue",
        json={"authority_id": "test-uuid", "stage": "metadata", "limit": 20},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["selected"] == 0
    assert data["processed"] == 0
    assert data["items"] == []
    mock_corelib.estimate_ark_operation_gas.assert_not_called()
