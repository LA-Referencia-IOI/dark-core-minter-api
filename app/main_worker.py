"""
dARK Core Worker - Standalone ARK publisher process.

Runs the publish scheduler outside of the API process so it can be
deployed as a singleton service.
"""

import argparse
import hashlib
import logging
import os
import signal
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.config import get_settings
from app.database import init_db, close_db
from app.database.connection import SessionLocal
from app.dependencies import (
    init_metadata_storage,
    init_orchestrator,
    shutdown_metadata_storage,
    shutdown_orchestrator,
)
from app.repositories import WorkerRuntimeRepository
from app.workers.publisher import ARKPublisher


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("worker.log"),
    ],
)
logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()
_scheduler: Optional[BackgroundScheduler] = None
_PID_FILE = Path("/tmp/dark-core-worker.pid")


def _utc_now() -> datetime:
    """Return current UTC time as naive datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _handle_shutdown_signal(signum: int, frame: Optional[FrameType]) -> None:
    """Signal handler to stop worker gracefully."""
    del frame
    logger.info(f"Received shutdown signal: {signum}")
    _shutdown_event.set()


def _is_process_running(pid: int) -> bool:
    """Return True if process exists, False otherwise."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_worker_pid() -> Optional[int]:
    """Read worker PID from pidfile if available."""
    try:
        raw_pid = _PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    if not raw_pid:
        return None

    try:
        return int(raw_pid)
    except ValueError:
        return None


def _acquire_worker_pid() -> None:
    """Create pidfile and ensure a single worker process."""
    current_pid = os.getpid()
    pid_payload = f"{current_pid}\n".encode("utf-8")

    try:
        fd = os.open(_PID_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing_pid = _read_worker_pid()
        if existing_pid and _is_process_running(existing_pid):
            raise RuntimeError(f"Worker already running with PID {existing_pid}")
        try:
            _PID_FILE.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(_PID_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)

    with os.fdopen(fd, "wb") as handle:
        handle.write(pid_payload)


def _release_worker_pid() -> None:
    """Remove pidfile when current process exits."""
    current_pid = str(os.getpid())

    try:
        raw_pid = _PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError:
        return

    if raw_pid == current_pid:
        try:
            _PID_FILE.unlink()
        except OSError:
            logger.warning(f"Failed to remove pidfile: {_PID_FILE}")


def worker_status() -> int:
    """
    Print worker process status.

    Returns:
        0 when running, 1 when not running.
    """
    pid = _read_worker_pid()
    if pid and _is_process_running(pid):
        print(f"RUNNING pid={pid}")
        return 0

    print("NOT_RUNNING")
    return 1


def run_worker_status() -> None:
    """CLI entrypoint to print worker status."""
    raise SystemExit(worker_status())


def _extract_runtime_stats(publisher: Optional[ARKPublisher]) -> Dict[str, Any]:
    """Extract worker statistics for heartbeat persistence."""
    if publisher is None:
        return {
            "total_processed": 0,
            "total_succeeded": 0,
            "total_failed": 0,
            "total_permanent_failures": 0,
            "last_cycle_at": None,
        }

    raw_stats = publisher.stats
    return {
        "total_processed": int(raw_stats.get("total_processed", 0) or 0),
        "total_succeeded": int(raw_stats.get("total_succeeded", 0) or 0),
        "total_failed": int(raw_stats.get("total_failed", 0) or 0),
        "total_permanent_failures": int(raw_stats.get("total_permanent_failures", 0) or 0),
        "last_cycle_at": raw_stats.get("last_run_at"),
    }


def _build_advisory_lock_key(worker_name: str) -> int:
    """
    Build a stable PostgreSQL advisory lock key from worker name.

    Uses 63-bit positive integer range accepted by bigint advisory locks.
    """
    digest = hashlib.sha256(worker_name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") & 0x7FFFFFFFFFFFFFFF


class _WorkerAdvisoryLock:
    """Session-scoped PostgreSQL advisory lock held for worker lifetime."""

    def __init__(self, key: int):
        self.key = key
        self._db = None

    def acquire(self) -> bool:
        """Try to acquire advisory lock. Returns True when acquired."""
        session_factory = SessionLocal()
        db = session_factory()
        try:
            acquired = bool(
                db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": self.key},
                ).scalar()
            )
            if acquired:
                self._db = db
                return True
        except Exception:
            db.close()
            raise

        db.close()
        return False

    def release(self) -> None:
        """Release advisory lock and close dedicated lock session."""
        if self._db is None:
            return
        try:
            self._db.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": self.key},
            )
            self._db.commit()
        except Exception as e:
            logger.warning(f"Failed to release advisory lock: {e}")
            self._db.rollback()
        finally:
            self._db.close()
            self._db = None


