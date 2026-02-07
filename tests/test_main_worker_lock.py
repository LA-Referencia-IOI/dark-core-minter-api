"""
Unit tests for PostgreSQL advisory lock helpers in main_worker.
"""

from unittest.mock import Mock, patch

from app.main_worker import _WorkerAdvisoryLock, _build_advisory_lock_key


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
