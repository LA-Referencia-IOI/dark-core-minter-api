"""
Unit tests for PostgreSQL advisory lock helpers in main_worker.
"""

import threading

from unittest.mock import Mock, patch

from app.main_worker import (
    _HeartbeatSupervisor,
    _WorkerAdvisoryLock,
    _build_advisory_lock_key,
    _rpc_pause_sleep_seconds,
    _select_next_sleep_seconds,
    _shutdown_event,
    _wait_with_heartbeats,
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
