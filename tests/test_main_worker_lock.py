"""
Unit tests for PostgreSQL advisory lock helpers in main_worker.
"""

import threading
from types import SimpleNamespace
import os
from unittest.mock import Mock, patch

import pytest

from app.main_worker import (
    _HeartbeatSupervisor,
    _WorkerAdvisoryLock,
    _acquire_worker_pid,
    _build_advisory_lock_key,
    _clamp_page_size,
    _congestion_pause_sleep_seconds,
    _last_cycle_congestion_saturated,
    _last_cycle_page_size,
    _next_higher_page_size,
    _page_size_levels,
    _rpc_pause_sleep_seconds,
    _progressive_idle_sleep_seconds,
    _release_worker_pid,
    _select_next_sleep_seconds,
    _shutdown_event,
    _storage_pause_sleep_seconds,
    _wait_with_heartbeats,
    _worker_runtime_config,
)


class _ScalarResult:
    """Small helper to mimic SQLAlchemy scalar result object."""

    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


def test_build_advisory_lock_key_is_stable_and_in_range():
    """Lock key generation should be deterministic and valid bigint positive range."""
    key_a = _build_advisory_lock_key("ark-publisher")
    key_b = _build_advisory_lock_key("ark-publisher")
    key_c = _build_advisory_lock_key("another-worker")

    assert key_a == key_b
    assert key_a != key_c
    assert 0 <= key_a <= 0x7FFFFFFFFFFFFFFF


def test_worker_advisory_lock_acquire_and_release_success():
    """Acquire should keep session open; release should unlock and close session."""
    db = Mock()
    db.execute.return_value = _ScalarResult(True)
    session_factory = Mock(return_value=db)

    with patch("app.main_worker.SessionLocal", return_value=session_factory):
        lock = _WorkerAdvisoryLock(123)
        assert lock.acquire() is True
        db.close.assert_not_called()

        lock.release()

    assert db.execute.call_count == 2
    db.commit.assert_called_once()
    db.close.assert_called_once()


def test_worker_advisory_lock_acquire_failure_closes_session():
    """Acquire should close session when advisory lock is already held."""
    db = Mock()
    db.execute.return_value = _ScalarResult(False)
    session_factory = Mock(return_value=db)

    with patch("app.main_worker.SessionLocal", return_value=session_factory):
        lock = _WorkerAdvisoryLock(456)
        assert lock.acquire() is False

    db.close.assert_called_once()


def test_replication_worker_uses_its_own_runtime_identity_and_defaults():
    settings = SimpleNamespace(
        replication_worker_enabled=True,
        replication_worker_runtime_name="replication-reconciler",
        replication_worker_page_size=50,
        replication_worker_concurrency=2,
        replication_worker_sleep_seconds=30,
        replication_first_pin_recheck_seconds=15,
        replication_first_pin_second_recheck_seconds=60,
        replication_first_pin_max_recheck_seconds=300,
        replication_durability_recheck_seconds=300,
        replication_durability_second_recheck_seconds=900,
        replication_durability_max_recheck_seconds=3600,
        replication_worker_storage_retry_seconds=10,
    )
    with patch("app.main_worker.get_settings", return_value=settings):
        runtime = _worker_runtime_config("replication")

    assert runtime == {
        "enabled": True,
        "worker_name": "replication-reconciler",
        "page_size": 50,
        "concurrency": 2,
        "sleep_seconds": 30,
        "first_pin_recheck_seconds": 15,
        "first_pin_second_recheck_seconds": 60,
        "first_pin_max_recheck_seconds": 300,
        "durability_recheck_seconds": 300,
        "durability_second_recheck_seconds": 900,
        "durability_max_recheck_seconds": 3600,
        "repair_grace_seconds": 120,
        "repair_cooldown_seconds": 900,
        "status_batch_size": 200,
        "promotion_batch_size": 100,
        "maintenance_cycle_seconds": 5,
        "idle_sleep_seconds": 2,
        "promotion_pressure_high_percent": 80,
        "promotion_pressure_medium_percent": 50,
        "promotion_min_batch_size": 20,
        "storage_retry_seconds": 10,
    }
    assert _build_advisory_lock_key(runtime["worker_name"]) != _build_advisory_lock_key(
        "metadata-publisher"
    )


def test_metadata_and_chain_progressively_back_off_empty_cycles():
    assert _progressive_idle_sleep_seconds("metadata", 0, 2, 2, 10) == (
        5, 3, "idle_backoff_5s"
    )
    assert _progressive_idle_sleep_seconds("chain", 0, 9, 2, 10) == (
        10, 10, "idle_backoff_10s"
    )
    assert _progressive_idle_sleep_seconds("chain", 1, 10, 2, 10) == (2, 0, "sleep")


