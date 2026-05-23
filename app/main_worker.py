"""
dARK Core Worker - standalone asynchronous worker processes.

The metadata worker persists ARK metadata to the configured metadata backend.
The chain worker publishes ARKs with persisted metadata to blockchain.
"""

import argparse
import hashlib
import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Dict, Optional

from dark_core_lib.exceptions import ConnectionError as CoreConnectionError
from sqlalchemy import text

from app.config import get_settings
from app.database import close_db, init_db
from app.database.connection import SessionLocal
from app.dependencies import (
    init_corelib_client,
    init_metadata_storage,
    shutdown_corelib_client,
    shutdown_metadata_storage,
)
from app.repositories import WorkerRuntimeRepository
from app.utils.storage_health import check_metadata_storage_health
from app.workers.publisher import ChainPublisherWorker, MetadataPersistenceWorker


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


def _read_worker_pid(pid_file: Path = _PID_FILE) -> Optional[int]:
    """Read worker PID from pidfile if available."""
    try:
        raw_pid = pid_file.read_text(encoding="utf-8").strip()
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


def _acquire_worker_pid(pid_file: Path = _PID_FILE) -> None:
    """Create pidfile and ensure a single worker process per worker name."""
    current_pid = os.getpid()
    pid_payload = f"{current_pid}\n".encode("utf-8")

    try:
        fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing_pid = _read_worker_pid(pid_file)
        if existing_pid and _is_process_running(existing_pid):
            raise RuntimeError(f"Worker already running with PID {existing_pid}")
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)

    with os.fdopen(fd, "wb") as handle:
        handle.write(pid_payload)


def _release_worker_pid(pid_file: Path = _PID_FILE) -> None:
    """Remove pidfile when current process exits."""
    current_pid = str(os.getpid())

    try:
        raw_pid = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError:
        return

    if raw_pid == current_pid:
        try:
            pid_file.unlink()
        except OSError:
            logger.warning(f"Failed to remove pidfile: {pid_file}")


def worker_status(pid_file: Path = _PID_FILE) -> int:
    """
    Print worker process status.

    Returns:
        0 when running, 1 when not running.
    """
    pid = _read_worker_pid(pid_file)
    if pid and _is_process_running(pid):
        print(f"RUNNING pid={pid}")
        return 0

    print("NOT_RUNNING")
    return 1


def run_worker_status() -> None:
    """CLI entrypoint to print worker status."""
    raise SystemExit(worker_status())