def _persist_worker_heartbeat(
    worker_name: str,
    instance_id: str,
    host: str,
    pid: int,
    status: str,
    started_at: datetime,
    publisher: Optional[ARKPublisher] = None,
    last_error: Optional[str] = None,
) -> None:
    """Persist worker heartbeat/status in DB."""
    stats = _extract_runtime_stats(publisher)

    session_factory = SessionLocal()
    db = session_factory()
    try:
        repo = WorkerRuntimeRepository(db)
        repo.upsert_status(
            worker_name=worker_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status=status,
            heartbeat_at=_utc_now(),
            started_at=started_at,
            last_cycle_at=stats["last_cycle_at"],
            last_error=last_error,
            total_processed=stats["total_processed"],
            total_succeeded=stats["total_succeeded"],
            total_failed=stats["total_failed"],
            total_permanent_failures=stats["total_permanent_failures"],
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to persist worker heartbeat: {e}")
    finally:
        db.close()


def run_worker() -> None:
    """
    Run the standalone publisher worker process.

    This process is intended to be deployed separately from the API.
    """
    global _scheduler

    settings = get_settings()
    if not settings.worker_enabled:
        logger.warning("WORKER_ENABLED is false. Exiting worker process.")
        return

    acquired_pid = False
    _acquire_worker_pid()
    acquired_pid = True
    _shutdown_event.clear()
    instance_id = uuid.uuid4().hex
    host = socket.gethostname()
    pid = os.getpid()
    started_at = _utc_now()
    heartbeat_interval = max(1, settings.worker_heartbeat_interval_seconds)
    publisher: Optional[ARKPublisher] = None
    advisory_lock: Optional[_WorkerAdvisoryLock] = None

    logger.info("Starting dARK Core Worker...")

    try:
        logger.info("Initializing database...")
        init_db()
        logger.info("Database initialized successfully")
        lock_key = _build_advisory_lock_key(settings.worker_runtime_name)
        advisory_lock = _WorkerAdvisoryLock(lock_key)
        if not advisory_lock.acquire():
            raise RuntimeError(
                f"Worker advisory lock already held for {settings.worker_runtime_name}"
            )
        logger.info(
            f"Acquired PostgreSQL advisory lock for worker={settings.worker_runtime_name} key={lock_key}"
        )
        _persist_worker_heartbeat(
            worker_name=settings.worker_runtime_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status="STARTING",
            started_at=started_at,
        )

        orchestrator = init_orchestrator()
        logger.info(f"Connected to blockchain at block {orchestrator.get_block_number()}")

        metadata_storage = init_metadata_storage()
        logger.info("Metadata storage initialized successfully")

        publisher = ARKPublisher(
            orchestrator=orchestrator,
            metadata_storage=metadata_storage,
            batch_size=settings.worker_batch_size,
            max_retries=settings.worker_max_retries,
            backoff_base=settings.worker_retry_backoff_base,
        )

        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            func=publisher.run_publish_cycle,
            trigger=IntervalTrigger(seconds=settings.worker_interval_seconds),
            id="ark_publisher",
            name="ARK Publisher Worker",
            max_instances=1,
            replace_existing=True,
        )
        _scheduler.start()
        logger.info(
            f"Worker scheduler started (interval: {settings.worker_interval_seconds}s, "
            f"batch_size: {settings.worker_batch_size})"
        )
        _persist_worker_heartbeat(
            worker_name=settings.worker_runtime_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status="RUNNING",
            started_at=started_at,
            publisher=publisher,
        )

        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)

        next_heartbeat_at = _utc_now()
        while not _shutdown_event.wait(timeout=1.0):
            now = _utc_now()
            if now >= next_heartbeat_at:
                _persist_worker_heartbeat(
                    worker_name=settings.worker_runtime_name,
                    instance_id=instance_id,
                    host=host,
                    pid=pid,
                    status="RUNNING",
                    started_at=started_at,
                    publisher=publisher,
                )
                next_heartbeat_at = now + timedelta(seconds=heartbeat_interval)

    except Exception as e:
        _persist_worker_heartbeat(
            worker_name=settings.worker_runtime_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status="ERROR",
            started_at=started_at,
            publisher=publisher,
            last_error=str(e)[:1000],
        )
        logger.error(f"Worker startup/runtime failed: {e}")
        raise

    finally:
        logger.info("Shutting down dARK Core Worker...")

        if _scheduler and _scheduler.running:
            _scheduler.shutdown(wait=True)
            logger.info("Worker scheduler stopped")

        _persist_worker_heartbeat(
            worker_name=settings.worker_runtime_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status="STOPPED",
            started_at=started_at,
            publisher=publisher,
        )
        shutdown_orchestrator()
        shutdown_metadata_storage()
        if advisory_lock is not None:
            advisory_lock.release()
            logger.info("Released PostgreSQL advisory lock")
        close_db()
        if acquired_pid:
            _release_worker_pid()
        logger.info("Worker shutdown complete")


def main() -> None:
    """CLI for worker process commands."""
    parser = argparse.ArgumentParser(description="dARK Core standalone worker")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "status"],
        default="run",
        help="run worker loop or check process status",
    )
    args = parser.parse_args()

    if args.command == "status":
        raise SystemExit(worker_status())
    run_worker()


if __name__ == "__main__":
    main()