def test_replication_pidfile_blocks_duplicate_local_process(tmp_path):
    pid_file = tmp_path / "dark-core-replication-reconciler.pid"

    _acquire_worker_pid(pid_file)
    try:
        assert pid_file.exists()
        with pytest.raises(RuntimeError, match="Worker already running"):
            _acquire_worker_pid(pid_file)
    finally:
        _release_worker_pid(pid_file)

    assert not pid_file.exists()


def test_pidfile_with_reused_current_pid_is_replaced(tmp_path):
    pid_file = tmp_path / "dark-core-chain-publisher.pid"
    current_pid = os.getpid()
    pid_file.write_text(f"{current_pid}\n", encoding="utf-8")

    with patch("app.main_worker.Path.exists", return_value=True):
        _acquire_worker_pid(pid_file)

    assert pid_file.read_text(encoding="utf-8").strip() == str(current_pid)
    _release_worker_pid(pid_file)


def test_select_next_sleep_uses_sleep_for_partial_page():
    """Worker loop should sleep when the last cycle did not fill a page."""
    publisher = Mock()
    publisher.stats = {"last_run_processed": 9}
    runtime = {"page_size": 10, "sleep_seconds": 5}

    assert _select_next_sleep_seconds(publisher, runtime) == (5, "sleep")


def test_select_next_sleep_continues_for_full_page():
    """Worker loop should continue immediately when the last cycle filled a page."""
    publisher = Mock()
    publisher.stats = {"last_run_processed": 10}
    runtime = {"page_size": 10, "sleep_seconds": 5}

    assert _select_next_sleep_seconds(publisher, runtime) == (0, "continue")


def test_rpc_pause_sleep_uses_chain_retry_seconds():
    """Chain worker should use explicit RPC retry sleep while paused."""
    assert _rpc_pause_sleep_seconds({"rpc_retry_seconds": 12}) == 12


def test_storage_pause_sleep_uses_metadata_retry_seconds():
    """Metadata worker should use explicit storage retry sleep while paused."""
    assert _storage_pause_sleep_seconds({"storage_retry_seconds": 14}) == 14


def test_congestion_pause_sleep_uses_chain_retry_seconds():
    """Chain worker should use explicit congestion retry sleep while paused."""
    assert _congestion_pause_sleep_seconds({"congestion_retry_seconds": 45}) == 45


def test_last_cycle_congestion_saturated_requires_full_deferred_page():
    """A full page of infrastructure deferrals should pause the chain worker."""
    publisher = Mock()
    publisher.stats = {
        "last_run_processed": 20,
        "last_run_failed": 20,
        "last_run_deferred": 20,
    }

    assert _last_cycle_congestion_saturated(publisher, {"page_size": 20}) is True


def test_last_cycle_congestion_saturated_ignores_partial_page():
    """Partial deferred pages should not trigger the chain congestion pause."""
    publisher = Mock()
    publisher.stats = {
        "last_run_processed": 10,
        "last_run_failed": 10,
        "last_run_deferred": 10,
    }

    assert _last_cycle_congestion_saturated(publisher, {"page_size": 20}) is False


def test_last_cycle_congestion_saturated_uses_effective_page_size():
    """Adaptive pages should be evaluated against the effective page size."""
    publisher = Mock()
    publisher.stats = {
        "last_run_page_size": 5,
        "last_run_processed": 5,
        "last_run_failed": 5,
        "last_run_deferred": 5,
    }

    assert _last_cycle_page_size(publisher, {"page_size": 20}) == 5
    assert _last_cycle_congestion_saturated(publisher, {"page_size": 20}) is True


def test_adaptive_page_size_levels_are_conservative():
    assert _page_size_levels(1, 20) == [1, 5, 10, 20]
    assert _clamp_page_size(50, 1, 20) == 20
    assert _next_higher_page_size(5, 1, 20) == 10


def test_wait_with_heartbeats_stops_cleanly_when_shutdown_is_set():
    """Sleep helper should return immediately when shutdown is requested."""
    heartbeat = Mock()
    _shutdown_event.set()

    try:
        assert _wait_with_heartbeats(30, 10, heartbeat) is True
    finally:
        _shutdown_event.clear()

    heartbeat.assert_not_called()


def test_heartbeat_supervisor_emits_heartbeats_during_long_cycle():
    """Supervisor should keep heartbeats flowing while the main thread is busy."""
    heartbeat_count = 0
    heartbeat_lock = threading.Lock()
    observed_multiple_heartbeats = threading.Event()
    _shutdown_event.clear()

    def heartbeat():
        nonlocal heartbeat_count
        with heartbeat_lock:
            heartbeat_count += 1
            if heartbeat_count >= 2:
                observed_multiple_heartbeats.set()

    supervisor = _HeartbeatSupervisor(
        heartbeat_interval_seconds=0.01,
        heartbeat_callback=heartbeat,
    )

    try:
        supervisor.start()
        assert observed_multiple_heartbeats.wait(timeout=1.0)
    finally:
        supervisor.stop()

    with heartbeat_lock:
        assert heartbeat_count >= 2