def _extract_runtime_stats(publisher: Optional[Any]) -> Dict[str, Any]:
    """Extract worker statistics for heartbeat persistence."""
    if publisher is None:
        return {
            "total_processed": 0,
            "total_succeeded": 0,
            "total_failed": 0,
            "total_deferred": 0,
            "total_permanent_failures": 0,
            "last_cycle_at": None,
            "last_cycle_duration_seconds": None,
            "last_cycle_processed": None,
            "last_cycle_succeeded": None,
            "last_cycle_failed": None,
            "last_cycle_deferred": None,
        }

    raw_stats = publisher.stats
    return {
        "total_processed": int(raw_stats.get("total_processed", 0) or 0),
        "total_succeeded": int(raw_stats.get("total_succeeded", 0) or 0),
        "total_failed": int(raw_stats.get("total_failed", 0) or 0),
        "total_deferred": int(raw_stats.get("total_deferred", 0) or 0),
        "total_permanent_failures": int(raw_stats.get("total_permanent_failures", 0) or 0),
        "last_cycle_at": raw_stats.get("last_run_at"),
        "last_cycle_duration_seconds": raw_stats.get("last_run_duration"),
        "last_cycle_processed": raw_stats.get("last_run_processed"),
        "last_cycle_succeeded": raw_stats.get("last_run_succeeded"),
        "last_cycle_failed": raw_stats.get("last_run_failed"),
        "last_cycle_deferred": raw_stats.get("last_run_deferred"),
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
    publisher: Optional[Any] = None,
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
            last_cycle_duration_seconds=stats["last_cycle_duration_seconds"],
            last_cycle_processed=stats["last_cycle_processed"],
            last_cycle_succeeded=stats["last_cycle_succeeded"],
            last_cycle_failed=stats["last_cycle_failed"],
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


def _worker_runtime_config(worker_kind: str):
    """Return effective runtime config for one worker kind."""
    settings = get_settings()
    if worker_kind == "metadata":
        return {
            "enabled": settings.metadata_worker_enabled,
            "worker_name": settings.metadata_worker_runtime_name,
            "page_size": settings.metadata_worker_page_size,
            "sleep_seconds": settings.metadata_worker_sleep_seconds,
            "storage_retry_seconds": settings.metadata_worker_storage_retry_seconds,
            "max_retries": settings.metadata_worker_max_retries,
            "backoff_base": settings.metadata_worker_retry_backoff_base,
        }
    if worker_kind == "chain":
        return {
            "enabled": settings.chain_worker_enabled,
            "worker_name": settings.chain_worker_runtime_name,
            "page_size": settings.chain_worker_page_size,
            "sleep_seconds": settings.chain_worker_sleep_seconds,
            "rpc_retry_seconds": settings.chain_worker_rpc_retry_seconds,
            "congestion_retry_seconds": settings.chain_worker_congestion_retry_seconds,
            "block_stall_seconds": settings.chain_worker_block_stall_seconds,
            "adaptive_page_enabled": settings.chain_worker_adaptive_page_enabled,
            "min_page_size": settings.chain_worker_min_page_size,
            "recovery_success_cycles": settings.chain_worker_recovery_success_cycles,
            "max_retries": settings.chain_worker_max_retries,
            "backoff_base": settings.chain_worker_retry_backoff_base,
        }
    raise ValueError(f"Unsupported worker kind: {worker_kind}")


def _last_cycle_processed(publisher: Optional[Any]) -> int:
    """Return how many records the publisher processed in its last cycle."""
    if publisher is None:
        return 0
    return int((publisher.stats or {}).get("last_run_processed") or 0)


def _select_next_sleep_seconds(publisher: Optional[Any], runtime: Dict[str, Any]) -> tuple[int, str]:
    """Choose whether to continue immediately or sleep after a worker page."""
    page_size = _last_cycle_page_size(publisher, runtime)
    if _last_cycle_processed(publisher) >= page_size:
        return 0, "continue"
    return max(int(runtime["sleep_seconds"]), 0), "sleep"


def _rpc_pause_sleep_seconds(runtime: Dict[str, Any]) -> int:
    """Return sleep duration used while chain worker waits for RPC recovery."""
    return max(int(runtime.get("rpc_retry_seconds", 10) or 0), 1)


def _congestion_pause_sleep_seconds(runtime: Dict[str, Any]) -> int:
    """Return sleep duration used while chain worker waits for chain recovery."""
    return max(int(runtime.get("congestion_retry_seconds", 30) or 0), 1)


def _storage_pause_sleep_seconds(runtime: Dict[str, Any]) -> int:
    """Return sleep duration used while metadata worker waits for storage recovery."""
    return max(int(runtime.get("storage_retry_seconds", 10) or 0), 1)


def _last_cycle_failed(publisher: Optional[Any]) -> int:
    """Return how many records failed in the publisher's last cycle."""
    if publisher is None:
        return 0
    return int((publisher.stats or {}).get("last_run_failed") or 0)


def _last_cycle_deferred(publisher: Optional[Any]) -> int:
    """Return how many records were deferred for infrastructure in the last cycle."""
    if publisher is None:
        return 0
    return int((publisher.stats or {}).get("last_run_deferred") or 0)


def _last_cycle_page_size(publisher: Optional[Any], runtime: Dict[str, Any]) -> int:
    """Return the effective page size used for the last cycle."""
    if publisher is not None:
        page_size = int((publisher.stats or {}).get("last_run_page_size") or 0)
        if page_size > 0:
            return page_size
    return max(int(runtime["page_size"]), 1)


def _last_cycle_congestion_saturated(publisher: Optional[Any], runtime: Dict[str, Any]) -> bool:
    """Return True when a full chain page failed only because of infrastructure."""
    page_size = _last_cycle_page_size(publisher, runtime)
    return (
        _last_cycle_processed(publisher) >= page_size
        and _last_cycle_failed(publisher) >= page_size
        and _last_cycle_deferred(publisher) >= page_size
    )


def _page_size_levels(min_page_size: int, max_page_size: int) -> list[int]:
    """Return conservative adaptive page-size levels up to the nominal maximum."""
    safe_min = max(int(min_page_size or 1), 1)
    safe_max = max(int(max_page_size or safe_min), safe_min)
    levels = {safe_min, safe_max}
    for level in (5, 10):
        if safe_min <= level <= safe_max:
            levels.add(level)
    return sorted(levels)


def _clamp_page_size(value: int, min_page_size: int, max_page_size: int) -> int:
    return max(min(int(value or 0), max_page_size), min_page_size)


def _next_higher_page_size(current: int, min_page_size: int, max_page_size: int) -> int:
    levels = _page_size_levels(min_page_size, max_page_size)
    for level in levels:
        if level > current:
            return level
    return levels[-1]


def _wait_with_heartbeats(
    sleep_seconds: int,
    heartbeat_interval_seconds: int,
    heartbeat_callback,
) -> bool:
    """
    Sleep in heartbeat-sized chunks.

    Returns True when shutdown was requested during the wait.
    """
    deadline = time.monotonic() + max(sleep_seconds, 0)
    heartbeat_interval = max(heartbeat_interval_seconds, 1)

    while not _shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False

        wait_seconds = min(remaining, heartbeat_interval)
        if _shutdown_event.wait(timeout=wait_seconds):
            return True

        if time.monotonic() < deadline:
            heartbeat_callback()

    return True


class _HeartbeatSupervisor:
    """Background heartbeat loop for long-running worker cycles."""

    def __init__(
        self,
        heartbeat_interval_seconds: float,
        heartbeat_callback,
    ):
        self.heartbeat_interval_seconds = max(float(heartbeat_interval_seconds), 0.01)
        self.heartbeat_callback = heartbeat_callback
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the heartbeat thread if it is not already running."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="dark-core-worker-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Stop the heartbeat thread and wait briefly for it to exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout_seconds)

    def _run(self) -> None:
        """Persist periodic RUNNING heartbeats until explicitly stopped."""
        while not self._stop_event.wait(self.heartbeat_interval_seconds):
            try:
                self.heartbeat_callback()
            except Exception as e:
                logger.warning(f"Worker heartbeat supervisor failed: {e}")


def run_worker(worker_kind: str = "chain") -> None:
    """
    Run one worker process.

    Metadata and chain workers are intended to be deployed separately from the API.
    """
    runtime = _worker_runtime_config(worker_kind)
    if not runtime["enabled"]:
        logger.warning(f"{worker_kind} worker is disabled. Exiting worker process.")
        return

    acquired_pid = False
    worker_name = runtime["worker_name"]
    pid_file = Path(f"/tmp/dark-core-{worker_name}.pid")
    _acquire_worker_pid(pid_file)
    acquired_pid = True
    _shutdown_event.clear()
    instance_id = uuid.uuid4().hex
    host = socket.gethostname()
    pid = os.getpid()
    started_at = _utc_now()
    heartbeat_interval = max(1, get_settings().worker_heartbeat_interval_seconds)
    publisher: Optional[Any] = None
    advisory_lock: Optional[_WorkerAdvisoryLock] = None
    heartbeat_supervisor: Optional[_HeartbeatSupervisor] = None
    effective_chain_page_size = max(int(runtime.get("page_size", 1)), 1)
    healthy_capacity_cycles = 0

    logger.info(f"Starting dARK Core {worker_kind} worker...")

    try:
        logger.info("Initializing database...")
        init_db()
        logger.info("Database initialized successfully")
        lock_key = _build_advisory_lock_key(worker_name)
        advisory_lock = _WorkerAdvisoryLock(lock_key)
        if not advisory_lock.acquire():
            raise RuntimeError(f"Worker advisory lock already held for {worker_name}")
        logger.info(f"Acquired PostgreSQL advisory lock for worker={worker_name} key={lock_key}")

        _persist_worker_heartbeat(
            worker_name=worker_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status="STARTING",
            started_at=started_at,
        )

        if worker_kind == "metadata":
            metadata_storage = init_metadata_storage()
            logger.info("Metadata storage initialized successfully")
            publisher = MetadataPersistenceWorker(
                metadata_storage=metadata_storage,
                page_size=runtime["page_size"],
                max_retries=runtime["max_retries"],
                backoff_base=runtime["backoff_base"],
            )
        else:
            logger.info("Chain worker will initialize blockchain client after RPC is available")

        logger.info(
            f"Worker loop started (page_size: {runtime['page_size']}, "
            f"sleep: {runtime['sleep_seconds']}s, "
            f"rpc_retry: {runtime.get('rpc_retry_seconds', 'n/a')}s)"
        )

        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)

        heartbeat_state = {
            "status": "RUNNING",
            "last_error": None,
        }

        def set_heartbeat_state(status: str, last_error: Optional[str] = None) -> None:
            heartbeat_state["status"] = status
            heartbeat_state["last_error"] = last_error

        def persist_current_heartbeat():
            _persist_worker_heartbeat(
                worker_name=worker_name,
                instance_id=instance_id,
                host=host,
                pid=pid,
                status=str(heartbeat_state["status"]),
                started_at=started_at,
                publisher=publisher,
                last_error=heartbeat_state["last_error"],
            )

        def ensure_chain_publisher() -> bool:
            """Initialize chain publisher after RPC has passed health checks."""
            nonlocal publisher
            if publisher is not None:
                return True

            try:
                corelib_client = init_corelib_client(
                    max_wait_seconds=get_settings().dark_rpc_connect_retry_seconds,
                    retry_interval_seconds=get_settings().dark_rpc_connect_retry_interval_seconds,
                )
            except CoreConnectionError as exc:
                error = f"RPC unavailable while initializing chain worker: {exc}"
                set_heartbeat_state("PAUSED_RPC_UNAVAILABLE", error[:1000])
                persist_current_heartbeat()
                logger.warning(error)
                return False

            logger.info(f"Connected to blockchain at block {corelib_client.get_block_number()}")
            publisher = ChainPublisherWorker(
                corelib_client=corelib_client,
                page_size=runtime["page_size"],
                max_retries=runtime["max_retries"],
                backoff_base=runtime["backoff_base"],
            )
            return True

        persist_current_heartbeat()
        heartbeat_supervisor = _HeartbeatSupervisor(
            heartbeat_interval_seconds=heartbeat_interval,
            heartbeat_callback=persist_current_heartbeat,
        )
        heartbeat_supervisor.start()

        while not _shutdown_event.is_set():
            cycle_page_size = None
            if worker_kind == "chain":
                if not ensure_chain_publisher():
                    sleep_seconds = _rpc_pause_sleep_seconds(runtime)
                    _wait_with_heartbeats(sleep_seconds, heartbeat_interval, persist_current_heartbeat)
                    continue

                capacity = publisher.corelib_client.get_chain_capacity(
                    max_page_size=runtime["page_size"],
                )
                recommended_page_size = int(getattr(capacity, "recommended_page_size", 0) or 0)
                capacity_state = str(getattr(capacity, "state", "unknown") or "unknown")
                capacity_reason = str(getattr(capacity, "reason", "") or "")
                capacity_available = bool(getattr(capacity, "available", False))

                if (
                    not capacity_available
                    or capacity_state in {"unavailable", "stalled"}
                    or recommended_page_size <= 0
                ):
                    if not capacity_available or capacity_state == "unavailable":
                        status = "PAUSED_RPC_UNAVAILABLE"
                        sleep_seconds = _rpc_pause_sleep_seconds(runtime)
                    elif capacity_state == "stalled":
                        status = "PAUSED_CHAIN_STALLED"
                        sleep_seconds = _congestion_pause_sleep_seconds(runtime)
                    else:
                        status = "PAUSED_CHAIN_CONGESTED"
                        sleep_seconds = _congestion_pause_sleep_seconds(runtime)

                    error = f"Chain capacity {capacity_state}: {capacity_reason}"
                    set_heartbeat_state(status, error[:1000])
                    persist_current_heartbeat()
                    logger.warning(
                        "Chain worker paused because core-lib capacity is %s; retrying in %ss: %s",
                        capacity_state,
                        sleep_seconds,
                        capacity_reason,
                    )
                    _wait_with_heartbeats(sleep_seconds, heartbeat_interval, persist_current_heartbeat)
                    continue

                if runtime.get("adaptive_page_enabled", True):
                    min_page_size = max(int(runtime.get("min_page_size", 1) or 1), 1)
                    max_page_size = max(int(runtime.get("page_size", 1) or 1), min_page_size)
                    target_page_size = _clamp_page_size(
                        recommended_page_size,
                        min_page_size,
                        max_page_size,
                    )

                    if capacity_state == "healthy":
                        healthy_capacity_cycles += 1
                        if (
                            effective_chain_page_size < target_page_size
                            and healthy_capacity_cycles
                            >= max(int(runtime.get("recovery_success_cycles", 3) or 1), 1)
                        ):
                            effective_chain_page_size = min(
                                target_page_size,
                                _next_higher_page_size(
                                    effective_chain_page_size,
                                    min_page_size,
                                    max_page_size,
                                ),
                            )
                            healthy_capacity_cycles = 0
                    else:
                        healthy_capacity_cycles = 0
                        effective_chain_page_size = min(effective_chain_page_size, target_page_size)

                    cycle_page_size = effective_chain_page_size
                else:
                    cycle_page_size = max(int(runtime["page_size"]), 1)

            if worker_kind == "metadata":
                storage_health = check_metadata_storage_health(metadata_storage)
                if not storage_health["available"]:
                    error = f"Metadata storage unavailable: {storage_health['last_error']}"
                    set_heartbeat_state("PAUSED_STORAGE_UNAVAILABLE", error[:1000])
                    persist_current_heartbeat()
                    sleep_seconds = _storage_pause_sleep_seconds(runtime)
                    logger.warning(
                        "Metadata worker paused because storage is unavailable; retrying in %ss",
                        sleep_seconds,
                    )
                    _wait_with_heartbeats(sleep_seconds, heartbeat_interval, persist_current_heartbeat)
                    continue

            set_heartbeat_state("RUNNING", None)
            if worker_kind == "chain":
                publisher.run_publish_cycle(effective_page_size=cycle_page_size)
            else:
                publisher.run_publish_cycle()

            if worker_kind == "chain" and _last_cycle_congestion_saturated(publisher, runtime):
                effective_chain_page_size = max(int(runtime.get("min_page_size", 1) or 1), 1)
                healthy_capacity_cycles = 0
                error = (
                    "Chain congestion suspected: full page deferred for infrastructure "
                    f"({_last_cycle_page_size(publisher, runtime)} ARKs)"
                )
                set_heartbeat_state("PAUSED_CHAIN_CONGESTED", error[:1000])
                persist_current_heartbeat()
                sleep_seconds = _congestion_pause_sleep_seconds(runtime)
                logger.warning(
                    "Chain worker paused after a full infrastructure-failed page; retrying in %ss",
                    sleep_seconds,
                )
                _wait_with_heartbeats(sleep_seconds, heartbeat_interval, persist_current_heartbeat)
                continue

            persist_current_heartbeat()

            sleep_seconds, sleep_reason = _select_next_sleep_seconds(publisher, runtime)
            logger.info(
                f"Worker cycle finished (processed: {_last_cycle_processed(publisher)}, "
                f"sleep: {sleep_seconds}s, reason: {sleep_reason})"
            )
            _wait_with_heartbeats(sleep_seconds, heartbeat_interval, persist_current_heartbeat)

    except Exception as e:
        if heartbeat_supervisor is not None:
            heartbeat_supervisor.stop()
        _persist_worker_heartbeat(
            worker_name=worker_name,
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
        logger.info(f"Shutting down dARK Core {worker_kind} worker...")

        if heartbeat_supervisor is not None:
            heartbeat_supervisor.stop()

        _persist_worker_heartbeat(
            worker_name=worker_name,
            instance_id=instance_id,
            host=host,
            pid=pid,
            status="STOPPED",
            started_at=started_at,
            publisher=publisher,
        )
        if worker_kind == "chain":
            shutdown_corelib_client()
        if worker_kind == "metadata":
            shutdown_metadata_storage()
        if advisory_lock is not None:
            advisory_lock.release()
            logger.info("Released PostgreSQL advisory lock")
        close_db()
        if acquired_pid:
            _release_worker_pid(pid_file)
        logger.info("Worker shutdown complete")


def main() -> None:
    """CLI for worker process commands."""
    parser = argparse.ArgumentParser(description="dARK Core worker")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "status", "metadata", "chain"],
        default="run",
        help="run chain worker, run a specific worker kind, or check process status",
    )
    args = parser.parse_args()

    if args.command == "status":
        raise SystemExit(worker_status())
    if args.command == "metadata":
        run_worker("metadata")
    else:
        run_worker("chain")


if __name__ == "__main__":
    main()
